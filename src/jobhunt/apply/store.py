"""`application_runs`: one row per dry run / submission attempt of an application (the screenshots live in `out_dir`),
and `apply_reservations`: one per attempted send (quota + duplicate guard), pending (navigation click) or firm (final click)."""

from __future__ import annotations

import contextlib
import json
import os
import sys
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Callable

from pydantic import BaseModel

from jobhunt.apply.adapters import Attempt, Receipt, clean_receipt
from jobhunt.apply.errors import ApplyError
from jobhunt.apply.types import Mode, RunResult, RunStatus
from jobhunt.db import _dt, _iso, connect, now

_suppress = contextlib.suppress

UNCONFIRMED_NOTE = "Envoi non confirmé : vérifier sur le site de l'entreprise avant tout nouvel essai"
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


def _from_row(r) -> ApplyRun:
    return ApplyRun(
        id=r["id"], application_id=r["application_id"], rev=r["rev"], mode=r["mode"], trigger=r["trigger"], status=r["status"],
        message=r["message"] or "", result=RunResult.model_validate_json(r["result_json"]) if r["result_json"] else None,
        out_dir=r["out_dir"], started_at=_dt(r["started_at"]), finished_at=_dt(r["finished_at"]),
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
            c.execute("UPDATE application_runs SET status='failed', message='interrompu', finished_at=? WHERE id=?",
                      (_iso(ts), r["id"]))
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
    finished = result.finished_at or now()
    result = result.model_copy(update={"finished_at": finished})
    with connect() as c:
        cur = c.execute("UPDATE application_runs SET status=?, message=?, result_json=?, finished_at=? WHERE id=? AND status='running'",
                        (result.status, result.message, result.model_dump_json(), _iso(finished), run_id))
        if cur.rowcount == 0:
            raise ApplyError(NOT_ACTIVE)
    return get_run(run_id)  # type: ignore[return-value]


def save_progress(run_id: int, result: RunResult) -> None:
    """Persist a partial result (steps so far, `submit_clicked` as it is about to be) on a run that stays `running`, so a
    cancellation / crash after a click still has the record of what was typed. Only the still-running NEWEST run of its
    application: otherwise ApplyError (the engine then does not click)."""
    with connect() as c:
        c.execute("BEGIN IMMEDIATE")
        cur = c.execute(
            "UPDATE application_runs SET result_json=? WHERE id=? AND status='running' AND id=("
            "SELECT MAX(id) FROM application_runs WHERE application_id=(SELECT application_id FROM application_runs WHERE id=?))",
            (result.model_dump_json(), run_id, run_id))
        if cur.rowcount == 0:
            raise ApplyError(NOT_ACTIVE)


def finish_and_settle(run_id: int, result: RunResult, *, sent_origin_url: str | None = None,
                      receipt: Receipt | None = None) -> ApplyRun:
    """Preserve any possible send atomically; no generic engine result is an attempt-bound receipt.

    Only a typed receipt authenticated by a registered adapter and matching a live, durable
    reservation can settle automatically. Generic statuses and the legacy URL argument are not evidence.
    """
    from jobhunt.cv import store as cvstore  # noqa: PLC0415 - cv.store does not import this module; kept lazy anyway
    from jobhunt.cv.errors import CVError  # noqa: PLC0415

    finished = result.finished_at or now()
    ts = _iso(finished)
    with connect() as c:
        c.execute("BEGIN IMMEDIATE")
        run = c.execute("SELECT * FROM application_runs WHERE id=?", (run_id,)).fetchone()
        if (run is None or run["status"] != "running"
                or c.execute("SELECT MAX(id) FROM application_runs WHERE application_id=?", (run["application_id"],)).fetchone()[0] != run_id):
            raise ApplyError(NOT_ACTIVE)
        if run["mode"] in ("submit", "supervised"):
            held = c.execute("SELECT * FROM apply_reservations WHERE run_id=? AND released_at IS NULL", (run_id,)).fetchone()
            verified = False
            if (receipt and held and run["mode"] == "submit" and receipt.attempt.adapter and clean_receipt(receipt, receipt.attempt)
                    and receipt.attempt.run_id == run_id and receipt.attempt.application_id == run["application_id"]
                    and receipt.attempt.rev == run["rev"] and held["binding_json"] == json.dumps(asdict(receipt.attempt))
                    and _binding_current(c, run, receipt.attempt)):
                _save_receipt(c, run_id, receipt)
                if receipt.outcome == "confirmed":
                    cvstore.tx_mark_submitted(c, run["application_id"], run["rev"], sent_origin_url=held["url_key"])
                    c.execute("UPDATE applications SET sent_run_id=? WHERE id=?", (run_id, run["application_id"]))
                    c.execute("UPDATE jobs SET status='applied' WHERE id=(SELECT job_id FROM applications WHERE id=?)",
                              (run["application_id"],))
                    result = result.model_copy(update={"status": "submitted", "message": "Candidature envoyée (reçu vérifié)."})
                else:
                    c.execute("UPDATE apply_reservations SET released_at=? WHERE id=?", (ts, held["id"]))
                    c.execute("UPDATE application_runs SET operator_not_sent_at=? WHERE id=?", (ts, run_id))
                    cvstore.tx_set_status(c, run["application_id"], "draft")
                    result = RunResult(status="blocked", message="Non-envoi vérifié. Tout nouvel essai doit être lancé manuellement.")
                verified = True
            elif held and held["binding_json"]:
                with _suppress(CVError):
                    cvstore.tx_force_failed(c, run["application_id"], UNCONFIRMED_NOTE)
                status = "needs_input" if run["mode"] == "supervised" and result.status == "needs_input" else "failed"
                result = result.model_copy(update={"status": status, "submit_clicked": False,
                                                   "message": UNCONFIRMED_NOTE + ". " + result.message})
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
        c.execute("UPDATE application_runs SET status=?, message=?, result_json=?, finished_at=? WHERE id=?",
                  (result.status, result.message, result.model_copy(update={"finished_at": finished}).model_dump_json(), ts, run_id))
    return get_run(run_id)  # type: ignore[return-value]


_OUTSTANDING_RUNS = """
    SELECT run.*, app.id AS owner_application_id, EXISTS (
        SELECT 1 FROM application_runs retry WHERE retry.application_id = app.id
        AND retry.operator_not_sent_at IS NOT NULL
    ) AS manual_retry, EXISTS (
        SELECT 1 FROM application_runs handoff WHERE handoff.application_id = app.id
        AND handoff.status IN ('needs_email', 'needs_human')
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
        return conn.execute("SELECT 1 FROM application_runs WHERE application_id=? "
                            "AND status IN ('needs_email', 'needs_human') LIMIT 1",
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
        row = conn.execute("SELECT binding_json FROM apply_reservations WHERE run_id=? AND released_at IS NULL", (run_id,)).fetchone()
    return Attempt(**json.loads(row["binding_json"])) if row and row["binding_json"] else None


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
            c.execute("UPDATE application_runs SET status='failed', message='interrompu', finished_at=? WHERE id=?",
                      (_iso(now()), run_id))
        held = c.execute("SELECT url_key, binding_json FROM apply_reservations WHERE run_id=? AND released_at IS NULL", (out["id"],)).fetchone()
        if receipt:
            attempt = receipt.attempt
            job = c.execute("SELECT origin_url FROM jobs WHERE id=? AND profile_id=?", (a["job_id"], a["profile_id"])).fetchone()
            if (not held or not attempt.adapter or not clean_receipt(receipt, attempt) or attempt.run_id != run_id
                    or attempt.application_id != application_id or attempt.rev != a["current_rev"]
                    or not job or job["origin_url"] != attempt.url
                    or held["binding_json"] != json.dumps(asdict(attempt)) or sent != (receipt.outcome == "confirmed")):
                raise ApplyError("Reçu périmé ou différent de la réservation.")
            _save_receipt(c, run_id, receipt)
        if a["status"] != "failed":
            cvstore.tx_force_failed(c, application_id, UNCONFIRMED_NOTE)
        if sent:
            cvstore.tx_mark_sent_after_failure(c, application_id, sent_origin_url=held["url_key"] if held and held["url_key"] else None)
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


def _binding_current(conn, run, attempt: Attempt) -> bool:
    row = conn.execute("SELECT a.status, a.updated_at, a.current_rev, a.approved_rev, a.approved_sha256, j.origin_url,"
                       " j.status AS job_status FROM applications a JOIN jobs j ON j.id=a.job_id AND j.profile_id=a.profile_id"
                       " WHERE a.id=?", (attempt.application_id,)).fetchone()
    return bool(row and row["status"] == "approved" and row["updated_at"] == run["application_version"]
                and row["current_rev"] == row["approved_rev"] == attempt.rev
                and row["approved_sha256"] == attempt.pdf_sha256 and row["origin_url"] == attempt.url
                and row["job_status"] not in ("applied", "rejected"))


def binding_active(attempt: Attempt) -> bool:
    with connect() as conn:
        run = conn.execute("SELECT r.* FROM application_runs r JOIN apply_reservations v ON v.run_id=r.id"
                           " WHERE r.id=? AND r.status='running' AND v.released_at IS NULL AND v.binding_json=?",
                           (attempt.run_id, json.dumps(asdict(attempt)))).fetchone()
        return bool(run and run["application_id"] == attempt.application_id and run["rev"] == attempt.rev
                    and _binding_current(conn, run, attempt))


def reserve_send(run_id: int, *, profile_id: int, application_id: int, trigger: str, role_key: str, url_key: str,
                 daily_cap: int, pending: bool = False, at: datetime | None = None, binding: Attempt | None = None) -> None:
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
                        or (run["mode"] == "submit" and not binding.adapter)
                        or not _binding_current(c, run, binding)):
            raise ApplyError("Approbation ou destination modifiée avant l'envoi")
        own = c.execute("SELECT id, pending FROM apply_reservations WHERE run_id=? AND released_at IS NULL", (run_id,)).fetchone()
        if own is not None:
            if binding:
                raise ApplyError("Cet essai a déjà été réservé")
            if not pending:
                c.execute("UPDATE apply_reservations SET pending=0 WHERE id=?", (own["id"],))
                c.execute("UPDATE application_runs SET submit_clicked_at=COALESCE(submit_clicked_at, ?) WHERE id=?", (_iso(ts), run_id))
            return
        rows = c.execute("SELECT application_id, trigger, role_key, url_key, created_at FROM apply_reservations"
                         " WHERE profile_id=? AND released_at IS NULL", (profile_id,)).fetchall()
        if trigger == "auto":
            day = start_of_today_paris(ts)
            if sum(1 for r in rows if r["trigger"] == "auto" and _dt(r["created_at"]) >= day) >= daily_cap:
                raise ApplyError(f"Plafond de {daily_cap} envois automatiques par jour atteint")
        for r in rows:
            if r["application_id"] == application_id:
                raise ApplyError("Un envoi de cette candidature a déjà été tenté : vérifier sur le site de l'entreprise")
            if (role_key and role_key != "|" and r["role_key"] == role_key) or (url_key and r["url_key"] == url_key):
                raise ApplyError("Candidature déjà envoyée (ou tentée) pour la même offre")
        if binding and c.execute("SELECT 1 FROM apply_reservations WHERE dedupe_key=? AND released_at IS NULL",
                                 (binding.dedupe_key,)).fetchone():
            raise ApplyError("Un essai existe déjà pour ce poste chez cet employeur.")
        if binding and trigger == "auto" and c.execute(
                "SELECT 1 FROM apply_reservations WHERE dedupe_key=? AND released_at IS NOT NULL", (binding.dedupe_key,)).fetchone():
            raise ApplyError("Nouvel essai manuel requis pour ce poste.")
        c.execute("INSERT INTO apply_reservations(profile_id, application_id, run_id, trigger, role_key, url_key, created_at, pending, binding_json, dedupe_key)"
                  " VALUES (?,?,?,?,?,?,?,?,?,?)",
                  (profile_id, application_id, run_id, trigger, role_key or "", url_key or "", _iso(ts), 1 if pending else 0,
                   json.dumps(asdict(binding)) if binding else None, binding.dedupe_key if binding else None))
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
