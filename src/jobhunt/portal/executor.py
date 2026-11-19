"""Apply portal approvals under the send lock; only enrolled adapters may dispatch."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable, Collection

from jobhunt import db
from jobhunt.apply import facts, service
from jobhunt.apply import store as apply_store
from jobhunt.apply.errors import ApplyError
from jobhunt.cv import service as cvs
from jobhunt.cv import store as cvstore
from jobhunt.cv.errors import CVError
from jobhunt.portal.journal import GrantRow, Journal, ProfileRow
from jobhunt.portal.settings import MAX_SEND_MINUTES, get_portal_settings
from jobhunt.portal.snapshot import interrupted_after_click

CHANGED = "La candidature a changé depuis votre accord : rechargez la page."
APPROVAL_REFUSED = "CV à corriger : cette candidature ne peut pas être envoyée telle quelle."
SEND_FAILED = "Envoi impossible."
SEND_UNCONFIRMED = "Envoi non confirmé : à vérifier sur le site de l'entreprise."
SUPERVISED = "À envoyer en mode supervisé sur le Mac."
PAUSE_STATUSES = ("needs_input", "needs_email", "needs_human")
Runner = Callable[..., Awaitable[Any]]


def _monotonic() -> float:  # test seam
    return time.monotonic()


@dataclass
class Outcome:
    profile_id: int
    app_id: int
    event_id: str | None        # None: an automatic resume (it has no event)
    state: str
    message: str
    run_id: int | None = None


@dataclass
class BatchReport:
    outcomes: list[Outcome] = field(default_factory=list)
    locked: bool = False
    skipped: dict[int, str] = field(default_factory=dict)  # profile id -> why it was not considered
    stopped_for_time: bool = False

    @property
    def did_anything(self) -> bool:
        return bool(self.outcomes)

    def profiles(self) -> set[int]:
        return {o.profile_id for o in self.outcomes}


@dataclass
class _Work:
    profile_id: int
    event_id: str | None
    grant: GrantRow | None
    profile: ProfileRow
    # a binding for an automatic resume (no grant)
    app_id: int = 0
    rev: int = 0
    pdf_sha256: str = ""
    origin_sha: str = ""
    run_id: int = 0
    mode: str = "submit"
    action: str = "resume"

    def bind(self) -> GrantRow | _Work:
        return self.grant or self


def _live_binding_problem(w: _Work, binding: Any, *, approval_only: bool) -> str | None:
    """None when the live state still equals what the user agreed to, else a reason."""
    app = cvstore.get_application(binding.app_id)
    if app is None or app.profile_id != w.profile_id or app.status in ("submitted", "withdrawn"):
        return "app"
    if app.current_rev != binding.rev:
        return "rev"
    last = apply_store.last_run(app.id)
    if interrupted_after_click(last):
        return "run"
    pdf = cvs.safe_pdf(app)
    if cvstore.sha256_file(pdf) != binding.pdf_sha256:
        return "pdf"
    job = db.get_job(app.job_id)
    if job is None or service.origin_digest(job.origin_url) != binding.origin_sha:
        return "origin"
    if approval_only != (not service.unattended_available(job)):
        return "adapter"
    if binding.action == "send" and binding.mode != ("supervised" if approval_only else "submit"):
        return "mode"
    if binding.action == "resume":
        if (last is None or last.id != binding.run_id or last.rev != binding.rev or last.mode != "submit"
                or binding.mode != "submit" or last.status not in PAUSE_STATUSES):
            return "run"
        if apply_store.outstanding_attempt(app.id) is not None:
            return "run"
        if w.grant is None or last.status == "needs_input":
            if apply_store.has_site_handoff(app.id):
                return "run"
            item = next((item for item in service.resumable(w.profile_id) if item.app.id == app.id), None)
            if item is None or item.last_run.id != last.id or item.problems or not _questions_answered(w.profile_id, item):
                return "questions"
        elif last.status != "needs_email":
            return "run"
    elif binding.action == "send":
        if apply_store.has_site_handoff(app.id) or apply_store.requires_manual_retry(app.id):
            return "run"
        item = next((i for i in service.queue(w.profile_id) if i.app.id == app.id), None)
        if item is None or item.bucket != "to_validate" or not (item.selectable or item.supervised):
            return "bucket"
    else:
        return "action"
    return None


def _outcome_of(run: Any) -> tuple[str, str]:
    st = run.status
    if st == "submitted":
        return "done", "Envoyée."
    if st == "needs_input":
        return "failed", "En pause : une question attend votre réponse."
    if st == "needs_email":
        return "failed", "En pause : un e-mail de confirmation est à valider."
    if st == "needs_human":
        return "failed", "En pause : un CAPTCHA doit être résolu sur le Mac."
    clicked = bool(run.submit_clicked_at or run.nav_clicked_at or (run.result and run.result.submit_clicked))
    if isinstance(run, apply_store.ApplyRun) and apply_store.held_binding(run.id) is not None:
        clicked = True
    if st == "running":
        return "interrupted", SEND_UNCONFIRMED if clicked else "Résultat inconnu : vérifiez avant de réessayer."
    if clicked:
        return "failed", SEND_UNCONFIRMED
    if st == "blocked" and run.result and not run.result.steps and not run.result.final_url and not run.result.untrusted_host:
        if run.message.startswith("Envoi impossible : "):
            return "failed", run.message[:300]
    if st in ("blocked", "failed"):
        return "failed", SEND_FAILED
    return "failed", "Résultat inattendu."


def _questions_answered(profile_id: int, item: service.QueueItem) -> bool:
    answered = {facts.norm_question(entry["question"]): entry["answer"] for entry in facts.custom_answers(profile_id)}
    return bool(item.questions) and all((value := answered.get(facts.norm_question(question.label)))
                                        and (not question.options or value in question.options) for question in item.questions)


def _collect(journal: Journal, profile: ProfileRow) -> list[_Work]:
    work: list[_Work] = []
    for ev in journal.events(profile.profile_id, ("queued",), ("intent",)):
        g = journal.get_grant(ev.payload.get("grant", ""))
        if g is not None and g.profile_id == profile.profile_id and g.action in ("send", "resume"):
            work.append(_Work(profile.profile_id, ev.event_id, g, profile))
    if get_portal_settings().auto_resume and not profile.review_mode:
        candidates = journal.resume_candidates(profile.profile_id)
        if not candidates:
            return work
        taken = {w.grant.app_id for w in work if w.grant}
        try:
            live = set()
            for it in service.resumable(profile.profile_id):
                last = it.last_run
                if last is None or last.mode != "submit" or (it.app.id, last.id) not in candidates:
                    continue
                live.add((it.app.id, last.id))
                if it.app.id in taken or it.problems or not _questions_answered(profile.profile_id, it):
                    continue
                pdf = cvstore.sha256_file(cvs.safe_pdf(it.app))
                if not pdf:
                    continue
                work.append(_Work(profile.profile_id, None, None, profile, app_id=it.app.id, rev=it.app.current_rev, pdf_sha256=pdf,
                                  origin_sha=it.origin_sha, run_id=last.id, mode="submit", action="resume"))
            for candidate in candidates - live:
                journal.forget_resume(profile.profile_id, *candidate)
        except ApplyError:
            pass
    return work


async def run_batch(journal: Journal, profile_ids: Collection[int], now: datetime, *, runner: Runner | None = None) -> BatchReport:
    """Run eligible intents and durable resume candidates. `runner` replaces `service.run` in tests."""
    report = BatchReport()
    ps = get_portal_settings()
    if journal.paused:
        return report
    work: list[_Work] = []
    for pid in profile_ids:
        p = journal.get_profile(pid)
        if p is None or not p.enabled:
            continue
        if p.held:
            report.skipped[pid] = "profil suspendu"
        elif journal.events(pid, ("received",)):
            report.skipped[pid] = "événements non traités"
        else:
            work += _collect(journal, p)
    if not work:
        return report
    from jobhunt import schedule
    from jobhunt.config import get_settings

    if schedule.is_running(data_dir=get_settings().data_dir):
        for w in work:
            report.skipped[w.profile_id] = "une recherche quotidienne est en cours"
        return report
    try:
        with service.send_lock():
            await _execute(journal, work, runner or service.run, report, ps)
    except service.SendLocked:
        report.locked = True
    return report


async def _execute(journal: Journal, work: list[_Work], runner: Runner, report: BatchReport, ps) -> None:
    started = _monotonic()
    stamped: set[int] = set()
    for w in work:
        if _monotonic() - started >= MAX_SEND_MINUTES * 60:
            report.stopped_for_time = True
            break
        b = w.bind()
        try:
            app = cvstore.get_application(b.app_id)
            job = db.get_job(app.job_id) if app else None
            approval_only = b.action == "send" and not service.unattended_available(job)
            claim = journal.claim_send(w.profile_id, w.event_id, w.grant.grant_id if w.grant else None, db.now,
                                       ps.max_sends_per_day, spacing=timedelta(minutes=ps.send_every_min),
                                       batch_started=w.profile_id in stamped, expected=w.profile, approval_only=approval_only,
                                       validate=lambda: _live_binding_problem(w, b, approval_only=approval_only) is None, refusal=CHANGED,
                                       resume_candidate=(b.app_id, b.run_id) if w.grant is None else None)
        except Exception:  # noqa: BLE001
            report.skipped[w.profile_id] = "Erreur technique."
            continue
        if claim == "refused":
            report.outcomes.append(Outcome(w.profile_id, b.app_id, w.event_id, "refused", CHANGED))
        elif claim == "budget":
            if w.event_id:
                journal.update_event(w.event_id, db.now(), only_from=("queued",), message="En attente : limite quotidienne d'envois atteinte.")
            report.skipped[w.profile_id] = "limite quotidienne"
        elif claim == "spacing":
            report.skipped[w.profile_id] = "lot récent"
        elif claim == "inbox":
            report.skipped[w.profile_id] = "événements non traités"
        elif claim == "claimed":
            if not approval_only:
                stamped.add(w.profile_id)
            await _one(journal, w, b, db.now(), runner, report, approval_only=approval_only)


async def _one(journal: Journal, w: _Work, b: Any, now: datetime, runner: Runner, report: BatchReport, *, approval_only: bool) -> None:
    def done(state: str, message: str, run_id: int | None = None) -> None:
        if w.event_id:
            journal.update_event(w.event_id, now, only_from=("running",), state=state, message=message[:300], run_id=run_id)
        report.outcomes.append(Outcome(w.profile_id, b.app_id, w.event_id, state, message[:300], run_id))

    previous = None
    started = False
    try:
        if b.action == "send":
            app = cvstore.get_application(b.app_id)
            if app is None or app.current_rev != b.rev:
                return done("refused", CHANGED)
            if app.status == "draft" or (approval_only and app.approved_by != "user"):
                try:
                    app = cvs.approve(b.app_id, b.rev, expected_sha256=b.pdf_sha256)
                except CVError:
                    return done("refused", APPROVAL_REFUSED)
            if app.approved_sha256 != b.pdf_sha256:
                return done("refused", CHANGED)
        if approval_only:
            return done("done", SUPERVISED)
        previous = apply_store.last_run(b.app_id)
        started = True
        run = await runner(b.app_id, mode="submit", trigger="user", headless=None, _lock_held=True, expected_rev=b.rev,
                           expected_origin=b.origin_sha, expected_pdf=b.pdf_sha256)
        state, message = _outcome_of(run)
        done(state, message, getattr(run, "id", None))
    except Exception as error:
        if started:
            try:
                recorded = apply_store.last_run(b.app_id)
            except Exception:
                recorded = None
            if (recorded is not None and (previous is None or recorded.id > previous.id)
                    and recorded.rev == b.rev and recorded.mode == "submit" and recorded.trigger == "user"):
                state, message = _outcome_of(recorded)
                return done(state, message, recorded.id)
        done("failed", SEND_FAILED if isinstance(error, ApplyError) else "Erreur technique.")
