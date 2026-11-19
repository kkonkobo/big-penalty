"""portal/sync.py against the in-memory fake server: full passes, offline, crashes, 409, streams, documents."""

from __future__ import annotations

from datetime import timedelta

import pytest

from jobhunt import db
from jobhunt.apply import accounts, facts, service
from jobhunt.apply import store as apply_store
from jobhunt.apply.types import Question, RunResult
from jobhunt.cv import store as cvstore
from jobhunt.portal import apply_events, executor, manage, protocol, sync
from jobhunt.portal.journal import pass_lock
from jobhunt.portal.snapshot import stored_answer

from apply_service_fakes import FakePool, stub_engine
from portal_fakes import NOW, Portal, journal, make_run, master, new_app, paired, portal, server  # noqa: F401


@pytest.fixture(autouse=True)
def no_keychain(monkeypatch):
    monkeypatch.setattr(accounts, "create_password", lambda host, email: "Pw-Test-1234")
    monkeypatch.setattr(accounts, "get_password", lambda host, email, **kw: None)


def go(p: Portal, now=NOW, execute=False, ids=None):
    return sync.sync_all(ids, execute, journal=p.journal, client=p.client, now=now)


def srv(p: Portal):
    return p.fake.profiles[p.key]


def test_first_pass_pushes_a_snapshot(portal):
    (rep,) = go(portal)
    assert rep.ok and rep.pushed and srv(portal).snapshot_rev == 1 and srv(portal).snapshot.display_name == "Léa"
    row = portal.journal.get_profile(portal.profile_id)
    assert row.snapshot_rev == 1 and row.last_sync_at == NOW and row.last_error is None


def test_full_round_trip(portal):
    go(portal)
    ev = portal.fake.edit_answer(portal.key, "city", "Lyon")
    (rep,) = go(portal, NOW + timedelta(minutes=5))
    assert rep.ok and rep.pulled == 1 and stored_answer(portal.profile_id, "city") == "Lyon"
    s = srv(portal)
    assert s.snapshot.answers["city"].value == "Lyon" and s.snapshot_rev == 2 and s.acked == 1 and s.events == []  # acked events are gone
    assert [(r.event_id, r.status) for r in s.results] == [(ev, "applied")]
    assert s.snapshot.answers["city"].ver == portal.journal.get_event(ev).ver_after
    go(portal, NOW + timedelta(minutes=10))
    assert srv(portal).snapshot_rev == 3 and portal.journal.get_profile(portal.profile_id).cursor_seq == 1


def test_edit_chain_and_conflict_are_reported_in_results(portal):
    go(portal)
    base = srv(portal).snapshot.answers["city"].ver
    e1 = portal.fake.edit_answer(portal.key, "city", "Lyon", base_ver=base)
    e2 = portal.fake.edit_answer(portal.key, "city", "Nice", after=e1, base_ver=base)
    e3 = portal.fake.edit_answer(portal.key, "postal_code", "69000")
    facts.save_answers(portal.profile_id, {"postal_code": "75000"})
    go(portal)
    st = {r.event_id: r.status for r in srv(portal).results}
    assert st == {e1: "applied", e2: "applied", e3: "conflict"} and stored_answer(portal.profile_id, "city") == "Nice"
    assert srv(portal).snapshot.answers["postal_code"].value == "75000"  # he now sees the Mac value


def test_offline_server_is_a_noop(portal):
    go(portal)
    before = portal.journal.get_profile(portal.profile_id)
    portal.fake.offline = True
    (rep,) = go(portal, NOW + timedelta(minutes=5))
    after = portal.journal.get_profile(portal.profile_id)
    assert not rep.ok and "injoignable" in rep.error and after.last_error and after.last_sync_at == before.last_sync_at
    assert (after.cursor_seq, after.snapshot_rev) == (before.cursor_seq, before.snapshot_rev)
    portal.fake.offline = False
    (rep,) = go(portal, NOW + timedelta(minutes=10))
    assert rep.ok and portal.journal.get_profile(portal.profile_id).last_error is None


def test_offline_pass_applies_nothing_already_journaled(portal):
    go(portal)
    e = portal.fake.edit_answer(portal.key, "city", "Lyon")
    portal.fake.offline = True
    go(portal)
    assert portal.journal.get_event(e) is None  # never pulled
    assert stored_answer(portal.profile_id, "city") == ""


def test_kill_between_journaling_and_applying_then_finish_once(portal, monkeypatch):
    go(portal)
    e = portal.fake.edit_answer(portal.key, "city", "Lyon")
    real = apply_events.apply_pending

    def die(*a, **k):
        raise KeyboardInterrupt

    monkeypatch.setattr(apply_events, "apply_pending", die)
    with pytest.raises(KeyboardInterrupt):
        go(portal)
    j = portal.journal
    assert j.get_profile(portal.profile_id).cursor_seq == 1 and j.get_event(e).state == "received"
    assert stored_answer(portal.profile_id, "city") == "" and len(srv(portal).events) == 1  # custody, not yet acked
    monkeypatch.setattr(apply_events, "apply_pending", real)
    writes = []
    real_save = facts.save_answers
    monkeypatch.setattr(facts, "save_answers", lambda pid, v: (writes.append(v), real_save(pid, v))[1])
    (rep,) = go(portal)
    assert rep.ok and rep.pulled == 0 and j.get_event(e).state == "applied" and len(writes) == 1
    assert srv(portal).events == [] and not pass_lock_busy()
    go(portal)
    assert len(writes) == 1


def pass_lock_busy():
    with pass_lock("sync.lock") as got:
        return not got


def test_kill_after_the_snapshot_was_stored_but_before_it_was_recorded(portal, monkeypatch):
    go(portal)
    real = type(portal.journal).update_profile
    state = {"n": 0}

    def flaky(self, pid, **kw):
        if "snapshot_rev" in kw and state["n"] == 0:
            state["n"] += 1
            raise KeyboardInterrupt  # killed right after the server answered 200
        return real(self, pid, **kw)

    monkeypatch.setattr(type(portal.journal), "update_profile", flaky)
    with pytest.raises(KeyboardInterrupt):
        go(portal)
    assert portal.journal.get_profile(portal.profile_id).snapshot_rev == 1 and srv(portal).snapshot_rev == 2
    (rep,) = go(portal, NOW + timedelta(minutes=5))  # 409 rev: adopt current + 1
    assert rep.ok and srv(portal).snapshot_rev == 3 and portal.journal.get_profile(portal.profile_id).snapshot_rev == 3


def test_409_rev_is_adopted_once(portal):
    srv(portal).snapshot_rev = 10
    (rep,) = go(portal)
    assert rep.ok and srv(portal).snapshot_rev == 11
    n = portal.fake.snapshot_pushes
    srv(portal).snapshot_rev = 40

    def always_409(req):
        import httpx
        if req.method == "PUT" and req.url.path.endswith("/snapshot"):
            return httpx.Response(409, json={"error": "rev", "current": 99})

    portal.fake.override += [always_409, always_409]
    (rep,) = go(portal)
    assert not rep.ok and rep.error  # the second 409 is an error of this pass, not an endless loop
    assert portal.fake.snapshot_pushes == n


@pytest.mark.parametrize("method", ["get_inbox", "put_snapshot"])
def test_old_stream_reply_cannot_change_reset_metadata(portal, monkeypatch, method):
    go(portal)
    portal.fake.edit_answer(portal.key, "city", "Lyon")
    go(portal)
    journal = portal.journal
    profile_id = portal.profile_id
    before = journal.get_profile(profile_id)
    assert before.cursor_seq == before.confirmed_ack == 1
    new_stream = protocol.new_id()
    original = getattr(portal.client, method)

    def reset_before_reply(*args, **kwargs):
        response = original(*args, **kwargs)
        journal.update_profile(profile_id, stream_id=new_stream, held=True)
        return response

    monkeypatch.setattr(portal.client, method, reset_before_reply)
    (report,) = go(portal)
    after = journal.get_profile(profile_id)
    assert not report.ok and report.error == sync.STREAM_MSG
    assert after.stream_id == new_stream and after.held
    assert after.cursor_seq == after.confirmed_ack == 0
    assert after.snapshot_rev == before.snapshot_rev


def test_in_flight_inbox_response_cannot_cross_an_epoch_change(portal, monkeypatch):
    assert go(portal)[0].ok
    event_id = portal.fake.edit_answer(portal.key, "city", "Lyon")
    get_inbox = portal.client.get_inbox

    def revoke_before_reply(*args, **kwargs):
        reply = get_inbox(*args, **kwargs)
        manage.revoke(portal.profile_id, journal=portal.journal, client=portal.client)
        return reply

    with monkeypatch.context() as scoped:
        scoped.setattr(portal.client, "get_inbox", revoke_before_reply)
        assert not go(portal)[0].ok
    assert portal.journal.get_event(event_id) is None
    assert go(portal)[0].ok
    assert portal.journal.get_event(event_id).state == "cancelled"
    assert stored_answer(portal.profile_id, "city") == ""


def test_wrong_stream_stops_the_profile_and_applies_nothing(portal):
    go(portal)
    e = portal.fake.edit_answer(portal.key, "city", "Lyon")
    srv(portal).stream_id = protocol.new_id()  # e.g. a server restored from a backup
    (rep,) = go(portal)
    j = portal.journal
    assert not rep.ok and rep.error == sync.STREAM_MSG and j.get_profile(portal.profile_id).last_error == sync.STREAM_MSG
    assert j.events(portal.profile_id) == [] and stored_answer(portal.profile_id, "city") == ""


def test_a_gap_in_the_sequence_fails_closed(portal):
    go(portal)
    portal.fake.edit_answer(portal.key, "city", "Lyon")
    srv(portal).events[0]["seq"] = 5
    srv(portal).last_seq = 5
    (rep,) = go(portal)
    assert not rep.ok and rep.error == sync.STREAM_MSG and portal.journal.get_profile(portal.profile_id).cursor_seq == 0
    assert portal.journal.events(portal.profile_id) == []


def test_server_going_backwards_fails_closed(portal):
    go(portal)
    portal.fake.edit_answer(portal.key, "city", "Lyon")
    go(portal)
    srv(portal).last_seq = 0
    (rep,) = go(portal)
    assert not rep.ok and rep.error == sync.STREAM_MSG


def test_server_ack_going_backwards_fails_closed_even_with_new_events(portal):
    go(portal)
    portal.fake.edit_answer(portal.key, "city", "Lyon")
    go(portal)
    assert portal.journal.get_profile(portal.profile_id).confirmed_ack == 1
    srv(portal).acked = 0
    pending = portal.fake.edit_answer(portal.key, "city", "Paris")
    (rep,) = go(portal)
    assert not rep.ok and rep.error == sync.STREAM_MSG
    assert portal.journal.get_event(pending) is None
    assert stored_answer(portal.profile_id, "city") == "Lyon"


def test_incomplete_second_inbox_page_applies_nothing(portal):
    go(portal)
    first = portal.fake.edit_answer(portal.key, "city", "Lyon")
    srv(portal).last_seq += 1
    (rep,) = go(portal)
    assert not rep.ok and rep.error == sync.STREAM_MSG
    assert portal.journal.get_event(first).state == "received"
    assert srv(portal).acked == 0 and stored_answer(portal.profile_id, "city") == ""


def test_unknown_event_is_not_exported(portal):
    go(portal)
    unknown = portal.fake.edit_answer(portal.key, "city", "Lyon")
    srv(portal).events[0]["kind"] = "unknown_kind"
    (rep,) = go(portal)
    assert rep.ok and portal.journal.get_event(unknown).state == "rejected"
    assert srv(portal).acked == 1 and srv(portal).results == []


def test_resync_starts_a_new_stream(portal):
    go(portal)
    pending = portal.fake.edit_answer(portal.key, "city", "Lyon")
    go(portal, execute=False)  # taken and applied
    queued = portal.fake.add_event(portal.key, "intent", grant="1" * 32)
    old_stream = srv(portal).stream_id
    manage.resync(portal.profile_id, journal=portal.journal, client=portal.client)
    row = portal.journal.get_profile(portal.profile_id)
    assert row.stream_id != old_stream
    assert srv(portal).stream_id == row.stream_id and srv(portal).events == [] and row.cursor_seq == 0
    (rep,) = go(portal)
    assert rep.ok and rep.pulled == 0
    new = portal.fake.edit_answer(portal.key, "postal_code", "69000")
    go(portal)
    assert portal.journal.get_event(new).state == "applied" and portal.journal.get_profile(portal.profile_id).cursor_seq == 1


def test_invalid_event_body_is_rejected_and_the_stream_continues(portal):
    go(portal)
    s = srv(portal)
    s.last_seq += 1
    s.events.append({"seq": 1, "event_id": "a" * 32, "at": NOW.isoformat(), "kind": "answer", "path": "BAD PATH"})
    good = portal.fake.edit_answer(portal.key, "city", "Lyon")
    (rep,) = go(portal)
    assert rep.ok and portal.journal.get_event("a" * 32).state == "rejected" and portal.journal.get_event(good).state == "applied"
    assert {r.status for r in srv(portal).results} == {"rejected", "applied"}


def test_paused_keeps_custody_but_applies_nothing(portal):
    go(portal)
    manage.pause(journal=portal.journal)
    e = portal.fake.edit_answer(portal.key, "city", "Lyon")
    (rep,) = go(portal)
    assert rep.ok and portal.journal.get_event(e).state == "received" and srv(portal).events == [] and srv(portal).snapshot.paused
    assert [(r.event_id, r.status) for r in srv(portal).results] == [(e, "received")]
    manage.resume(journal=portal.journal)
    go(portal)
    assert portal.journal.get_event(e).state == "applied"


def test_server_hold_is_remembered(portal):
    go(portal)
    srv(portal).held = True
    e = portal.fake.edit_answer(portal.key, "city", "Lyon")
    go(portal)
    assert portal.journal.get_profile(portal.profile_id).held and portal.journal.get_event(e).state == "received"
    srv(portal).held = False
    go(portal)
    assert portal.journal.get_event(e).state == "applied"


def test_interrupted_intent_is_swept_at_pass_start(portal, profile, master, tmp_path):
    _, app = new_app(profile, master, tmp_path, 1, approve=True)
    go(portal)
    g = srv(portal).snapshot.awaiting[0].grant
    e = portal.fake.add_event(portal.key, "intent", grant=g)
    go(portal)
    assert portal.journal.get_event(e).state == "queued"
    assert portal.journal.claim_intent(portal.profile_id, e, g, NOW, 60) == "claimed"  # the process died while running
    go(portal, NOW + timedelta(hours=1))
    assert portal.journal.get_event(e).state == "interrupted"
    assert [r.status for r in srv(portal).results if r.event_id == e] == ["interrupted"]


def test_documents_are_uploaded_once(portal, profile, master, tmp_path):
    new_app(profile, master, tmp_path, 1)
    new_app(profile, master, tmp_path, 2)
    (rep,) = go(portal)
    assert rep.uploaded == 2 and len(portal.fake.doc_uploads) == 2 and len(srv(portal).docs) == 2
    (rep,) = go(portal, NOW + timedelta(minutes=5))
    assert rep.uploaded == 0 and len(portal.fake.doc_uploads) == 2
    for a in srv(portal).snapshot.awaiting:
        assert a.doc_id in srv(portal).docs


def test_a_document_that_changed_since_the_snapshot_is_not_uploaded(portal, profile, master, tmp_path, monkeypatch):
    new_app(profile, master, tmp_path, 1)
    from jobhunt.portal import snapshot as snapmod

    real = snapmod.build_snapshot

    def tampering(j, pid, now):
        snap, docs = real(j, pid, now)
        for path in docs.values():
            path.write_bytes(b"%PDF-1.4 changed after the snapshot, longer")
        return snap, docs

    monkeypatch.setattr(sync, "build_snapshot", tampering)
    (rep,) = go(portal)
    assert rep.ok and rep.uploaded == 0 and portal.fake.doc_uploads == []


def test_two_profiles_one_pass(portal, journal, master, tmp_path):
    from cv_fakes import save_confirmed

    other = db.save_profile(db.get_profile(portal.profile_id).model_copy(update={"id": None, "name": "Théo Martin"}))
    save_confirmed(other.id)
    creds = manage.enable(other.id, journal=journal, client=portal.client)
    key2 = journal.get_profile(other.id).profile_key
    assert creds.handle != portal.credentials.handle
    go(portal)
    e1 = portal.fake.edit_answer(portal.key, "city", "Lyon")
    e2 = portal.fake.edit_answer(key2, "city", "Nice")
    reps = go(portal)
    assert [r.ok for r in reps] == [True, True]
    assert stored_answer(portal.profile_id, "city") == "Lyon" and stored_answer(other.id, "city") == "Nice"
    assert portal.fake.profiles[key2].snapshot.display_name == "Théo"
    only = go(portal, ids=[other.id])
    assert [r.profile_id for r in only] == [other.id]


def test_one_failing_profile_does_not_stop_the_other(portal, journal, monkeypatch):
    other = db.save_profile(db.get_profile(portal.profile_id).model_copy(update={"id": None, "name": "Théo"}))
    from cv_fakes import save_confirmed

    save_confirmed(other.id)
    manage.enable(other.id, journal=journal, client=portal.client)
    key2 = journal.get_profile(other.id).profile_key
    go(portal)
    portal.fake.profiles[key2].stream_id = protocol.new_id()
    portal.fake.edit_answer(portal.key, "city", "Lyon")
    r1, r2 = go(portal)
    assert r1.ok and not r2.ok and stored_answer(portal.profile_id, "city") == "Lyon"


def test_busy_lock_reports_and_does_nothing(portal):
    with pass_lock("sync.lock") as got:
        assert got
        (rep,) = go(portal)
    assert rep.profile_id is None and rep.error == "une synchronisation est déjà en cours" and portal.fake.snapshot_pushes == 0


def test_not_paired_reports_a_clear_error(profile, journal):
    journal.save_profile(profile.id, profile_key="a" * 32, stream_id="b" * 32, enabled=True)
    (rep,) = sync.sync_all(journal=journal, now=NOW)
    assert not rep.ok and "appairé" in rep.error


def test_sync_profile_signature(portal):
    rep = sync.sync_profile(portal.journal, portal.client, portal.profile_id, NOW, execute=False)
    assert rep.ok and rep.profile_id == portal.profile_id
    assert sync.sync_profile(portal.journal, portal.client, 4242, NOW).message == "profil inconnu"


def test_disabled_profile_is_not_synced(portal):
    manage.disable(portal.profile_id, journal=portal.journal, client=portal.client)
    n = portal.fake.snapshot_pushes
    assert go(portal) == []
    assert portal.fake.snapshot_pushes == n


def test_results_are_capped_and_hold_no_value(portal):
    go(portal)
    for i in range(120):
        portal.fake.edit_answer(portal.key, "city", f"Ville-secrete-{i}", base_ver=srv(portal).snapshot.answers["city"].ver)
    go(portal)
    res = srv(portal).results
    assert len(res) == 100 and all("Ville-secrete" not in r.message and "Ville-secrete" not in r.label for r in res)


# ------------------------------------------------------------------ with sends
@pytest.fixture
def engine(monkeypatch):
    async def hook(kw):
        assert await kw["before_submit"]() == []
        await kw["on_submit_click"]()

    calls = stub_engine(monkeypatch, RunResult(status="submitted", message="Candidature envoyée.", submit_clicked=True), hook=hook)
    monkeypatch.setattr(service, "BrowserPool", lambda headless=None: FakePool())
    return calls


async def test_send_intent_end_to_end(portal, profile, master, tmp_path, engine):
    job, app = new_app(profile, master, tmp_path, 1)
    await sync.async_sync_all(journal=portal.journal, client=portal.client, now=NOW)
    g = srv(portal).snapshot.awaiting[0].grant
    e = portal.fake.add_event(portal.key, "intent", grant=g)
    (rep,) = await sync.async_sync_all(journal=portal.journal, client=portal.client, now=NOW + timedelta(minutes=5))
    assert rep.ok and rep.executed == 1 and len(engine) == 1
    assert [(r.event_id, r.status, r.message) for r in srv(portal).results] == [(e, "done", "Envoyée.")]
    snap = srv(portal).snapshot  # pushed again after the batch
    assert snap.awaiting == [] and [s.company for s in snap.sent] == ["Société 1"] and snap.sent[0].inferred == []
    assert cvstore.get_application(app.id).status == "submitted"
    await sync.async_sync_all(journal=portal.journal, client=portal.client, now=NOW + timedelta(hours=1))
    assert len(engine) == 1  # an event is never executed twice


async def test_no_execute_leaves_the_intent_queued(portal, profile, master, tmp_path, engine):
    new_app(profile, master, tmp_path, 1)
    await sync.async_sync_all(journal=portal.journal, client=portal.client, now=NOW)
    e = portal.fake.add_event(portal.key, "intent", grant=srv(portal).snapshot.awaiting[0].grant)
    await sync.async_sync_all(execute=False, journal=portal.journal, client=portal.client, now=NOW)
    assert portal.journal.get_event(e).state == "queued" and not engine
    assert [r.status for r in srv(portal).results] == ["queued"]


async def test_one_batch_for_all_profiles(portal, journal, profile, master, tmp_path, engine, monkeypatch):
    from cv_fakes import save_confirmed

    other = db.save_profile(profile.model_copy(update={"id": None, "name": "Théo"}))
    m2 = save_confirmed(other.id)
    manage.enable(other.id, journal=journal, client=portal.client)
    key2 = journal.get_profile(other.id).profile_key
    new_app(profile, master, tmp_path, 1)
    new_app(other, m2, tmp_path, 2)
    await sync.async_sync_all(journal=journal, client=portal.client, now=NOW)
    portal.fake.add_event(portal.key, "intent", grant=srv(portal).snapshot.awaiting[0].grant)
    portal.fake.add_event(key2, "intent", grant=portal.fake.profiles[key2].snapshot.awaiting[0].grant)
    batches = []
    real = executor.run_batch

    async def spy(j, ids, now, **kw):
        batches.append(list(ids))
        return await real(j, ids, now, **kw)

    monkeypatch.setattr(executor, "run_batch", spy)
    reps = await sync.async_sync_all(journal=journal, client=portal.client, now=NOW + timedelta(minutes=5))
    assert len(batches) == 1 and sorted(batches[0]) == sorted([profile.id, other.id]) and [r.executed for r in reps] == [1, 1]
    assert len(engine) == 2


async def test_busy_send_lock_leaves_it_queued_and_says_so(portal, profile, master, tmp_path, engine):
    new_app(profile, master, tmp_path, 1)
    await sync.async_sync_all(journal=portal.journal, client=portal.client, now=NOW)
    e = portal.fake.add_event(portal.key, "intent", grant=srv(portal).snapshot.awaiting[0].grant)
    with service.send_lock():
        (rep,) = await sync.async_sync_all(journal=portal.journal, client=portal.client, now=NOW + timedelta(minutes=5))
    assert rep.ok and rep.executed == 0 and "autre envoi" in rep.message and not engine
    assert portal.journal.get_event(e).state == "queued" and apply_store.list_runs(1) == []


async def test_answering_a_question_resumes_the_paused_run(portal, profile, master, tmp_path, engine):
    _, app = new_app(profile, master, tmp_path, 1, approve=True)
    make_run(app, tmp_path, status="needs_input", questions=[Question(label="Ville ?")])
    await sync.async_sync_all(journal=portal.journal, client=portal.client, now=NOW)
    q = srv(portal).snapshot.questions[0]
    assert q.label == "Ville ?" and q.qid == facts.custom_id("Ville ?")
    portal.fake.add_event(portal.key, "question_answer", qid=q.qid, answer="Lyon")
    (rep,) = await sync.async_sync_all(journal=portal.journal, client=portal.client, now=NOW + timedelta(minutes=5))
    assert rep.executed == 1 and len(engine) == 1 and cvstore.get_application(app.id).status == "submitted"
    assert srv(portal).snapshot.questions == [] and [r.status for r in srv(portal).results] == ["applied"]
