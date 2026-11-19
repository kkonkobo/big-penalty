"""portal/executor.py: the batch of send / resume intents under the engine's send lock."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch
from types import SimpleNamespace

import pytest

from jobhunt import db
from jobhunt.apply import accounts, facts, service
from jobhunt.apply import store as apply_store
from jobhunt.apply.errors import ApplyError
from jobhunt.apply.types import Question, RunResult
from jobhunt.cv import service as cvs
from jobhunt.cv import store as cvstore
from jobhunt.cv.errors import ApprovalError, CVError
from jobhunt.portal import executor
from jobhunt.portal.apply_events import apply_pending
from jobhunt.portal.executor import APPROVAL_REFUSED, CHANGED
from jobhunt.portal.journal import NewEvent

from apply_service_fakes import FakePool, stub_engine
from portal_fakes import NOW, fixture_ats, journal, make_run, make_sent, master, new_app, set_pdf  # noqa: F401

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


class Runner:
    """Stands in for service.run: records calls, returns scripted runs."""

    def __init__(self, status="submitted", **kw):
        self.calls, self.status, self.kw, self.raises = [], status, kw, None

    async def __call__(self, app_id, **kw):
        self.calls.append((app_id, kw))
        if self.raises:
            raise self.raises
        return SimpleNamespace(id=1000 + len(self.calls), status=self.status, message=self.kw.get("message", ""),
                               submit_clicked_at=self.kw.get("clicked"), nav_clicked_at=None, result=None)


def queue_intent(jp, profile, app, action="send", *, run_id=0, mode="", now=NOW, sha=None, origin=None):
    item = next(i for i in service.queue() if i.app.id == app.id)
    g = jp.find_or_create_grant(jp.get_profile(profile.id), action, now, app_id=app.id, rev=app.current_rev,
                                pdf_sha256=sha or cvstore.sha256_file(cvs.safe_pdf(app)), origin_sha=origin if origin is not None else item.origin_sha,
                                run_id=run_id, mode=mode or ("submit" if action == "send" else ""))
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
    runner = Runner()
    with service.send_lock():
        rep = await run_batch(jp, [profile.id], NOW, runner=runner)
    assert rep.locked and not rep.did_anything and not runner.calls
    assert jp.get_event(eid).state == "queued" and jp.get_grant(g).state == "offered" and jp.sends_today(profile.id, NOW) == 0
    assert runs_of(app) == [] and jp.get_profile(profile.id).last_send_batch_at is None


async def test_a_send_runs_with_the_bound_arguments(jp, profile, master, tmp_path):
    _, app = new_app(profile, master, tmp_path, 1, approve=True)
    eid, g = queue_intent(jp, profile, app)
    runner = Runner()
    rep = await run_batch(jp, [profile.id], NOW, runner=runner)
    (app_id, kw), = runner.calls
    item = next(i for i in service.queue(profile.id) if i.app.id == app.id)
    assert app_id == app.id and kw == {"mode": "submit", "trigger": "user", "headless": None, "_lock_held": True,
                                       "expected_rev": app.current_rev, "expected_origin": item.origin_sha,
                                       "expected_pdf": app.pdf_sha256}
    assert "attended" not in kw
    ev = jp.get_event(eid)
    assert ev.state == "done" and ev.message == "Envoyée." and ev.run_id == 1001 and jp.get_grant(g).state == "used"
    assert jp.sends_today(profile.id, NOW) == 1 and rep.did_anything
    assert jp.get_profile(profile.id).last_send_batch_at == NOW


async def test_the_batch_takes_the_lock_once_and_runs_inside_it(jp, profile, master, tmp_path):
    _, a = new_app(profile, master, tmp_path, 1, approve=True)
    _, b = new_app(profile, master, tmp_path, 2, approve=True)
    queue_intent(jp, profile, a)
    queue_intent(jp, profile, b)
    held = []

    async def probe(app_id, **kw):
        try:
            with service.send_lock():
                held.append(False)
        except service.SendLocked:
            held.append(True)
        return SimpleNamespace(id=1, status="submitted", message="", submit_clicked_at=None, nav_clicked_at=None, result=None)

    await run_batch(jp, [profile.id], NOW, runner=probe)
    assert held == [True, True]
    with service.send_lock():  # released afterwards
        pass


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
    runner = Runner()
    await run_batch(jp, [profile.id], NOW, runner=runner)
    assert not runner.calls and jp.get_event(eid).state == "refused" and jp.get_event(eid).message == CHANGED
    assert jp.get_grant(g).state == "void" and jp.sends_today(profile.id, NOW) == 0


async def test_application_of_another_profile_is_refused(jp, profile, master, tmp_path):
    other = db.save_profile(profile.model_copy(update={"id": None, "name": "Théo"}))
    jp.save_profile(other.id, profile_key="d" * 32, stream_id=STREAM, enabled=True)
    _, app = new_app(profile, master, tmp_path, 1, approve=True)
    eid, _ = queue_intent(jp, other, app)  # a grant of profile B bound to an application of profile A
    runner = Runner()
    await run_batch(jp, [other.id], NOW, runner=runner)
    assert not runner.calls and jp.get_event(eid).state == "refused" and runs_of(app) == []


@pytest.mark.parametrize("handoff", ["needs_email", "needs_human"])
async def test_general_send_remains_blocked_after_a_later_run(jp, profile, master, tmp_path, handoff):
    _, app = new_app(profile, master, tmp_path, 1, approve=True)
    event_id, _ = queue_intent(jp, profile, app)
    make_run(app, tmp_path, status=handoff)
    make_run(app, tmp_path, mode="dry_run", status="ready")
    runner = Runner()
    await run_batch(jp, [profile.id], NOW, runner=runner)
    assert jp.get_event(event_id).state == "refused" and not runner.calls
    assert jp.sends_today(profile.id, NOW) == 0


async def test_resume_checks_the_bound_run_mode_and_status(jp, profile, master, tmp_path):
    _, app = new_app(profile, master, tmp_path, 1, approve=True)
    paused = make_run(app, tmp_path, status="needs_email")
    eid, g = queue_intent(jp, profile, app, "resume", run_id=paused.id, mode="submit")
    runner = Runner(status="needs_email")
    await run_batch(jp, [profile.id], NOW, runner=runner)
    assert len(runner.calls) == 1 and jp.get_event(eid).state == "failed"
    assert jp.get_event(eid).message == "En pause : un e-mail de confirmation est à valider."

    _, app2 = new_app(profile, master, tmp_path, 2, approve=True)
    p2 = make_run(app2, tmp_path, status="needs_email")
    e2, g2 = queue_intent(jp, profile, app2, "resume", run_id=p2.id, mode="submit", now=NOW + timedelta(minutes=30))
    make_run(app2, tmp_path, status="needs_human")  # a newer run exists: the bound one is no longer the last
    runner2 = Runner()
    await run_batch(jp, [profile.id], NOW + timedelta(minutes=30), runner=runner2)
    assert not runner2.calls and jp.get_event(e2).state == "refused"


async def test_resume_of_a_dry_run_or_a_finished_run_is_refused(jp, profile, master, tmp_path):
    _, app = new_app(profile, master, tmp_path, 1, approve=True)
    dry = make_run(app, tmp_path, mode="dry_run", status="needs_email")
    e1, _ = queue_intent(jp, profile, app, "resume", run_id=dry.id, mode="dry_run")
    _, app2 = new_app(profile, master, tmp_path, 2, approve=True)
    ready = make_run(app2, tmp_path, status="failed")
    e2, _ = queue_intent(jp, profile, app2, "resume", run_id=ready.id, mode="submit")
    runner = Runner()
    await run_batch(jp, [profile.id], NOW, runner=runner)
    assert not runner.calls and jp.get_event(e1).state == jp.get_event(e2).state == "refused"


async def test_daily_budget_leaves_the_rest_queued_until_tomorrow(jp, profile, master, tmp_path, monkeypatch):
    monkeypatch.setenv("JOBHUNT_PORTAL_MAX_SENDS_PER_DAY", "1")
    monkeypatch.setenv("JOBHUNT_PORTAL_SEND_EVERY_MIN", "0")
    _, a = new_app(profile, master, tmp_path, 1, approve=True, score=90)
    _, b = new_app(profile, master, tmp_path, 2, approve=True, score=80)
    ea, _ = queue_intent(jp, profile, a)
    eb, gb = queue_intent(jp, profile, b)
    runner = Runner()
    await run_batch(jp, [profile.id], NOW, runner=runner)
    assert [c[0] for c in runner.calls] == [a.id] and jp.get_event(eb).state == "queued" and jp.get_grant(gb).state == "offered"
    assert "limite quotidienne" in jp.get_event(eb).message
    await run_batch(jp, [profile.id], NOW + timedelta(hours=1), runner=runner)
    assert len(runner.calls) == 1  # still the same Paris day
    await run_batch(jp, [profile.id], NOW + timedelta(days=1), runner=runner)
    assert [c[0] for c in runner.calls] == [a.id, b.id] and jp.get_event(eb).state == "done"


async def test_send_every_n_minutes(jp, profile, master, tmp_path):
    _, a = new_app(profile, master, tmp_path, 1, approve=True)
    _, b = new_app(profile, master, tmp_path, 2, approve=True)
    queue_intent(jp, profile, a)
    runner = Runner()
    await run_batch(jp, [profile.id], NOW, runner=runner)
    eb, _ = queue_intent(jp, profile, b)
    rep = await run_batch(jp, [profile.id], NOW + timedelta(minutes=14), runner=runner)
    assert rep.skipped == {profile.id: "lot récent"} and len(runner.calls) == 1 and jp.get_event(eb).state == "queued"
    await run_batch(jp, [profile.id], NOW + timedelta(minutes=16), runner=runner)
    assert len(runner.calls) == 2 and jp.get_event(eb).state == "done"


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
    runner = Runner()
    await run_batch(jp, [profile.id], NOW, runner=runner)
    assert not runner.calls and jp.get_event(eid).state == ("queued" if how in {"held", "received"} else "cancelled") and runs_of(app) == []


async def test_a_daily_run_in_progress_blocks_the_batch(jp, profile, master, tmp_path, monkeypatch):
    from jobhunt import schedule

    _, app = new_app(profile, master, tmp_path, 1, approve=True)
    eid, _ = queue_intent(jp, profile, app)
    monkeypatch.setattr(schedule, "is_running", lambda **kw: True)
    runner = Runner()
    await run_batch(jp, [profile.id], NOW, runner=runner)
    assert not runner.calls and jp.get_event(eid).state == "queued"


async def test_pause_during_the_batch_stops_the_next_intent(jp, profile, master, tmp_path):
    _, a = new_app(profile, master, tmp_path, 1, approve=True, score=90)
    _, b = new_app(profile, master, tmp_path, 2, approve=True, score=80)
    queue_intent(jp, profile, a)
    eb, _ = queue_intent(jp, profile, b)

    async def runner(app_id, **kw):
        jp.set_paused(True)
        return SimpleNamespace(id=1, status="submitted", message="", submit_clicked_at=None, nav_clicked_at=None, result=None)

    await run_batch(jp, [profile.id], NOW, runner=runner)
    assert jp.get_event(eb).state == "cancelled"


async def test_disable_after_collection_does_not_start_or_charge_send(jp, profile, master, tmp_path, monkeypatch):
    _, app = new_app(profile, master, tmp_path, 1, approve=True)
    event, grant = queue_intent(jp, profile, app)
    real_claim = jp.claim_send

    def race(*args, **kwargs):
        jp.save_profile(profile.id, enabled=False, control_time=NOW)
        return real_claim(*args, **kwargs)

    monkeypatch.setattr(jp, "claim_send", race)
    runner = Runner()
    await run_batch(jp, [profile.id], NOW, runner=runner)
    assert not runner.calls and jp.sends_today(profile.id, NOW) == 0
    assert jp.get_profile(profile.id).last_send_batch_at is None
    assert jp.get_event(event).state == "cancelled" and jp.get_grant(grant).state == "void"


async def test_a_running_intent_is_interrupted_and_never_reexecuted(jp, profile, master, tmp_path):
    _, app = new_app(profile, master, tmp_path, 1, approve=True)
    eid, g = queue_intent(jp, profile, app)
    assert jp.claim_intent(profile.id, eid, g, NOW, 60) == "claimed"  # claimed, then the process died
    assert jp.interrupted_sweep(profile.id, NOW) == 1
    runner = Runner()
    rep = await run_batch(jp, [profile.id], NOW + timedelta(hours=1), runner=runner)
    assert not runner.calls and not rep.did_anything
    ev = jp.get_event(eid)
    assert ev.state == "interrupted" and ev.message == "Résultat inconnu : vérifiez avant de réessayer."


async def test_a_draft_is_approved_for_the_bound_revision_only(jp, profile, master, tmp_path):
    _, draft = new_app(profile, master, tmp_path, 1)
    eid, _ = queue_intent(jp, profile, draft)
    seen = {}

    async def runner(app_id, **kw):
        a = cvstore.get_application(app_id)
        seen.update(status=a.status, approved_rev=a.approved_rev, by=a.approved_by, rev=kw["expected_rev"])
        return SimpleNamespace(id=1, status="submitted", message="", submit_clicked_at=None, nav_clicked_at=None, result=None)

    await run_batch(jp, [profile.id], NOW, runner=runner)
    assert seen == {"status": "approved", "approved_rev": draft.current_rev, "by": "user", "rev": draft.current_rev}


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
    runner = Runner()
    await run_batch(jp, [profile.id], NOW, runner=runner)
    assert jp.get_event(ea).state == "refused" and jp.get_event(ea).message == APPROVAL_REFUSED and jp.get_grant(ga).state == "used"
    assert [c[0] for c in runner.calls] == [b.id] and jp.get_event(eb).state == "done"


async def test_other_cv_errors_use_fixed_message(jp, profile, master, tmp_path, monkeypatch):
    _, a = new_app(profile, master, tmp_path, 1)
    ea, _ = queue_intent(jp, profile, a)
    monkeypatch.setattr(cvs, "approve", lambda *x, **k: (_ for _ in ()).throw(CVError("Version périmée")))
    await run_batch(jp, [profile.id], NOW, runner=Runner())
    assert jp.get_event(ea).state == "refused" and jp.get_event(ea).message == APPROVAL_REFUSED


@pytest.mark.parametrize("status,state,message", [
    ("needs_input", "failed", "En pause : une question attend votre réponse."),
    ("needs_email", "failed", "En pause : un e-mail de confirmation est à valider."),
    ("needs_human", "failed", "En pause : un CAPTCHA doit être résolu sur le Mac."),
    ("blocked", "failed", "Envoi impossible."),
    ("failed", "failed", "Envoi impossible."),
])
async def test_run_outcomes_map_to_french_states(jp, profile, master, tmp_path, status, state, message):
    _, app = new_app(profile, master, tmp_path, 1, approve=True)
    eid, _ = queue_intent(jp, profile, app)
    await run_batch(jp, [profile.id], NOW, runner=Runner(status, message="Le lien a changé"))
    ev = jp.get_event(eid)
    assert (ev.state, ev.message) == (state, message)


async def test_failed_run_with_a_recorded_click_is_to_check(jp, profile, master, tmp_path):
    _, app = new_app(profile, master, tmp_path, 1, approve=True)
    eid, _ = queue_intent(jp, profile, app)
    await run_batch(jp, [profile.id], NOW, runner=Runner("failed", message="délai", clicked=NOW))
    assert jp.get_event(eid).message == "Envoi non confirmé : à vérifier sur le site de l'entreprise."


async def test_exceptions_are_contained_and_the_batch_continues(jp, profile, master, tmp_path):
    _, a = new_app(profile, master, tmp_path, 1, approve=True, score=90)
    _, b = new_app(profile, master, tmp_path, 2, approve=True, score=80)
    _, c = new_app(profile, master, tmp_path, 3, approve=True, score=70)
    ea, _ = queue_intent(jp, profile, a)
    eb, _ = queue_intent(jp, profile, b)
    ec, _ = queue_intent(jp, profile, c)
    n = []

    async def runner(app_id, **kw):
        n.append(app_id)
        if app_id == a.id:
            raise RuntimeError("page content SECRET")
        if app_id == b.id:
            raise ApplyError("Un envoi est déjà en cours pour cette candidature")
        return SimpleNamespace(id=1, status="submitted", message="", submit_clicked_at=None, nav_clicked_at=None, result=None)

    await run_batch(jp, [profile.id], NOW, runner=runner)
    assert (jp.get_event(ea).state, jp.get_event(ea).message) == ("failed", "Erreur technique.")
    assert jp.get_event(eb).state == "failed" and jp.get_event(eb).message == "Envoi impossible."
    assert jp.get_event(ec).state == "done" and len(n) == 3 and "SECRET" not in jp.get_event(ea).message


async def test_exception_after_recorded_send_uses_recorded_outcome(jp, profile, master, tmp_path):
    job, app = new_app(profile, master, tmp_path, 1, approve=True)
    event_id, _ = queue_intent(jp, profile, app)

    async def runner(*args, **kwargs):
        await service.run(*args, **kwargs)
        raise RuntimeError("Private browser data")

    await run_batch(jp, [profile.id], NOW, runner=runner)
    event = jp.get_event(event_id)
    assert (event.state, event.message, event.run_id) == ("done", "Envoyée.", apply_store.sent_run(app.id).id)


async def test_no_new_run_after_the_time_limit(jp, profile, master, tmp_path, monkeypatch):
    _, a = new_app(profile, master, tmp_path, 1, approve=True, score=90)
    _, b = new_app(profile, master, tmp_path, 2, approve=True, score=80)
    queue_intent(jp, profile, a)
    eb, _ = queue_intent(jp, profile, b)
    clock = iter([0.0, 0.0, 21 * 60.0, 21 * 60.0, 21 * 60.0])
    monkeypatch.setattr(executor, "_monotonic", lambda: next(clock))
    runner = Runner()
    rep = await run_batch(jp, [profile.id], NOW, runner=runner)
    assert len(runner.calls) == 1 and rep.stopped_for_time and jp.get_event(eb).state == "queued"


async def test_two_profiles_in_one_batch(jp, profile, master, tmp_path):
    other = db.save_profile(profile.model_copy(update={"id": None, "name": "Théo"}))
    jp.save_profile(other.id, profile_key="d" * 32, stream_id=STREAM, enabled=True)
    from cv_fakes import save_confirmed

    m2 = save_confirmed(other.id)
    _, a = new_app(profile, master, tmp_path, 1, approve=True)
    _, b = new_app(other, m2, tmp_path, 2, approve=True)
    ea, _ = queue_intent(jp, profile, a)
    eb, _ = queue_intent(jp, other, b)
    runner = Runner()
    rep = await run_batch(jp, [profile.id, other.id], NOW, runner=runner)
    assert {c[0] for c in runner.calls} == {a.id, b.id} and rep.profiles() == {profile.id, other.id}


# ------------------------------------------------------------------ automatic resume
def paused_on(jp, profile, master, tmp_path, n=1, labels=("Ville ?",)):
    _, app = new_app(profile, master, tmp_path, n, approve=True)
    run_ = make_run(app, tmp_path, status="needs_input", questions=[Question(label=l) for l in labels])
    return app, run_


async def test_auto_resume_after_a_portal_answer(jp, profile, master, tmp_path):
    app, paused = paused_on(jp, profile, master, tmp_path)
    unrelated, _ = paused_on(jp, profile, master, tmp_path, 2, labels=("Autre question ?",))
    facts.save_custom_answers(profile.id, [("Autre question ?", "Oui")])
    record_question_answer(jp, profile.id, "Ville ?", "Lyon")
    runner = Runner()
    rep = await run_batch(jp, [profile.id], NOW, runner=runner)
    assert [c[0] for c in runner.calls] == [app.id] and runner.calls[0][1]["_lock_held"] is True
    assert runner.calls[0][1]["mode"] == "submit" and "attended" not in runner.calls[0][1]
    assert [(o.event_id, o.app_id, o.state) for o in rep.outcomes] == [(None, app.id, "done")]
    assert jp.sends_today(profile.id, NOW) == 1  # counted in the budget
    assert not jp.resume_candidates(profile.id) and len(runs_of(unrelated)) == 1


def record_question_answer(journal, profile_id, question, answer):
    seq = journal.get_profile(profile_id).cursor_seq + 1
    event_id = f"{profile_id:08x}{seq:024x}"
    journal.insert_page(profile_id, STREAM, [NewEvent(seq=seq, event_id=event_id, kind="question_answer",
                                payload={"qid": facts.custom_id(question), "answer": answer}, at=NOW)], NOW)
    apply_pending(journal, profile_id, NOW)
    assert journal.get_event(event_id).state == "applied"


async def test_auto_resume_conditions(jp, profile, master, tmp_path, monkeypatch):
    app, _ = paused_on(jp, profile, master, tmp_path, 1, labels=("Ville ?", "Permis B ?"))
    record_question_answer(jp, profile.id, "Ville ?", "Lyon")
    runner = Runner()
    await run_batch(jp, [profile.id], NOW, runner=runner)
    assert not runner.calls
    facts.save_custom_answers(profile.id, [("Permis B ?", "Oui")])
    monkeypatch.setenv("JOBHUNT_PORTAL_AUTO_RESUME", "0")
    await run_batch(jp, [profile.id], NOW, runner=runner)
    assert not runner.calls
    monkeypatch.delenv("JOBHUNT_PORTAL_AUTO_RESUME")
    await run_batch(jp, [profile.id], NOW, runner=runner)
    assert [c[0] for c in runner.calls] == [app.id]


async def test_auto_resume_skips_dry_runs_and_respects_the_budget(jp, profile, master, tmp_path, monkeypatch):
    _, dry = new_app(profile, master, tmp_path, 1, approve=True)
    make_run(dry, tmp_path, mode="dry_run", status="needs_input", questions=[Question(label="Ville ?")])
    a, _ = paused_on(jp, profile, master, tmp_path, 2)
    record_question_answer(jp, profile.id, "Ville ?", "Lyon")
    monkeypatch.setenv("JOBHUNT_PORTAL_MAX_SENDS_PER_DAY", "0")
    runner = Runner()
    await run_batch(jp, [profile.id], NOW, runner=runner)
    assert not runner.calls
    monkeypatch.setenv("JOBHUNT_PORTAL_MAX_SENDS_PER_DAY", "5")
    await run_batch(jp, [profile.id], NOW, runner=runner)
    assert [c[0] for c in runner.calls] == [a.id]  # the dry run was never resumed


async def test_auto_resume_does_not_duplicate_a_queued_resume_intent(jp, profile, master, tmp_path):
    app, paused = paused_on(jp, profile, master, tmp_path)
    record_question_answer(jp, profile.id, "Ville ?", "Lyon")
    queue_intent(jp, profile, app, "resume", run_id=paused.id, mode="submit")
    runner = Runner()
    await run_batch(jp, [profile.id], NOW, runner=runner)
    assert len(runner.calls) == 1


# ------------------------------------------------------------------ the real service.run with the engine stubbed
async def test_real_service_run_reaches_submitted(jp, profile, master, tmp_path, fixture_ats):
    job, draft = new_app(profile, master, tmp_path, 1)
    eid, g = queue_intent(jp, profile, draft)
    rep = await run_batch(jp, [profile.id], NOW)
    ev = jp.get_event(eid)
    assert ev.state == "done" and ev.run_id and len(fixture_ats) == 1
    assert fixture_ats[0].request.content == cvs.safe_pdf(draft).read_bytes()
    a = cvstore.get_application(draft.id)
    assert a.status == "submitted" and a.approved_by == "user" and db.get_job(job.id).status == "applied"
    run_ = apply_store.get_run(ev.run_id)
    assert run_.mode == "submit" and run_.trigger == "user" and run_.status == "submitted"
    assert rep.outcomes[0].state == "done"


async def test_real_service_run_binding_is_enforced_by_the_engine_too(jp, profile, master, tmp_path, monkeypatch):
    _, app = new_app(profile, master, tmp_path, 1, approve=True)
    eid, _ = queue_intent(jp, profile, app)
    calls = stub_engine(monkeypatch)
    monkeypatch.setattr(service, "BrowserPool", lambda headless=None: FakePool())
    real_run = service.run

    async def racing(app_id, **kw):  # the job's link changes between our check and the engine's own check
        with db.connect() as c:
            c.execute("UPDATE jobs SET origin_url=? WHERE id=?", ("https://careers.exemple.test/jobs/other", app.job_id))
        return await real_run(app_id, **kw)

    await run_batch(jp, [profile.id], NOW, runner=racing)
    ev = jp.get_event(eid)
    assert not calls and ev.state == "failed" and "lien de l'offre a changé" in ev.message
