"""portal/journal.py: migrations, atomic pages, claims, sweeps, results."""

from __future__ import annotations

import os
import sqlite3
import stat
import threading
from datetime import timedelta

import pytest

from jobhunt.portal import protocol
from jobhunt.portal.journal import Journal, NewEvent, StreamError, paris_day

from portal_fakes import NOW, journal  # noqa: F401


def mk(j: Journal, pid=1, stream="a" * 32, **kw):
    j.save_profile(pid, profile_key="b" * 32, stream_id=stream, enabled=True, **kw)
    return j.get_profile(pid)


def ev(seq, kind="answer", **kw):
    return NewEvent(seq=seq, event_id=f"{seq:032x}", kind=kind, payload={"x": seq}, at=NOW, **kw)


def test_migration_and_permissions(tmp_path):
    p = tmp_path / "d" / "portal.sqlite3"
    j = Journal(p)
    assert j.schema_version() == 7
    assert stat.S_IMODE(p.stat().st_mode) == 0o600 and stat.S_IMODE(p.parent.stat().st_mode) == 0o700
    Journal(p)  # opening again does not migrate twice or fail
    assert j.schema_version() == 7
    c = sqlite3.connect(p)
    assert c.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    c.execute("PRAGMA user_version=99")
    c.close()
    with pytest.raises(Exception):
        Journal(p)


def test_secret_is_stable_and_random(journal):
    s = journal.local_secret()
    assert len(s) == 32 and journal.local_secret() == s
    assert Journal(journal.path).local_secret() == s


def test_approval_migration_preserves_confirmed_ack(tmp_path):
    from jobhunt.portal.journal import _MIGRATIONS

    path = tmp_path / "journal" / "portal.sqlite3"
    path.parent.mkdir()
    with sqlite3.connect(path) as connection:
        connection.executescript(_MIGRATIONS[1] + _MIGRATIONS[2])
        connection.execute("PRAGMA user_version=2")
        connection.execute("INSERT INTO profiles(profile_id,profile_key,confirmed_ack) VALUES(1,'key',7)")
    assert Journal(path).get_profile(1).confirmed_ack == 7


def test_page_and_cursor_are_one_transaction(journal):
    mk(journal)
    assert journal.insert_page(1, "a" * 32, [ev(1), ev(2)], NOW) == 2
    assert journal.get_profile(1).cursor_seq == 2
    assert [e.seq for e in journal.events(1)] == [1, 2] and journal.events(1)[0].state == "received"
    with pytest.raises(StreamError):  # a gap: nothing written, cursor unchanged
        journal.insert_page(1, "a" * 32, [ev(3), ev(5)], NOW)
    assert journal.get_profile(1).cursor_seq == 2 and len(journal.events(1)) == 2


def test_wrong_stream_and_duplicate_id_refused(journal):
    mk(journal)
    with pytest.raises(StreamError):
        journal.insert_page(1, "c" * 32, [ev(1)], NOW)
    journal.insert_page(1, "a" * 32, [ev(1)], NOW)
    dup = ev(2)
    dup.event_id = ev(1).event_id
    with pytest.raises(StreamError):
        journal.insert_page(1, "a" * 32, [dup], NOW)
    assert journal.get_profile(1).cursor_seq == 1
    with pytest.raises(StreamError):  # replaying an already journaled seq
        journal.insert_page(1, "a" * 32, [ev(1)], NOW)


def queued_intent(j, grant_id, seq=1):
    event = ev(seq, "intent", state="queued")
    event.payload = {"grant": grant_id}
    j.insert_page(1, "a" * 32, [event], NOW)
    return event.event_id


def test_claim_is_atomic_and_one_shot(journal):
    p = mk(journal)
    gid = journal.find_or_create_grant(p, "send", NOW, app_id=7, rev=1, pdf_sha256="x", origin_sha="y")
    eid = queued_intent(journal, gid)
    assert journal.claim_intent(1, eid, gid, NOW, 60) == "claimed"
    assert journal.get_event(eid).state == "running" and journal.get_grant(gid).state == "used"
    assert journal.sends_today(1, NOW) == 1
    assert journal.claim_intent(1, eid, gid, NOW, 60) == "not_queued"
    assert journal.sends_today(1, NOW) == 1


@pytest.mark.parametrize("change", ["mode", "epoch", "disabled", "stream", "pause"])
def test_invalidation_failure_rolls_back_control_change(journal, change):
    before = mk(journal)
    grant_id = journal.find_or_create_grant(before, "send", NOW, app_id=7, rev=1)
    event_id = queued_intent(journal, grant_id)
    with sqlite3.connect(journal.path) as connection:
        connection.execute("CREATE TRIGGER fail_event_update BEFORE UPDATE ON events BEGIN SELECT RAISE(ABORT,'test'); END")
    fields = {"mode": {"review_mode": True}, "epoch": {"epoch": 1}, "disabled": {"enabled": False},
              "stream": {"stream_id": "c" * 32}}
    with pytest.raises(sqlite3.IntegrityError, match="test"):
        if change == "pause":
            journal.set_paused(True, NOW)
        else:
            journal.save_profile(1, control_time=NOW, **fields[change])
    assert journal.get_profile(1).review_mode == before.review_mode and not journal.paused
    assert journal.get_profile(1).enabled and journal.get_profile(1).stream_id == before.stream_id
    assert journal.get_profile(1).epoch == before.epoch
    assert journal.get_grant(grant_id).state == "offered" and journal.get_event(event_id).state == "queued"


def test_claim_requires_recorded_review_and_stamps_claim_clock(journal):
    profile = mk(journal, review_mode=True)
    grant_id = journal.find_or_create_grant(profile, "send", NOW, app_id=7, rev=1)
    event_id = queued_intent(journal, grant_id)
    wrong_id = journal.find_or_create_grant(profile, "send", NOW, app_id=8, rev=1)
    claim_time = NOW + timedelta(days=1)
    assert journal.claim_intent(1, event_id, wrong_id, NOW, 1) == "not_queued"
    assert journal.claim_intent(1, event_id, grant_id, NOW, 1) == "controls"
    assert journal.get_profile(1).last_send_batch_at is None
    journal.update_event(event_id, NOW, approved=True)
    assert journal.claim_intent(1, event_id, grant_id, lambda: claim_time, 1) == "claimed"
    assert journal.get_event(event_id).updated_at == journal.get_profile(1).last_send_batch_at == claim_time
    assert journal.sends_today(1, NOW) == 0 and journal.sends_today(1, claim_time) == 1


def test_two_threads_claim_one_grant_one_wins(journal):
    p = mk(journal)
    gid = journal.find_or_create_grant(p, "send", NOW, app_id=7, rev=1)
    for s in (1, 2):
        queued_intent(journal, gid, s)
    out = []
    barrier = threading.Barrier(2)

    def go(seq):
        barrier.wait()
        out.append(journal.claim_intent(1, ev(seq).event_id, gid, NOW, 60))

    ts = [threading.Thread(target=go, args=(s,)) for s in (1, 2)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sorted(out) == ["claimed", "no_grant"] and journal.sends_today(1, NOW) == 1


def test_claim_refused_for_expired_void_foreign_grant_and_budget(journal):
    p = mk(journal)
    mk(journal, 2, stream="d" * 32)
    gid = journal.find_or_create_grant(p, "send", NOW, app_id=7, rev=1)
    eid = queued_intent(journal, gid)
    assert journal.claim_intent(1, eid, gid, NOW + timedelta(days=8), 60) == "no_grant"
    assert journal.claim_intent(1, eid, gid, NOW, 0) == "budget"
    assert journal.get_event(eid).state == "queued" and journal.get_grant(gid).state == "offered"
    journal.save_profile(1, epoch=1)  # a new epoch invalidates the grant
    assert journal.claim_intent(1, eid, gid, NOW, 60) == "not_queued"


def test_grant_reuse_expiry_and_no_revival(journal):
    p = mk(journal)
    a = journal.find_or_create_grant(p, "send", NOW, app_id=1, rev=1, pdf_sha256="p", origin_sha="o")
    assert journal.find_or_create_grant(p, "send", NOW + timedelta(days=1), app_id=1, rev=1, pdf_sha256="p", origin_sha="o") == a
    assert journal.find_or_create_grant(p, "send", NOW, app_id=1, rev=2, pdf_sha256="p", origin_sha="o") != a
    g = journal.get_grant(a)
    assert g.expires_at - g.issued_at == timedelta(days=7)
    b = journal.find_or_create_grant(p, "send", NOW + timedelta(days=8), app_id=1, rev=1, pdf_sha256="p", origin_sha="o")
    assert b != a  # expired: a new one, the old one is not extended
    assert journal.get_grant(a).expires_at == g.expires_at
    journal.void_grants(1, NOW)
    assert journal.find_or_create_grant(p, "send", NOW, app_id=1, rev=1, pdf_sha256="p", origin_sha="o") not in (a, b)


def test_void_grants_cancels_queued_but_not_awaiting_review(journal):
    p = mk(journal)
    gid = journal.find_or_create_grant(p, "send", NOW, app_id=1)
    journal.insert_page(1, "a" * 32, [ev(1, "intent"), ev(2, "intent")], NOW)
    journal.update_event(ev(1).event_id, NOW, state="queued")
    journal.update_event(ev(2).event_id, NOW, state="awaiting_review")
    journal.void_grants(1, NOW)
    assert journal.get_event(ev(1).event_id).state == "cancelled" and journal.get_event(ev(2).event_id).state == "awaiting_review"
    assert journal.get_grant(gid).state == "void"


def test_interrupted_sweep_and_results(journal):
    mk(journal)
    journal.insert_page(1, "a" * 32, [ev(1, "intent"), ev(2), ev(3)], NOW)
    journal.update_event(ev(1).event_id, NOW, state="running")
    journal.update_event(ev(2).event_id, NOW, state="applied", message="Enregistrée.", label="Prénom")
    assert journal.interrupted_sweep(1, NOW + timedelta(minutes=1)) == 1
    assert journal.get_event(ev(1).event_id).state == "interrupted"
    res = journal.latest_results(1)
    assert {r.status for r in res} == {"interrupted", "applied", "received"}
    assert all(isinstance(r, protocol.EventResult) for r in res)
    assert journal.latest_results(1, 1)[0].status == "interrupted"
    assert journal.interrupted_sweep(1, NOW) == 0


def test_versions_keep_until_value_changes(journal):
    v1 = journal.version_for(1, "a:city", "h1")
    assert journal.version_for(1, "a:city", "h1") == v1
    v2 = journal.version_for(1, "a:city", "h2")
    assert v2 != v1 and journal.version_for(1, "a:city", "h2") == v2
    assert journal.version_for(2, "a:city", "h2") != v2


def test_reset_stream_marks_open_events_lost(journal):
    p = mk(journal)
    gid = journal.find_or_create_grant(p, "send", NOW, app_id=1)
    journal.insert_page(1, "a" * 32, [ev(1), ev(2)], NOW)
    journal.update_event(ev(2).event_id, NOW, state="applied")
    journal.reset_stream(1, "e" * 32, NOW)
    assert journal.get_event(ev(1).event_id).state == "lost" and journal.get_event(ev(2).event_id).state == "applied"
    assert journal.get_grant(gid).state == "void"
    assert journal.get_profile(1).cursor_seq == 0 and journal.get_profile(1).stream_id == "e" * 32


def test_budget_is_per_paris_day(journal):
    mk(journal)
    assert journal.take_budget(1, NOW, 2) and journal.take_budget(1, NOW, 2) and not journal.take_budget(1, NOW, 2)
    assert journal.take_budget(1, NOW + timedelta(days=1), 2)
    assert paris_day(NOW) == "2026-11-18"


def test_ref_is_stable(journal):
    assert journal.ref_for(1, "sent", 5) == journal.ref_for(1, "sent", 5) != journal.ref_for(1, "sent", 6)
