"""portal/manage.py: pairing, enabling, rotating, modes, disabling, recovery, review."""

from __future__ import annotations

from datetime import timedelta

import pytest

from jobhunt.apply import facts
from jobhunt.portal import manage, protocol, secrets, sync
from jobhunt.portal.journal import Journal
from jobhunt.portal.manage import ManageError
from jobhunt.portal.snapshot import stored_answer

from portal_fakes import BASE, NOW, PAIRING_CODE, Portal, journal, master, new_app, paired, portal, server  # noqa: F401

ORIGIN = "http://127.0.0.1:8099"


def srv(p):
    return p.fake.profiles[p.key]


def test_pair_stores_pairing_and_a_token_the_server_only_knows_by_hash(journal, server):
    info = manage.pair(BASE, PAIRING_CODE, "Mac de test", "file", journal=journal, client=server.client(None))
    assert (info.origin, info.base_path, info.token_store) == (ORIGIN, "/app", "file") and info.device_id
    token = secrets.load_token(ORIGIN, "file")
    assert token and protocol.sha256_hex(token) in server.devices and token not in str(server.devices)
    assert journal.get_setting("device_id") == info.device_id
    assert token.encode() not in journal.path.read_bytes()
    assert server.requests == [("POST", "/app/api/v2/pair")]


def test_pair_twice_is_refused_until_unpair(journal, server):
    manage.pair(BASE, PAIRING_CODE, "Mac", "file", journal=journal, client=server.client(None))
    with pytest.raises(ManageError, match="déjà appairé"):
        manage.pair(BASE, PAIRING_CODE, "Mac", "file", journal=journal, client=server.client(None))
    manage.unpair(journal=journal)
    assert secrets.load_token(ORIGIN, "file") is None and journal.get_setting("origin") is None


def test_pair_with_a_wrong_code_leaves_nothing_behind(journal, server):
    with pytest.raises(ManageError):
        manage.pair(BASE, "wrong-code", "Mac", "file", journal=journal, client=server.client(None))
    assert journal.get_setting("origin") is None and secrets.load_token(ORIGIN, "file") is None


def test_pair_refuses_plain_http_to_a_remote_host_and_a_missing_keyring(journal, server, monkeypatch):
    with pytest.raises(ManageError):
        manage.pair("http://example.org/app", PAIRING_CODE, "Mac", "file", journal=journal, client=server.client(None))
    monkeypatch.setattr(secrets, "_load_keyring", lambda: None)
    with pytest.raises(ManageError, match="(?i)trousseau"):
        manage.pair(BASE, PAIRING_CODE, "Mac", "keyring", journal=journal, client=server.client(None))
    assert journal.get_setting("origin") is None and not secrets._file().exists()


def test_pair_retry_reuses_the_token_of_an_interrupted_pairing(journal, server):
    c = server.client(None)
    manage.pair(BASE, PAIRING_CODE, "Mac", "file", journal=journal, client=c)
    token = secrets.load_token(ORIGIN, "file")
    journal.delete_settings("origin", "base_path", "device_id", "device_name", "token_store")  # the process died before recording
    manage.pair(BASE, PAIRING_CODE, "Mac", "file", journal=journal, client=c)
    assert secrets.load_token(ORIGIN, "file") == token and len(server.devices) == 1


def test_enable_returns_credentials_once_and_stores_no_passcode(portal):
    c = portal.credentials
    assert c.address == f"{ORIGIN}/app/" and len(c.handle) == protocol.HANDLE_LEN and len(c.passcode) == protocol.PASSCODE_LEN
    assert set(c.passcode) <= set(protocol.CODE_ALPHABET)
    s, row = srv(portal), portal.journal.get_profile(portal.profile_id)
    assert s.handle_sha == protocol.handle_digest(c.handle) and row.handle == c.handle and row.enabled and not row.review_mode
    assert protocol.passcode_hash(c.passcode, bytes.fromhex(s.passcode["salt"])).hex() == s.passcode["hash"]
    assert (s.config_rev, s.stream_id, s.epoch) == (row.config_rev, row.stream_id, row.epoch) and row.stream_id
    assert c.passcode.encode() not in portal.journal.path.read_bytes() and c.passcode not in repr(row)
    assert len(c.handle_shown.split("-")) == 2 and len(c.passcode_shown.split("-")) == 3


def test_enable_with_a_chosen_passcode_and_review_mode(profile, journal, paired, server, master):
    with pytest.raises(ManageError):
        manage.enable(profile.id, passcode="short", journal=journal, client=paired)
    with pytest.raises(ManageError):
        manage.enable(profile.id, passcode="a b-c" * 1 + "d", journal=journal, client=paired)  # normalised: too short
    assert server.profiles == {}
    c = manage.enable(profile.id, passcode="Mon Code-Perso 2026", review=True, journal=journal, client=paired)
    s = server.profiles[journal.get_profile(profile.id).profile_key]
    assert protocol.passcode_hash("mon code perso 2026", bytes.fromhex(s.passcode["salt"])).hex() == s.passcode["hash"]
    assert journal.get_profile(profile.id).review_mode and c.passcode == "Mon Code-Perso 2026"


def test_enable_twice_and_unknown_profile(portal):
    with pytest.raises(ManageError, match="déjà activé"):
        manage.enable(portal.profile_id, journal=portal.journal, client=portal.client)
    with pytest.raises(ManageError, match="introuvable"):
        manage.enable(4242, journal=portal.journal, client=portal.client)


def test_enable_requires_pairing(profile, journal):
    with pytest.raises(ManageError, match="pas appairé"):
        manage.enable(profile.id, journal=journal)


def test_enable_adopts_a_newer_server_config_revision(profile, journal, paired, server, master):
    c = manage.enable(profile.id, journal=journal, client=paired)
    key = journal.get_profile(profile.id).profile_key
    manage.disable(profile.id, journal=journal, client=paired)
    journal.save_profile(profile.id, config_rev=0)  # a lost journal row: the server is ahead
    c2 = manage.enable(profile.id, journal=journal, client=paired)
    row = journal.get_profile(profile.id)
    assert row.config_rev == server.profiles[key].config_rev and c2.handle != c.handle and row.epoch >= 1


def test_enable_again_after_disable_starts_a_clean_stream(portal):
    old = portal.journal.get_profile(portal.profile_id)
    sync.sync_all(journal=portal.journal, client=portal.client, now=NOW, execute=False)
    portal.fake.edit_answer(portal.key, "city", "Lyon")
    sync.sync_all(journal=portal.journal, client=portal.client, now=NOW, execute=False)
    manage.disable(portal.profile_id, journal=portal.journal, client=portal.client)
    manage.enable(portal.profile_id, journal=portal.journal, client=portal.client)
    new = portal.journal.get_profile(portal.profile_id)
    assert new.stream_id != old.stream_id and new.cursor_seq == 0 and new.epoch == old.epoch + 1 and new.enabled


def test_enable_retry_reuses_profile_identity_after_lost_response(profile, journal, paired, server, master, monkeypatch):
    from jobhunt.portal.client import PortalOffline

    client = paired
    put = client.put_profile
    staged = []

    def lost_response(key, config):
        row = journal.get_profile(profile.id)
        assert not row.enabled and journal.pending_config(profile.id) == "enable"
        assert (row.profile_key, row.stream_id, row.config_rev) == (key, config.stream_id, config.config_rev)
        staged.append(row)
        put(key, config)
        raise PortalOffline()

    with monkeypatch.context() as patcher:
        patcher.setattr(client, "put_profile", lost_response)
        with pytest.raises(ManageError):
            manage.enable(profile.id, journal=journal, client=client)
    with pytest.raises(ManageError, match="Modification confirmée"):
        manage.enable(profile.id, journal=journal, client=client)
    row = journal.get_profile(profile.id)
    assert row.enabled and not journal.pending_config(profile.id)
    assert (row.profile_key, row.stream_id, row.handle) == (staged[0].profile_key, staged[0].stream_id, staged[0].handle)
    assert len(server.profiles) == 1


def queued_intent(portal, profile, master, tmp_path, n=1):
    new_app(profile, master, tmp_path, n)
    sync.sync_all(journal=portal.journal, client=portal.client, now=NOW, execute=False)
    g = srv(portal).snapshot.awaiting[0].grant
    e = portal.fake.add_event(portal.key, "intent", grant=g)
    sync.sync_all(journal=portal.journal, client=portal.client, now=NOW, execute=False)
    assert portal.journal.get_event(e).state == "queued"
    return e, g


def test_rotate_passcode_new_verifier_epoch_and_voided_work(portal, profile, master, tmp_path):
    e, g = queued_intent(portal, profile, master, tmp_path)
    old_hash, old_epoch = srv(portal).passcode["hash"], srv(portal).epoch
    c = manage.rotate_passcode(portal.profile_id, journal=portal.journal, client=portal.client)
    s = srv(portal)
    assert c.handle == portal.credentials.handle and c.passcode != portal.credentials.passcode and s.passcode["hash"] != old_hash
    assert protocol.passcode_hash(c.passcode, bytes.fromhex(s.passcode["salt"])).hex() == s.passcode["hash"]
    assert s.epoch == old_epoch + 1 == portal.journal.get_profile(portal.profile_id).epoch
    assert portal.journal.get_grant(g).state == "void" and portal.journal.get_event(e).state == "cancelled"
    assert c.passcode.encode() not in portal.journal.path.read_bytes()


def test_new_grants_after_rotation_carry_the_new_epoch(portal, profile, master, tmp_path):
    new_app(profile, master, tmp_path, 1)
    sync.sync_all(journal=portal.journal, client=portal.client, now=NOW, execute=False)
    g1 = srv(portal).snapshot.awaiting[0].grant
    manage.rotate_passcode(portal.profile_id, passcode="another chosen code", journal=portal.journal, client=portal.client)
    sync.sync_all(journal=portal.journal, client=portal.client, now=NOW, execute=False)
    g2 = srv(portal).snapshot.awaiting[0].grant
    assert g2 != g1 and portal.journal.get_grant(g2).epoch == 1
    e = portal.fake.add_event(portal.key, "intent", grant=g1)  # a confirmation page from before the rotation
    sync.sync_all(journal=portal.journal, client=portal.client, now=NOW, execute=False)
    assert portal.journal.get_event(e).state == "refused"


def test_revoke_keeps_the_verifier_and_bumps_the_epoch(portal):
    before = dict(srv(portal).passcode)
    manage.revoke(portal.profile_id, journal=portal.journal, client=portal.client)
    assert srv(portal).passcode == before and srv(portal).epoch == 1 == portal.journal.get_profile(portal.profile_id).epoch


def test_set_mode_voids_grants_but_keeps_review_events(portal, profile, master, tmp_path):
    manage.set_mode(portal.profile_id, "review", journal=portal.journal)
    new_app(profile, master, tmp_path, 1)
    sync.sync_all(journal=portal.journal, client=portal.client, now=NOW, execute=False)
    held = portal.fake.edit_answer(portal.key, "city", "Lyon")
    sync.sync_all(journal=portal.journal, client=portal.client, now=NOW, execute=False)
    g = srv(portal).snapshot.awaiting[0].grant
    assert portal.journal.get_event(held).state == "awaiting_review" and srv(portal).snapshot.review_mode
    manage.set_mode(portal.profile_id, "direct", journal=portal.journal)
    assert portal.journal.get_event(held).state == "awaiting_review" and portal.journal.get_grant(g).state == "void"
    assert not portal.journal.get_profile(portal.profile_id).review_mode
    with pytest.raises(ManageError):
        manage.set_mode(portal.profile_id, "weird", journal=portal.journal)


def test_disable_is_local_first_and_survives_an_offline_server(portal, profile, master, tmp_path):
    e, g = queued_intent(portal, profile, master, tmp_path)
    portal.fake.offline = True
    res = manage.disable(portal.profile_id, journal=portal.journal, client=portal.client)
    assert not res.server_updated and res.error
    row = portal.journal.get_profile(portal.profile_id)
    assert not row.enabled and portal.journal.get_grant(g).state == "void" and portal.journal.get_event(e).state == "cancelled"
    portal.fake.offline = False
    assert manage.disable(portal.profile_id, journal=portal.journal, client=portal.client).server_updated
    assert srv(portal).enabled is False


def test_disable_with_purge_deletes_the_server_profile(portal):
    assert manage.disable(portal.profile_id, purge=True, journal=portal.journal, client=portal.client).server_updated
    assert portal.key not in portal.fake.profiles
    assert manage.disable(portal.profile_id, purge=True, journal=portal.journal, client=portal.client).server_updated  # idempotent


def test_unpair_needs_disabled_profiles_or_force(portal):
    with pytest.raises(ManageError, match="activés"):
        manage.unpair(journal=portal.journal)
    with pytest.warns(UserWarning, match="restent sur le serveur"):
        manage.unpair(force=True, journal=portal.journal)
    assert not portal.journal.get_profile(portal.profile_id).enabled and portal.journal.get_setting("origin") is None
    assert secrets.load_token(ORIGIN, "file") is None
    with pytest.raises(ManageError):
        manage.unpair(journal=portal.journal)


def test_pause_and_resume(journal):
    assert not journal.paused
    manage.pause(journal=journal)
    assert journal.paused and manage.status(journal=journal).paused
    manage.resume(journal=journal)
    assert not journal.paused


def test_resync_new_stream_cursor_zero_and_lost_events(portal, profile, master, tmp_path):
    e, g = queued_intent(portal, profile, master, tmp_path)
    old = portal.journal.get_profile(portal.profile_id)
    manage.resync(portal.profile_id, journal=portal.journal, client=portal.client)
    row = portal.journal.get_profile(portal.profile_id)
    assert row.stream_id != old.stream_id and row.cursor_seq == 0 and srv(portal).stream_id == row.stream_id
    assert portal.journal.get_event(e).state == "lost" and portal.journal.get_grant(g).state == "void"
    assert portal.journal.get_event(e).stream_id == old.stream_id


@pytest.mark.parametrize("operation", ["rotate_passcode", "revoke", "resync"])
def test_configuration_is_strict_locally_before_remote_and_reuses_staged_identity(portal, profile, master, tmp_path,
                                                                                  monkeypatch, operation):
    from jobhunt.portal.client import PortalOffline

    event, grant = queued_intent(portal, profile, master, tmp_path)
    journal = portal.journal
    before = journal.get_profile(profile.id)
    journal.save_profile(profile.id, confirmed_ack=1)
    put = portal.client.put_profile
    staged = []
    code_options = {"passcode": "code-synthétique-1234"} if operation == "rotate_passcode" else {}

    def lost_response(key, config):
        row = journal.get_profile(profile.id)
        assert journal.controls(profile.id)[1] == "configuration en attente"
        assert journal.get_grant(grant).state == "void"
        assert journal.get_event(event).state == ("lost" if operation == "resync" else "cancelled")
        assert (row.profile_key, row.stream_id, row.epoch, row.config_rev) == (key, config.stream_id, config.sessions_epoch, config.config_rev)
        if operation == "resync":
            assert row.stream_id != before.stream_id and row.cursor_seq == row.confirmed_ack == 0
        else:
            assert row.epoch == before.epoch + 1
        staged.append(row)
        put(key, config)
        raise PortalOffline()

    change = getattr(manage, operation)
    with monkeypatch.context() as patcher:
        patcher.setattr(portal.client, "put_profile", lost_response)
        with pytest.raises(ManageError):
            change(profile.id, journal=journal, client=portal.client, **code_options)
    change(profile.id, journal=journal, client=portal.client, **code_options)
    row = journal.get_profile(profile.id)
    assert (row.profile_key, row.handle, row.stream_id, row.epoch) == (
        staged[0].profile_key, staged[0].handle, staged[0].stream_id, staged[0].epoch)
    assert not journal.pending_config(profile.id)


def test_status_summary(portal, profile, master, tmp_path):
    queued_intent(portal, profile, master, tmp_path)
    manage.set_mode(portal.profile_id, "review", journal=portal.journal)
    st = manage.status(journal=portal.journal)
    assert st.paired and st.origin == ORIGIN and st.device_name == "Mac de test" and not st.paused
    (p,) = st.profiles
    assert (p.profile_id, p.name, p.enabled, p.mode, p.handle) == (portal.profile_id, "Léa", True, "review", portal.credentials.handle)
    assert p.last_sync_at == NOW and p.last_error is None and not p.held and p.sends_today == 0
    assert (p.pending, p.awaiting_review) == (0, 0)


def test_status_when_not_paired(journal):
    st = manage.status(journal=journal)
    assert not st.paired and st.profiles == []


def test_review_list_apply_reject_and_journal_tail(portal):
    manage.set_mode(portal.profile_id, "review", journal=portal.journal)
    sync.sync_all(journal=portal.journal, client=portal.client, now=NOW, execute=False)
    a = portal.fake.edit_answer(portal.key, "city", "Lyon")
    b = portal.fake.edit_answer(portal.key, "postal_code", "69000")
    c = portal.fake.edit_answer(portal.key, "country", "France")
    sync.sync_all(journal=portal.journal, client=portal.client, now=NOW, execute=False)
    items = manage.review_list(portal.profile_id, journal=portal.journal)
    assert [(i.label, i.before_text, i.after_text) for i in items] == [("Ville", "", "Lyon"), ("Code postal", "", "69000"), ("Pays", "", "France")]
    assert manage.status(journal=portal.journal).profiles[0].awaiting_review == 3
    rep = manage.review_apply(portal.profile_id, [a], journal=portal.journal, client=portal.client)
    assert rep.applied_answers and stored_answer(portal.profile_id, "city") == "Lyon"
    assert manage.review_reject(portal.profile_id, [b], journal=portal.journal) == 1
    assert manage.review_apply(portal.profile_id, None, journal=portal.journal, client=portal.client).applied_answers
    assert stored_answer(portal.profile_id, "country") == "France" and stored_answer(portal.profile_id, "postal_code") == ""
    tail = manage.journal_tail(portal.profile_id, 2, journal=portal.journal)
    assert len(tail) == 2 and {t.state for t in manage.journal_tail(portal.profile_id, journal=portal.journal)} == {"applied", "rejected"}
    sync.sync_all(journal=portal.journal, client=portal.client, now=NOW + timedelta(minutes=5), execute=False)
    assert {r.event_id: r.status for r in srv(portal).results} == {a: "applied", b: "rejected", c: "applied"}


def test_review_actions_refuse_while_a_sync_pass_runs(portal):
    from jobhunt.portal.journal import pass_lock

    with pass_lock("sync.lock"):
        with pytest.raises(ManageError, match="synchronisation"):
            manage.review_apply(portal.profile_id, journal=portal.journal)
        with pytest.raises(ManageError):
            manage.review_reject(portal.profile_id, journal=portal.journal)


@pytest.mark.parametrize("gate", ["offline", "device", "disabled", "held", "epoch", "cutoff", "config"])
def test_review_requires_current_server_controls(portal, gate):
    import httpx

    journal, profile_id = portal.journal, portal.profile_id
    manage.set_mode(profile_id, "review", journal=journal)
    sync.sync_all(journal=journal, client=portal.client, now=NOW, execute=False)
    event_id = portal.fake.edit_answer(portal.key, "city", "Lyon")
    assert sync.sync_all(journal=journal, client=portal.client, now=NOW, execute=False)[0].ok
    remote = srv(portal)
    if gate == "offline":
        portal.fake.offline = True
    elif gate == "device":
        portal.fake.override.append(lambda _: httpx.Response(401, json={"error": "unauthorized"}))
    elif gate == "disabled":
        remote.enabled = False
    elif gate == "held":
        remote.held = True
    elif gate == "epoch":
        remote.epoch += 1
    elif gate == "config":
        remote.config_rev += 1
    else:
        remote.revoked_through_seq = remote.last_seq

    if gate in ("held", "cutoff"):
        result = manage.review_apply(profile_id, journal=journal, client=portal.client)
        assert not result.applied_answers
    else:
        with pytest.raises(ManageError):
            manage.review_apply(profile_id, journal=journal, client=portal.client)
    event = journal.get_event(event_id)
    assert not event.approved and stored_answer(profile_id, "city") == ""
    assert event.state == ("cancelled" if gate == "cutoff" else "awaiting_review")


def test_rotation_replays_exact_verifier_after_reopening_journal(portal, monkeypatch):
    from jobhunt.portal.client import PortalOffline

    profile_id = portal.profile_id
    journal = portal.journal
    put = portal.client.put_profile
    bodies = []
    chosen = "code-de-test-1234"

    def drop_first_response(key, config):
        bodies.append(protocol.body_digest(config))
        result = put(key, config)
        if len(bodies) == 1:
            raise PortalOffline()
        return result

    monkeypatch.setattr(portal.client, "put_profile", drop_first_response)
    with pytest.raises(ManageError):
        manage.rotate_passcode(profile_id, chosen, journal=journal, client=portal.client)
    committed = srv(portal)
    epoch, rev, cutoff = committed.epoch, committed.config_rev, committed.revoked_through_seq
    committed.last_seq += 1  # a post-rotation request must not be retired by a retry
    journal = Journal(journal.path)
    assert chosen.encode() not in journal.path.read_bytes()
    with pytest.raises(ManageError, match="code initial"):
        manage.rotate_passcode(profile_id, "another-test-code", journal=journal, client=portal.client)
    credentials = manage.rotate_passcode(profile_id, chosen, journal=journal, client=portal.client)
    assert credentials.passcode == chosen and len(bodies) == 2 and bodies[0] == bodies[1]
    assert (committed.epoch, committed.config_rev, committed.revoked_through_seq) == (epoch, rev, cutoff)
    assert journal.pending_config(profile_id) is None and journal.pending_verifier(profile_id) is None


def test_upgrade_retires_unverified_legacy_reviews_without_reconciling(portal, monkeypatch):
    import sqlite3

    from jobhunt.portal import apply_events

    journal, profile_id = portal.journal, portal.profile_id
    manage.set_mode(profile_id, "review", journal=journal)
    sync.sync_all(journal=journal, client=portal.client, now=NOW, execute=False)
    event_id = portal.fake.edit_answer(portal.key, "city", "Lyon")
    assert sync.sync_all(journal=journal, client=portal.client, now=NOW, execute=False)[0].ok
    assert journal.get_event(event_id).state == "awaiting_review"
    journal.update_event(event_id, NOW, write_ver=protocol.new_id(), approved=True)
    with sqlite3.connect(journal.path) as connection:
        connection.execute("DROP TABLE pending_verifiers")
        connection.execute("PRAGMA user_version=7")

    def unexpected(*args):
        pytest.fail("migration must not replay or reconcile unvalidated work")

    monkeypatch.setattr(apply_events, "reconcile_write_plans", unexpected)
    upgraded = Journal(journal.path)
    assert upgraded.get_event(event_id).state == "cancelled"
    report = manage.review_apply(profile_id, [event_id], journal=upgraded, client=portal.client)
    assert not report.applied_answers and stored_answer(profile_id, "city") == ""


def test_recover_lists_the_servers_profiles_for_a_fresh_journal(portal, tmp_path):
    fresh = Journal(tmp_path / "fresh" / "portal.sqlite3")  # the old journal is lost; the token survives
    found = manage.recover(BASE, "file", journal=fresh, client=portal.client)
    assert [r.profile_key for r in found] == [portal.key] and found[0].enabled and fresh.get_setting("origin") == ORIGIN
    with pytest.raises(ManageError, match="déjà appairé"):
        manage.recover(BASE, "file", journal=fresh, client=portal.client)


def test_recover_without_a_token_or_server(portal, tmp_path):
    secrets.delete_token(ORIGIN, "file")
    with pytest.raises(ManageError, match="Jeton introuvable"):
        manage.recover(BASE, "file", journal=Journal(tmp_path / "f1" / "p.sqlite3"), client=portal.client)


def test_re_enable_after_recovery_reuses_the_server_key(portal, profile, tmp_path):
    fresh = Journal(tmp_path / "fresh" / "portal.sqlite3")
    manage.recover(BASE, "file", journal=fresh, client=portal.client)
    c = manage.enable(profile.id, profile_key=portal.key, journal=fresh, client=portal.client)  # 409 config_rev is adopted
    assert fresh.get_profile(profile.id).profile_key == portal.key and c.handle != portal.credentials.handle
    assert srv(portal).config_rev == fresh.get_profile(profile.id).config_rev
    (rep,) = sync.sync_all(journal=fresh, client=portal.client, now=NOW, execute=False)
    assert rep.ok


def test_forget_server_profile(portal):
    manage.forget_server_profile(portal.key, journal=portal.journal, client=portal.client)
    assert portal.key not in portal.fake.profiles
    with pytest.raises(ManageError):
        manage.forget_server_profile(portal.key, journal=portal.journal, client=portal.client)
