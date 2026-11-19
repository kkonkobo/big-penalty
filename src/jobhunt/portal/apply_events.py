"""Apply the events taken from the portal inbox through the engine's own functions (DESIGN §5.2 step 3, §5.4).

Rules: cancels first; then, in `seq` order, standing answers, custom answers, answers to pending questions and intents.
Nothing is applied while the portal is paused locally or the profile is held; in review mode events wait as
`awaiting_review` until `apply_reviewed`. A result message never contains a value."""

from __future__ import annotations

import contextlib
import os
import time
from dataclasses import dataclass, field
from datetime import datetime

from jobhunt import db
from jobhunt.apply import facts, service
from jobhunt.apply import store as apply_store
from jobhunt.apply.errors import ApplyError
from jobhunt.config import get_settings
from jobhunt.cv import store as cvstore
from jobhunt.filelock import try_lock, unlock
from jobhunt.portal import protocol
from jobhunt.portal.journal import EventRow, Journal, ProfileRow
from jobhunt.portal.snapshot import (answer_key, custom_entry, custom_key, field_version, question_id, settlement_binding,
                                     stored_answer, value_hash)

TOO_LATE = "Trop tard : l'action avait déjà commencé."
GRANT_GONE = "Cette action n'est plus valable : rechargez la page."
CONFLICT = ("Modifiée sur le Mac entre-temps : la valeur du Mac est conservée. Vérifiez-la et ressaisissez la vôtre si besoin.")
NOT_PENDING = "Cette question n'est plus en attente."
TECHNICAL = "Erreur technique."
UNVERIFIED = "L'enregistrement n'a pas pu être vérifié : réessayez."
OPEN_STATES = ("received", "awaiting_review")
LOCK_TIMEOUT_S = 5.0


class _LockBusy(Exception):
    pass


@dataclass
class ApplyReport:
    applied_answers: bool = False
    states: dict[str, int] = field(default_factory=dict)
    handled: list[str] = field(default_factory=list)
    busy: int = 0                          # events left as they were because the answers lock was busy
    skipped_reason: str = ""

    def count(self, state: str) -> None:
        self.states[state] = self.states.get(state, 0) + 1


def _engine_lock():
    """The engine's `facts.answers_lock` when it has one (test seam)."""
    return getattr(facts, "answers_lock", None)


@contextlib.contextmanager
def answers_lock(profile_id: int):
    """`facts.answers_lock` when the engine has it, else a portal lock file next to answers.yaml."""
    ext = _engine_lock()
    if ext is not None:
        stack = contextlib.ExitStack()
        try:
            stack.enter_context(ext(profile_id))
        except ApplyError:
            raise _LockBusy from None
        with stack:
            yield
        return
    path = facts.answers_path(profile_id).with_name("answers.yaml.portal-lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        deadline = time.monotonic() + LOCK_TIMEOUT_S
        while not try_lock(fd):
            if time.monotonic() >= deadline:
                raise _LockBusy
            time.sleep(0.05)
        try:
            yield
        finally:
            unlock(fd)
    finally:
        os.close(fd)


class _Ctx:
    def __init__(self, journal: Journal, profile_id: int, now: datetime, report: ApplyReport):
        self.j, self.pid, self.now, self.report = journal, profile_id, now, report

    def profile(self) -> ProfileRow:
        p = self.j.get_profile(self.pid)
        assert p is not None
        return p

    def checkpoint(self, ev: EventRow) -> bool:
        before = self.profile()
        self.j.checkpoint()
        profile, stop = self.j.controls(self.pid)
        current = self.j.get_event(ev.event_id)
        if stop:
            self.report.skipped_reason = stop
        return bool(not stop and profile.epoch == before.epoch and profile.stream_id == ev.stream_id
                    and current and current.state == ev.state and (not profile.review_mode or current.approved))

    def finish(self, ev: EventRow, state: str, message: str = "", **extra) -> None:
        if self.j.update_event(ev.event_id, self.now, only_from=OPEN_STATES, state=state, message=message[:300], **extra):
            self.report.count(state)
            self.report.handled.append(ev.event_id)


def _same(f: facts.AnswerField, a: str, b: str) -> bool:
    if a == b:
        return True
    if f.widget == "date":
        da, db_ = facts.from_french_date(a), facts.from_french_date(b)
        return da is not None and da == db_
    return False


def _expected_text(f: facts.AnswerField, value: str, current: str) -> str:
    """The text `facts.save_answers` will store for `value` (ApplyError when the engine refuses it)."""
    cleaned = facts._clean_value(f, value)
    if not cleaned and f.widget == "date" and current and facts.from_french_date(current) is None:
        return current  # a legacy free-text date is kept unless a date is picked
    return cleaned


def _allowed(ctx: _Ctx, cur_ver: str, base_ver: str, after: str | None) -> bool:
    if cur_ver == base_ver:
        return True
    if after:
        prev = ctx.j.get_event(after)
        return bool(prev and prev.profile_id == ctx.pid and prev.ver_after and prev.ver_after == cur_ver)
    return False


# ---------------------------------------------------------------- labels
def _intent_label(ctx: _Ctx, grant_id: str) -> str:
    g = ctx.j.get_grant(grant_id)
    app = cvstore.get_application(g.app_id) if g and g.profile_id == ctx.pid else None
    return f"{app.company or 'Société'} — {app.job_title or 'Poste'}"[:500] if app else ""


def _describe(ctx: _Ctx, ev: EventRow) -> tuple[str, str, str]:
    """(label, before_text, after_text) for the operator's local view."""
    p, pid = ev.payload, ctx.pid
    if ev.kind == "answer":
        f = facts.ANSWER_BY_PATH.get(p.get("path", ""))
        return (f.label if f else str(p.get("path", ""))), stored_answer(pid, p["path"]) if f else "", str(p.get("value", ""))
    if ev.kind in ("custom_edit", "custom_delete"):
        e = custom_entry(pid, p.get("custom_id", ""))
        return (e["question"] if e else "Réponse enregistrée"), (e["answer"] if e else ""), (str(p.get("answer", "")) if ev.kind == "custom_edit" else "(supprimée)")
    if ev.kind == "question_answer":
        q = next((q for _a, _r, q in service.pending_questions(pid) if question_id(q.label) == p.get("qid")), None)
        return (q.label if q else "Question"), "", str(p.get("answer", ""))
    if ev.kind == "intent":
        return _intent_label(ctx, p.get("grant", "")), "", ""
    return "", "", ""


# ---------------------------------------------------------------- handlers
def _planned_write(ctx: _Ctx, ev: EventRow, key: str, current: str | None, label: str) -> bool:
    if not ev.write_ver:
        return False
    current_hash = value_hash(ctx.j, current)
    if current_hash == ev.planned_hash:
        ctx.j.finalize_write(ctx.pid, ev.event_id, key, current_hash, ev.write_ver, ctx.now, "Enregistrée.")
        ctx.report.applied_answers = True
        ctx.report.count("applied")
        ctx.report.handled.append(ev.event_id)
        return True
    if current_hash != ev.before_hash:
        ctx.finish(ev, "conflict", CONFLICT, label=label)
        return True
    return False


def _write_plan(ctx: _Ctx, ev: EventRow, current: str | None, planned: str | None, **display) -> str | None:
    planned_hash = value_hash(ctx.j, planned)
    if ev.write_ver:
        if ev.planned_hash != planned_hash:
            ctx.finish(ev, "conflict", CONFLICT)
            return None
        return ev.write_ver
    version = protocol.new_id()
    ctx.j.update_event(ev.event_id, ctx.now, write_ver=version, before_hash=value_hash(ctx.j, current),
                       planned_hash=planned_hash, **display)
    return version if ctx.checkpoint(ev) else None


def reconcile_write_plans(journal: Journal, profile_id: int, now: datetime) -> None:
    """Settle finished writes without replaying unstarted writes during a control change."""
    ctx = _Ctx(journal, profile_id, now, ApplyReport())
    for event in journal.events(profile_id, OPEN_STATES, tuple(_ANSWERS)):
        if not event.write_ver:
            continue
        try:
            if event.kind == "answer":
                path = event.payload["path"]
                key, current = answer_key(path), stored_answer(profile_id, path)
            else:
                custom_id = event.payload["qid" if event.kind == "question_answer" else "custom_id"]
                entry = custom_entry(profile_id, custom_id)
                key, current = custom_key(custom_id), entry["answer"] if entry else None
            _planned_write(ctx, event, key, current, event.label)
        except (ApplyError, KeyError, UnicodeError):
            ctx.finish(event, "interrupted", "Écriture interrompue : vérifiez la réponse sur le Mac.")


def _answer(ctx: _Ctx, ev: EventRow) -> None:
    p, pid = ev.payload, ctx.pid
    f = facts.ANSWER_BY_PATH.get(p["path"])
    if f is None:
        return ctx.finish(ev, "rejected", "Ce champ n'existe pas.", label=str(p["path"])[:500])
    label = f.label
    if ev.ver_after:  # replay of an event whose write is already recorded
        return ctx.finish(ev, "applied", "Enregistrée.", label=label)
    key = answer_key(f.path)
    current = stored_answer(pid, f.path)
    if _planned_write(ctx, ev, key, current, label):
        return
    try:
        expected = _expected_text(f, p["value"], current)
    except ApplyError as e:
        return ctx.finish(ev, "rejected", str(e), label=label, before_text=current, after_text=str(p["value"]))
    cur_ver = field_version(ctx.j, pid, key, current)
    if not _allowed(ctx, cur_ver, p["base_ver"], p.get("after")):
        return ctx.finish(ev, "conflict", CONFLICT, label=label, before_text=current, after_text=expected)
    new_ver = _write_plan(ctx, ev, current, expected, label=label, before_text=current, after_text=expected)
    if not new_ver:
        return
    ok = False
    for _ in range(2):
        try:
            facts.save_answers(pid, {f.path: p["value"]})
        except ApplyError as e:
            return ctx.finish(ev, "rejected", str(e), label=label)
        got = stored_answer(pid, f.path)
        if _same(f, got, expected):
            ok = True
            break
    if not ok:
        return ctx.finish(ev, "failed", UNVERIFIED, label=label)
    ctx.j.finalize_write(pid, ev.event_id, key, value_hash(ctx.j, got), new_ver, ctx.now, "Enregistrée.")
    ctx.report.applied_answers = True
    ctx.report.count("applied")
    ctx.report.handled.append(ev.event_id)


def _custom(ctx: _Ctx, ev: EventRow) -> None:
    p, pid = ev.payload, ctx.pid
    cid, delete = p["custom_id"], ev.kind == "custom_delete"
    first = custom_entry(pid, cid)
    label = first["question"] if first else "Réponse enregistrée"
    if ev.ver_after:
        return ctx.finish(ev, "applied", "Enregistrée.", label=label)
    expected = None if delete else " ".join(str(p["answer"]).split())[:500]
    if expected == "":
        return ctx.finish(ev, "rejected", "Réponse vide : utilisez « Retirer » pour la supprimer.", label=label)
    key = custom_key(cid)
    entry = custom_entry(pid, cid)
    cur_text = entry["answer"] if entry else None
    if _planned_write(ctx, ev, key, cur_text, label):
        return
    cur_ver = field_version(ctx.j, pid, key, cur_text)
    before = cur_text or ""
    if entry is None or not _allowed(ctx, cur_ver, p["base_ver"], p.get("after")):
        return ctx.finish(ev, "conflict", CONFLICT, label=label, before_text=before)
    new_ver = _write_plan(ctx, ev, cur_text, expected, label=label, before_text=before, after_text=expected or "(supprimée)")
    if not new_ver:
        return
    ok = False
    for _ in range(2):
        try:
            done = (facts.delete_custom_answer(pid, cid, cur_text) if delete
                    else facts.update_custom_answer(pid, cid, p["answer"], cur_text))
        except ApplyError as e:
            return ctx.finish(ev, "rejected", str(e), label=label)
        if not done and custom_entry(pid, cid) is not None and (custom_entry(pid, cid)["answer"] == expected):
            done = True
        if not done:
            return ctx.finish(ev, "conflict", CONFLICT, label=label, before_text=before)
        after = custom_entry(pid, cid)
        if (after["answer"] if after else None) == expected:
            ok = True
            break
    if not ok:
        return ctx.finish(ev, "failed", UNVERIFIED, label=label)
    ctx.j.finalize_write(pid, ev.event_id, key, value_hash(ctx.j, expected), new_ver, ctx.now, "Enregistrée.")
    ctx.report.applied_answers = True
    ctx.report.count("applied")
    ctx.report.handled.append(ev.event_id)


def _question(ctx: _Ctx, ev: EventRow) -> None:
    p, pid = ev.payload, ctx.pid
    qid, answer = p["qid"], str(p["answer"]).strip()
    key = custom_key(qid)
    existing = custom_entry(pid, qid)
    current = existing["answer"] if existing else None
    if ev.ver_after:
        return ctx.finish(ev, "applied", "Enregistrée.", label=ev.label)
    if _planned_write(ctx, ev, key, current, ev.label):
        return
    match = next((q for _a, _r, q in service.pending_questions(pid) if question_id(q.label) == qid), None)
    if match is None:
        return ctx.finish(ev, "rejected", NOT_PENDING)
    label = match.label[:500]
    if match.options and answer not in match.options:
        return ctx.finish(ev, "rejected", "Cette réponse ne fait pas partie des choix proposés.", label=label)
    if not ctx.profile().review_mode:
        candidates = [(item.app.id, item.last_run.id) for item in service.resumable(pid)
                      if item.last_run is not None and item.last_run.mode == "submit"
                      and any(question_id(q.label) == qid for q in item.questions)]
        if candidates and not ev.write_ver:
            ctx.j.record_resume_candidates(pid, ev.event_id, candidates)
    new_ver = _write_plan(ctx, ev, current, answer, label=label, after_text=answer)
    if not new_ver:
        return
    facts.save_custom_answers(pid, [(match.label, answer)])
    saved = custom_entry(pid, qid)
    if saved is None or saved["answer"] != answer:
        return ctx.finish(ev, "failed", UNVERIFIED, label=label)
    ctx.report.applied_answers = True
    ctx.j.finalize_write(pid, ev.event_id, key, value_hash(ctx.j, saved["answer"]), new_ver, ctx.now, "Enregistrée.")
    ctx.report.count("applied")
    ctx.report.handled.append(ev.event_id)


def _intent(ctx: _Ctx, ev: EventRow) -> None:
    gid = ev.payload["grant"]
    profile = ctx.profile()
    label = _intent_label(ctx, gid)
    g = ctx.j.usable_grant(gid, profile, ctx.now)
    retry = False
    if g is None:
        old = ctx.j.get_grant(gid)
        if (old is None or old.profile_id != ctx.pid or old.stream_id != ev.stream_id or old.epoch != profile.epoch
                or old.state != "used" or old.used_by != ev.event_id or old.action not in ("resolve_sent", "resolve_not_sent")):
            return ctx.finish(ev, "refused", GRANT_GONE, label=label)
        g, retry = old, True
    if g.action in ("resolve_sent", "resolve_not_sent"):
        out = apply_store.outstanding_attempt(g.app_id)
        app = cvstore.get_application(g.app_id)
        job = db.get_job(app.job_id) if app else None
        if (out is None or out.id != g.run_id or out.mode != g.mode or app is None or app.profile_id != ctx.pid
                or app.current_rev != g.rev or any(getattr(g, key) != value for key, value in settlement_binding(
                    app, out, service.origin_digest(job.origin_url if job else None), apply_store.held_binding(out.id)).items())):
            return ctx.finish(ev, "interrupted" if retry else "refused", GRANT_GONE, label=label)
        if not retry:
            other = "resolve_not_sent" if g.action == "resolve_sent" else "resolve_sent"
            sibling = next((x.grant_id for x in ctx.j.grants(ctx.pid) if x.action == other and x.app_id == g.app_id and x.run_id == g.run_id
                            and x.state == "offered" and x.stream_id == g.stream_id and x.epoch == g.epoch), None)
            if not ctx.j.use_grant_pair(g.grant_id, sibling, ev.event_id, ctx.now):
                return ctx.finish(ev, "refused", GRANT_GONE, label=label)
            if not ctx.checkpoint(ev):
                return
            return _intent(ctx, ev)
        try:
            service.resolve_unconfirmed(g.app_id, sent=g.action == "resolve_sent", run_id=g.run_id)
        except ApplyError:
            return ctx.finish(ev, "interrupted" if retry else "refused", GRANT_GONE, label=label)
        return ctx.finish(ev, "done", "Notée comme envoyée." if g.action == "resolve_sent"
                          else "Notée comme non envoyée. Nouvelle tentative manuelle uniquement.", label=label)
    ctx.finish(ev, "queued", "En attente de traitement sur le Mac.", label=label)


def _cancel(ctx: _Ctx, ev: EventRow) -> None:
    target = ctx.j.get_event(str(ev.payload.get("target", "")))
    if target is None or target.profile_id != ctx.pid or target.stream_id != ev.stream_id or target.kind != "intent":
        ctx.finish(ev, "refused", "Action introuvable.")
    elif target.state == "cancelled":
        ctx.finish(ev, "done", "Annulée.", label=target.label)
    elif ctx.j.update_event(target.event_id, ctx.now, only_from=(*OPEN_STATES, "queued"), state="cancelled", message="Annulée."):
        grant = ctx.j.get_grant(target.payload.get("grant", ""))
        if grant and grant.action == "resume" and grant.profile_id == ctx.pid and grant.stream_id == ev.stream_id:
            ctx.j.forget_resume(ctx.pid, grant.app_id, grant.run_id)
        ctx.report.count("cancelled")
        ctx.finish(ev, "done", "Annulée.", label=target.label)
    else:
        ctx.finish(ev, "refused", TOO_LATE, label=target.label)


_ANSWERS = {"answer": _answer, "custom_edit": _custom, "custom_delete": _custom, "question_answer": _question}
_HANDLERS = {**_ANSWERS, "intent": _intent, "cancel": _cancel}


def _apply_one(ctx: _Ctx, ev: EventRow) -> None:
    try:
        _HANDLERS[ev.kind](ctx, ev)
    except Exception:  # noqa: BLE001 - one event never stops the others; no detail (it could hold a value)
        ctx.finish(ev, "failed", TECHNICAL)


# ---------------------------------------------------------------- entry points
def _process(ctx: _Ctx, candidate: EventRow, decision: bool | None = None, *, validated: ProfileRow | None = None) -> None:
    lock = answers_lock(ctx.pid) if candidate.kind in _ANSWERS and decision is not False else contextlib.nullcontext()
    try:
        with lock, ctx.j.transaction() as journal:
            current = _Ctx(journal, ctx.pid, ctx.now, ctx.report)
            profile, stop = journal.controls(ctx.pid)
            if stop:
                ctx.report.skipped_reason = stop
                return
            if decision is True and (validated is None or profile != validated):
                ctx.report.skipped_reason = "contrôles non validés"
                return
            ev = journal.get_event(candidate.event_id)
            if (ev is None or ev.profile_id != ctx.pid or ev.stream_id != profile.stream_id or ev.state not in OPEN_STATES
                    or (decision is not False and not profile.revoked_through_seq < ev.seq <= profile.cursor_seq)):
                return
            if decision is not None:
                if ev.state != "awaiting_review":
                    return
                if not decision:
                    current.finish(ev, "rejected", "Refusée sur le Mac.")
                    return
                journal.update_event(ev.event_id, ctx.now, approved=True)
            elif not ev.approved:
                if ev.state == "awaiting_review":
                    return
                if profile.review_mode:
                    label, before, after = _describe(current, ev)
                    current.finish(ev, "awaiting_review", "En attente de validation sur le Mac.",
                                   label=label[:500], before_text=before, after_text=after)
                    return
            _apply_one(current, ev)
    except _LockBusy:
        ctx.report.busy += 1


def apply_pending(journal: Journal, profile_id: int, now: datetime) -> ApplyReport:
    report = ApplyReport()
    _, stop = journal.controls(profile_id)
    if stop:
        report.skipped_reason = stop
        return report
    ctx = _Ctx(journal, profile_id, now, report)
    events = journal.events(profile_id, OPEN_STATES)
    for ev in sorted(events, key=lambda item: (not item.write_ver, item.kind != "cancel")):
        if ev.state == "received" or ev.approved:
            busy = report.busy
            _process(ctx, ev)
            if ev.write_ver and report.busy > busy:
                break
    return report


def apply_reviewed(journal: Journal, profile_id: int, event_ids: list[str] | None, approve: bool, now: datetime | None = None,
                   *, validated: ProfileRow | None = None) -> ApplyReport:
    """The operator's decision on `awaiting_review` events (all of them when `event_ids` is None)."""
    now = now or db.now()
    report = ApplyReport()
    profile, stop = journal.controls(profile_id)
    if stop:
        report.skipped_reason = stop
        return report
    if approve and (validated is None or profile != validated):
        report.skipped_reason = "contrôles non validés"
        return report
    ctx = _Ctx(journal, profile_id, now, report)
    ids = set(event_ids) if event_ids is not None else None
    chosen = [e for e in journal.events(profile_id, ("awaiting_review",)) if ids is None or e.event_id in ids]
    if approve and ((ids is not None and len(chosen) != len(ids)) or any(
        ev.stream_id != profile.stream_id or not profile.revoked_through_seq < ev.seq <= profile.cursor_seq for ev in chosen
    )):
        report.skipped_reason = "sélection périmée : vérifiez les actions affichées"
        return report
    for ev in sorted(chosen, key=lambda item: item.kind != "cancel"):
        _process(ctx, ev, approve, validated=validated)
        if report.skipped_reason:
            break
    return report
