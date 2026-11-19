"""Portal approvals under the application send lock."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

import pytest

from jobhunt import db
from jobhunt.apply import accounts, facts, service
from jobhunt.apply import store as apply_store
from jobhunt.apply.types import Question
from jobhunt.cv import service as cvs
from jobhunt.cv import store as cvstore
from jobhunt.cv.errors import ApprovalError, CVError
from jobhunt.portal import executor
from jobhunt.portal.apply_events import apply_pending
from jobhunt.portal.executor import APPROVAL_REFUSED, CHANGED
from jobhunt.portal.journal import NewEvent

from portal_fakes import NOW, fixture_ats, journal, make_run, master, new_app, set_pdf  # noqa: F401

pytestmark = pytest.mark.usefixtures("fixture_ats")

STREAM = "b" * 32


async def run_batch(journal, profiles, now, **kwargs):
    with patch.object(db, "now", return_value=now):
        return await executor.run_batch(journal, profiles, now, **kwargs)


@pytest.fixture(autouse=True)
def no_keychain(monkeypatch):
    monkeypatch.setattr(accounts, "create_password", lambda host, email: "Pw-Test-1234")
    monkeypatch.setattr(accounts, "get_password", lambda host, email, **kw: None)


@pytest.fixture
def jp(journal, profile):
    journal.save_profile(profile.id, profile_key="a" * 32, stream_id=STREAM, enabled=True)
    return journal


def queue_intent(jp, profile, app, action="send", *, run_id=0, mode="", now=NOW, sha=None, origin=None):
    item = next(i for i in service.queue() if i.app.id == app.id)
    g = jp.find_or_create_grant(jp.get_profile(profile.id), action, now, app_id=app.id, rev=app.current_rev,
                                pdf_sha256=sha or cvstore.sha256_file(cvs.safe_pdf(app)), origin_sha=origin if origin is not None else item.origin_sha,
                                run_id=run_id, mode=mode or ("supervised" if action == "send" else ""))
    p = jp.get_profile(profile.id)
    seq = p.cursor_seq + 1
    eid = f"{profile.id:08x}{seq:024x}"
    jp.insert_page(profile.id, STREAM, [NewEvent(seq=seq, event_id=eid, kind="intent", payload={"grant": g}, at=NOW)], now)
    apply_pending(jp, profile.id, now)
    assert jp.get_event(eid).state == "queued"
    return eid, g


def runs_of(app):
    return apply_store.list_runs(app.id)


async def test_locked_send_lock_leaves_everything_queued_without_a_run_row(jp, profile, master, tmp_path):
    _, app = new_app(profile, master, tmp_path, 1, approve=True)
    eid, g = queue_intent(jp, profile, app)
    with service.send_lock():
        rep = await run_batch(jp, [profile.id], NOW)
    assert rep.locked and not rep.did_anything
    assert jp.get_event(eid).state == "queued" and jp.get_grant(g).state == "offered" and jp.sends_today(profile.id, NOW) == 0
    assert runs_of(app) == [] and jp.get_profile(profile.id).last_send_batch_at is None


@pytest.mark.parametrize("change", ["rev", "pdf", "origin", "withdrawn", "paused"])
async def test_send_binding_mismatch_is_refused_and_not_run(jp, profile, master, tmp_path, change):
    job, app = new_app(profile, master, tmp_path, 1, approve=True)
    eid, g = queue_intent(jp, profile, app)
    if change == "rev":
        with db.connect() as c:
            c.execute("UPDATE applications SET current_rev=current_rev+1 WHERE id=?", (app.id,))
    elif change == "pdf":
        set_pdf(cvstore.get_application(app.id), b"%PDF-1.4 other bytes")
    elif change == "origin":
        with db.connect() as c:
            c.execute("UPDATE jobs SET origin_url=? WHERE id=?", ("https://careers.exemple.test/jobs/other", job.id))
    elif change == "withdrawn":
        cvs.withdraw(app.id)
    else:
        make_run(app, tmp_path, status="needs_email")
    await run_batch(jp, [profile.id], NOW)
    assert jp.get_event(eid).state == "refused" and jp.get_event(eid).message == CHANGED
    assert jp.get_grant(g).state == "void" and jp.sends_today(profile.id, NOW) == 0


async def test_application_of_another_profile_is_refused(jp, profile, master, tmp_path):
    other = db.save_profile(profile.model_copy(update={"id": None, "name": "Théo"}))
    jp.save_profile(other.id, profile_key="d" * 32, stream_id=STREAM, enabled=True)
    _, app = new_app(profile, master, tmp_path, 1, approve=True)
    eid, _ = queue_intent(jp, other, app)  # a grant of profile B bound to an application of profile A
    await run_batch(jp, [other.id], NOW)
    assert jp.get_event(eid).state == "refused" and runs_of(app) == []


@pytest.mark.parametrize("handoff", ["needs_email", "needs_human"])
async def test_general_send_remains_blocked_after_a_later_run(jp, profile, master, tmp_path, handoff):
    _, app = new_app(profile, master, tmp_path, 1, approve=True)
    event_id, _ = queue_intent(jp, profile, app)
    make_run(app, tmp_path, status=handoff)
    make_run(app, tmp_path, mode="dry_run", status="ready")
    await run_batch(jp, [profile.id], NOW)
    assert jp.get_event(event_id).state == "refused"
    assert jp.sends_today(profile.id, NOW) == 0


async def test_resume_of_a_dry_run_or_a_finished_run_is_refused(jp, profile, master, tmp_path):
    _, app = new_app(profile, master, tmp_path, 1, approve=True)
    dry = make_run(app, tmp_path, mode="dry_run", status="needs_email")
    e1, _ = queue_intent(jp, profile, app, "resume", run_id=dry.id, mode="dry_run")
    _, app2 = new_app(profile, master, tmp_path, 2, approve=True)
    ready = make_run(app2, tmp_path, status="failed")
    e2, _ = queue_intent(jp, profile, app2, "resume", run_id=ready.id, mode="submit")
    await run_batch(jp, [profile.id], NOW)
    assert jp.get_event(e1).state == jp.get_event(e2).state == "refused"


@pytest.mark.parametrize("how", ["paused", "held", "disabled", "received"])
async def test_preconditions_block_the_profile(jp, profile, master, tmp_path, how):
    _, app = new_app(profile, master, tmp_path, 1, approve=True)
    eid, _ = queue_intent(jp, profile, app)
    if how == "paused":
        jp.set_paused(True)
    elif how == "held":
        jp.save_profile(profile.id, held=True)
    elif how == "disabled":
        jp.save_profile(profile.id, enabled=False)
    else:  # an event not applied yet (it could be a cancel of this very intent)
        jp.insert_page(profile.id, STREAM, [NewEvent(seq=2, event_id="2" * 32, kind="cancel", payload={"target": eid}, at=NOW)], NOW)
    await run_batch(jp, [profile.id], NOW)
    assert jp.get_event(eid).state == ("queued" if how in {"held", "received"} else "cancelled") and runs_of(app) == []


async def test_a_daily_run_in_progress_blocks_the_batch(jp, profile, master, tmp_path, monkeypatch):
    from jobhunt import schedule

    _, app = new_app(profile, master, tmp_path, 1, approve=True)
    eid, _ = queue_intent(jp, profile, app)
    monkeypatch.setattr(schedule, "is_running", lambda **kw: True)
    await run_batch(jp, [profile.id], NOW)
    assert jp.get_event(eid).state == "queued"


async def test_disable_after_collection_does_not_start_or_charge_send(jp, profile, master, tmp_path, monkeypatch):
    _, app = new_app(profile, master, tmp_path, 1, approve=True)
    event, grant = queue_intent(jp, profile, app)
    real_claim = jp.claim_send

    def race(*args, **kwargs):
        jp.save_profile(profile.id, enabled=False, control_time=NOW)
        return real_claim(*args, **kwargs)

    monkeypatch.setattr(jp, "claim_send", race)
    await run_batch(jp, [profile.id], NOW)
    assert jp.sends_today(profile.id, NOW) == 0
    assert jp.get_profile(profile.id).last_send_batch_at is None
    assert jp.get_event(event).state == "cancelled" and jp.get_grant(grant).state == "void"


async def test_a_running_intent_is_interrupted_and_never_reexecuted(jp, profile, master, tmp_path):
    _, app = new_app(profile, master, tmp_path, 1, approve=True)
    eid, g = queue_intent(jp, profile, app)
    assert jp.claim_intent(profile.id, eid, g, NOW, 60) == "claimed"  # claimed, then the process died
    assert jp.interrupted_sweep(profile.id, NOW) == 1
    rep = await run_batch(jp, [profile.id], NOW + timedelta(hours=1))
    assert not rep.did_anything
    ev = jp.get_event(eid)
    assert ev.state == "interrupted" and ev.message == "Résultat inconnu : vérifiez avant de réessayer."


async def test_approval_error_is_a_clear_refusal_and_the_batch_continues(jp, profile, master, tmp_path, monkeypatch):
    _, a = new_app(profile, master, tmp_path, 1, score=90)
    _, b = new_app(profile, master, tmp_path, 2, approve=True, score=80)
    ea, ga = queue_intent(jp, profile, a)
    eb, _ = queue_intent(jp, profile, b)
    real = cvs.approve

    def approve(app_id, rev, **kw):
        if app_id == a.id:
            raise ApprovalError("erreur bloquante")
        return real(app_id, rev, **kw)

    monkeypatch.setattr(cvs, "approve", approve)
    await run_batch(jp, [profile.id], NOW)
    assert jp.get_event(ea).state == "refused" and jp.get_event(ea).message == APPROVAL_REFUSED and jp.get_grant(ga).state == "used"
    assert jp.get_event(eb).state == "done" and not runs_of(b)


async def test_other_cv_errors_use_fixed_message(jp, profile, master, tmp_path, monkeypatch):
    _, a = new_app(profile, master, tmp_path, 1)
    ea, _ = queue_intent(jp, profile, a)
    monkeypatch.setattr(cvs, "approve", lambda *x, **k: (_ for _ in ()).throw(CVError("Version périmée")))
    await run_batch(jp, [profile.id], NOW)
    assert jp.get_event(ea).state == "refused" and jp.get_event(ea).message == APPROVAL_REFUSED


async def test_no_new_run_after_the_time_limit(jp, profile, master, tmp_path, monkeypatch):
    _, a = new_app(profile, master, tmp_path, 1, approve=True, score=90)
    _, b = new_app(profile, master, tmp_path, 2, approve=True, score=80)
    queue_intent(jp, profile, a)
    eb, _ = queue_intent(jp, profile, b)
    clock = iter([0.0, 0.0, 21 * 60.0, 21 * 60.0, 21 * 60.0])
    monkeypatch.setattr(executor, "_monotonic", lambda: next(clock))
    rep = await run_batch(jp, [profile.id], NOW)
    assert len(rep.outcomes) == 1 and rep.stopped_for_time and jp.get_event(eb).state == "queued"


async def test_two_profiles_in_one_batch(jp, profile, master, tmp_path):
    other = db.save_profile(profile.model_copy(update={"id": None, "name": "Théo"}))
    jp.save_profile(other.id, profile_key="d" * 32, stream_id=STREAM, enabled=True)
    from cv_fakes import save_confirmed

    m2 = save_confirmed(other.id)
    _, a = new_app(profile, master, tmp_path, 1, approve=True)
    _, b = new_app(other, m2, tmp_path, 2, approve=True)
    ea, _ = queue_intent(jp, profile, a)
    eb, _ = queue_intent(jp, other, b)
    rep = await run_batch(jp, [profile.id, other.id], NOW)
    assert {outcome.app_id for outcome in rep.outcomes} == {a.id, b.id} and rep.profiles() == {profile.id, other.id}
    assert jp.get_event(ea).state == jp.get_event(eb).state == "done"
    assert not runs_of(a) and not runs_of(b)


# ------------------------------------------------------------------ automatic resume
def paused_on(jp, profile, master, tmp_path, n=1, labels=("Ville ?",)):
    _, app = new_app(profile, master, tmp_path, n, approve=True)
    run_ = make_run(app, tmp_path, status="needs_input", questions=[Question(label=l) for l in labels])
    return app, run_


def record_question_answer(journal, profile_id, question, answer):
    seq = journal.get_profile(profile_id).cursor_seq + 1
    event_id = f"{profile_id:08x}{seq:024x}"
    journal.insert_page(profile_id, STREAM, [NewEvent(seq=seq, event_id=event_id, kind="question_answer",
                                payload={"qid": facts.custom_id(question), "answer": answer}, at=NOW)], NOW)
    apply_pending(journal, profile_id, NOW)
    assert journal.get_event(event_id).state == "applied"


async def test_legacy_resume_work_cannot_start_an_attempt(jp, profile, master, tmp_path):
    app, run = paused_on(jp, profile, master, tmp_path)
    record_question_answer(jp, profile.id, "Ville ?", "Lyon")
    answer, = jp.events(profile.id, ("applied",), ("question_answer",))
    assert jp.resume_candidates(profile.id) == set()
    jp.record_resume_candidates(profile.id, answer.event_id, [(app.id, run.id)])
    event, grant = queue_intent(jp, profile, app, "resume", run_id=run.id, mode="submit")
    await run_batch(jp, [profile.id], NOW)
    assert jp.get_event(event).state == "refused" and jp.get_grant(grant).state == "void"
    assert [item.id for item in runs_of(app)] == [run.id] and jp.sends_today(profile.id, NOW) == 0


async def test_approval_is_not_a_dispatch_authorization(jp, profile, master, tmp_path, fixture_ats):
    _, draft = new_app(profile, master, tmp_path)
    event, grant = queue_intent(jp, profile, draft)
    report = await run_batch(jp, [profile.id], NOW)
    assert report.did_anything and jp.get_event(event).state == "done" and jp.get_grant(grant).state == "used"
    approved = cvstore.get_application(draft.id)
    assert (approved.approved_rev, approved.approved_sha256, approved.approved_by) == (draft.current_rev, draft.pdf_sha256, "portal")
    assert not runs_of(draft) and not fixture_ats and jp.sends_today(profile.id, NOW) == 0
    assert (await service.run(draft.id, mode="submit", trigger="user")).status == "blocked"
    assert (await service.run(draft.id, mode="submit", trigger="auto")).status == "blocked"
    assert not fixture_ats


async def test_legacy_submit_grant_is_refused(jp, profile, master, tmp_path):
    _, draft = new_app(profile, master, tmp_path)
    event, grant = queue_intent(jp, profile, draft, mode="submit")
    await run_batch(jp, [profile.id], NOW)
    assert jp.get_event(event).state == "refused" and jp.get_grant(grant).state == "void"
    assert cvstore.get_application(draft.id).status == "draft" and not runs_of(draft)
