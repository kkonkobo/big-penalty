"""Orchestration of milestone 2b: prepare -> (auto-)approve -> dry run / send on the company's site -> bookkeeping.

Public API (used by the CLI, the daily run and web/apply_routes.py). Guardrails live in apply/policy.py (who may be sent)
and apply/engine.py (what may be typed / clicked); this module wires them to the browser, the keychain and the stores.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import secrets
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, Field

from jobhunt import db
from jobhunt.apply import accounts, mail, policy
from jobhunt.apply.adapters import Attempt, CandidateFact, Dispatch, Handoff, Match, Receipt, WriteGate, clean_receipt, origin, registry
from jobhunt.apply.adapter_browser import AdapterAudience
from jobhunt.apply.autonomous import Audience
from jobhunt.apply.apply_entry import discover
from jobhunt.apply.entry_llm import FALLBACK_CODES, advisor_for, discover_llm
from jobhunt.apply import scoped_choices, store as apply_store
from jobhunt.apply.engine import UNSUPPORTED_DISPATCH, Uploads, run_application
from jobhunt.apply.page_outline import (DIAGNOSED_STATUSES, OUTLINE_FILE, note_stop_reason, save_failure_diagnostics,
                                        save_run_record)
from jobhunt.apply.destination import RECIPIENT_REFUSED, RecipientGuard, clean_url, contains_verification_code, is_bound_pdf
from jobhunt.apply.errors import ApplyError
from jobhunt.apply.identity import canonical_identity, dedupe_identity, dispatch_url, resolved_identity
from jobhunt.apply.facts import answers_path as facts_answers_path
from jobhunt.apply.facts import build_facts, custom_answers, load_answers, norm_question, save_custom_answers, trusted_hosts
from jobhunt.apply.missing_answers import editable_custom
from jobhunt.apply.prompt_rules import CREDENTIAL_ERROR
from jobhunt.apply.planner import LLMPlanner
from jobhunt.apply.store import ApplyRun
from jobhunt.apply.supervised import FillPreview, Session, UNAVAILABLE, local_bridge, require_posting
from jobhunt.apply.types import DispatchDiagnostics, FilledField, Mode, Question, RunResult
from jobhunt.apply.unattended import handoff_result
from jobhunt.browser import BrowserPool
from jobhunt.config import get_settings
from jobhunt.cv import service as cvs
from jobhunt.cv import store as cvstore
from jobhunt.cv.errors import CVError
from jobhunt.cv.master import MasterError, load_master
from jobhunt.cv.store import Application
from jobhunt.db import now
from jobhunt.filelock import try_lock, unlock
from jobhunt.freshness import age_days
from jobhunt.llm import LLMError, get_llm
from jobhunt.origin import MIN_CONFIDENCE, ats_of_host, is_aggregator
from jobhunt.sources.base import ChallengeRequired

# Where an application stands in the sending queue (one bucket per application):
#   to_validate = CV ready, waits for the user's OK (fit < auto threshold, or auto-send switched off / refused by a guardrail)
#   paused      = the last run needs the user: unknown question(s), e-mail verification link, CAPTCHA, a form hosted on a
#                 site the user has not trusted yet (pause_reason "host")
#   sending     = a run is in progress
#   sent        = submitted
#   needs_verification = an attempt may have sent; only the operator can settle it
#   failed      = a technical failure, with no outstanding send to settle
#   blocked     = cannot be sent as is (CV errors to fix, job too old / closed, duplicate, no company link)
Bucket = Literal["to_validate", "paused", "sending", "sent", "needs_verification", "failed", "blocked"]
SUPERVISED_ONLY = "À envoyer en mode supervisé"
SUPERVISED_HANDOFF = ("Vérifiez le formulaire et complétez-le si nécessaire. Vous seul pouvez cliquer sur Envoyer. "
                      "Fermez ensuite le navigateur et confirmez le résultat dans Big Penalty.")


class QueueItem(BaseModel):
    app: Application
    bucket: Bucket
    score: float | None = None
    origin_url: str | None = None
    origin_host: str = ""
    origin_sha: str = ""               # origin_digest(origin_url) at render time: bound by the send / resume forms
    age_days: float | None = None
    last_run: ApplyRun | None = None
    outstanding: ApplyRun | None = None
    auto: bool = False                 # fit >= auto threshold (sent without asking when automatic sending is on)
    problems: list[str] = Field(default_factory=list)   # why it cannot be sent now (policy.submit_problems, trigger "user")
    questions: list[Question] = Field(default_factory=list)  # paused on unknown questions
    pause_reason: str = ""             # "input" | "email" | "human" | "host" | ""
    untrusted_host: str = ""           # pause_reason "host": the host the form lives on
    selectable: bool = False           # may be ticked for "Envoyer les N sélectionnées"
    supervised: bool = False


class ApplySummary(BaseModel):
    """What the daily run did for one profile."""
    profile_id: int
    prepared: int = 0                  # CVs generated
    sent: int = 0                      # automatic submissions confirmed
    waiting: int = 0                   # applications waiting for the user's OK
    paused: int = 0                    # runs waiting for an answer / e-mail link / CAPTCHA
    failed: int = 0
    supervised: int = 0
    to_check: int = 0                  # sent attempts left unconfirmed: verify on the company's site
    to_fix: int = 0                    # CV with blocking errors: skipped until corrected
    paused_by: dict[str, int] = Field(default_factory=dict)   # pause reason (question / e-mail / CAPTCHA / compte / site non approuvé) -> n
    asked: dict[str, int] = Field(default_factory=dict)       # normalized unknown-question label -> how many paused runs asked it
    errors: list[str] = Field(default_factory=list)

    def top_questions(self, n: int = 10) -> list[tuple[str, int]]:
        """The `n` most frequent pause questions (normalized label, count), most frequent first."""
        return sorted(self.asked.items(), key=lambda kv: (-kv[1], kv[0]))[:n]

    def breakdown(self) -> str:
        """One line the owner can act on: sent, paused by reason, to check, to fix, blocked, technical failures."""
        paused = f"{self.paused} en pause" + (" (" + ", ".join(f"{k} {v}" for k, v in sorted(self.paused_by.items())) + ")" if self.paused_by else "")
        supervised = f" · {self.supervised} à envoyer en mode supervisé" if self.supervised else ""
        return (f"{self.sent} envoyée(s) · {paused} · {self.to_check} à vérifier · {self.to_fix} à corriger · "
                f"{self.waiting} bloquée(s) · {self.failed} échec(s) technique(s){supervised}")


BUCKET_ORDER: tuple[str, ...] = ("needs_verification", "to_validate", "paused", "sending", "failed", "blocked", "sent")
PAUSE_REASON = {"needs_input": "input", "needs_email": "email", "needs_human": "human"}
UNCONFIRMED_NOTE = apply_store.UNCONFIRMED_NOTE
SENT_NOT_RECORDED_NOTE = apply_store.SENT_NOT_RECORDED_NOTE
SEND_LOCK_NAME = "apply-send.lock"


# ---------------------------------------------------------------- queue
def _host(url: str | None) -> str:
    if not url:
        return ""
    try:
        return urlparse(url).hostname or ""
    except ValueError:
        return ""


def make_host_allowed(origin_url: str | None, trusted: list[str] | set[str] = ()) -> Callable[[str], bool]:
    """Predicate the engine uses before typing data / clicking on a page, frame or form-action host. Hosts are canonicalised
    first (lowercase, no port / userinfo / trailing dot, punycode; invalid -> refused). True for: the job's own site (same
    registrable domain as `origin_url`, public suffixes such as co.uk / github.io respected), any tenant of a known ATS
    (`origin.ats_of_host`, label-boundary safe) or a host the user trusts (the host itself or its registrable domain is in
    `trusted`). IP addresses and single-label names (localhost) are only allowed when listed exactly in `trusted`."""
    own_host = accounts.hostname(_host(origin_url))
    own = accounts.registrable_host(own_host)
    own_ok = bool(own) and "." in own and not accounts.is_ip(own) and not accounts.is_public_suffix(own)
    trusted_set = {h for h in (accounts.hostname(t) for t in trusted) if h}

    def allowed(host: str) -> bool:
        h = accounts.hostname(host or "")
        if not h:
            return False
        if accounts.is_ip(h) or "." not in h:
            return h in trusted_set
        if h in trusted_set:
            return True
        if accounts.is_public_suffix(h):
            return False
        reg = accounts.registrable_host(h)
        return (own_ok and reg == own) or ats_of_host(h) is not None or reg in trusted_set

    return allowed


def origin_digest(url: str | None) -> str:
    """sha256 hex of a job's `origin_url`: what a send form carries so the destination the user saw is the one used."""
    return hashlib.sha256(clean_url(url, navigation=True).encode("utf-8")).hexdigest()


def untrusted_host_of(run_: ApplyRun | None) -> str:
    """The host a `blocked` run refused to use (RunResult.untrusted_host), else ""."""
    return run_.result.untrusted_host if run_ is not None and run_.status == "blocked" and run_.result is not None else ""


def _host_pause(app: Application, job, run_: ApplyRun | None) -> str:
    """The host of the last run when the application is waiting for the user to trust it (and still untrusted), else ""."""
    host = untrusted_host_of(run_)
    if not host or run_ is None or run_.rev != app.current_rev:
        return ""
    try:
        trusted = trusted_hosts(app.profile_id)
    except ApplyError:
        trusted = []
    return "" if make_host_allowed(job.origin_url if job else None, trusted)(host) else host


def _draft_problems(app: Application, job, reads: policy.QueuePolicyReads) -> list[str]:
    """Why the application could not be sent even once the user approves it (a draft is judged as if approved)."""
    out: list[str] = []
    probe = app
    if app.status == "draft":
        probe = app.model_copy(update={"status": "approved", "approved_rev": app.current_rev, "approved_sha256": app.pdf_sha256})
        if app.errors:
            out.append(f"{len(app.errors)} erreur(s) bloquante(s) dans le CV")
    out += policy.queue_problems(probe, job, reads)
    if (job is None or job.profile_id != app.profile_id or job.id != app.job_id) and cvs.safe_pdf(app) is None:
        out.append("PDF absent")
    return list(dict.fromkeys(out))


def _item(app: Application, job, run_: ApplyRun | None, settings, reads: policy.QueuePolicyReads,
          outstanding: ApplyRun | None, manual_retry: bool, site_handoff: bool, paused_hosts: set[str]) -> QueueItem:
    problems = [] if app.status in ("submitted", "failed") else _draft_problems(app, job, reads)
    pause = ""
    questions: list[Question] = []
    untrusted = "" if app.status in ("submitted", "failed") else _host_pause(app, job, run_)
    if app.status == "submitted":
        bucket = "sent"
    elif outstanding:
        bucket = "needs_verification"
    elif app.status == "failed":
        bucket = "failed"
    elif run_ is not None and run_.status == "running" and now() - run_.started_at < apply_store.STALE_RUN_AFTER:
        bucket = "sending"
    elif run_ is not None and run_.status in PAUSE_REASON and not run_.email_resumed and run_.rev == app.current_rev:
        bucket, pause = "paused", PAUSE_REASON[run_.status]
        questions = list(run_.result.questions) if run_.result else []
    elif untrusted:
        bucket, pause = "paused", "host"
    elif site_handoff:
        bucket = "blocked"
        problems.append("intervention manuelle requise sur le site")
    elif problems:
        bucket = "blocked"
    elif run_ is not None and run_.status == "failed" and not run_.operator_not_sent_at:
        bucket = "failed"
    else:
        bucket = "to_validate"
    open_ = app.status in ("draft", "approved") and not problems
    adapted = bool(job and _unattended(job))
    generic = (_generic_enabled(job, check_host=False)
               and _host(dispatch_url(job.origin_url, job.extra, autonomous=True)) not in paused_hosts)
    autonomous_ready = generic and app.status == "approved" and not site_handoff
    if generic and run_ and run_.mode == "submit" and run_.status == "needs_human":
        autonomous_ready = False
    return QueueItem(
        app=app, bucket=bucket, score=job.score if job else None, origin_url=clean_url(job.origin_url, navigation=True) if job else None,  # type: ignore[arg-type]
        origin_host=_host(job.origin_url if job else None), origin_sha=origin_digest(job.origin_url if job else None), age_days=age_days(job) if job else None, last_run=run_,
        outstanding=outstanding,
        auto=bool((adapted or autonomous_ready) and not (manual_retry or outstanding or site_handoff) and job and job.score is not None
                  and job.score >= settings.apply_auto_min_score), problems=problems,
        questions=questions, pause_reason=pause, untrusted_host=untrusted if pause == "host" else "",
        selectable=(adapted or autonomous_ready) and (bucket == "to_validate" or (bucket in ("paused", "failed") and open_ and pause != "host")),
        supervised=not adapted and open_ and not outstanding and bucket not in ("sending", "blocked"))


def queue(profile_id: int | None = None) -> list[QueueItem]:
    """Every non-withdrawn application (of the profile, or of all profiles) with its bucket; best score first inside a bucket."""
    settings = get_settings()
    apps = cvstore.list_applications(profile_id)
    apps_by_profile: dict[int, dict[int, Application]] = {}
    for app in apps:
        apps_by_profile.setdefault(app.profile_id, {})[app.id] = app
    jobs = db.get_jobs(app.job_id for app in apps)
    paused_hosts = apply_store.paused_hosts() if settings.apply_autonomous and jobs else set()
    items = []
    for pid, profile_apps in apps_by_profile.items():
        visible = [app for app in profile_apps.values() if app.status != "withdrawn"]
        if not visible:
            continue
        runs = apply_store.latest_runs(pid)
        outstanding, manual_retry, site_handoffs = apply_store.queue_attempts(pid)
        reads = policy.QueuePolicyReads(pid, profile_apps, jobs, apply_store.active_reservations(pid),
                                        apply_store.unconfirmed_application_ids(pid))
        items.extend(_item(app, jobs.get(app.job_id), runs.get(app.id), settings, reads, outstanding.get(app.id),
                           app.id in manual_retry, app.id in site_handoffs, paused_hosts)
                     for app in visible)
    items.sort(key=lambda i: (BUCKET_ORDER.index(i.bucket), -(i.score if i.score is not None else -1.0), -i.app.id))
    return items


# ---------------------------------------------------------------- one run
class SendLocked(ApplyError):
    pass


@contextlib.contextmanager
def send_lock():
    """Exclusive non-blocking file lock `data/apply-send.lock`: one real send at a time across processes (the quota and
    duplicate checks of the final click are only meaningful then). NOT re-entrant and not inherited by child tasks: the
    outermost caller (`run` on its own, `send_selected`, `daily_apply`) takes it once and passes `_lock_held=True` down.
    Raises SendLocked when another process (or another task of this process) holds it. The kernel drops it when the process dies."""
    path = get_settings().data_dir / SEND_LOCK_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    locked = False
    try:
        if not try_lock(fd):
            raise SendLocked("un autre envoi est en cours")
        locked = True
        yield
    finally:
        if locked:
            unlock(fd)
        os.close(fd)  # the file stays


def _blocked_result(reasons: list[str]) -> RunResult:
    return RunResult(status="blocked", message="Envoi impossible : " + " ; ".join(reasons) if reasons else "Envoi impossible")


def _refuse(app: Application, job, mode: Mode, trigger: str, reasons: list[str]) -> ApplyRun:
    """A refusal is a finished `blocked` run (recorded, no browser)."""
    run_ = apply_store.start_run(app.id, app.current_rev, mode, trigger, lambda rid: _run_dir(app.job_id, rid))
    result = _blocked_result(reasons)
    if job and not policy._usable_origin(job):
        result.diagnostic_code = "aggregator_origin" if is_aggregator(job.origin_url) else "origin_not_usable"
    return apply_store.finish_run(run_.id, result)


def _run_dir(job_id: int, run_id: int) -> Path:
    d = get_settings().applications_dir / str(job_id) / "runs" / str(run_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _bound_problems(app: Application, job, expected_rev: int | None, expected_origin: str | None) -> list[str]:
    """What the user was shown (CV revision, destination digest) must still be what is about to be used."""
    out: list[str] = []
    if expected_rev is not None and app.current_rev != expected_rev:
        out.append("le CV a changé depuis l'affichage")
    if expected_origin is not None and (job is None or origin_digest(job.origin_url) != expected_origin):
        out.append("le lien de l'offre a changé depuis l'affichage")
    return out


def _gate(app: Application, job, mode: Mode, trigger: str, *, confirmed_distinct: frozenset[int] = frozenset()) -> list[str]:
    if app.status == "submitted":
        return ["candidature déjà envoyée"]
    if app.status == "withdrawn":
        return ["candidature retirée"]
    if job is not None and policy._usable_origin(job) and not clean_url(job.origin_url, navigation=True):
        return ["Lien de candidature non autorisé."]
    if mode in ("submit", "supervised"):
        if mode == "supervised" and (trigger != "user" or app.approved_by != "user"):
            return ["Le mode supervisé nécessite l'approbation de ce CV par l'utilisateur."]
        problems = policy.submit_problems(app, job, trigger=trigger, **_distinct(confirmed_distinct))
        return problems or (cvs.is_eligible(app)[1] if mode == "supervised" else [])
    out: list[str] = []
    if job is None or job.profile_id != app.profile_id or job.id != app.job_id:
        return ["offre introuvable pour ce profil"]
    if not policy._usable_origin(job):
        out.append("pas de lien vers le site de l'entreprise (http(s), hors agrégateur)")
    if cvs.safe_pdf(app) is None:
        out.append("PDF absent")
    return out


class _Llm:
    """Lazily created LLM client, closed only if we created it."""

    def __init__(self, llm=None):
        self.llm, self.own = llm, False

    def get(self):
        if self.llm is None:
            self.llm, self.own = get_llm(), True
        return self.llm

    async def aclose(self) -> None:
        if self.own and self.llm is not None:
            with contextlib.suppress(Exception):
                await self.llm.aclose()


def _default_verifier(llm: _Llm):
    """The LLM answer verifier of the engine (apply/answer_check.py); None while that module is not available."""
    try:
        from jobhunt.apply.answer_check import LLMAnswerVerifier  # noqa: PLC0415 - lazy: optional at import time
    except ImportError:
        return None
    return LLMAnswerVerifier(llm.get())


def _unattended(job) -> bool:
    match = registry.find(job.origin_url, job.origin_ats)
    return bool(match and match.capability == "unattended" and (
        not getattr(match.adapter, "browser_planner", None) or
        (get_settings().apply_autonomous and not apply_store.host_paused(_host(job.origin_url)))))


def _browser_adapter(job) -> bool:
    match = registry.find(job.origin_url, job.origin_ats)
    return bool(match and match.capability == "unattended" and getattr(match.adapter, "browser_planner", None))


def _dispatch_match(job, mode: Mode) -> Match | None:
    match = registry.find(job.origin_url, job.origin_ats)
    if mode == "supervised" and match and match.capability == "unattended" and getattr(match.adapter, "browser_planner", None):
        return Match(match.adapter, match.requisition, "supervised", getattr(match.adapter, "supervised_receipt_kind", match.receipt_kind))
    return match


def _generic_enabled(job, *, check_host: bool = True) -> bool:
    return bool(get_settings().apply_autonomous and job and policy._usable_origin(job)
                and clean_url(job.origin_url, navigation=True)
                and not ((match := registry.find(job.origin_url, job.origin_ats)) and match.capability == "unattended")
                and (not check_host or not apply_store.host_paused(_host(dispatch_url(job.origin_url, job.extra, autonomous=True)))))


def _distinct(ids: frozenset[int]) -> dict:
    return {"confirmed_distinct": ids} if ids else {}


def _check_boundary(run_: ApplyRun, started_job, mode: Mode, trigger: str, *, reserved: bool = False,
                    confirmed_distinct: frozenset[int] = frozenset()):
    live = cvstore.get_application(run_.application_id)
    job = db.get_job(started_job.id)
    if live is None or job is None or live.current_rev != run_.rev or job != started_job:
        raise ApplyError("La candidature ou la destination a changé.")
    problems = policy.submit_problems(live, job, trigger=trigger, run_id=run_.id if reserved else None,
                                      **_distinct(confirmed_distinct))
    if mode == "supervised":
        problems += cvs.is_eligible(live)[1]
        if live.approved_by != "user" or _unattended(job):
            problems.append("Mode supervisé indisponible pour cette approbation ou cette destination.")
    if problems:
        raise ApplyError(" ; ".join(dict.fromkeys(problems)))
    url = clean_url(job.origin_url, navigation=True)
    parsed = urlparse(url)
    if (not parsed.hostname or parsed.username is not None or parsed.password is not None or
            (parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "::1", "localhost"}))
            or (job.origin_confidence or 0) < MIN_CONFIDENCE):
        raise ApplyError("Destination de l'offre non vérifiée.")
    return live, job, parsed.hostname, url


def _reserve(run_: ApplyRun, started_job, mode: Mode, trigger: str, match: Match | None = None, *,
             browser_adapter: bool = False,
             confirmed_distinct: frozenset[int] = frozenset(), duplicate_confirmations: tuple[str, ...] = ()) -> Dispatch:
    app, job, host, url = _check_boundary(run_, started_job, mode, trigger, confirmed_distinct=confirmed_distinct)
    pdf_path = cvs.safe_pdf(app)
    if pdf_path is None:
        raise ApplyError("PDF absent")
    pdf = pdf_path.read_bytes()
    digest = hashlib.sha256(pdf).hexdigest()
    if digest != app.approved_sha256 or digest != app.pdf_sha256:
        raise ApplyError("PDF différent de celui approuvé")
    requisition = match.requisition if match else None
    resolved = resolved_identity(job.extra)
    adapter_identity = canonical_identity(requisition.tenant, requisition.requisition_id) if requisition else None
    adapter_dedupe = getattr(match.adapter, "dedupe_identity", None) if adapter_identity else None
    if adapter_dedupe:
        adapter_identity = adapter_dedupe(url) or adapter_identity
    metadata = job.extra.get("origin_resolution") if isinstance(job.extra, dict) else None
    if resolved and adapter_identity and (resolved != adapter_identity or (
            browser_adapter and metadata.get("canonical_ok") is True and metadata.get("submit_ats") != match.adapter.name)):
        raise ApplyError("Identité du poste incohérente : envoi annulé.")
    identity = resolved or adapter_identity
    dedupe = dedupe_identity(identity)
    canonical = ["req", *dedupe] if dedupe else ["job", job.id]
    dedupe_key = hashlib.sha256(json.dumps([app.profile_id, *canonical]).encode()).hexdigest()
    autonomous = mode == "submit" and (not match or match.capability != "unattended")
    if autonomous:
        url = dispatch_url(job.origin_url, job.extra, autonomous=True)
        host = _host(url)
    dispatch_match = None if autonomous and not browser_adapter else match
    autonomous = autonomous or browser_adapter
    tenant, requisition_id = ((requisition.tenant, requisition.requisition_id) if requisition and dispatch_match
                              else identity or ("", None))
    attempt = Attempt(run_.id, app.id, run_.rev, digest, url, host, secrets.token_urlsafe(24),
                      dispatch_match.adapter.name if dispatch_match else None, tenant, requisition_id, dedupe_key, app.profile_id,
                      dispatch_match.receipt_kind if dispatch_match else "none", autonomous=autonomous)
    facts = tuple(CandidateFact(fact.key, fact.label, fact.value, tuple(fact.variants))
                  for fact in _candidate_facts(app, job).facts if fact.value and not fact.secret) if dispatch_match else ()
    apply_store.reserve_send(run_.id, profile_id=app.profile_id, application_id=app.id, trigger=trigger,
                             role_key=policy.normalize_role(job.company, job.title), url_key=apply_store._url_key(url),
                             daily_cap=get_settings().apply_daily_cap, binding=attempt, duplicate_confirmations=duplicate_confirmations)
    return Dispatch(attempt, pdf, facts)


def _write_gate(request: Dispatch, run_: ApplyRun, job, mode: Mode, trigger: str, match: Match, *,
                confirmed_distinct: frozenset[int] = frozenset(), diagnostics: DispatchDiagnostics | None = None) -> WriteGate:
    def recheck() -> None:
        _check_boundary(run_, job, mode, trigger, reserved=True, confirmed_distinct=confirmed_distinct)
        if not apply_store.binding_active(request.attempt) or _dispatch_match(job, mode) != match:
            raise ApplyError("Réservation ou politique d'adaptateur périmée.")

    destinations = frozenset({origin(request.attempt.url), *match.requisition.write_origins})
    if any(not value or origin(value) != value for value in destinations):
        raise ApplyError("Destination d'adaptateur non vérifiée.")
    return WriteGate(capability=match.capability, policies=match.adapter.boundaries, origins=destinations,
                     recheck=recheck, dispatch=request, endpoints=match.requisition.endpoints, diagnostics=diagnostics)


def _candidate_facts(app: Application, job):
    profile = db.get_profile(app.profile_id)
    try:
        master = load_master(app.profile_id)
    except MasterError:
        master = None
    return build_facts(profile, master if master and master.is_confirmed else None, load_answers(app.profile_id), job,
                       employer=app.company)


_SUPERVISED: dict[int, Session] = {}


def _fill_session(app_id: int, run_id: int | None) -> Session:
    session = _SUPERVISED.get(app_id)
    if not session or (run_id is not None and session.attempt.run_id != run_id):
        raise ApplyError(UNAVAILABLE)
    return session


async def preview_fill_step(app_id: int, *, run_id: int | None = None) -> FillPreview:
    return await _fill_session(app_id, run_id).preview()


async def fill_step(app_id: int, token: str, *, run_id: int | None = None) -> RunResult:
    return await _fill_session(app_id, run_id).run(token)


async def _early_diagnose(page, run_, status: str, reason: str = "", code: str = "") -> None:
    """Outline + screenshot + run record for a stop the engine never saw (entry navigation, guard refusal, crash); best effort."""
    try:
        out_dir = Path(run_.out_dir) if run_.out_dir else None
        if out_dir is not None and not (out_dir / OUTLINE_FILE).is_file():
            await save_failure_diagnostics(page, out_dir, status=status, diagnostic_code=code, reason=reason)
    except Exception:
        pass


async def _drive_generic(app, job, run_, *, trigger, pool, planner, llm, verifier, browser, out, match,
                         confirmed_distinct, duplicate_confirmations, diagnostics, browser_adapter=False):
    if not (_unattended(job) if browser_adapter else _generic_enabled(job)):
        return RunResult(status="needs_human", message=SUPERVISED_ONLY)
    request = _reserve(run_, job, "submit", trigger, match, confirmed_distinct=confirmed_distinct,
                       duplicate_confirmations=duplicate_confirmations, browser_adapter=browser_adapter)
    attempt = request.attempt

    def check():
        live, _, _, _ = _check_boundary(run_, job, "submit", trigger, reserved=True, confirmed_distinct=confirmed_distinct)
        eligible, reasons = cvs.is_eligible(live)
        if not eligible:
            raise ApplyError(" ; ".join(reasons))
        if (not get_settings().apply_autonomous or registry.find(job.origin_url, job.origin_ats) != match
                or not apply_store.binding_active(attempt)
                or apply_store.host_paused(attempt.host)
                or (attempt.landing_host and apply_store.host_paused(attempt.landing_host))):
            raise ApplyError(SUPERVISED_ONLY)

    async def recheck():
        if guard.refused:
            return [RECIPIENT_REFUSED]
        try:
            check()
        except ApplyError as exc:
            return [str(exc)]
        return []

    metadata = job.extra.get("origin_resolution") if isinstance(job.extra, dict) else None
    raw_urls = metadata.get("verified_urls") if isinstance(metadata, dict) and metadata.get("canonical_ok") is True else None
    verified = [clean_url(url, navigation=True) for url in raw_urls if isinstance(url, str)] if isinstance(raw_urls, list) else []
    if browser_adapter:
        gate = _write_gate(request, run_, job, "submit", trigger, match, confirmed_distinct=confirmed_distinct)
        audience = AdapterAudience(attempt, match, gate, check, pdf_bytes=request.pdf)
        planner = match.adapter.browser_planner(attempt)
    else:
        audience = Audience(attempt.url, verified, check, pdf_bytes=request.pdf)
        expected = resolved_identity(job.extra)
        if expected and isinstance(metadata, dict) and isinstance(metadata.get("submit_ats"), str):
            from jobhunt.origin import workday_requisition

            ats = metadata["submit_ats"]
            audience.expected_identity = ats, (expected[0], workday_requisition(expected[1]) if ats == "workday" else expected[1])
    guard = RecipientGuard(frozenset({origin(attempt.url), *(match.requisition.write_origins if browser_adapter else ())}),
                           audience=audience)
    browser.append((attempt, guard))
    facts = _candidate_facts(app, job)
    email_fact = facts.get("account_email")
    email = email_fact.value if email_fact else None
    password_fact = facts.get("account_password")
    if email and password_fact:
        password_fact.value = "disponible"

    password, account_known = accounts.guarded_account_callbacks(
        email, lambda host: host == _host(audience.document) and audience.write_recipient(audience.document),
        posting_url=attempt.url,
    ) if email and (not browser_adapter or match.adapter.boundaries.get("account_creation") == "auto") else (None, None)

    async def mark_submit():
        apply_store.mark_autonomous_submit(attempt)
        guard.final_started = True
        audience.final_started = True

    async def unmark_submit():
        apply_store.unmark_autonomous_submit(attempt)
        guard.final_started = guard.final_response = False
        audience.final_started = audience.final_response = audience.final_json = False
        audience.final_receipt_url = ""

    if getattr(audience, "trusting", False):
        audience.unmark_submit = unmark_submit

    async def verify_mail():
        return await mail.poll(email, audience.recipient_url, run_.started_at)

    async def follow_mail(tab, link, still_current):
        if not await mail.open_link(tab.context, audience.recipient_url, link, still_current):
            return False
        diagnostics.email = 'email_link_used'
        if await still_current():
            return False
        target = audience.recipient_url
        with contextlib.suppress(ApplyError):
            accounts.set_state(_host(target), email, 'active', site=accounts.workday_site(target))
        audience.clicking = audience.account_click = False
        audience.next_document = audience.account_next = ''
        audience.active = False
        await guard.navigate(tab, target, lambda current, url: pool.goto(current, url, challenge_timeout_s=0))
        if guard.refused or not (audience.core or audience.initial_redirect(target, tab.url)):
            return False  # (core mode: the page reached after the verification link is followed as any other)
        audience.document = clean_url(tab.url, navigation=True)
        await audience.bind_document(tab)
        return True

    async def make_letter():
        """The cover letter of this revision, written only when a form requires one (rule-checked and verified; None = unavailable)."""
        from jobhunt.apply.letter import ensure_letter  # noqa: PLC0415 - letter imports the engine

        try:
            master = load_master(app.profile_id)
        except MasterError:
            return None
        if master is None or not master.is_confirmed:
            return None
        return await ensure_letter(cvstore.get_application(app.id) or app, job, master, llm.get())

    mail_flow = mail.BrowserVerification(verify_mail, follow_mail) if mail.provider(email) and (
        not browser_adapter or match.adapter.boundaries.get('account_creation') == 'auto') else None

    def refresh_accounts(target):
        nonlocal password, account_known
        if email and (not browser_adapter or match.adapter.boundaries.get("account_creation") == "auto"):
            password, account_known = accounts.guarded_account_callbacks(
                email, lambda host: host == _host(audience.document) and audience.write_recipient(audience.document),
                posting_url=target,
            )

    def live_known():
        """The (refreshable) account check handed to the engine, with its `unconfirm` hook."""
        if not account_known:
            return None
        known = lambda url: account_known(url)  # noqa: E731
        known.unconfirm = lambda url: getattr(account_known, "unconfirm", lambda _url: None)(url)  # type: ignore[attr-defined]
        known.confirm = lambda url: getattr(account_known, "confirm", lambda _url: None)(url)  # type: ignore[attr-defined]
        return known

    def bind_landing(target, method):
        """Bind the recipient to a landing reached before any input. The first landing is reserved durably (landing dedupe
        included); once one is reserved, later landings must stay on its registrable domain."""
        nonlocal attempt
        if attempt.landing_digest:
            if not audience.write_recipient(target):
                raise ApplyError(SUPERVISED_ONLY)
        else:
            attempt = apply_store.bind_apply_landing(attempt, target, method=method, confirmed_distinct=confirmed_distinct)
            browser[-1] = (attempt, guard)
        audience.bind_recipient(target)
        refresh_accounts(target)

    async def follow_landing(tab):
        """The page moved before any candidate data was entered (redirect, script, pre-data click): rebind the recipient
        to the landing when it is the same site or a recognized ATS with no conflicting posting identity."""
        target = clean_url(tab.url, navigation=True)
        if guard.refused or not target or not audience.landing_allowed(target):
            return False
        try:
            if not audience.write_recipient(target):
                bind_landing(target, "continuity")
        except ApplyError:
            return False
        audience.document = target
        await audience.bind_document(tab)
        guard.page = tab
        return True

    async def follow_core(tab):
        """Core mode: the page moved (redirect, locale switch, script, click, new tab): rebind to it, whatever it is. A new
        landing on another trusted site before any input is also reserved (landing dedupe), never required to proceed."""
        nonlocal attempt
        guard.page = tab
        if tab.is_closed():
            return False
        url = tab.url.split("#", 1)[0]
        if (not guard.refused and not audience.data_entered and audience.trusted(url) and not audience.write_recipient(url)):
            target = clean_url(url, navigation=True)
            if target and not attempt.landing_digest:
                attempt = apply_store.bind_apply_landing(attempt, target, method="continuity", confirmed_distinct=confirmed_distinct)
                browser[-1] = (attempt, guard)
            audience.bind_recipient(target or url)
            refresh_accounts(target or url)
        await audience.rebind(tab)
        return True

    if not browser_adapter:
        audience.follow_landing = follow_core if audience.core else follow_landing

    diagnostics.control = "navigation"
    entry_advisor = None if browser_adapter else advisor_for(planner, llm)
    async with pool.page(app.profile_id, f"apply-auto-{attempt.host}") as page:
        await guard.install(page)
        async def open_apply_core(tab, *, require_posting):
            """Core mode: look for the application entry, never stop for not finding or proving one (the engine goes on from the
            current page and its planner clicks "apply"). Only the reservation can refuse: a landing that duplicates another."""
            landing = target = None
            try:
                try:
                    landing, target = await discover(tab, guard, audience, requisition=attempt.requisition_id,
                                                     require_posting=require_posting, platform=job.origin_ats)
                except Exception as exc:
                    note_stop_reason(run_.out_dir, f"entry step: {type(exc).__name__}: {exc}")
                if (landing is None and not guard.refused and entry_advisor is not None and not tab.is_closed()
                        and target != "challenge_or_rate_limited"):
                    guard.probe = None
                    try:
                        found = await discover_llm(tab, guard, audience, entry_advisor, requisition=attempt.requisition_id,
                                                   platform=job.origin_ats, job_title=job.title, company=job.company or "")
                    except Exception as exc:
                        found = None
                        note_stop_reason(run_.out_dir, f"entry fallback: {type(exc).__name__}: {exc}")
                    if found is not None:
                        landing, target = found
                if landing is not None:
                    await follow_core(landing)
                return landing, target or "apply_target_unverified"
            except ApplyError as exc:
                note_stop_reason(run_.out_dir, f"entry step: {type(exc).__name__}: {exc}")
                return None, "reservation_refused"
            finally:
                guard.probe = None

        async def open_apply(tab, *, require_posting):
            nonlocal attempt, password, account_known
            if audience.core and not browser_adapter:
                return await open_apply_core(tab, require_posting=require_posting)
            try:
                landing, target = await discover(tab, guard, audience, requisition=attempt.requisition_id,
                                                 require_posting=require_posting, platform=job.origin_ats)
                if landing is None and target in FALLBACK_CODES and not guard.refused and entry_advisor is not None:
                    # Nothing was entered and the deterministic patterns did not match: let the planner's LLM find the way
                    # (close blockers, pick the apply control, follow it), under the same probe / guard / binding rules.
                    guard.probe = None
                    found = await discover_llm(tab, guard, audience, entry_advisor, requisition=attempt.requisition_id,
                                               platform=job.origin_ats, job_title=job.title, company=job.company or "")
                    if found is not None:
                        landing, target = found
                if landing is not None:
                    if clean_url(landing.url, navigation=True) != target or guard.refused:
                        return None, "apply_target_unverified"
                    bind_landing(target, audience.entry_binding)
                    await audience.bind_document(landing, entry=True)
                    if clean_url(audience.live_url, navigation=True) != target or guard.refused:
                        return None, "apply_target_unverified"
                    guard.page = landing
                return landing, target
            except ApplyError as exc:
                note_stop_reason(run_.out_dir, f"entry step: {type(exc).__name__}: {exc}")
                return None, "apply_target_unverified"
            finally:
                guard.probe = None

        try:
            await guard.navigate(page, attempt.url, lambda tab, url: pool.goto(tab, url, challenge_timeout_s=0))
            if guard.refused:
                raise ApplyError(SUPERVISED_ONLY)
            for _ in range(40):  # a script redirect followed hop by hop by the guard shows a transient error page meanwhile
                if not page.url.startswith("chrome-error:") or page.is_closed():
                    break
                await asyncio.sleep(0.15)
            landed = clean_url(page.url, navigation=True)
            if audience.core:
                await audience.sync(page)
            elif audience.write_recipient(page.url) and audience.initial_redirect(attempt.url, landed):
                audience.document = landed
                await audience.bind_document(page)
            elif browser_adapter or not await follow_landing(page):
                raise ApplyError(SUPERVISED_ONLY if browser_adapter or guard.refused else "Destination de candidature non vérifiée.")
            result = await run_application(
                page, url=attempt.url, facts=facts,
                planner=planner if planner is not None else LLMPlanner(llm.get(), autonomy=bool(getattr(audience, "trusting", False))),
                verifier=verifier if verifier is not None or planner is not None else _default_verifier(llm),
                uploads=Uploads(cv_pdf=cvs.safe_pdf(app), cv_bytes=request.pdf,
                                letter=make_letter if getattr(audience, "trusting", False) else None),
                mode="submit", current_step=True,
                autonomous=audience, autonomous_attempt=attempt, before_submit=recheck, on_submit_click=mark_submit,
                supervised_widgets=browser_adapter,
                job_title=job.title, company=job.company or "", out_dir=Path(run_.out_dir),
                password=(lambda url: password(url)) if password else None,
                account_known=live_known(),
                email_verification=mail_flow,
                diagnostics=diagnostics, apply_entry=None if browser_adapter else open_apply,
                host_allowed=audience.trusted_host if audience.core else (lambda host: host == _host(audience.document)),
                max_steps=max(30, get_settings().apply_max_steps) if getattr(audience, "trusting", False) else min(12, get_settings().apply_max_steps),
                choices=scoped_choices.load(app.profile_id, app.id, app.current_rev, origin_digest(attempt.url)))
        except ChallengeRequired:
            result = handoff_result("human", diagnostics=diagnostics).model_copy(
                update={"message": "Un défi anti-robot doit être résolu à la main."})
            note_stop_reason(run_.out_dir, "bot challenge (ChallengeRequired)")
        except Exception as exc:
            await _early_diagnose(page, run_, "failed", f"{type(exc).__name__}: {exc}")
            if not guard.refused and not page.is_closed():
                raise
            result = handoff_result("human", diagnostics=diagnostics)
            note_stop_reason(run_.out_dir, f"{type(exc).__name__}: {exc}")
        if guard.refused and getattr(guard, "posting_gone", False):
            # The posting URL itself now redirects to the site's root: an expired ad, not a pause worth retrying.
            note_stop_reason(run_.out_dir, "posting URL redirected to the site root (closed)")
            result = RunResult(status="blocked", message="Offre clôturée", diagnostic_code="job_closed",
                               dispatch_diagnostics=diagnostics)
        elif guard.refused:
            diagnostics.receipt_kind = "none"
            diagnostics.receipt_reason = "recipient_refused"
            result = result.model_copy(update={"status": "needs_human", "message": RECIPIENT_REFUSED + " " + SUPERVISED_ONLY,
                                               "diagnostic_code": guard.refusal_code,
                                               "dispatch_diagnostics": diagnostics})
        if guard.aborted_hosts:
            aborted = sorted(guard.aborted_hosts, key=lambda host: (-guard.aborted_hosts[host], host))[:20]
            diagnostics.aborted_hosts = aborted
            diagnostics.aborted_requests = min(sum(guard.aborted_hosts.values()), 100000)
            result = result.model_copy(update={"dispatch_diagnostics": result.dispatch_diagnostics.model_copy(
                update={"aborted_hosts": aborted, "aborted_requests": diagnostics.aborted_requests})})
        if result.status in DIAGNOSED_STATUSES:  # the run ended before / outside the engine's own record: leave one anyway
            await _early_diagnose(page, run_, result.status, result.message if not result.steps else "", result.diagnostic_code)
        if out is not None:
            out.append(result)
    return result


async def _drive(app: Application, job, run_: ApplyRun, *, mode: Mode, trigger: str, pool, planner,
                 llm: _Llm, verifier=None, out: list[RunResult] | None = None,
                 receipts: list[Receipt] | None = None, confirmed_distinct: frozenset[int] = frozenset(),
                 duplicate_confirmations: tuple[str, ...] = (), browser=None,
                 diagnostics: DispatchDiagnostics | None = None) -> RunResult:
    """`out` receives the engine's result the moment it returns, so a cancellation / error during the page cleanup that
    follows cannot lose it."""
    match = _dispatch_match(job, mode) if mode != "dry_run" else None
    if diagnostics is None:
        diagnostics = DispatchDiagnostics()
    if mode == "submit":
        if match and match.capability == "unattended" and getattr(match.adapter, "browser_planner", None):
            return await _drive_generic(app, job, run_, trigger=trigger, pool=pool, planner=planner, llm=llm,
                                        verifier=verifier, browser=browser, out=out, match=match,
                                        confirmed_distinct=confirmed_distinct, duplicate_confirmations=duplicate_confirmations,
                                        diagnostics=diagnostics, browser_adapter=True)
        if (match is None or match.capability != "unattended") and get_settings().apply_autonomous and browser is not None:
            return await _drive_generic(app, job, run_, trigger=trigger, pool=pool, planner=planner, llm=llm,
                                        verifier=verifier, browser=browser, out=out, match=match,
                                        confirmed_distinct=confirmed_distinct, duplicate_confirmations=duplicate_confirmations,
                                        diagnostics=diagnostics)
        if match is None or match.capability != "unattended":
            return RunResult(status="blocked", message=UNSUPPORTED_DISPATCH + " " + SUPERVISED_ONLY + ".")
        request = _reserve(run_, job, mode, trigger, match, confirmed_distinct=confirmed_distinct,
                           duplicate_confirmations=duplicate_confirmations)
        gate = _write_gate(request, run_, job, mode, trigger, match, confirmed_distinct=confirmed_distinct,
                           diagnostics=diagnostics)
        try:
            evidence = await match.adapter.dispatch(request, gate)
            receipt = await match.adapter.verify_receipt(request.attempt, evidence=evidence)
        except Handoff as exc:
            if exc.reason in {"otp", "captcha", "403", "429", "unknown_field", "consent"} and not gate.final_called:
                return handoff_result("email" if exc.reason == "otp" else "human", diagnostics=diagnostics)
            if exc.reason == "redirect":
                diagnostics.receipt_reason = "recipient_refused"
            raise
        if (gate.final_called and not gate.stopped and isinstance(receipt, Receipt)
                and clean_receipt(receipt, request.attempt) and receipts is not None):
            receipts.append(receipt)
            diagnostics.receipt_kind = receipt.kind
            diagnostics.receipt_reason = "attested"
        elif gate.final_called:
            diagnostics.receipt_reason = "receipt_rejected"
        progress = apply_store.get_run(run_.id)
        return RunResult(status="failed", message="Aucun reçu authentifié pour cet essai.",
                         steps=progress.result.steps if progress and progress.result else [], dispatch_diagnostics=diagnostics)
    request = _reserve(run_, job, mode, trigger, match, confirmed_distinct=confirmed_distinct,
                       duplicate_confirmations=duplicate_confirmations) if mode == "supervised" else None
    facts = _candidate_facts(app, job)
    cv_pdf = cvs.safe_pdf(app)
    assert cv_pdf is not None and job.origin_url
    started_origin = dispatch_url(job.origin_url, job.extra, autonomous=mode == "dry_run" and not _unattended(job))
    host = _host(started_origin)
    challenge: list[bool] = []

    async def goto(page, url: str) -> None:
        try:
            await pool.goto(page, url)
        except ChallengeRequired:
            challenge.append(True)
            raise

    try:
        trusted = trusted_hosts(app.profile_id)
    except ApplyError:
        trusted = []
    host_allowed = (lambda name: name == host) if request else make_host_allowed(started_origin, trusted)

    async def recheck() -> list[str]:
        try:
            if browser_guard and browser_guard.refused:
                return [RECIPIENT_REFUSED]
            live, _, _, _ = _check_boundary(run_, job, mode, trigger, reserved=True, confirmed_distinct=confirmed_distinct)
            if not apply_store.binding_active(request.attempt):
                return ["Réservation ou approbation périmée : saisie annulée."]
            if match and _dispatch_match(job, mode) != match:
                return ["Politique d’adaptateur périmée : saisie annulée."]
            if page.url != "about:blank" and (origin(page.url) != origin(started_origin)
                    or not clean_url(page.url, navigation=True)
                    or (route_policy and not route_policy(request.attempt, page.url, "GET", True))):
                return ["Destination non vérifiée : saisie annulée."]
            pdf = cvs.safe_pdf(live)
            if (pdf is None or live.pdf_sha256 != request.attempt.pdf_sha256
                    or hashlib.sha256(pdf.read_bytes()).hexdigest() != request.attempt.pdf_sha256):
                return ["Le PDF approuvé a changé : saisie annulée."]
        except (ApplyError, OSError) as exc:
            if isinstance(exc, OSError):
                return ["Le PDF approuvé est inaccessible : saisie annulée."]
            return [str(exc)]
        return []

    if planner is None and not request:
        planner = LLMPlanner(llm.get())
    site = f"apply-supervised-{host}" if request else "applydry-" + accounts.registrable_host(host)
    async with pool.page(app.profile_id, site, not request) as page:
        browser_guard = None
        guarded_fields = bool(request and match and match.adapter.boundaries.get("field_entry") == "auto")
        gate = None
        navigating = True
        upload_pending = False
        manual = False
        route_policy = getattr(match.adapter, "browser_request_allowed", None) if match else None
        upload_route = getattr(match.adapter, "browser_upload_route", None) if match else None

        def allow_request(url: str, method: str, navigation: bool) -> bool:
            if guarded_fields and navigation and not (navigating or manual):
                return False
            return route_policy is None or route_policy(request.attempt, url, method, navigation)

        async def allow_fill_write(network_request) -> bool:
            nonlocal upload_pending
            target = network_request.url
            endpoint = gate and gate.matches_endpoint("cv_upload_parse", target)
            upload = endpoint or (upload_route and upload_route(request.attempt, target))
            if manual and not upload:
                return origin(target) == origin(started_origin)
            if not endpoint or manual or network_request.is_navigation_request() or await recheck():
                return False
            require_posting(match, request.attempt, page.url, network_request.frame.url)
            if network_request.method == "OPTIONS":
                return upload_pending
            if (not upload_pending or network_request.method != "POST"
                    or not is_bound_pdf(network_request.headers.get("content-type", ""), network_request.post_data_buffer,
                                        request.pdf)):
                return False
            gate._enter("cv_upload_parse", target)
            upload_pending = False
            return True

        if request and not page.is_closed():
            browser_guard = RecipientGuard(
                frozenset({origin(started_origin), *(match.requisition.write_origins if match else ())}),
                read_only=frozenset(match.requisition.read_origins if match else ()),
                request_allowed=allow_request,
                allow_write=allow_fill_write if guarded_fields else None,
            )
            await browser_guard.install(page)

        if not request:
            inspect_navigation = None
            if mode == "dry_run" and isinstance(pool, BrowserPool):
                audience = Audience(started_origin, (), lambda: None)
                guard = RecipientGuard(frozenset({origin(started_origin)}), audience=audience, inspection_only=True)
                await guard.install(page)

                async def inspect_goto(tab, url):
                    try:
                        await guard.navigate(tab, url, lambda p, u: pool.goto(p, u, challenge_timeout_s=0))
                    except ChallengeRequired:
                        challenge.append(True)
                        raise
                    audience.document = clean_url(tab.url, navigation=True)
                    if not guard.refused:
                        await audience.bind_document(tab)

                async def inspect_navigation(tab, *, require_posting):
                    identity = resolved_identity(job.extra)
                    try:
                        landing, target = await discover(tab, guard, audience, requisition=identity[1] if identity else None,
                                                         require_posting=require_posting, platform=job.origin_ats)
                        if landing is not None:
                            audience.bind_recipient(target)
                            await audience.bind_document(landing, entry=True)
                            guard.page = landing
                        return landing, target
                    except ApplyError:
                        return None, "apply_target_unverified"
                    finally:
                        guard.probe = None

            res = await run_application(page, url=started_origin, facts=facts, planner=planner,
                                        uploads=Uploads(cv_pdf=cv_pdf), mode=mode, verifier=verifier,
                                        out_dir=Path(run_.out_dir or _run_dir(job.id, run_.id)),
                                        job_title=job.title, company=job.company or "",
                                        host_allowed=(lambda name: name == audience.host) if inspect_navigation else host_allowed,
                                        max_steps=get_settings().apply_max_steps, goto=inspect_goto if inspect_navigation else goto,
                                        apply_entry=inspect_navigation,
                                        choices=scoped_choices.load(app.profile_id, app.id, app.current_rev,
                                                                   origin_digest(started_origin)) if trigger == "user" else None)
            if inspect_navigation and guard.refused:
                res = res.model_copy(update={"status": "needs_human", "diagnostic_code": guard.refusal_code,
                                             "message": RECIPIENT_REFUSED})
            if challenge and res.status == "failed":
                res = res.model_copy(update={"status": "needs_human", "diagnostic_code": "challenge_or_rate_limited"})
            if out is not None:
                out.append(res)
            return res

        session = None
        steps = []
        identity_match = match if match and canonical_identity(match.requisition.tenant, match.requisition.requisition_id) else None
        try:
            if page.is_closed():
                raise ApplyError(UNAVAILABLE)
            try:
                await browser_guard.navigate(page, started_origin, goto)
            except ChallengeRequired:
                return RunResult(status="needs_human", message="Un défi anti-robot doit être résolu à la main.")
            finally:
                navigating = False
            res = RunResult(status="needs_human", message=SUPERVISED_HANDOFF)

            async def fill_pass(expected):
                nonlocal gate, manual, upload_pending
                gate = _write_gate(request, run_, job, mode, trigger, match, confirmed_distinct=confirmed_distinct) if match else None
                available, used = True, False
                fill_result = None

                def before_field(op):
                    nonlocal upload_pending
                    boundary = "cv_upload_parse" if op == "upload_cv" else "field_entry" if guarded_fields else "draft_write"
                    gate._enter(boundary, started_origin)
                    if op == "upload_cv":
                        upload_pending = True

                async def fill(selected_planner):
                    nonlocal used, fill_result
                    if not available or used:
                        raise ApplyError("Une seule saisie est autorisée par demande.")
                    used = True
                    options = {}
                    if gate:
                        options["before_field"] = before_field
                    if guarded_fields:
                        options["attended_accounts"] = match.adapter.boundaries.get("login_otp") == "pause_for_human"
                    fill_result = await run_application(
                        page, url=started_origin, facts=facts, planner=selected_planner,
                        uploads=Uploads(cv_pdf=cv_pdf, cv_bytes=request.pdf), mode=mode, before_submit=recheck,
                         supervised_attempt=request.attempt, current_step=True, generic_step=identity_match is None,
                         supervised_widgets=guarded_fields,
                        live_mutation_check=lambda target: session.before_mutation(target, expected),
                        out_dir=Path(run_.out_dir or _run_dir(job.id, run_.id)) / f"pass-{len(steps)}",
                        job_title=job.title, company=job.company or "", host_allowed=host_allowed,
                        verifier=verifier if verifier is not None or planner is not None or match else _default_verifier(llm), max_steps=1,
                        choices=scoped_choices.load(app.profile_id, app.id, app.current_rev, origin_digest(started_origin)),
                        **options)
                    return fill_result

                manual = False
                try:
                    result = (await match.adapter.supervise(request, fill) if match else
                              await fill(planner if planner is not None else LLMPlanner(llm.get())))
                finally:
                    available = False
                    upload_pending = False
                if fill_result is not None:
                    result = fill_result
                for step in result.steps:
                    steps.append(step.model_copy(update={"index": len(steps) + 1}))
                result = result.model_copy(update={"steps": list(steps)})
                apply_store.save_progress(run_.id, result)
                manual = result.status not in ("blocked", "failed")
                return result

            session = Session(attempt=request.attempt, page=page, match=identity_match, company=job.company or "",
                              title=job.title, recheck=recheck, fill=fill_pass)
            async with local_bridge(session):
                _SUPERVISED[app.id] = session
                if identity_match:
                    res = await session.run(None, initial=True)
                else:
                    manual = True
                if not page.is_closed():
                    with contextlib.suppress(Exception):
                        await page.evaluate("""message => {
                            const banner = document.createElement('aside');
                            banner.style.cssText = 'position:fixed;bottom:0;left:0;right:0;z-index:2147483647;pointer-events:none';
                            const notice = document.createElement('div');
                            notice.style.cssText = 'background:#152c4a;color:white;padding:12px;font:16px system-ui';
                            notice.textContent = message;
                            banner.attachShadow({mode:'closed'}).append(notice);
                            document.documentElement.append(banner);
                        }""", "Utilisez « Remplir cette étape » dans Envois pour lancer la saisie. " + SUPERVISED_HANDOFF)
                if not page.is_closed():
                    await page.wait_for_event("close", timeout=0)
                async with session.lock:
                    session.closed = True
                    res = session.last or res
        except Exception:
            if not browser_guard or not browser_guard.refused:
                raise
            res = _blocked_result([RECIPIENT_REFUSED])
        finally:
            if _SUPERVISED.get(app.id) is session:
                _SUPERVISED.pop(app.id, None)
        if browser_guard and browser_guard.refused:
            res = _blocked_result([RECIPIENT_REFUSED])
        if out is not None:
            out.append(res)
    if challenge and res.status == "failed":
        res = res.model_copy(update={"status": "needs_human", "message": "Un défi anti-robot doit être résolu à la main."})
    return res


UNRESOLVED = "Un envoi précédent n'est pas confirmé : vérifier sur le site de l'entreprise et le régler avant tout nouvel essai"


async def run(app_id: int, *, mode: Mode, trigger: Literal["auto", "user"], headless: bool | None = None,
              pool=None, planner=None, llm=None, verifier=None, _lock_held: bool = False, expected_rev: int | None = None,
              expected_origin: str | None = None, attended: bool = False,
              duplicate_confirmations: tuple[str, ...] = ()) -> ApplyRun:
    """ONE dry run or submission attempt of the application's current revision on job.origin_url. Never raises for an
    expected refusal: the returned run has status `blocked` and the reasons in `message`. A submission holds the send lock
    (`send_lock`) for its whole duration unless the caller already holds it (`_lock_held=True`: send_selected, daily_apply);
    if another send is in progress the run is recorded as blocked."""
    app = cvstore.get_application(app_id)
    if app is None:
        raise ApplyError("Candidature introuvable")
    job = db.get_job(app.job_id)
    if mode != "dry_run":
        pending = apply_store.outstanding_attempt(app.id)
        if pending is not None:
            return pending.model_copy(update={"status": "blocked", "result": None, "message": UNRESOLVED})
    confirmed = frozenset(other for other, token in apply_store.confirmation_tokens(app.profile_id, app.id).items()
                          if token in duplicate_confirmations) if trigger == "user" and duplicate_confirmations else frozenset()
    reasons = _bound_problems(app, job, expected_rev, expected_origin) or _gate(app, job, mode, trigger, **_distinct(confirmed))
    if mode == "supervised" and (headless is True or _lock_held):
        reasons = ["Le mode supervisé exige un navigateur visible et un lancement individuel."]
    if reasons:
        return _refuse(app, job, mode, trigger, reasons)
    try:
        with send_lock() if mode != "dry_run" and not _lock_held else contextlib.nullcontext():
            return await _run_locked(app, job, mode=mode, trigger=trigger, headless=headless, pool=pool, planner=planner,
                                     llm=llm, verifier=verifier, attended=attended, confirmed_distinct=confirmed,
                                     duplicate_confirmations=duplicate_confirmations)
    except SendLocked as e:
        return _refuse(app, job, mode, trigger, [str(e)])


async def _run_locked(app: Application, job, *, mode: Mode, trigger: str, headless, pool, planner, llm, verifier,
                      attended: bool = False, confirmed_distinct: frozenset[int] = frozenset(),
                      duplicate_confirmations: tuple[str, ...] = ()) -> ApplyRun:
    run_ = apply_store.start_run(app.id, app.current_rev, mode, trigger, lambda rid: _run_dir(app.job_id, rid),
                                 origin_sha=origin_digest(job.origin_url))
    own_pool = pool is None
    shared_llm = _Llm(llm)
    reraise: BaseException | None = None
    crashed, crash_reason = False, ""
    diagnostics = DispatchDiagnostics()
    returned: list[RunResult] = []  # the engine's own result, as soon as it exists
    receipts: list[Receipt] = []
    browser = []
    try:
        try:
            if own_pool and (mode != "submit" or _generic_enabled(job, check_host=False) or _browser_adapter(job)):
                pool = BrowserPool(headless=False if mode == "supervised" else headless)
            result = await _drive(app, job, run_, mode=mode, trigger=trigger, pool=pool, planner=planner,
                                  llm=shared_llm, verifier=verifier, out=returned, receipts=receipts,
                                  confirmed_distinct=confirmed_distinct, duplicate_confirmations=duplicate_confirmations, browser=browser,
                                  diagnostics=diagnostics)
        except ApplyError as e:
            result = _blocked_result([str(e)])
        except Exception as e:  # noqa: BLE001 - never re-raised: the run is finished as failed (type name only, no page content)
            crash_reason = f"{type(e).__name__}: {e}"
            result, crashed = RunResult(status="failed", message="Erreur technique.",
                                        diagnostic_code=diagnostics.exception_code(e)), True
        except asyncio.CancelledError as e:  # the row must not stay `running`: settle it, then let the cancellation through
            result, reraise, crashed = RunResult(status="failed", message="Interrompu."), e, True
        if crashed and returned:  # the engine had finished: cleanup failed or was cancelled afterwards, its result stands
            result, crashed = returned[0], False
        if result.dispatch_diagnostics == DispatchDiagnostics():
            result = result.model_copy(update={"dispatch_diagnostics": diagnostics})
        if run_.out_dir and result.status in DIAGNOSED_STATUSES:  # local failure record (never values), next to the page outline
            with contextlib.suppress(Exception):
                trace = result.dispatch_diagnostics
                save_run_record(Path(run_.out_dir), status=result.status, diagnostic_code=result.diagnostic_code,
                                final_url=result.final_url, step=trace.step, control=trace.control,
                                receipt_reason=trace.receipt_reason,
                                refused_request=getattr(browser[0][1], "refused_request", "") if browser else "",
                                reason=crash_reason or ("" if result.steps or result.diagnostic_code == "credential_page"
                                                        or contains_verification_code(result.message) else result.message))
        # Settlement is synchronous (no await): a cancellation arriving during the cleanup below cannot strand the run.
        try:
            done = _settle(run_, mode, result, crashed, receipt=receipts[0] if receipts else None,
                           browser=browser[0] if browser else None)
        except Exception:
            if not receipts and not browser:
                raise
            current = apply_store.get_run(run_.id)
            if current is None:
                raise
            if current.status != "running":
                done = current
            else:
                done = _settle(run_, mode, result.model_copy(update={"status": "failed",
                    "message": "Reçu vérifié, mais enregistrement impossible. Ne pas renvoyer cette candidature."
                }), crashed, browser=browser[0] if browser else None)
    finally:
        try:
            if own_pool and pool is not None:
                with contextlib.suppress(Exception):
                    await pool.close()
        finally:
            await shared_llm.aclose()
    if done.status == "blocked" and done.message.startswith(BAD_ORIGIN_PREFIX):
        _drop_bad_origin(job)
    if reraise is not None:
        raise reraise
    return done


BAD_ORIGIN_PREFIX = "Lien de l'offre inexploitable"


def _drop_bad_origin(job) -> None:
    """The engine found the job's link unusable: forget it (kept in `extra["bad_origin_url"]`) so the next origin
    re-resolution looks for another one. The run is `blocked`, which never counts as a failed attempt."""
    with db.connect() as c:
        row = c.execute("SELECT origin_url, extra FROM jobs WHERE id=? AND profile_id=?", (job.id, job.profile_id)).fetchone()
        if row is None or not row["origin_url"]:
            return
        extra = db._json_object(row["extra"])
        extra["bad_origin_url"] = clean_url(row["origin_url"])
        for key in ("origin_fingerprint", "origin_posted_at", "origin_date_checked", "origin_via"):
            extra.pop(key, None)
        c.execute("UPDATE jobs SET origin_url=NULL, origin_ats=NULL, origin_confidence=NULL, extra=? WHERE id=? AND profile_id=?",
                  (json.dumps(extra, ensure_ascii=False), job.id, job.profile_id))


def _settle(run_: ApplyRun, mode: Mode, result: RunResult, crashed: bool, *, receipt: Receipt | None = None, browser=None) -> ApplyRun:
    if mode == "dry_run":
        return apply_store.finish_run(run_.id, result)
    row = apply_store.get_run(run_.id) if crashed else None
    if row is not None and row.result is not None:  # the engine gave no result: keep what it reported before (steps, deductions)
        result = result.model_copy(update={"steps": row.result.steps, "final_url": row.result.final_url,
                                           "submit_clicked": row.result.submit_clicked})
    if browser:
        attempt, guard = browser
        confirmed = guard.final_response and not guard.refused and not crashed
        if (guard.core and guard.final_started and result.status == "submitted" and not guard.refused and not crashed
                and result.dispatch_diagnostics.receipt_reason == "confirmation_text"):
            confirmed = True  # core mode: the receipt page itself (form gone, receipt wording) after the recorded final click
        account_only_email = (result.status == 'needs_email' and result.dispatch_diagnostics.email is not None
                              and not guard.non_account_transmitted)
        # only the candidate-account login / signup was posted (no application data): a stop after it is not "possibly sent"
        account_only_stop = result.status in ('blocked', 'needs_human', 'needs_input') and not guard.non_account_transmitted
        clear = (not guard.transmitted or account_only_email or account_only_stop) and not guard.final_started and not crashed
        if guard.core and not guard.final_started:
            # Core mode: only the final click can send an application. A draft write, an upload or an autosave made while
            # filling is not a send: a run that stops before its final click stays retriable.
            clear = True
        return apply_store.finish_and_settle(run_.id, result, browser_attempt=attempt,
                                             browser_confirmed=confirmed, browser_clear=clear)
    return apply_store.finish_and_settle(run_.id, result, receipt=receipt)


# ---------------------------------------------------------------- batches
class _Pool:
    """Lazily created BrowserPool shared by a batch, closed only if we created it."""

    def __init__(self, pool=None, *, headless: bool | None):
        self.pool, self.own, self.headless = pool, False, headless

    def get(self):
        if self.pool is None:
            self.pool, self.own = BrowserPool(headless=self.headless), True
        return self.pool

    async def aclose(self) -> None:
        if self.own and self.pool is not None:
            with contextlib.suppress(Exception):
                await self.pool.close()


async def send_selected(app_ids: list[int], *, headless: bool | None = None,
                        on_progress: Callable[[int, ApplyRun], None] | None = None, pool=None, planner=None,
                        llm=None, expected_revs: dict[int, int] | None = None, verifier=None,
                        expected_origins: dict[int, str] | None = None) -> list[ApplyRun]:
    """The user's OK ("Envoyer les N sélectionnées"): approve each draft's current revision (user approval, no override),
    then submit them one after the other (trigger "user"), holding the send lock for the whole batch. An application that
    already has a run in progress is skipped. If another send holds the lock every application gets a `blocked` run.
    `expected_revs` (application id -> the revision the user was shown): a different (or missing) revision is recorded as a
    `blocked` run ("le CV a changé depuis l'affichage") and nothing is approved or sent. `expected_origins` (application id ->
    `origin_digest` of the job link the user was shown): a different (or missing) digest at launch is recorded as a `blocked`
    run ("le lien de l'offre a changé depuis l'affichage")."""
    try:
        with send_lock():
            return await _send_each(app_ids, headless=headless, on_progress=on_progress, pool=pool, planner=planner, llm=llm,
                                    expected_revs=expected_revs, verifier=verifier, expected_origins=expected_origins)
    except SendLocked as e:
        runs: list[ApplyRun] = []
        for app_id in dict.fromkeys(app_ids):
            app = cvstore.get_application(app_id)
            if app is None:
                continue
            try:
                res = _refuse(app, db.get_job(app.job_id), "submit", "user", [str(e)])
            except ApplyError:
                continue
            runs.append(res)
            if on_progress:
                on_progress(app_id, res)
        return runs


async def _send_each(app_ids: list[int], *, headless, on_progress, pool, planner, llm, expected_revs, verifier,
                     expected_origins=None) -> list[ApplyRun]:
    """The body of `send_selected`, with the send lock already held."""
    runs: list[ApplyRun] = []
    shared, shared_llm = _Pool(pool, headless=headless), _Llm(llm)
    try:
        for app_id in dict.fromkeys(app_ids):
            app = cvstore.get_application(app_id)
            if app is None:
                continue
            try:
                live_job = db.get_job(app.job_id)
                if app.status == "failed" and apply_store.outstanding_attempt(app.id) is not None:
                    res = await run(app_id, mode="submit", trigger="user", _lock_held=True)  # unresolved attempt: no new row
                    runs.append(res)
                    if on_progress:
                        on_progress(app_id, res)
                    continue
                bound = _bound_problems(app, live_job, None if expected_revs is None else expected_revs.get(app_id, -1),
                                        None if expected_origins is None else expected_origins.get(app_id, ""))
                if bound:
                    res = _refuse(app, live_job, "submit", "user", bound)
                    runs.append(res)
                    if on_progress:
                        on_progress(app_id, res)
                    continue
                generic = _generic_enabled(live_job)
                if not live_job or not (_unattended(live_job) or generic):
                    res = _refuse(app, live_job, "submit", "user", [SUPERVISED_ONLY])
                    runs.append(res)
                    if on_progress:
                        on_progress(app_id, res)
                    continue
                if app.status == "draft" and not generic:
                    try:
                        cvs.approve(app.id, app.current_rev)
                    except CVError as e:
                        res = _refuse(app, db.get_job(app.job_id), "submit", "user", [str(e)])
                        runs.append(res)
                        if on_progress:
                            on_progress(app_id, res)
                        continue
                res = await run(app_id, mode="submit", trigger="user", headless=headless, _lock_held=True,
                                **({"pool": shared.get(), "planner": planner, "llm": llm, "verifier": verifier}
                                   if generic or _browser_adapter(live_job) else {}),
                                expected_rev=None if expected_revs is None else expected_revs.get(app_id, -1),
                                expected_origin=None if expected_origins is None else expected_origins.get(app_id, ""))
            except ApplyError:
                continue
            runs.append(res)
            if on_progress:
                on_progress(app_id, res)
    finally:
        await shared.aclose()
        await shared_llm.aclose()
    return runs


def answer_questions(app_id: int, answers: list[tuple[str, str]]) -> None:
    """Store the user's answers to the questions of a paused run (answers.yaml `custom`), reused by every later form."""
    app = cvstore.get_application(app_id)
    if app is None:
        raise ApplyError("Candidature introuvable")
    pairs = [(q, a) for q, a in answers if (a or "").strip() and (q or "").strip()]
    if pairs:
        run = apply_store.last_run(app_id)
        restricted = {norm_question(q.label) for q in run.result.questions if not editable_custom(q)} if run and run.result else set()
        if any(norm_question(label) in restricted for label, _ in pairs):
            raise ApplyError(CREDENTIAL_ERROR)
        save_custom_answers(app.profile_id, pairs)


def resolve_unconfirmed(app_id: int, *, sent: bool, run_id: int | None = None) -> Application:
    """Settle the exact attempt after the operator checks the outcome; never approve or resend here."""
    apply_store.resolve_attempt(app_id, sent=sent, run_id=run_id)
    return cvstore.get_application(app_id)  # type: ignore[return-value]


async def reconcile_attempt(run_id: int, *, evidence: object = None) -> Receipt | None:
    """Check a held adapter attempt without dispatching or opening a new write boundary."""
    with send_lock():
        attempt = apply_store.held_binding(run_id)
        if not attempt or not attempt.adapter or (evidence is not None and attempt.adapter != "workday"):
            return None
        outstanding = apply_store.outstanding_attempt(attempt.application_id)
        app = cvstore.get_application(attempt.application_id)
        job = db.get_job(app.job_id) if app else None
        if not outstanding or outstanding.id != run_id or not job or clean_url(job.origin_url, navigation=True) != attempt.url:
            return None
        match = registry.find(attempt.url, job.origin_ats)
        if (not match or match.adapter.name != attempt.adapter
                or (match.receipt_kind != attempt.receipt_kind and
                    getattr(match.adapter, 'supervised_receipt_kind', None) != attempt.receipt_kind)
                or match.requisition.tenant != attempt.tenant
                or match.requisition.requisition_id != attempt.requisition_id):
            return None
        receipt = await match.adapter.verify_receipt(attempt, evidence=evidence)
        if not isinstance(receipt, Receipt) or not clean_receipt(receipt, attempt):
            return None
        apply_store.resolve_attempt(attempt.application_id, run_id=run_id, sent=receipt.outcome == "confirmed", receipt=receipt)
        return receipt


async def reconcile_workday_submission(app_id: int, run_id: int, posting_url: str) -> Receipt:
    """Record the operator's explicit confirmation of a submitted Candidate Home listing."""
    from jobhunt.apply.workday import CandidateHomeEvidence

    attempt = apply_store.held_binding(run_id)
    if not attempt or attempt.application_id != app_id or attempt.adapter != "workday":
        raise ApplyError("Essai Workday introuvable.")
    receipt = await reconcile_attempt(run_id, evidence=CandidateHomeEvidence(attempt.nonce, posting_url, "submitted"))
    if receipt is None:
        raise ApplyError("Le poste Workday confirmé ne correspond pas à l'essai en attente.")
    return receipt


def inferred_answers(app_id: int) -> list[FilledField]:
    """Answers deduced from the profile (not backed by a known fact) in the run recorded as the application's sending attempt
    (`applications.sent_run_id`), else in its most recent run of the CURRENT revision with status `submitted`, else in its most recent `ready` run: what the candidate should prepare for an interview.
    Deduplicated by label, order kept."""
    app = cvstore.get_application(app_id)
    if app is None:
        return []
    run_ = apply_store.sent_run(app_id)  # the recorded sending attempt first (also when the user confirmed it by hand)
    if run_ is None or run_.result is None:
        runs = [r for r in apply_store.list_runs(app_id) if r.rev == app.current_rev and r.result is not None]
        run_ = next((r for r in runs if r.status == "submitted"), None) or next((r for r in runs if r.status == "ready"), None)
    out: dict[str, FilledField] = {}
    for st in (run_.result.steps if run_ and run_.result else []):
        for f in st.filled:
            if f.inferred and f.label not in out:
                out[f.label] = f
    return list(out.values())


def resumable(profile_id: int) -> list[QueueItem]:
    """Applications of the profile paused on unknown question(s) (their last run waits for an answer)."""
    return [i for i in queue(profile_id) if i.bucket == "paused" and i.pause_reason == "input" and i.last_run is not None]


def question_counts(profile_id: int) -> dict[str, int]:
    """Normalized question label -> how many paused applications (current revision) are waiting for it, answered ones left out."""
    return answer_queue(profile_id)[0]


def pending_questions(profile_id: int) -> list[tuple[Application, ApplyRun, Question]]:
    """Questions of the paused `needs_input` runs of the profile (current revision), one per normalized label (the first
    application that asked it), MOST FREQUENT FIRST (ties keep the queue order), leaving out those already answered in `custom`."""
    return answer_queue(profile_id)[1]


def answer_queue(profile_id: int) -> tuple[dict[str, int], list[tuple[Application, ApplyRun, Question]], list[QueueItem]]:
    """Build counts, representative questions and resumable items from one queue read."""
    answered = {norm_question(entry["question"]) for entry in custom_answers(profile_id)}
    items = resumable(profile_id)
    counts: dict[str, int] = {}
    first: dict[str, tuple[Application, ApplyRun, Question]] = {}
    for item in items:
        if item.last_run is None:
            continue
        seen: set[str] = set()
        for question in item.questions:
            key = norm_question(question.label)
            if key and key not in answered and key not in seen:
                seen.add(key)
                counts[key] = counts.get(key, 0) + 1
                first.setdefault(key, (item.app, item.last_run, question))
    pending = [first[key] for key in sorted(first, key=counts.__getitem__, reverse=True)]
    return counts, pending, items




def _questions_answered_since(app: Application, last: ApplyRun) -> bool:
    """A paused `needs_input` run can go on: every one of its questions now has a custom answer (same normalized question), or
    the answers file was modified after the run finished (a hard fact may now be filled)."""
    asked = [norm_question(q.label) for q in (last.result.questions if last.result else [])]
    answered = {norm_question(c["question"]) for c in custom_answers(app.profile_id)}
    if asked and all(k in answered for k in asked):
        return True
    try:
        mtime = datetime.fromtimestamp(facts_answers_path(app.profile_id).stat().st_mtime, UTC)
    except OSError:
        return False
    return last.finished_at is not None and mtime > last.finished_at


def _waiting_on_user(app: Application, job, last: ApplyRun | None) -> bool:
    """A CAPTCHA or email pause requires an explicit resume, not a new automatic pass.

    New local answers or explicit host trust can still unblock their respective handoffs.
    """
    if last is None:
        return False
    if last.status == 'needs_email' and last.email_resumed:
        return False
    if apply_store.stale_engine_pause(last):  # the engine paused itself under older code: retry once, unattended
        return False
    if last.status in ("needs_email", "needs_human"):
        return True
    if last.rev != app.current_rev:
        return False
    if last.status == "needs_input" and _questions_answered_since(app, last):
        return False
    return last.status in PAUSE_REASON or bool(_host_pause(app, job, last))


async def resume_email(profile_id: int | None = None, *, pool=None, _lock_held: bool = False) -> int:
    """Activate account-level links on still-approved holds without sending an application."""
    if not get_settings().apply_autonomous:
        return 0
    if not _lock_held:
        try:
            with send_lock():
                return await resume_email(profile_id, pool=pool, _lock_held=True)
        except SendLocked:
            return 0
    shared = _Pool(pool, headless=True)
    resumed = 0
    try:
        for pid in ([profile_id] if profile_id is not None else [profile.id for profile in db.list_profiles()]):
            try:
                address = mail.profile_address(pid)
            except ApplyError:
                continue
            applications = {app.id: app for app in cvstore.list_applications(pid)}
            for held in apply_store.latest_runs(pid).values():
                app = applications.get(held.application_id)
                if (not app or held.mode != 'submit' or held.status != 'needs_email' or held.email_resumed
                        or not held.finished_at or now() - held.started_at > mail.RESUME_WINDOW):
                    continue
                job = db.get_job(app.job_id)
                if not job or not held.origin_sha or origin_digest(job.origin_url) != held.origin_sha:
                    continue
                match = _dispatch_match(job, 'submit')
                if match and match.capability == 'unattended' and match.adapter.boundaries.get('account_creation') != 'auto':
                    continue
                metadata = job.extra.get('origin_resolution') if isinstance(job.extra, dict) else None
                verified = metadata.get('verified_urls') if isinstance(metadata, dict) and metadata.get('canonical_ok') is True else None
                urls = [dispatch_url(job.origin_url, job.extra, autonomous=True), clean_url(job.origin_url, navigation=True)]
                if isinstance(verified, list):
                    urls.extend(clean_url(url, navigation=True) for url in verified if isinstance(url, str))
                scope = apply_store.email_scope(held.id, urls)
                if not scope:
                    continue

                async def recheck():
                    current, live = cvstore.get_application(app.id), db.get_job(app.job_id)
                    if (not current or current.status != 'approved' or current.updated_at != app.updated_at
                            or current.current_rev != held.rev or not live or live.origin_url != job.origin_url or live.extra != job.extra
                            or _dispatch_match(live, 'submit') != match or apply_store.get_run(held.id) != held
                            or apply_store.outstanding_attempt(app.id) or apply_store.requires_manual_retry(app.id)
                            or apply_store.host_paused(_host(scope)) or not get_settings().apply_autonomous
                            or _gate(current, live, 'submit', held.trigger)):
                        return ['Candidature modifiée ou non autorisée.']
                    return []

                if await recheck():
                    continue
                verification, code = await mail.poll(address, scope, held.started_at, links_only=True)
                if not verification or verification.kind != 'link':
                    if code:
                        apply_store.record_email_resume(held, code)
                    if code == 'email_auth_failed':
                        break
                    continue
                try:
                    browser = await shared.get().context(pid, f'apply-auto-{_host(scope)}', headless=True)
                except Exception:
                    continue
                if await mail.open_link(browser, scope, verification.value, recheck) and not await recheck():
                    with contextlib.suppress(ApplyError):
                        accounts.set_state(_host(scope), address, 'active', site=accounts.workday_site(scope))
                    resumed += apply_store.record_email_resume(held, 'email_link_used')
    finally:
        await shared.aclose()
    return resumed


def _print_now(line: str) -> None:
    print(line, flush=True)


_LINK_CODES = frozenset({"apply_target_unverified", "job_page_no_apply_control", "apply_control_ambiguous",
                         "cross_domain_apply_target", "public_page_shell", "posting_mismatch", "destination_unverified"})
_PAUSE_DETAIL = {
    "destination": "destination ou transmission non autorisée",
    "lien de candidature": "lien de candidature non vérifié",
    "anti-robot": "défi anti-robot ou accès limité",
    "CAPTCHA": "CAPTCHA à résoudre",
    "compte": "compte à créer ou connexion requise",
    "intervention": "intervention humaine requise",
}


def pause_reason_label(res: ApplyRun) -> str:
    """Why a run paused, in one word or two, from its diagnostic code: question / e-mail / destination /
    lien de candidature / anti-robot / CAPTCHA / compte / intervention / site non approuvé."""
    if untrusted_host_of(res):
        return "site non approuvé"
    if res.status == "needs_input":
        return "question"
    if res.status == "needs_email":
        return "e-mail"
    code = res.result.diagnostic_code if res.result else ""
    steps = res.result.steps if res.result else []
    message = res.message.lower()
    if code.startswith("recipient_refused"):
        return "destination"
    if code in _LINK_CODES:
        return "lien de candidature"
    if code == "challenge_or_rate_limited" or "anti-robot" in message:
        return "anti-robot"
    if code == "challenge" or "captcha" in message or (steps and steps[-1].page_kind == "captcha"):
        return "CAPTCHA"
    if (code == "credential_page" or "compte" in message
            or (steps and steps[-1].page_kind in ("account_login", "account_signup"))):
        return "compte"
    return "intervention"


def _pause_detail(res: ApplyRun) -> str:
    if untrusted_host_of(res):
        return f"site non approuvé : {untrusted_host_of(res)}"
    if res.status == "needs_input":
        labels = [q.label for q in (res.result.questions if res.result else [])]
        return "question : " + " ; ".join(labels[:3]) if labels else "question sans réponse"
    if res.status == "needs_email":
        return "e-mail à confirmer"
    if res.status == "needs_human":
        return _PAUSE_DETAIL[pause_reason_label(res)]
    return res.message


async def daily_apply(profile_id: int, *, emit: Callable[[str], None] = _print_now, llm=None, pool=None, planner=None,
                      verifier=None, headless: bool = True, limit: int | None = None, min_score: float | None = None,
                      attended: bool = False) -> ApplySummary:
    """Automatic pass (daily run, or `apply auto`): prepare CVs for the best fresh jobs. If automatic sending is on,
    auto-approve and send those at or above the auto threshold, within the daily cap. Jobs that already have a CV come FIRST (sends
    start at once), the others are prepared and sent one after the other. One French progress line per application
    (`[n/N] Société — Poste : envoyée | en pause (…) | à vérifier | à corriger | bloquée (raison)`), then the counts. Never
    raises for one job's failure.
    `limit`: stop after that many applications SENT. `min_score`: raises the prepare / auto thresholds.
    Legacy browser options never switch this pass into supervised mode or enable a CAPTCHA wait."""
    s = get_settings()
    summary = ApplySummary(profile_id=profile_id)
    try:
        master = load_master(profile_id)
    except MasterError:
        master = None
    if master is None or not master.is_confirmed:
        summary.errors.append("CV maître absent ou non confirmé : aucune candidature préparée")
        emit(f"Candidatures : {summary.errors[0]}")
        return summary
    shared_llm, shared_pool = _Llm(llm), _Pool(pool, headless=headless)
    send_stack, lock_held = contextlib.ExitStack(), False
    try:
        cands = policy.auto_candidates(profile_id)
        if min_score is not None:
            cands = [j for j in cands if (j.score or 0) >= min_score]
        apps = cvstore.applications_by_job(profile_id)
        try:  # one lock for the whole sending pass
            send_stack.enter_context(send_lock())
            lock_held = True
        except SendLocked:
            summary.errors.append("Un autre envoi est en cours : aucun envoi automatique cette fois-ci")
            emit(f"Candidatures : {summary.errors[-1]}")
        if lock_held and s.apply_auto and s.apply_autonomous:
            await resume_email(profile_id, _lock_held=True)
        async def regenerate(app: Application, job) -> Application | None:
            """A new revision for a draft whose current one has blocking errors (or no PDF): at most `apply_cv_retries` times per
            application (counted in the database), within the preparation cap of the pass. None = give up."""
            if apply_store.auto_regens(app.id) >= s.apply_cv_retries or budget[0] >= s.apply_prepare_daily_cap:
                return None
            budget[0] += 1
            apply_store.bump_auto_regens(app.id)
            try:
                new = await cvs.prepare_application(profile_id, job.id, shared_llm.get())
            except (CVError, LLMError) as e:
                summary.errors.append(f"{job.company or 'entreprise'} — {job.title} : {e}")
                return None
            summary.prepared += 1
            return new

        async def refresh(app: Application, job) -> Application | None:
            """A CV generated from an older master can never be sent: one new revision from the confirmed master, within the
            preparation cap of the pass (not counted as a retry: the old revision was not broken). None = left blocked."""
            if budget[0] >= s.apply_prepare_daily_cap:
                return None
            budget[0] += 1
            try:
                new = await cvs.prepare_application(profile_id, job.id, shared_llm.get())
            except (CVError, LLMError) as e:
                summary.errors.append(f"{job.company or 'entreprise'} — {job.title} : {e}")
                return None
            summary.prepared += 1
            return new

        order = [j for j in cands if j.id in apps] + [j for j in cands if j.id not in apps]  # existing CVs first
        total, budget = len(order), [0]  # budget[0]: CVs generated this pass (first versions and regenerations)
        for n, job in enumerate(order, 1):
            if limit is not None and summary.sent >= limit:
                emit(f"Limite de {limit} envoi{'s' if limit > 1 else ''} atteinte : fin de la passe")
                break
            tag = f"[{n}/{total}] {job.company or 'entreprise'} — {job.title}"
            try:
                if job.id not in apps:
                    if budget[0] >= s.apply_prepare_daily_cap:
                        emit(f"Plafond de {s.apply_prepare_daily_cap} CV préparés atteint : fin de la passe")
                        break
                    budget[0] += 1
                    try:
                        apps[job.id] = await cvs.prepare_application(profile_id, job.id, shared_llm.get())
                        summary.prepared += 1
                        emit(f"CV préparé : {tag}")
                    except (CVError, LLMError) as e:
                        summary.errors.append(f"{tag} : {e}")
                        emit(f"{tag} : bloquée (CV non préparé : {e})")
                        continue
                await _auto_one(apps[job.id], job, tag, summary, emit, lock_held=lock_held, pool=shared_pool, llm=shared_llm,
                                planner=planner, verifier=verifier, headless=headless, attended=attended, min_score=min_score, regenerate=regenerate,
                                refresh=refresh, master_hash=master.content_hash)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - one application never stops the pass
                summary.failed += 1
                summary.errors.append(f"{tag} : erreur inattendue ({type(e).__name__})")
                emit(f"{tag} : échec (erreur inattendue : {type(e).__name__})")
    finally:
        send_stack.close()
        await shared_pool.aclose()
        await shared_llm.aclose()
    emit(f"Passe terminée : {summary.sent} envoyée(s), {summary.paused} en pause, {summary.to_check} à vérifier, "
         f"{summary.to_fix} à corriger, {summary.waiting} en attente, {summary.failed} en échec, {summary.prepared} CV préparé(s)")
    emit("Détail : " + summary.breakdown())
    top = summary.top_questions(10)
    if top:
        emit("Questions les plus fréquentes (à renseigner d'abord sur la page « Réponses aux formulaires ») :")
        for i, (label, n) in enumerate(top, 1):
            emit(f"  {i}. {label} (×{n})")
    return summary


async def _auto_one(app: Application, job, tag: str, summary: ApplySummary, emit, *, lock_held: bool, pool: _Pool, llm: _Llm,
                    planner, verifier, headless: bool, attended: bool, min_score: float | None, regenerate=None,
                    refresh=None, master_hash: str | None = None) -> None:
    """One application of the automatic pass: checks, auto-approval, the send itself and its progress line."""
    s = get_settings()
    app = cvstore.get_application(app.id) or app  # someone else (the web portal, the user) may have moved it since the pass began
    if app.status in ("submitted", "withdrawn"):
        emit(f"{tag} : déjà traitée par ailleurs ({'envoyée' if app.status == 'submitted' else 'retirée'})")
        return
    if apply_store.requires_manual_retry(app.id):
        summary.waiting += 1
        emit(f"{tag} : en attente d'un envoi manuel après vérification")
        return
    last = apply_store.last_run(app.id)
    if last is not None and last.status == "running" and last.pid and last.pid != os.getpid() and apply_store.pid_alive(last.pid):
        summary.waiting += 1
        emit(f"{tag} : bloquée (un envoi est déjà en cours pour cette candidature)")
        return
    if _waiting_on_user(app, job, last) or apply_store.has_site_handoff(app.id):
        summary.paused += 1
        summary.paused_by["en attente"] = summary.paused_by.get("en attente", 0) + 1
        emit(f"{tag} : en pause (en attente de votre réponse)")
        return
    if sum(1 for r in apply_store.list_runs(app.id) if r.rev == app.current_rev and apply_store.counts_as_failure(r)) >= 2:
        summary.failed += 1
        emit(f"{tag} : échec (deux essais ont déjà échoué)")
        return
    live = db.get_job(job.id)  # the score / status may have changed during the (long) preparation of the CVs
    if live is None or live.status != job.status or live.status in policy.CLOSED_JOB_STATUSES:
        summary.waiting += 1
        emit(f"{tag} : bloquée (offre modifiée entre-temps)")
        return
    job = live
    generic = _generic_enabled(job)
    if not (_unattended(job) or generic):
        summary.supervised += 1
        emit(f"{tag} : {SUPERVISED_ONLY.lower()}")
        return
    if refresh is not None and master_hash and app.status in ("draft", "approved") and app.master_hash != master_hash:
        new = await refresh(app, job)  # generated from an older master: rebuild it from the confirmed one
        if new is not None:
            app = new
            emit(f"{tag} : CV régénéré (CV maître modifié)")
    def broken(a: Application) -> bool:
        return a.status == "draft" and (bool(a.errors) or cvs.safe_pdf(a) is None)

    while broken(app) and regenerate is not None:  # blocking errors / no PDF: a fresh revision may be clean (bounded retries)
        new = await regenerate(app, job)
        if new is None:
            break
        app = new
        emit(f"{tag} : CV régénéré (erreurs bloquantes), nouvel essai")
    if broken(app):
        summary.to_fix += 1
        emit(f"{tag} : à corriger ({len(app.errors)} erreur(s) bloquante(s) dans le CV)" if app.errors else f"{tag} : à corriger (PDF absent)")
        return
    if not s.apply_auto:
        summary.waiting += 1
        emit(f"{tag} : bloquée (envoi automatique désactivé)")
        return
    threshold = max(s.apply_auto_min_score, min_score or 0)
    if app.status == "draft" and (job.score or 0) >= threshold:  # generic sites too: the send keeps its own guards
        try:
            app = cvs.auto_approve(app.id, expected_rev=app.current_rev)
        except CVError as e:  # ApprovalError, StaleRevision, TransitionError: this job only
            emit(f"CV à valider à la main : {tag} ({e})")
    problems = policy.submit_problems(app, job, trigger="auto")
    if problems:
        summary.waiting += 1
        emit(f"{tag} : bloquée ({problems[0]})")
        return
    if not lock_held:
        summary.waiting += 1
        emit(f"{tag} : bloquée (un autre envoi est en cours)")
        return
    try:
        res = await run(app.id, mode="submit", trigger="auto", headless=None if generic else headless, _lock_held=True,
                        **({"pool": pool.pool, "planner": planner, "verifier": verifier, "llm": llm.llm}
                           if generic or _browser_adapter(job) else {}))
    except (ApplyError, CVError) as e:
        summary.waiting += 1
        emit(f"{tag} : bloquée ({e})")
        return
    if res.status == "submitted":
        summary.sent += 1
        emit(f"{tag} : envoyée")
    elif apply_store.outstanding_attempt(app.id) is not None:
        summary.to_check += 1
        emit(f"{tag} : à vérifier (envoi non confirmé : {res.message})")
    elif res.status in PAUSE_REASON or untrusted_host_of(res):  # the pass never trusts a host by itself
        summary.paused += 1
        reason = pause_reason_label(res)
        summary.paused_by[reason] = summary.paused_by.get(reason, 0) + 1
        if res.status == "needs_input" and res.result:
            for key in dict.fromkeys(norm_question(q.label) for q in res.result.questions if norm_question(q.label)):
                summary.asked[key] = summary.asked.get(key, 0) + 1
        emit(f"{tag} : en pause ({_pause_detail(res)})")
    elif res.status == "failed" and (res.submit_clicked_at or res.nav_clicked_at or (res.result and res.result.submit_clicked)):
        summary.to_check += 1
        emit(f"{tag} : à vérifier (envoi non confirmé : {res.message})")
    elif res.status == "failed":
        summary.failed += 1
        emit(f"{tag} : échec ({res.message})")
    else:
        summary.waiting += 1
        emit(f"{tag} : bloquée ({res.message})")
