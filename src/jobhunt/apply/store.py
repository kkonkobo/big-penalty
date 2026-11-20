"""`application_runs`: one row per dry run / submission attempt of an application (the screenshots live in `out_dir`),
and `apply_reservations`: one per attempted send (quota + duplicate guard), pending (navigation click) or firm (final click)."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import sys
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Callable

from pydantic import BaseModel

from jobhunt.apply.adapters import Attempt, Receipt, clean_receipt
from jobhunt.apply.destination import RECIPIENT_REFUSED, audit_url, clean_text, clean_url, contains_verification_code, origin
from jobhunt.apply.errors import ApplyError
from jobhunt.apply.identity import canonical_identity, dedupe_identity, dispatch_url, posting_fingerprint, resolved_identity, similar_role
from jobhunt.apply.types import ACCOUNT_REASONS, Mode, RunResult, RunStatus
from jobhunt.db import _dt, _iso, connect, now
from jobhunt.dedupe import canonical_url

_suppress = contextlib.suppress

UNCONFIRMED_NOTE = "Envoi non confirmé : vérifier sur le site de l'entreprise avant tout nouvel essai"

_DIAGNOSTICS = {
    "failed": "La tentative a échoué.",
    "blocked": "L’action a été bloquée.",
    "credential_page": "Vérification ou connexion manuelle requise (credential_page).",
    **ACCOUNT_REASONS,
    "recipient_refused": RECIPIENT_REFUSED,
    "posting_mismatch": "Identité du poste non vérifiée : saisie annulée.",
    "reservation_refused": "La réservation de l’envoi ne permet pas de poursuivre (doublon possible ou site en pause).",
    "dry_untested": "Simulation non testée : inspection hors ligne, sans saisie, fichier ni envoi de candidature.",
    "dry_account_gate": "L’étape de connexion ou de création de compte a été atteinte, sans saisie.",
    "no_adapter": "Envoi bloqué : aucun adaptateur d'envoi et de reçu vérifié pour ce site. À envoyer en mode supervisé.",
    "supervised_only": "À envoyer en mode supervisé",
    "not_approved": "CV non approuvé",
    "pdf_missing": "PDF absent",
    "pdf_changed": "PDF modifié ou différent de celui approuvé",
    "missing_link": "pas de lien vers le site de l'entreprise (http(s), hors agrégateur)",
    "invalid_link": "Lien de candidature non autorisé.",
    "already_sent": "candidature déjà envoyée",
    "revision_changed": "le CV a changé depuis l'affichage",
    "link_changed": "le lien de l'offre a changé depuis l'affichage",
    "destination_changed": "La candidature ou la destination a changé.",
    "send_lock": "un autre envoi est en cours",
    "unconfirmed": UNCONFIRMED_NOTE,
    "not_sent": "Non-envoi vérifié. Tout nouvel essai doit être lancé manuellement.",
    "bad_origin": "Lien de l'offre inexploitable",
    "receipt_recording": "Reçu vérifié, mais enregistrement impossible. Ne pas renvoyer cette candidature.",
    "challenge": "Un défi anti-robot doit être résolu à la main.",
    "challenge_or_rate_limited": "Défi anti-robot ou accès limité par le site.",
    "public_page_shell": "Page publique incomplète : aucun formulaire vérifiable.",
    "untrusted_host": "Le formulaire est hébergé sur un site non reconnu.",
    "technical": "Erreur technique.",
    "interrupted": "interrompu",
    "cv_errors": "Le CV contient des erreurs bloquantes.",
    "duplicate": "Candidature déjà envoyée (ou tentée) pour la même offre.",
    "potential_duplicate": "Doublon possible : confirmez « ce n'est pas un doublon ».",
    "daily_cap": "Le plafond d’envois automatiques par jour est atteint.",
    "aggregator_origin": "Le lien est celui d'un agrégateur, pas du site de l'entreprise.",
    "origin_not_usable": "Il n’y a pas de lien exploitable vers le site de l’entreprise.",
    "job_page_no_apply_control": "Aucun lien de candidature vérifiable sur la page de l’offre.",
    "apply_target_unverified": "Destination de candidature non vérifiée.",
    "apply_control_ambiguous": "Plusieurs contrôles de candidature possibles.",
    "dry_apply_form_found": "Formulaire de candidature observé. Aucune donnée saisie ni envoyée.",
    "dry_apply_form_needs_input": "Formulaire de candidature observé ; des réponses sont requises.",
    "cross_domain_apply_target": "Lien de candidature sur une autre origine non vérifiée.",
    "duplicate_unconfirmed": "Doublon possible : confirmez « ce n'est pas un doublon ».",
    "approval_binding_changed": "Approbation ou destination modifiée avant l'envoi",
    "reservation_exists": "Cet essai a déjà été réservé",
    "destination_unverified": "Destination de l'offre non vérifiée.",
    "duplicate_reserved": "Un essai existe déjà pour ce poste chez cet employeur.",
    "recipient_refused_ats_navigation": RECIPIENT_REFUSED,
    "recipient_refused_other_navigation": RECIPIENT_REFUSED,
    "recipient_refused_other_write": RECIPIENT_REFUSED,
    "recipient_refused_ats_write": RECIPIENT_REFUSED,
    "recipient_refused_same_site_write": RECIPIENT_REFUSED,
    "recipient_refused_same_site_navigation": RECIPIENT_REFUSED,
    "recipient_refused_redirect": RECIPIENT_REFUSED,
    "recipient_refused_response": RECIPIENT_REFUSED,
    "recipient_refused_transport": RECIPIENT_REFUSED,
    "required_fields": "Informations requises à compléter.",
    "verification_required": "Validation par e-mail ou par code requise.",
    "human_required": "Une intervention humaine est nécessaire.",
    "form_actions_missing": "Aucune action possible sur le formulaire.",
    "step_limit": "Trop d'étapes : limite atteinte.",
    "job_closed": "Offre clôturée.",
    "manual_retry_required": "Nouvel essai manuel requis pour ce poste.",
    "prior_attempt_unverified": "Un envoi de cette candidature a déjà été tenté : vérifier sur le site de l'entreprise.",
    "requisition_conflict": "Identité du poste incohérente : envoi annulé.",
    "job_missing": "Offre introuvable pour ce profil.",
    "application_withdrawn": "Candidature retirée.",
    "no_attested_receipt": "Aucun reçu authentifié pour cet essai.",
}
_EXACT_DIAGNOSTICS = {message: code for code, message in _DIAGNOSTICS.items() if not code.startswith("recipient_refused_")}
_DIAGNOSTIC_PATTERNS = (
    (r"Erreur technique \([A-Za-z_]+\)\.", "technical"),
    (r"Interrompu\.", "interrupted"),
    (r"Le poste affiché ne correspond pas à la candidature réservée : fenêtre fermée\.", "posting_mismatch"),
    (r"Lien de l'offre inexploitable : .*", "bad_origin"),
    (r"Doublon possible : .*", "potential_duplicate"),
    (r"(?:candidature déjà envoyée pour la même offre \(n°\d+\)|Candidature déjà envoyée \(ou tentée\) pour la même offre)", "duplicate"),
    (r"[Pp]lafond de \d+ envois automatiques par jour atteint", "daily_cap"),
    (r"PDF différent de celui approuvé", "pdf_changed"),
    (r"Un envoi de cette candidature a déjà été tenté : vérifier sur le site de l'entreprise", "prior_attempt_unverified"),
    (r"Aucune action possible sur cette page\.", "form_actions_missing"),
    (r"Offre clôturée", "job_closed"),
    (r"Un CAPTCHA doit être résolu à la main\.", "challenge"),
    (r"offre introuvable pour ce profil", "job_missing"),
    (r"candidature retirée", "application_withdrawn"),
    (r"\d+ erreur\(s\) bloquante\(s\)(?: dans le CV)?", "cv_errors"),
    (r"\d+ erreur\(s\) à corriger avant d'approuver \(ou cocher « Ignorer et approuver »\)", "cv_errors"),
)
_HOST = re.compile(r"(?:[a-z0-9-]+\.)*[a-z0-9-]+|[a-f0-9:]+", re.I)


def _failure_result(result: RunResult, *, held: bool = False) -> RunResult:
    message = result.message
    explicit = result.diagnostic_code
    exception = re.fullmatch(r"exception:[A-Za-z_][A-Za-z_0-9]{0,63}@\d{1,5}/[a-z_]{1,32}", explicit)
    if explicit in _DIAGNOSTICS or exception:
        code = explicit
    elif (contains_verification_code(message) or any(
        step.page_kind in {"account_login", "account_signup", "email_verification"} or contains_verification_code(step.note)
        for step in result.steps
    )):
        code = "credential_page"
    elif result.untrusted_host and _HOST.fullmatch(result.untrusted_host):
        code = "untrusted_host"
    else:
        reasons = message.removeprefix("Envoi impossible : ").split(" ; ")
        code = next((known for reason in reasons if (known := _EXACT_DIAGNOSTICS.get(reason))), "")
        if not code:
            code = next((known for pattern, known in _DIAGNOSTIC_PATTERNS if any(re.fullmatch(pattern, reason) for reason in reasons)), "")
        if code == "potential_duplicate":
            code = "duplicate_unconfirmed"
        code = code or ("required_fields" if result.status == "needs_input" else
                        "verification_required" if result.status == "needs_email" else
                        "human_required" if result.status == "needs_human"
                        else f"{result.status}_at_{result.dispatch_diagnostics.control}")
    message = _DIAGNOSTICS.get(code, _DIAGNOSTICS.get(result.status, _DIAGNOSTICS["technical"]) if not exception
                               else _DIAGNOSTICS["technical"])
    if held and code != "unconfirmed":
        message = UNCONFIRMED_NOTE + ". " + message
    return RunResult(status=result.status, diagnostic_code=code, message=message, final_url=origin(result.final_url),
                     dispatch_diagnostics=result.dispatch_diagnostics,
                     consent_codes=_safe_consents(result),
                     untrusted_host=result.untrusted_host if code == "untrusted_host" else "",
                     submit_clicked=result.submit_clicked, finished_at=result.finished_at)


_RESULT_FIELDS = {"status": True, "message": True, "final_url": True, "submit_clicked": True, "dispatch_diagnostics": True,
                  "untrusted_host": True, "finished_at": True,
                  "steps": {"__all__": {"index", "url", "page_kind", "filled", "clicked", "screenshot", "note"}},
                  "questions": True}


def _safe_consents(result: RunResult) -> list[str]:
    return sorted(set(result.consent_codes) & {"application_privacy", "application_terms"})


def _safe_result(result: RunResult, *, held: bool = False) -> RunResult:
    if result.status in ("failed", "blocked"):
        return _failure_result(result, held=held)
    def scrub(value, key=""):
        if isinstance(value, dict):
            return {name: scrub(item, name) for name, item in value.items()}
        if isinstance(value, list):
            return [scrub(item) for item in value]
        if isinstance(value, str):
            if key in {"url", "final_url"}:
                return audit_url(value)
            if key == "screenshot":
                return value if os.getenv("JOBHUNT_APPLY_LOCAL_SCREENSHOTS") == "1" else ""
            return clean_text(value, audit=True)
        return value

    payload = result.model_dump(include=_RESULT_FIELDS)
    if result.status.startswith("needs_"):
        failure = _failure_result(result)
        payload.update(diagnostic_code=failure.diagnostic_code, message=failure.message)
    payload["consent_codes"] = _safe_consents(result)
    sensitive = {"account_login", "account_signup", "email_verification"}
    credential_page = contains_verification_code(result.message)
    for step in payload["steps"]:
        if step["page_kind"] in sensitive or contains_verification_code(step["note"] or ""):
            credential_page = True
            step.update(note="credential_page", filled=[], clicked="", screenshot="")
    if (credential_page or result.status == "needs_email") and result.diagnostic_code not in ACCOUNT_REASONS:
        payload.update(message="Vérification ou connexion manuelle requise (credential_page).", questions=[])
    return RunResult.model_validate(scrub(payload))


ACCOUNT_LANDING_RE = re.compile(r"/(?:log-?in|sign-?in|connexion|my-account|mon-compte|account|authentification)\b", re.I)


def _url_key(url: str) -> str:
    return "sha256:" + hashlib.sha256(url.encode()).hexdigest()


def binding_record(attempt: Attempt) -> dict:
    payload = asdict(attempt)
    if not attempt.autonomous:
        payload.pop("autonomous")
    if not attempt.landing_digest:
        payload.pop("landing_host")
        payload.pop("landing_digest")
    if not attempt.landing_binding:
        payload.pop("landing_binding")
    return {**payload, "url": audit_url(attempt.url), "url_digest": _url_key(attempt.url)}


def _binding_json(attempt: Attempt) -> str:
    return json.dumps(binding_record(attempt))
SENT_NOT_RECORDED_NOTE = "Envoyée sur le site, mais le statut n'a pas pu être mis à jour : ne pas la renvoyer"
STALE_RUN_AFTER = timedelta(minutes=45)  # a run still "running" after this long is dead unless its owner process is alive
NOT_ACTIVE = "Cet envoi n'est plus actif (remplacé ou interrompu)"


def pid_alive(pid: int | None) -> bool:
    """Is that process alive on this machine? (None / 0 / unknown -> False.) Never signals the process."""
    if not pid or pid < 0:
        return False
    if sys.platform == "win32":  # os.kill would TERMINATE the process there
        import ctypes  # noqa: PLC0415

        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
        if handle:
            ctypes.windll.kernel32.CloseHandle(handle)
        return bool(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _owner_confirmed_dead(pid: int | None) -> bool:
    if not pid or pid < 0 or sys.platform == "win32":
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except OSError:
        pass
    return False


class ApplyRun(BaseModel):
    id: int
    application_id: int
    rev: int
    mode: Mode
    trigger: str  # "auto" | "user"
    status: RunStatus
    message: str = ""
    result: RunResult | None = None
    out_dir: str | None = None
    started_at: datetime
    finished_at: datetime | None = None
    submit_clicked_at: datetime | None = None  # set (with a firm reservation) right before the final click: the form MAY have been sent
    nav_clicked_at: datetime | None = None     # first click after data was entered (pending reservation): MAY have been sent
    pid: int | None = None                     # process that drives the run (a live owner is never taken over)
    approved_by: str | None = None             # "auto" | "user": who had approved the application when this run started
    operator_not_sent_at: datetime | None = None
    origin_sha: str | None = None
    application_version: str | None = None
    email_resumed: bool = False


def _from_row(r) -> ApplyRun:
    return ApplyRun(
        id=r["id"], application_id=r["application_id"], rev=r["rev"], mode=r["mode"], trigger=r["trigger"], status=r["status"],
        message=r["message"] or "", result=RunResult.model_validate_json(r["result_json"]) if r["result_json"] else None,
        out_dir=r["out_dir"], started_at=_dt(r["started_at"]), finished_at=_dt(r["finished_at"]),
        email_resumed=bool(r["email_resumed"]),
        submit_clicked_at=_dt(r["submit_clicked_at"]), nav_clicked_at=_dt(r["nav_clicked_at"]), pid=r["pid"],
        approved_by=r["approved_by"], operator_not_sent_at=_dt(r["operator_not_sent_at"]),
        origin_sha=r["origin_sha"], application_version=r["application_version"])


def start_run(application_id: int, rev: int, mode: str, trigger: str, out_dir_for: Callable[[int], Path], *,
              origin_sha: str | None = None) -> ApplyRun:
    """Insert a `running` row (with this process's pid). ONE transaction: refused while another run of the application is
    running AND its owner is alive (pid alive on this machine; an unknown pid counts as alive for 45 min). A `running` row
    whose owner is dead is marked failed ("interrompu"); if it had reached a click (`submit_clicked_at` or `nav_clicked_at`)
    the application MAY have been sent: it is marked failed (never retried by itself), the run's reservation is made firm
    and the new run is refused."""
    ts = now()
    possibly_sent = False
    with connect() as c:
        c.execute("BEGIN IMMEDIATE")
        for r in c.execute("SELECT id, started_at, pid, submit_clicked_at, nav_clicked_at FROM application_runs"
                           " WHERE application_id=? AND status='running'", (application_id,)).fetchall():
            if pid_alive(r["pid"]) if r["pid"] else ts - _dt(r["started_at"]) < STALE_RUN_AFTER:
                raise ApplyError("Un envoi est déjà en cours pour cette candidature")
            interrupted = _failure_result(RunResult(status="failed", message="interrompu", finished_at=ts))
            c.execute("UPDATE application_runs SET status='failed', message=?, result_json=?, finished_at=? WHERE id=?",
                      (interrupted.message, interrupted.model_dump_json(), _iso(ts), r["id"]))
            if (r["submit_clicked_at"] is not None or r["nav_clicked_at"] is not None or c.execute(
                    "SELECT 1 FROM apply_reservations WHERE run_id=? AND released_at IS NULL", (r["id"],)).fetchone()):
                possibly_sent = True
                c.execute("UPDATE apply_reservations SET pending=0 WHERE run_id=? AND released_at IS NULL", (r["id"],))
        if possibly_sent:
            c.execute("UPDATE applications SET status='failed', approved_rev=NULL, approved_sha256=NULL, approval_override=0,"
                      " approved_by=NULL, notes=?, updated_at=? WHERE id=? AND status NOT IN ('submitted', 'withdrawn')",
                      (UNCONFIRMED_NOTE, _iso(ts), application_id))
        else:
            cur = c.execute("INSERT INTO application_runs(application_id, rev, mode, trigger, status, started_at, pid,"
                            " approved_by, origin_sha, application_version)"
                            " SELECT id, ?, ?, ?, 'running', ?, ?, CASE WHEN status='approved' THEN approved_by END, ?, updated_at"
                            " FROM applications WHERE id=?",
                            (rev, mode, trigger, _iso(ts), os.getpid(), origin_sha, application_id))
            run_id = int(cur.lastrowid)
            c.execute("UPDATE application_runs SET out_dir=? WHERE id=?", (str(out_dir_for(run_id)), run_id))
    if possibly_sent:  # raised after the commit above (an exception inside `connect()` would roll the takeover back)
        raise ApplyError("Un envoi précédent a peut-être abouti : vérifier sur le site de l'entreprise")
    return get_run(run_id)  # type: ignore[return-value]


def finish_run(run_id: int, result: RunResult) -> ApplyRun:
    """Record the end of a run that has nothing to settle (dry run, refusal). Only acts on a still `running` row."""
    result = _safe_result(result)
    finished = result.finished_at or now()
    result = result.model_copy(update={"finished_at": finished})
    with connect() as c:
        cur = c.execute("UPDATE application_runs SET status=?, message=?, result_json=?, finished_at=? WHERE id=? AND status='running'",
                        (result.status, result.message, result.model_dump_json(), _iso(finished), run_id))
        if cur.rowcount == 0:
            raise ApplyError(NOT_ACTIVE)
    return get_run(run_id)  # type: ignore[return-value]


def save_progress(run_id: int, result: RunResult) -> None:
    """Persist a partial result (steps so far, `submit_clicked` as it is about to be) on a run that stays `running`.
    Finalizing a failure replaces this snapshot with a diagnostic. Only the still-running NEWEST run of its
    application: otherwise ApplyError (the engine then does not click)."""
    result = _safe_result(result)
    with connect() as c:
        c.execute("BEGIN IMMEDIATE")
        cur = c.execute(
            "UPDATE application_runs SET result_json=? WHERE id=? AND status='running' AND id=("
            "SELECT MAX(id) FROM application_runs WHERE application_id=(SELECT application_id FROM application_runs WHERE id=?))",
            (result.model_dump_json(), run_id, run_id))
        if cur.rowcount == 0:
            raise ApplyError(NOT_ACTIVE)


def finish_and_settle(run_id: int, result: RunResult, *, sent_origin_url: str | None = None,
                      receipt: Receipt | None = None, browser_attempt: Attempt | None = None,
                      browser_confirmed: bool = False, browser_clear: bool = False) -> ApplyRun:
    """Preserve any possible send atomically; no generic engine result is an attempt-bound receipt.

    Only a typed receipt authenticated by a registered adapter and matching a live, durable
    reservation can settle automatically. Generic statuses and the legacy URL argument are not evidence.
    """
    from jobhunt.cv import store as cvstore  # noqa: PLC0415 - cv.store does not import this module; kept lazy anyway
    from jobhunt.cv.errors import CVError  # noqa: PLC0415

    finished = result.finished_at or now()
    ts = _iso(finished)
    awaiting_verification = False
    with connect() as c:
        c.execute("BEGIN IMMEDIATE")
        run = c.execute("SELECT * FROM application_runs WHERE id=?", (run_id,)).fetchone()
        if (run is None or run["status"] != "running"
                or c.execute("SELECT MAX(id) FROM application_runs WHERE application_id=?", (run["application_id"],)).fetchone()[0] != run_id):
            raise ApplyError(NOT_ACTIVE)
        if run["mode"] in ("submit", "supervised"):
            held = c.execute("SELECT * FROM apply_reservations WHERE run_id=? AND released_at IS NULL", (run_id,)).fetchone()
            verified = False
            if (browser_attempt and browser_attempt.autonomous and held and run["mode"] == "submit"
                    and held["binding_json"] == _binding_json(browser_attempt) and browser_attempt.run_id == run_id):
                if (browser_confirmed and result.status == "submitted" and run["submit_clicked_at"]
                        and _binding_current(c, run, browser_attempt)):
                    cvstore.tx_mark_submitted(c, run["application_id"], run["rev"], sent_origin_url=audit_url(browser_attempt.url))
                    c.execute("UPDATE applications SET sent_run_id=? WHERE id=?", (run_id, run["application_id"]))
                    c.execute("UPDATE jobs SET status='applied' WHERE id=(SELECT job_id FROM applications WHERE id=?)", (run["application_id"],))
                    kind = result.dispatch_diagnostics.receipt_kind if result.dispatch_diagnostics else 'none'
                    c.execute("UPDATE apply_reservations SET receipt_kind=?, receipt_outcome='confirmed', receipt_at=? WHERE id=?",
                              (kind if kind in {'first_party_confirmation', 'candidate_home_listing'} else 'first_party_confirmation',
                               ts, held["id"]))
                    verified = True
                elif browser_clear and not run["submit_clicked_at"] and not run["nav_clicked_at"] and not result.submit_clicked:
                    c.execute("UPDATE apply_reservations SET released_at=? WHERE id=?", (ts, held["id"]))
                else:
                    awaiting_verification = True
                    with _suppress(CVError):
                        cvstore.tx_force_failed(c, run["application_id"], UNCONFIRMED_NOTE)
                    if run["submit_clicked_at"] or result.status not in ("needs_input", "needs_email", "needs_human"):
                        result = result.model_copy(update={"status": "failed"})
                    c.execute("UPDATE apply_reservations SET receipt_kind='none', receipt_outcome='uncertain', receipt_at=? WHERE id=?",
                              (ts, held["id"]))
                    verified = True
            elif (receipt and held and run["mode"] == "submit" and receipt.attempt.adapter and clean_receipt(receipt, receipt.attempt)
                    and receipt.attempt.run_id == run_id and receipt.attempt.application_id == run["application_id"]
                    and receipt.attempt.rev == run["rev"] and held["binding_json"] == _binding_json(receipt.attempt)
                    and _binding_current(c, run, receipt.attempt)):
                _save_receipt(c, run_id, receipt)
                if receipt.outcome == "confirmed":
                    cvstore.tx_mark_submitted(c, run["application_id"], run["rev"], sent_origin_url=audit_url(receipt.attempt.url))
                    c.execute("UPDATE applications SET sent_run_id=? WHERE id=?", (run_id, run["application_id"]))
                    c.execute("UPDATE jobs SET status='applied' WHERE id=(SELECT job_id FROM applications WHERE id=?)",
                              (run["application_id"],))
                    result = result.model_copy(update={"status": "submitted", "message": "Candidature envoyée (reçu vérifié)."})
                else:
                    c.execute("UPDATE apply_reservations SET released_at=? WHERE id=?", (ts, held["id"]))
                    c.execute("UPDATE application_runs SET operator_not_sent_at=? WHERE id=?", (ts, run_id))
                    cvstore.tx_set_status(c, run["application_id"], "draft")
                    result = RunResult(status="blocked", message="Non-envoi vérifié. Tout nouvel essai doit être lancé manuellement.",
                                       dispatch_diagnostics=result.dispatch_diagnostics)
                verified = True
            elif held and held["binding_json"]:
                with _suppress(CVError):
                    cvstore.tx_force_failed(c, run["application_id"], UNCONFIRMED_NOTE)
                status = result.status if (run["mode"] == "supervised" and result.status == "needs_input"
                                           or run["mode"] == "submit" and result.status in ("needs_email", "needs_human")) else "failed"
                awaiting_verification = True
                result = result.model_copy(update={"status": status, "submit_clicked": False})
                if status == "needs_input":
                    result = result.model_copy(update={"message": UNCONFIRMED_NOTE + ". " + result.message})
                c.execute("UPDATE apply_reservations SET receipt_kind='none', receipt_outcome='uncertain', receipt_at=? WHERE id=?",
                          (ts, held["id"]))
            clicked = bool((held and not held["binding_json"]) or result.submit_clicked or result.status == "submitted"
                           or run["submit_clicked_at"] or run["nav_clicked_at"])
            if clicked and not verified and run["mode"] == "submit":
                c.execute("UPDATE apply_reservations SET pending=0 WHERE run_id=? AND released_at IS NULL", (run_id,))
                if not run["submit_clicked_at"]:
                    c.execute("UPDATE application_runs SET submit_clicked_at=? WHERE id=?", (ts, run_id))
                with _suppress(CVError):  # already submitted / withdrawn: nothing to flag
                    cvstore.tx_force_failed(c, run["application_id"], UNCONFIRMED_NOTE)
                result = result.model_copy(update={"status": "failed", "submit_clicked": True, "message": UNCONFIRMED_NOTE})
        result = _safe_result(result, held=awaiting_verification)
        c.execute("UPDATE application_runs SET status=?, message=?, result_json=?, finished_at=? WHERE id=?",
                  (result.status, result.message, result.model_copy(update={"finished_at": finished}).model_dump_json(), ts, run_id))
    return get_run(run_id)  # type: ignore[return-value]


# Autonomous runs that the engine itself failed or paused before this instant (UTC, the format of `started_at`) were judged by
# guard and page-reading code that has since been fixed: they no longer pause their host, and the automatic pass retries
# their engine-caused pauses (ENGINE_PAUSE_CODES) once. Move it forward when a change is expected to clear such outcomes.
# A run that may have sent the application (submit clicked, uncertain receipt) is never affected.
ENGINE_RETRY_EPOCH = "2026-11-20T21:05:00+00:00"

# Pause codes the engine produced on its own (its guard refused a request, it could not read the page or find the apply
# control): unlike a CAPTCHA, an e-mail or a login, nothing the candidate must do. Posting conflicts are not in this list.
ENGINE_PAUSE_CODES = frozenset({
    "recipient_refused_ats_navigation", "recipient_refused_ats_write", "recipient_refused_other_navigation",
    "recipient_refused_other_write", "recipient_refused_redirect", "recipient_refused_response",
    "recipient_refused_same_site_navigation", "recipient_refused_same_site_write", "recipient_refused_transport",
    "apply_target_unverified", "public_page_shell", "job_page_no_apply_control", "apply_control_ambiguous",
})


def _site_handoff(t: str) -> str:
    """SQL: run `t` hands the application over to the candidate on the site (a stale engine pause does not)."""
    codes = ", ".join(f"'{code}'" for code in sorted(ENGINE_PAUSE_CODES))
    return (f"(({t}.status='needs_human' AND NOT ({t}.started_at < '{ENGINE_RETRY_EPOCH}' AND {t}.submit_clicked_at IS NULL"
            f" AND COALESCE(json_extract({t}.result_json, '$.submit_clicked'), 0) = 0"
            f" AND json_extract({t}.result_json, '$.diagnostic_code') IN ({codes})))"
            f" OR ({t}.status='needs_email' AND {t}.email_resumed=0))")


_OUTSTANDING_RUNS = """
    SELECT run.*, app.id AS owner_application_id, EXISTS (
        SELECT 1 FROM application_runs retry WHERE retry.application_id = app.id
        AND retry.operator_not_sent_at IS NOT NULL
    ) AS manual_retry, EXISTS (
        SELECT 1 FROM application_runs handoff WHERE handoff.application_id = app.id
        AND """ + _site_handoff("handoff") + """
    ) AS site_handoff FROM applications app
    LEFT JOIN application_runs run ON run.id = COALESCE(
        (SELECT v.run_id FROM apply_reservations v JOIN application_runs held ON held.id = v.run_id
         WHERE v.application_id = app.id AND held.application_id = app.id
         AND app.sent_run_id IS NOT held.id
         AND (held.status != 'running' OR owner_confirmed_dead(held.pid))
         AND v.released_at IS NULL ORDER BY v.pending, v.id DESC LIMIT 1),
        (SELECT r.id FROM application_runs r WHERE r.application_id = app.id AND app.status = 'failed'
         AND r.mode = 'submit' AND r.status != 'running' AND r.operator_not_sent_at IS NULL
         AND (r.submit_clicked_at IS NOT NULL OR (r.nav_clicked_at IS NOT NULL AND NOT EXISTS (
             SELECT 1 FROM apply_reservations v WHERE v.run_id = r.id AND v.pending = 1 AND v.released_at IS NOT NULL
         ))) ORDER BY r.id DESC LIMIT 1)
    )
"""


def _outstanding_row(c, application_id: int):
    c.create_function("owner_confirmed_dead", 1, _owner_confirmed_dead)
    return c.execute(_OUTSTANDING_RUNS + " WHERE app.id = ? AND run.id IS NOT NULL", (application_id,)).fetchone()


def outstanding_attempt(application_id: int) -> ApplyRun | None:
    """The attempt a human has to settle: the run that owns the application's unreleased reservation, else the newest run
    that reached a click while the application is `failed`. Later runs (refused, dry runs) never hide it."""
    with connect() as c:
        r = _outstanding_row(c, application_id)
    return _from_row(r) if r else None


def requires_manual_retry(application_id: int) -> bool:
    """A verified non-send can only be retried by an explicit manual send."""
    with connect() as c:
        return c.execute("SELECT 1 FROM application_runs WHERE application_id=? AND operator_not_sent_at IS NOT NULL LIMIT 1",
                         (application_id,)).fetchone() is not None


def has_site_handoff(application_id: int) -> bool:
    """A site handoff requires manual follow-up; unrelated attempts cannot clear it."""
    with connect() as conn:
        return conn.execute("SELECT 1 FROM application_runs r WHERE r.application_id=? AND " + _site_handoff("r") + " LIMIT 1",
                            (application_id,)).fetchone() is not None


def queue_attempts(profile_id: int) -> tuple[dict[int, ApplyRun], set[int], set[int]]:
    """Read verification holds, manual retries and site handoffs for a queue."""
    with connect() as conn:
        conn.create_function("owner_confirmed_dead", 1, _owner_confirmed_dead)
        rows = conn.execute(_OUTSTANDING_RUNS + " WHERE app.profile_id = ? AND app.status NOT IN ('submitted','withdrawn')",
                            (profile_id,)).fetchall()
    return ({row["owner_application_id"]: _from_row(row) for row in rows if row["id"] is not None},
            {row["owner_application_id"] for row in rows if row["manual_retry"]},
            {row["owner_application_id"] for row in rows if row["site_handoff"]})


def auto_regens(application_id: int) -> int:
    """How many times the automatic pass regenerated this application's CV for blocking errors."""
    with connect() as c:
        r = c.execute("SELECT auto_regens FROM applications WHERE id=?", (application_id,)).fetchone()
    return int(r["auto_regens"]) if r else 0


def bump_auto_regens(application_id: int) -> int:
    """+1 on the counter (atomic); returns the new value."""
    with connect() as c:
        c.execute("BEGIN IMMEDIATE")
        c.execute("UPDATE applications SET auto_regens = auto_regens + 1 WHERE id=?", (application_id,))
        r = c.execute("SELECT auto_regens FROM applications WHERE id=?", (application_id,)).fetchone()
    return int(r["auto_regens"]) if r else 0


def sent_run(application_id: int) -> ApplyRun | None:
    """The run recorded as THE sending attempt of the application (`applications.sent_run_id`), if any."""
    with connect() as c:
        r = c.execute("SELECT r.* FROM application_runs r JOIN applications a ON a.sent_run_id=r.id"
                      " WHERE a.id=? AND r.application_id=a.id", (application_id,)).fetchone()
    return _from_row(r) if r else None


def _save_receipt(conn, run_id: int, receipt: Receipt) -> None:
    conn.execute("UPDATE apply_reservations SET receipt_kind=?, receipt_outcome=?, receipt_at=?"
                 " WHERE run_id=? AND released_at IS NULL", (receipt.kind, receipt.outcome, _iso(now()), run_id))


def held_binding(run_id: int) -> Attempt | None:
    with connect() as conn:
        row = conn.execute("SELECT v.binding_json, j.origin_url, j.extra FROM apply_reservations v JOIN applications a"
                           " ON a.id=v.application_id JOIN jobs j ON j.id=a.job_id AND j.profile_id=a.profile_id"
                           " WHERE v.run_id=? AND v.released_at IS NULL", (run_id,)).fetchone()
    if not row or not row["binding_json"]:
        return None
    data = json.loads(row["binding_json"])
    url = dispatch_url(row["origin_url"], row["extra"], autonomous=data.get("autonomous") is True)
    if not url or data.pop("url_digest", None) != _url_key(url):
        return None
    return Attempt(**{**data, "url": url})


def resolve_attempt(application_id: int, *, sent: bool, run_id: int | None = None, receipt: Receipt | None = None) -> None:
    """Human decision after a possibly-sent attempt, in ONE transaction (reservation,
    application status, job status). It is bound to the OUTSTANDING attempt (`outstanding_attempt`): `run_id`
    must be that run, else "page périmée". Without any such attempt: refused. Only that attempt's reservation is touched.
    sent=True: submitted (+ job applied, `sent_run_id` = that run), reservation kept firm. sent=False: reservation released,
    back to draft unless another attempt still needs verification."""
    from jobhunt.cv import store as cvstore  # noqa: PLC0415

    with connect() as c:
        c.execute("BEGIN IMMEDIATE")
        a = c.execute("SELECT status, profile_id, job_id, current_rev FROM applications WHERE id=?", (application_id,)).fetchone()
        if a is None:
            raise ApplyError("Candidature introuvable")
        if a["status"] not in ("failed", "draft", "approved"):
            raise ApplyError("Cette candidature n'est pas en échec d'envoi")
        out = _outstanding_row(c, application_id)
        if out is None:
            if a["status"] != "failed":
                raise ApplyError("Cette candidature n'est pas en échec d'envoi")
            raise ApplyError("Cette candidature n'a jamais atteint le clic d'envoi : rien à confirmer")
        if out["id"] != run_id:
            raise ApplyError("Page périmée : un autre essai a eu lieu depuis, recharger la page")
        for running in c.execute("SELECT id, pid FROM application_runs WHERE application_id=? AND mode IN ('submit','supervised') AND status='running'",
                                 (application_id,)).fetchall():
            if running["id"] != run_id or not _owner_confirmed_dead(running["pid"]):
                raise ApplyError("Un envoi est encore en cours : vérifier de nouveau après sa fin")
            interrupted = _failure_result(RunResult(status="failed", message="interrompu", finished_at=now()))
            c.execute("UPDATE application_runs SET status='failed', message=?, result_json=?, finished_at=? WHERE id=?",
                      (interrupted.message, interrupted.model_dump_json(), _iso(interrupted.finished_at), run_id))
        held = c.execute("SELECT url_key, binding_json FROM apply_reservations WHERE run_id=? AND released_at IS NULL", (out["id"],)).fetchone()
        if receipt:
            attempt = receipt.attempt
            job = c.execute("SELECT origin_url FROM jobs WHERE id=? AND profile_id=?", (a["job_id"], a["profile_id"])).fetchone()
            if (not held or not attempt.adapter or not clean_receipt(receipt, attempt) or attempt.run_id != run_id
                    or attempt.application_id != application_id or attempt.rev != a["current_rev"]
                    or not job or clean_url(job["origin_url"], navigation=True) != attempt.url
                    or held["binding_json"] != _binding_json(attempt) or sent != (receipt.outcome == "confirmed")):
                raise ApplyError("Reçu périmé ou différent de la réservation.")
            _save_receipt(c, run_id, receipt)
        if a["status"] != "failed":
            cvstore.tx_force_failed(c, application_id, UNCONFIRMED_NOTE)
        if sent:
            held_record = json.loads(held["binding_json"]) if held and held["binding_json"] else {}
            cvstore.tx_mark_sent_after_failure(c, application_id, sent_origin_url=held_record.get("url") or
                                              (held["url_key"] if held and held["url_key"] else None))
            c.execute("UPDATE applications SET sent_run_id=? WHERE id=?", (out["id"], application_id))
            c.execute("UPDATE apply_reservations SET pending=0 WHERE run_id=? AND released_at IS NULL", (out["id"],))
            c.execute("UPDATE jobs SET status='applied' WHERE id=? AND profile_id=?", (a["job_id"], a["profile_id"]))
            if receipt:
                result = RunResult(status="submitted", message="Candidature envoyée (reçu vérifié après rapprochement).",
                                   finished_at=now())
                c.execute("UPDATE application_runs SET status=?, message=?, result_json=? WHERE id=?",
                          (result.status, result.message, result.model_dump_json(), run_id))
        else:
            resolved_at = _iso(now())
            c.execute("UPDATE apply_reservations SET released_at=? WHERE run_id=? AND released_at IS NULL", (resolved_at, run_id))
            c.execute("UPDATE application_runs SET operator_not_sent_at=? WHERE id=?", (resolved_at, run_id))
            if _outstanding_row(c, application_id) is None:
                cvstore.tx_set_status(c, application_id, "draft")


def get_run(run_id: int) -> ApplyRun | None:
    with connect() as c:
        r = c.execute("SELECT * FROM application_runs WHERE id=?", (run_id,)).fetchone()
    return _from_row(r) if r else None


def last_run(application_id: int) -> ApplyRun | None:
    with connect() as c:
        r = c.execute("SELECT * FROM application_runs WHERE application_id=? ORDER BY id DESC LIMIT 1", (application_id,)).fetchone()
    return _from_row(r) if r else None


def list_runs(application_id: int) -> list[ApplyRun]:
    """Newest first."""
    with connect() as c:
        rows = c.execute("SELECT * FROM application_runs WHERE application_id=? ORDER BY id DESC", (application_id,)).fetchall()
    return [_from_row(r) for r in rows]


def latest_runs(profile_id: int) -> dict[int, ApplyRun]:
    """application_id -> its newest run, for every application of the profile."""
    with connect() as c:
        rows = c.execute("SELECT r.* FROM application_runs r JOIN applications a ON a.id=r.application_id"
                         " WHERE a.profile_id=? AND r.id=(SELECT MAX(id) FROM application_runs WHERE application_id=r.application_id)",
                         (profile_id,)).fetchall()
    return {r["application_id"]: _from_row(r) for r in rows}


def email_scope(run_id: int, candidates: list[str]) -> str:
    """Restore a scope only from current trusted URLs matching the released reservation digest."""
    with connect() as conn:
        row = conn.execute('SELECT binding_json FROM apply_reservations WHERE run_id=? AND released_at IS NOT NULL '
                           'AND receipt_outcome IS NULL', (run_id,)).fetchone()
    binding = json.loads(row[0]) if row and row[0] else {}
    if not binding.get('autonomous'):
        return ''
    digest = binding.get('landing_digest') or binding.get('url_digest')
    return next((candidate for candidate in candidates if _url_key(candidate) == digest), '')


def record_email_resume(run: ApplyRun, code: str) -> bool:
    if code not in {'email_link_used', 'email_not_found', 'email_auth_failed'} or not run.result:
        return False
    result = run.result.model_copy(deep=True)
    result.dispatch_diagnostics.email = code
    with connect() as conn:
        cursor = conn.execute("UPDATE application_runs SET email_resumed=?, result_json=? WHERE id=? AND email_resumed=0 "
                              "AND status='needs_email' AND id=(SELECT MAX(id) FROM application_runs WHERE application_id=?) "
                              "AND EXISTS (SELECT 1 FROM applications WHERE id=? AND status='approved' AND current_rev=? "
                              "AND updated_at=?)", (code == 'email_link_used', result.model_dump_json(), run.id,
                                                     run.application_id, run.application_id, run.rev, run.application_version))
    return cursor.rowcount == 1


def _binding_current(conn, run, attempt: Attempt) -> bool:
    row = conn.execute("SELECT a.status, a.updated_at, a.current_rev, a.approved_rev, a.approved_sha256, j.origin_url, j.extra,"
                       " j.status AS job_status FROM applications a JOIN jobs j ON j.id=a.job_id AND j.profile_id=a.profile_id"
                       " WHERE a.id=?", (attempt.application_id,)).fetchone()
    return bool(row and row["status"] == "approved" and row["updated_at"] == run["application_version"]
                and row["current_rev"] == row["approved_rev"] == attempt.rev
                and row["approved_sha256"] == attempt.pdf_sha256
                and dispatch_url(row["origin_url"], row["extra"], autonomous=attempt.autonomous) == attempt.url
                and row["job_status"] not in ("applied", "rejected"))


def binding_active(attempt: Attempt) -> bool:
    with connect() as conn:
        run = conn.execute("SELECT r.* FROM application_runs r JOIN apply_reservations v ON v.run_id=r.id"
                           " WHERE r.id=? AND r.status='running' AND v.released_at IS NULL AND v.binding_json=?",
                           (attempt.run_id, _binding_json(attempt))).fetchone()
        return bool(run and run["application_id"] == attempt.application_id and run["rev"] == attempt.rev
                    and _binding_current(conn, run, attempt))


def bind_apply_landing(attempt: Attempt, target: str, *, method="requisition", confirmed_distinct=frozenset()) -> Attempt:
    from urllib.parse import urlsplit
    from jobhunt.apply.identity import ats_target

    host = urlsplit(target).hostname
    if (method not in {"requisition", "continuity"} or not attempt.autonomous or attempt.landing_digest
            or not host or clean_url(target, navigation=True) != target):
        raise ApplyError(NOT_ACTIVE)
    _, canonical = ats_target(target)
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        run = conn.execute("SELECT r.* FROM application_runs r JOIN apply_reservations v ON v.run_id=r.id"
                           " WHERE r.id=? AND r.status='running' AND r.submit_clicked_at IS NULL"
                           " AND v.released_at IS NULL AND v.binding_json=?", (attempt.run_id, _binding_json(attempt))).fetchone()
        if not run or not _binding_current(conn, run, attempt) or _host_paused(conn, host):
            raise ApplyError(NOT_ACTIVE)
        digest = _url_key(target)
        # An account page is the landing of EVERY posting of that employer: its address says nothing about which posting this is
        # (job identity, origin URL and role similarity still do), so it must not make one posting a duplicate of another.
        generic = bool(ACCOUNT_LANDING_RE.search(urlsplit(target).path))
        duplicates = _potential_duplicates(conn, attempt.profile_id, attempt.application_id,
                                           canonical or canonical_identity(attempt.tenant, attempt.requisition_id),
                                           target_digests=() if generic else (digest,))
        if any(exact or app_id not in confirmed_distinct for app_id, exact in duplicates.items()):
            raise ApplyError(NOT_ACTIVE)
        updated = replace(attempt, landing_host=host, landing_digest=digest, landing_binding=method,
                          tenant=canonical[0] if canonical else attempt.tenant,
                          requisition_id=canonical[1] if canonical else attempt.requisition_id)
        conn.execute("UPDATE apply_reservations SET binding_json=? WHERE run_id=?", (_binding_json(updated), attempt.run_id))
        return updated


def stale_engine_pause(run: ApplyRun | None) -> bool:
    """`run` is an engine-caused pause judged by code older than ENGINE_RETRY_EPOCH, and it cannot have sent anything."""
    if (run is not None and run.status == "needs_input" and run.result is not None
            and run.result.diagnostic_code == "credential_page" and not run.result.submit_clicked
            and run.submit_clicked_at is None and run.nav_clicked_at is None and _iso(run.started_at) < ENGINE_RETRY_EPOCH):
        return True  # an account page the older engine could not get through (it now creates accounts): retry once
    return bool(run is not None and run.status == "needs_human" and run.result is not None
                and run.result.diagnostic_code in ENGINE_PAUSE_CODES and not run.result.submit_clicked
                and run.submit_clicked_at is None and _iso(run.started_at) < ENGINE_RETRY_EPOCH)


def counts_as_failure(run: ApplyRun) -> bool:
    """`run` counts towards the two failed attempts that end automatic retries: a failure judged by code older than
    ENGINE_RETRY_EPOCH that never reached a click (nothing can have been sent) does not."""
    if run.status != "failed":
        return False
    clicked = (run.submit_clicked_at is not None or run.nav_clicked_at is not None
               or bool(run.result is not None and run.result.submit_clicked))
    return clicked or _iso(run.started_at) >= ENGINE_RETRY_EPOCH


def _host_paused(conn, host: str) -> bool:
    outcomes = conn.execute("""SELECT r.status, v.receipt_outcome FROM apply_reservations v
        JOIN application_runs r ON r.id=v.run_id
        WHERE r.finished_at IS NOT NULL AND json_extract(v.binding_json, '$.autonomous')=1
        AND ? IN (json_extract(v.binding_json, '$.host'), json_extract(v.binding_json, '$.landing_host'))
        AND NOT (r.status='failed' AND r.started_at < ? AND r.submit_clicked_at IS NULL
                 AND COALESCE(v.receipt_outcome, '') != 'uncertain')
        ORDER BY v.id DESC LIMIT 2""", (host, ENGINE_RETRY_EPOCH)).fetchall()
    return len(outcomes) == 2 and all(row["status"] == "failed" or row["receipt_outcome"] == "uncertain" for row in outcomes)


def host_paused(host: str) -> bool:
    with connect() as conn:
        return _host_paused(conn, host)


def paused_hosts() -> set[str]:
    with connect() as conn:
        rows = conn.execute("""WITH eligible AS (
            SELECT v.id, json_extract(v.binding_json, '$.host') AS host,
            json_extract(v.binding_json, '$.landing_host') AS landing, v.receipt_outcome, r.status
            FROM apply_reservations v JOIN application_runs r ON r.id=v.run_id
            WHERE r.finished_at IS NOT NULL AND json_extract(v.binding_json, '$.autonomous')=1
            AND NOT (r.status='failed' AND r.started_at < ? AND r.submit_clicked_at IS NULL
                     AND COALESCE(v.receipt_outcome, '') != 'uncertain')
        ), hosts AS (
            SELECT id, host, receipt_outcome, status FROM eligible
            UNION ALL SELECT id, landing, receipt_outcome, status FROM eligible WHERE landing != host
        ), host_runs AS (
            SELECT host, receipt_outcome, status, ROW_NUMBER() OVER (PARTITION BY host ORDER BY id DESC) AS pos
            FROM hosts
        ) SELECT host FROM host_runs WHERE pos<=2 GROUP BY host
        HAVING SUM(CASE WHEN status='failed' OR receipt_outcome='uncertain' THEN 1 ELSE 0 END)=2""",
                            (ENGINE_RETRY_EPOCH,)).fetchall()
        return {row["host"] for row in rows}


def mark_autonomous_submit(attempt: Attempt) -> None:
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        run = conn.execute("SELECT r.* FROM application_runs r JOIN apply_reservations v ON v.run_id=r.id"
                           " WHERE r.id=? AND r.status='running' AND r.submit_clicked_at IS NULL"
                           " AND v.released_at IS NULL AND v.binding_json=?", (attempt.run_id, _binding_json(attempt))).fetchone()
        if not attempt.autonomous or not run or not _binding_current(conn, run, attempt):
            raise ApplyError(NOT_ACTIVE)
        conn.execute("UPDATE application_runs SET submit_clicked_at=? WHERE id=?", (_iso(now()), attempt.run_id))


def _potential_duplicates(conn, profile_id: int, application_id: int, canonical=None, *, target_digests=()) -> dict[int, bool]:
    current = conn.execute("SELECT j.id, j.company, j.title, j.extra, j.origin_url FROM applications a JOIN jobs j"
                           " ON j.id=a.job_id AND j.profile_id=a.profile_id WHERE a.id=? AND a.profile_id=?",
                           (application_id, profile_id)).fetchone()
    if current is None:
        return {}
    identities = {dedupe_identity(canonical), dedupe_identity(resolved_identity(current["extra"]))} - {None}
    current_url = clean_url(current["origin_url"])
    digests = {digest for digest in target_digests if digest}
    if current_url:
        digests.add(_url_key(current_url))
    rows = conn.execute("""SELECT a.id, a.job_id, COALESCE(j.company,a.company) AS company,
        COALESCE(j.title,a.job_title) AS title, j.extra, j.origin_url, a.sent_origin_url,
        v.binding_json, v.url_key FROM applications a LEFT JOIN jobs j ON j.id=a.job_id AND j.profile_id=a.profile_id
        LEFT JOIN apply_reservations v ON v.application_id=a.id AND
            (v.released_at IS NULL OR v.receipt_outcome='confirmed' OR v.run_id=a.sent_run_id)
        WHERE a.profile_id=? AND a.id<>? AND (a.status='submitted' OR v.id IS NOT NULL OR EXISTS (
            SELECT 1 FROM application_runs r WHERE r.application_id=a.id AND r.operator_not_sent_at IS NULL
            AND (r.submit_clicked_at IS NOT NULL OR r.nav_clicked_at IS NOT NULL)))""",
                        (profile_id, application_id))
    found = {}
    for row in rows:
        try:
            binding = json.loads(row["binding_json"] or "{}")
        except ValueError:
            binding = {}
        if not isinstance(binding, dict):
            binding = {}
        held = dedupe_identity((binding.get("tenant"), binding.get("requisition_id")))
        resolved = dedupe_identity(resolved_identity(row["extra"]))
        exact = (current["id"] == row["job_id"] or held in identities or resolved in identities
                 or any(digest in digests for digest in (row["url_key"], binding.get("url_digest"), binding.get("landing_digest"))
                        if isinstance(digest, str))
                 or bool(current_url and (row["url_key"] in (_url_key(current_url), canonical_url(current_url))
                         or any(current_url == clean_url(row[key]) for key in ("origin_url", "url_key", "sent_origin_url")))))
        if exact or similar_role(current["company"], current["title"], row["company"], row["title"]):
            found[row["id"]] = exact or found.get(row["id"], False)
    return found


def potential_duplicates(profile_id: int, application_id: int) -> dict[int, bool]:
    with connect() as conn:
        return _potential_duplicates(conn, profile_id, application_id)


def _confirmation_tokens(conn, profile_id: int, application_id: int, others) -> dict[int, str]:
    ids = {application_id, *others}
    if len(ids) < 2:
        return {}
    rows = conn.execute("SELECT a.id,j.company,j.title,j.location,j.origin_url,j.description,j.extra FROM applications a"
                        " JOIN jobs j ON j.id=a.job_id AND j.profile_id=a.profile_id WHERE a.profile_id=?"
                        f" AND a.id IN ({','.join('?' for _ in ids)})", (profile_id, *ids))
    fingerprints = {row["id"]: posting_fingerprint(row) for row in rows}
    if application_id not in fingerprints:
        return {}
    return {other: hashlib.sha256(json.dumps([application_id, fingerprints[application_id], other, fingerprint])
                                  .encode()).hexdigest() for other, fingerprint in fingerprints.items() if other != application_id}


def confirmation_tokens(profile_id: int, application_id: int) -> dict[int, str]:
    with connect() as conn:
        duplicates = _potential_duplicates(conn, profile_id, application_id)
        return _confirmation_tokens(conn, profile_id, application_id, [aid for aid, exact in duplicates.items() if not exact])


def reserve_send(run_id: int, *, profile_id: int, application_id: int, trigger: str, role_key: str, url_key: str,
                 daily_cap: int, pending: bool = False, at: datetime | None = None, binding: Attempt | None = None,
                 duplicate_confirmations: tuple[str, ...] = ()) -> None:
    """The last gate before a click that may send the form, in ONE `BEGIN IMMEDIATE` transaction. The run must still be
    `running`, belong to the application and be its newest run (else ApplyError). Idempotent per run: a run that already holds
    an unreleased reservation only gets it upgraded (pending -> firm) and never fails on its own reservation. Otherwise: for
    trigger `auto` the Paris day still has room (unreleased reservations, reservation time = click time); no other unreleased
    reservation of the profile has the same application (an earlier run: "possibly sent"), role or (non-empty) URL; then the
    reservation is inserted. `pending=True` (navigation click: the form may be submitted by it, the engine will tell) sets
    `nav_clicked_at`; `pending=False` (final click) sets `submit_clicked_at` and makes the reservation firm.
    Raises ApplyError when a check fails (the engine then does not click). A firm reservation is never released
    automatically: a possibly-sent attempt keeps using the quota and keeps blocking duplicates until a human decides."""
    from jobhunt.freshness import start_of_today_paris

    ts = at or now()
    with connect() as c:
        c.execute("BEGIN IMMEDIATE")
        run = c.execute("SELECT * FROM application_runs WHERE id=?", (run_id,)).fetchone()
        if (run is None or run["status"] != "running" or run["application_id"] != application_id
                or c.execute("SELECT MAX(id) FROM application_runs WHERE application_id=?", (application_id,)).fetchone()[0] != run_id):
            raise ApplyError(NOT_ACTIVE)
        if binding and (binding.run_id != run_id or binding.application_id != application_id or binding.rev != run["rev"]
                        or binding.profile_id != profile_id or not binding.dedupe_key
                        or run["trigger"] != trigger or run["mode"] not in ("submit", "supervised")
                        or (run["mode"] == "submit" and not (binding.adapter or binding.autonomous))
                        or not _binding_current(c, run, binding)):
            raise ApplyError("Approbation ou destination modifiée avant l'envoi")
        confirmed_distinct = frozenset()
        if binding:
            if binding.autonomous and _host_paused(c, binding.host):
                raise ApplyError("À envoyer en mode supervisé")
            duplicates = _potential_duplicates(c, profile_id, application_id,
                                               canonical_identity(binding.tenant, binding.requisition_id),
                                               target_digests=(_url_key(binding.url), binding.landing_digest))
            if any(duplicates.values()):
                raise ApplyError("Un essai existe déjà pour ce poste chez cet employeur.")
            if trigger == "user":
                tokens = _confirmation_tokens(c, profile_id, application_id, duplicates)
                confirmed_distinct = frozenset(other for other, token in tokens.items() if token in duplicate_confirmations)
            missing = sorted(set(duplicates) - confirmed_distinct)
            if missing:
                raise ApplyError("Doublon possible : confirmez « ce n'est pas un doublon » pour les candidatures "
                                 + ", ".join(f"n°{item}" for item in missing) + ".")
        own = c.execute("SELECT id, pending FROM apply_reservations WHERE run_id=? AND released_at IS NULL", (run_id,)).fetchone()
        if own is not None:
            if binding:
                raise ApplyError("Cet essai a déjà été réservé")
            if not pending:
                c.execute("UPDATE apply_reservations SET pending=0 WHERE id=?", (own["id"],))
                c.execute("UPDATE application_runs SET submit_clicked_at=COALESCE(submit_clicked_at, ?) WHERE id=?", (_iso(ts), run_id))
            return
        rows = c.execute("SELECT application_id, trigger, role_key, url_key, created_at, binding_json FROM apply_reservations"
                         " WHERE profile_id=? AND released_at IS NULL", (profile_id,)).fetchall()
        if trigger == "auto" or (binding and binding.autonomous):
            day = start_of_today_paris(ts)
            count = c.execute("""SELECT count(*) FROM apply_reservations WHERE profile_id=? AND created_at>=?
                AND ((trigger='auto' AND released_at IS NULL) OR json_extract(binding_json, '$.autonomous')=1)""",
                              (profile_id, _iso(day))).fetchone()[0]
            if count >= daily_cap:
                raise ApplyError(f"Plafond de {daily_cap} envois automatiques par jour atteint")
        for r in rows:
            if r["application_id"] == application_id:
                raise ApplyError("Un envoi de cette candidature a déjà été tenté : vérifier sur le site de l'entreprise")
            if ((role_key and role_key != "|" and r["role_key"] == role_key
                 and not (binding and trigger == "user" and r["application_id"] in confirmed_distinct))
                    or (url_key and r["url_key"] == url_key)):
                raise ApplyError("Candidature déjà envoyée (ou tentée) pour la même offre")
        if binding and c.execute("SELECT 1 FROM apply_reservations WHERE dedupe_key=? AND released_at IS NULL",
                                 (binding.dedupe_key,)).fetchone():
            raise ApplyError("Un essai existe déjà pour ce poste chez cet employeur.")
        if binding and trigger == "auto":
            # A released reservation only stops an automatic retry when its attempt may have sent the form (a click, a
            # receipt), a human verified the non-send (manual retry only) or its run is not over. A reservation the engine
            # released itself because it stopped before any click (browser_clear) proves nothing was sent.
            identity = dedupe_identity((binding.tenant, binding.requisition_id))
            released = c.execute("""SELECT v.dedupe_key, json_extract(v.binding_json, '$.tenant') AS tenant,
                    json_extract(v.binding_json, '$.requisition_id') AS requisition
                FROM apply_reservations v LEFT JOIN application_runs r ON r.id=v.run_id
                WHERE v.profile_id=? AND v.released_at IS NOT NULL AND (
                    r.id IS NULL OR r.status='running' OR r.submit_clicked_at IS NOT NULL OR r.nav_clicked_at IS NOT NULL
                    OR r.operator_not_sent_at IS NOT NULL OR v.receipt_outcome IS NOT NULL OR v.receipt_kind IS NOT NULL
                    OR COALESCE(json_extract(r.result_json, '$.submit_clicked'), 0) != 0)""", (profile_id,))
            if any(row["dedupe_key"] == binding.dedupe_key or (
                    identity and identity == dedupe_identity((row["tenant"], row["requisition"]))) for row in released):
                raise ApplyError("Nouvel essai manuel requis pour ce poste.")
        c.execute("INSERT INTO apply_reservations(profile_id, application_id, run_id, trigger, role_key, url_key, created_at, pending, binding_json, dedupe_key)"
                  " VALUES (?,?,?,?,?,?,?,?,?,?)",
                  (profile_id, application_id, run_id, trigger, role_key or "", url_key or "", _iso(ts), 1 if pending else 0,
                   _binding_json(binding) if binding else None, binding.dedupe_key if binding else None))
        if binding:
            return
        col = "nav_clicked_at" if pending else "submit_clicked_at"
        c.execute(f"UPDATE application_runs SET {col}=COALESCE({col}, ?) WHERE id=?", (_iso(ts), run_id))


def release_reservation(application_id: int) -> int:
    """Human action only ("elle n'a pas été envoyée"): frees the quota and the duplicate guard of the application's
    reservations. Returns how many were released."""
    with connect() as c:
        cur = c.execute("UPDATE apply_reservations SET released_at=? WHERE application_id=? AND released_at IS NULL",
                        (_iso(now()), application_id))
    return cur.rowcount


def active_reservations(profile_id: int) -> list[dict]:
    """Unreleased reservations of the profile: application_id, run_id, role_key, url_key, trigger, created_at, pending."""
    with connect() as c:
        rows = c.execute("SELECT application_id, run_id, trigger, role_key, url_key, created_at, pending FROM apply_reservations"
                         " WHERE profile_id=? AND released_at IS NULL ORDER BY id", (profile_id,)).fetchall()
    return [{**dict(r), "created_at": _dt(r["created_at"])} for r in rows]


def has_unconfirmed_click(application_id: int, *, except_run_id: int | None = None) -> bool:
    """A run of the application reached a click (navigation or final) and its reservation was not released: the form MAY have
    been sent. `except_run_id`: ignore that run's own reservation (the run that is asking)."""
    with connect() as c:
        r = c.execute("SELECT 1 FROM apply_reservations WHERE application_id=? AND released_at IS NULL AND run_id != ? LIMIT 1",
                      (application_id, except_run_id if except_run_id is not None else -1)).fetchone()
    return r is not None


def unconfirmed_application_ids(profile_id: int) -> set[int]:
    """Queue-display equivalent of has_unconfirmed_click, with no run excluded."""
    with connect() as conn:
        rows = conn.execute("SELECT DISTINCT a.id FROM applications a JOIN apply_reservations v ON v.application_id = a.id"
                            " WHERE a.profile_id = ? AND v.released_at IS NULL AND v.run_id != -1", (profile_id,))
        return {row["id"] for row in rows}


def count_auto_submitted(profile_id: int, day_start: datetime, *, except_run_id: int | None = None) -> int:
    """Automatic sends of the profile that reached a click (trigger auto, reservation not released) at/after `day_start`.
    A possibly-sent attempt counts until a human says it was not sent. `except_run_id`: leave out that run's reservation."""
    day_start = day_start if day_start.tzinfo else day_start.replace(tzinfo=UTC)
    return sum(1 for r in active_reservations(profile_id)
               if r["trigger"] == "auto" and r["created_at"] >= day_start and r["run_id"] != except_run_id)
