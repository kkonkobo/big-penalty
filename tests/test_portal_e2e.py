"""End to end: the REAL portal server (uvicorn on 127.0.0.1) against the REAL Mac-side sync client (manage / sync / PortalClient over
HTTP, journal + token file in the temp data dir). The job seeker is simulated through the server's own auth / ops functions.
Every scenario asserts on BOTH sides. Fictional data only; no sleeping (both sides take an injectable clock)."""

from __future__ import annotations

import asyncio
import hashlib
from datetime import timedelta

import httpx
import pytest

from jobhunt import db, schedule
from jobhunt.config import get_settings
from jobhunt.apply import accounts, facts, service
from jobhunt.apply import store as apply_store
from jobhunt.apply.adapters import Registry
from jobhunt.apply.types import Question, RunResult
from jobhunt.cv import service as cvs
from jobhunt.cv import store as cvstore
from jobhunt import daily
from jobhunt.portal import apply_events, manage, protocol, sync
from jobhunt.portal.protocol import CancelEvent, IntentEvent
from jobhunt.portal import secrets as portal_secrets
from jobhunt.portal.client import PortalClient, PortalError
from jobhunt.portal.journal import Journal
from jobhunt.portal.server import admin, ops

from apply_service_fakes import FakePool
from portal_e2e_helpers import Kill, LiveServer, Mac, Seeker, Setup
from portal_fakes import app_dir, fixture_ats, journal, make_outstanding, make_run, make_sent, master, new_app, set_pdf  # noqa: F401

pytestmark = pytest.mark.usefixtures("fixture_ats")


# ---------------------------------------------------------------- fixtures
@pytest.fixture(autouse=True)
def no_keychain(monkeypatch):
    monkeypatch.setattr(accounts, "create_password", lambda host, email: "Pw-Test-1234")
    monkeypatch.setattr(accounts, "get_password", lambda host, email, **kw: None)
    monkeypatch.setattr(service, "BrowserPool", lambda headless=None: FakePool())


@pytest.fixture
def live(tmp_path, monkeypatch):
    monkeypatch.setattr(protocol, "SCRYPT_N", 2 ** 6)  # cheap scrypt on both sides (one test below uses the real tuple)
    srv = LiveServer(tmp_path)
    yield srv
    srv.stop()


@pytest.fixture
def live_real(tmp_path):
    srv = LiveServer(tmp_path)
    yield srv
    srv.stop()


def _setup(live, profile, master, **enable_kw) -> Setup:
    mac = Mac(live)
    mac.pair()
    creds = mac.enable(profile.id, **enable_kw)
    return Setup(live, mac, profile.id, creds, Seeker.of(live, creds))


@pytest.fixture
def w(live, profile, master) -> Setup:
    """A paired Mac, one enabled profile (Léa), its seeker logged in."""
    return _setup(live, profile, master)


def answers_bytes(pid: int) -> bytes:
    p = facts.answers_path(pid)
    return p.read_bytes() if p.exists() else b""


def stored(pid: int, path: str) -> str:
    return facts.get_answer(facts.load_answers(pid), path) or ""


def srv_snap(w: Setup) -> protocol.Snapshot:
    return w.live.snapshot(w.key)


def srv_ver(w: Setup, path: str) -> str:
    return srv_snap(w).answers[path].ver


def srv_results(w: Setup) -> dict[str, str]:
    return {r.event_id: r.status for r in w.live.stored_results(w.key)}


@pytest.fixture
def engine(fixture_ats):
    return fixture_ats


@pytest.fixture
def run_spy(monkeypatch):
    """Records every real call of `service.run` made by the executor (and still runs it)."""
    calls: list[dict] = []
    real = service.run

    async def spy(app_id, **kw):
        app = cvstore.get_application(app_id)
        calls.append({"app_id": app_id, "status_at_call": app.status, "approved_rev": getattr(app, "approved_rev", None), **kw})
        return await real(app_id, **kw)

    monkeypatch.setattr(service, "run", spy)
    return calls


# ================================================================ 1. pairing
def test_01_pairing_wrong_code_right_code_second_device_retry_unpair(live, tmp_path):
    mac = Mac(live)
    with pytest.raises(manage.ManageError):
        mac.pair(code="x" * 40)
    st = manage.status()
    assert not st.paired and portal_secrets.load_token(live.origin, "file") is None  # nothing kept after a refusal
    assert live.conn.execute("SELECT COUNT(*) FROM devices").fetchone()[0] == 0

    info = mac.pair()
    assert info.origin == live.origin and info.base_path == "/app" and info.token_store == "file"
    token = mac.token()
    row = live.conn.execute("SELECT id, token_sha256 FROM devices").fetchone()
    assert row["id"] == info.device_id and row["token_sha256"] == hashlib.sha256(token.encode()).hexdigest()
    assert token not in live.raw_bytes().decode("latin-1")  # only its sha256 is stored
    assert manage.status().paired and Journal().get_setting("device_id") == info.device_id
    with pytest.raises(manage.ManageError):  # already paired
        mac.pair()

    # the code cannot be used by a second device (PORTAL_MAX_DEVICES raised: the refusal is the consumed code)
    live.restart(PORTAL_MAX_DEVICES="3")
    other_token = protocol.new_token()
    with PortalClient(live.base_url) as c, pytest.raises(PortalError) as e:
        c.pair(live.pairing_code, "Autre Mac", protocol.sha256_hex(other_token))
    assert e.value.status == 403
    assert live.conn.execute("SELECT COUNT(*) FROM devices").fetchone()[0] == 1

    # a retry of the same device (lost response) is idempotent and returns the same device id
    with PortalClient(live.base_url) as c:
        again = c.pair(live.pairing_code, "Mac de test", protocol.sha256_hex(token))
    assert again.device_id == info.device_id and live.conn.execute("SELECT COUNT(*) FROM devices").fetchone()[0] == 1
    # and the paired token works
    with PortalClient(live.base_url, token) as c:
        assert c.status().profiles == []

    manage.unpair()
    st = manage.status()
    assert not st.paired and portal_secrets.load_token(live.origin, "file") is None
    with pytest.raises(manage.ManageError):  # not paired any more
        manage.enable(1)
    # the consumed code cannot pair this Mac again with a NEW token: the operator must issue a new code
    with pytest.raises(manage.ManageError):
        mac.pair()


def test_01_pairing_lost_response_can_be_retried_by_the_mac(live, monkeypatch):
    """DESIGN §4: a lost response can be retried with the same token. The Mac must not throw its token away on a network error."""
    mac = Mac(live)
    real = PortalClient.pair
    state = {"n": 0}

    def lose_response(self, code, name, sha):
        resp = real(self, code, name, sha)  # the server consumed the code ...
        state["n"] += 1
        if state["n"] == 1:
            from jobhunt.portal.client import PortalOffline
            raise PortalOffline()  # ... but the answer never arrived
        return resp

    monkeypatch.setattr(PortalClient, "pair", lose_response)
    with pytest.raises(manage.ManageError):
        mac.pair()
    info = mac.pair()  # the same pairing code, the same token
    assert info.device_id == live.conn.execute("SELECT id FROM devices").fetchone()["id"]
    assert live.conn.execute("SELECT COUNT(*) FROM devices").fetchone()[0] == 1


def test_01_pairing_refused_after_the_expiry(live):
    mac = Mac(live)
    live.clock.advance(31 * 86400)  # past PORTAL_PAIRING_EXPIRES
    with pytest.raises(manage.ManageError):
        mac.pair()
    assert not manage.status().paired and live.conn.execute("SELECT COUNT(*) FROM devices").fetchone()[0] == 0


def test_01_pairing_refused_when_the_server_has_no_code(live):
    live.restart(PORTAL_PAIRING_CODE=None, PORTAL_PAIRING_EXPIRES=None)
    with pytest.raises(manage.ManageError):
        Mac(live).pair(code="anything-" * 6)


# ================================================================ 2. enable, snapshot, login, rotate, revoke
def test_02_enable_sync_shows_the_snapshot_and_the_printed_credentials_log_in(w, profile):
    live, mac, pid = w.live, w.mac, w.profile_id
    assert w.creds.address == f"{live.origin}/app/" and len(w.creds.handle) == protocol.HANDLE_LEN
    row = mac.journal.get_profile(pid)
    srow = live.profile_row(row.profile_key)
    assert srow["enabled"] == 1 and srow["stream_id"] == row.stream_id and srow["handle_sha256"] == protocol.handle_digest(w.creds.handle)
    assert srow["snapshot_rev"] == 0 and live.snapshot(row.profile_key) is None  # nothing pushed before the first pass

    rep = mac.sync_one(pid)
    assert rep.ok and rep.pushed
    snap = srv_snap(w)
    assert [f.path for f in snap.answer_schema.fields] == [f.path for f in facts.ANSWER_FIELDS]  # same paths, same order
    assert snap.answer_schema.sections == list(facts.ANSWER_SECTIONS)
    assert {f.path for f in snap.answer_schema.fields if f.write_only} == {"disability", "criminal_record"}
    assert snap.display_name == "Léa" and set(snap.answers) == {f.path for f in facts.ANSWER_FIELDS}
    v = w.seeker.view()
    assert v.has_snapshot and list(v.fields) == [f.path for f in facts.ANSWER_FIELDS]
    assert mac.journal.get_profile(pid).last_sync_at == mac.now and mac.journal.get_profile(pid).last_error is None

    # the address / identifiant / code as printed, in every form the seeker may type them
    for h, c in [(w.creds.handle, w.creds.passcode), (w.creds.handle_shown, w.creds.passcode_shown),
                 (w.creds.handle_shown.upper(), w.creds.passcode_shown.upper()), (f"  {w.creds.handle.upper()} ", w.creds.passcode.upper())]:
        assert live.login(h, c).kind == "ok"
    assert w.creds.handle_shown.count("-") == 1 and w.creds.passcode_shown.count("-") == 2
    wrong = live.login(w.creds.handle, "wrongcode123")
    assert wrong.kind == "refused" and wrong.session_id is None
    assert live.login("unknownhand", w.creds.passcode).kind == "refused"


def test_02_rotate_passcode_kills_sessions_and_the_old_code_revoke_keeps_the_code(w):
    live, mac, pid, seeker = w.live, w.mac, w.profile_id, w.seeker
    mac.sync_one(pid)
    assert seeker.alive()
    epoch_before = live.profile_row(w.key)["sessions_epoch"]

    # revoke: sessions (and known browsers) die, the code and identifiant stay valid
    manage.revoke(pid)
    assert not seeker.alive() and live.profile_row(w.key)["sessions_epoch"] == epoch_before + 1
    assert live.conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0
    seeker.login()
    assert seeker.alive()

    # rotate: new code, the old one and every session are dead
    new = manage.rotate_passcode(pid)
    assert new.handle == w.creds.handle and new.passcode != w.creds.passcode
    assert not seeker.alive()
    assert live.login(w.creds.handle, w.creds.passcode).kind == "refused"
    fresh = Seeker.of(live, new)
    assert fresh.alive() and fresh.view().has_snapshot
    # the pass after a rotation still works (grants of the old epoch are void, the stream is the same)
    assert mac.sync_one(pid).ok


@pytest.mark.parametrize("operation", ["revoke", "rotate"])
def test_02_unpulled_answers_are_retired_without_resetting_the_stream(w, operation):
    live, mac, pid = w.live, w.mac, w.profile_id
    assert mac.sync_one(pid).ok
    before = mac.journal.get_profile(pid)
    (old_event,) = w.seeker.edit("city", "Lyon").event_ids
    if operation == "rotate":
        credentials = manage.rotate_passcode(pid)
    else:
        manage.revoke(pid)
        credentials = w.creds
    fresh = Seeker.of(live, credentials)
    assert fresh.value("city") != "Lyon"
    (new_event,) = fresh.edit("city", "Nice").event_ids
    assert mac.sync_one(pid).ok
    after = mac.journal.get_profile(pid)
    assert after.stream_id == before.stream_id and after.epoch == before.epoch + 1
    assert after.revoked_through_seq == before.cursor_seq + 1
    assert after.cursor_seq == after.confirmed_ack == before.cursor_seq + 2
    assert mac.journal.get_event(old_event).payload == {}
    assert fresh.statuses() == {old_event: "cancelled", new_event: "applied"}
    assert stored(pid, "city") == "Nice"


def test_02_revoke_retires_journaled_review_items(w):
    mac, seeker, pid = w.mac, w.seeker, w.profile_id
    manage.set_mode(pid, "review")
    assert mac.sync_one(pid).ok
    (event_id,) = seeker.edit("city", "Lyon").event_ids
    assert mac.sync_one(pid).ok and seeker.statuses()[event_id] == "awaiting_review"
    cursor = mac.journal.get_profile(pid).cursor_seq

    manage.revoke(pid)
    fresh = Seeker.of(w.live, w.creds)
    assert fresh.statuses()[event_id] == "cancelled"
    assert mac.journal.get_profile(pid).cursor_seq == cursor
    assert mac.journal.get_event(event_id).state == "cancelled"
    apply_events.apply_reviewed(mac.journal, pid, [event_id], approve=True, now=mac.now)
    assert stored(pid, "city") == ""


def test_02_login_with_the_real_scrypt_parameters(live_real, profile, master):
    w = _setup(live_real, profile, master)
    assert protocol.SCRYPT_N == 2 ** 15
    w.mac.sync_one(w.profile_id)
    srow = live_real.profile_row(w.key)
    assert len(bytes(srow["passcode_hash"])) == protocol.SCRYPT_DKLEN
    out = live_real.login(w.creds.handle_shown.upper(), w.creds.passcode_shown.upper())
    assert out.kind == "ok" and w.seeker.alive()
    assert live_real.login(w.creds.handle, "not-the-code-00").kind == "refused"


# ================================================================ 3. answers, portal -> Mac
def test_03_answers_portal_to_mac(w, profile, master, tmp_path):
    live, mac, pid, seeker = w.live, w.mac, w.profile_id, w.seeker
    _, app = new_app(profile, master, tmp_path, 1, approve=True)
    make_run(app, tmp_path, status="needs_input", questions=[Question(label="Ville de naissance ?")])
    mac.sync_one(pid, execute=False)
    snap = srv_snap(w)
    (q,) = snap.questions
    assert q.label == "Ville de naissance ?" and q.qid == facts.custom_id(q.label)
    before = {p: srv_ver(w, p) for p in ("city", "stage.available_from", "driving_licence")}

    o1 = seeker.edit("city", "Lyon")
    o2 = seeker.edit("stage.available_from", "2027-09-01")
    o3 = seeker.edit("driving_licence", "Oui (permis B)")
    o4 = seeker.answer_question(q.qid, "Albi")
    assert all(o.ok and o.stored == 1 for o in (o1, o2, o3, o4))
    ids = [o.event_ids[0] for o in (o1, o2, o3, o4)]
    assert [e.seq for e in live.pending(w.key)] == [1, 2, 3, 4]
    assert all(seeker.view().pending) and seeker.value("city") == "Lyon"  # he sees his pending edit at once

    rep = mac.sync_one(pid, execute=False)
    assert rep.ok and rep.pulled == 4 and rep.applied.applied_answers
    # Mac files, through the engine's own functions
    answers = facts.load_answers(pid)
    assert facts.get_answer(answers, "city") == "Lyon"
    assert facts.get_answer(answers, "stage.available_from") == "1er septembre 2027"
    assert facts.get_answer(answers, "driving_licence") == "Oui (permis B)"
    assert [c for c in facts.custom_answers(pid) if c["question"] == "Ville de naissance ?"][0]["answer"] == "Albi"
    # server side: events gone, acked, results applied, snapshot shows the values, versions changed
    assert live.pending(w.key) == [] and live.profile_row(w.key)["acked_seq"] == 4 == live.profile_row(w.key)["last_seq"]
    assert [seeker.statuses()[i] for i in ids] == ["applied"] * 4 and srv_results(w) == dict.fromkeys(ids, "applied")
    snap = srv_snap(w)
    assert snap.answers["city"].value == "Lyon" and snap.answers["stage.available_from"].value == "2027-09-01"
    assert snap.answers["driving_licence"].value == "Oui (permis B)"
    assert snap.questions == [] and [c.answer for c in snap.custom if c.question == "Ville de naissance ?"] == ["Albi"]
    j = mac.journal
    for path, eid in zip(("city", "stage.available_from", "driving_licence"), ids):
        assert snap.answers[path].ver != before[path] and snap.answers[path].ver == j.get_event(eid).ver_after
    v = seeker.view()
    assert not v.pending and v.fields["city"].value == "Lyon" and v.fields["stage.available_from"].value == "2027-09-01"
    assert "Lyon" not in " ".join(r.message + r.label for r in v.results)  # a result never carries a value

    # a second pass is a no-op: same file bytes, same versions, nothing new in the journal
    file_before, vers = answers_bytes(pid), {p: a.ver for p, a in snap.answers.items()}
    events_before = len(j.events(pid))
    rep = mac.sync_one(pid, execute=False)
    assert rep.ok and rep.pulled == 0 and answers_bytes(pid) == file_before
    assert {p: a.ver for p, a in srv_snap(w).answers.items()} == vers and len(j.events(pid)) == events_before
    assert live.profile_row(w.key)["snapshot_rev"] > 1  # the revision advances, the content does not change


# ================================================================ 4. answers, Mac -> portal
def test_04_local_edit_reaches_the_portal_and_different_fields_both_survive(w):
    mac, pid, seeker = w.mac, w.profile_id, w.seeker
    mac.sync_one(pid)
    facts.save_answers(pid, {"nationality": "Française"})  # the local UI
    mac.sync_one(pid)
    assert seeker.value("nationality") == "Française" and srv_snap(w).answers["nationality"].is_set

    # both sides edit DIFFERENT fields inside the same sync interval
    seeker.edit("city", "Lyon")
    facts.save_answers(pid, {"postal_code": "69001"})
    rep = mac.sync_one(pid)
    assert rep.ok and stored(pid, "city") == "Lyon" and stored(pid, "postal_code") == "69001"
    assert srv_snap(w).answers["city"].value == "Lyon" and srv_snap(w).answers["postal_code"].value == "69001"
    assert set(seeker.statuses().values()) == {"applied"}


# ================================================================ 5. conflict
def test_05_conflict_keeps_the_mac_value_and_the_seeker_can_resubmit(w):
    mac, pid, seeker = w.mac, w.profile_id, w.seeker
    facts.save_answers(pid, {"city": "Toulouse"})
    mac.sync_one(pid)
    page_ver = srv_ver(w, "city")
    seeker.view()  # the page he is looking at
    facts.save_answers(pid, {"city": "Paris"})  # changed on the Mac after that snapshot
    out = seeker.edit("city", "Lyon")
    assert out.stored == 1
    rep = mac.sync_one(pid)
    assert rep.ok and stored(pid, "city") == "Paris"
    assert seeker.statuses()[out.event_ids[0]] == "conflict"
    assert "Modifiée sur le Mac" in seeker.messages()[out.event_ids[0]]
    assert seeker.value("city") == "Paris" and srv_ver(w, "city") != page_ver
    # he re-submits against what he sees now
    out2 = seeker.edit("city", "Lyon")
    assert out2.stored == 1
    mac.sync_one(pid)
    assert stored(pid, "city") == "Lyon" and seeker.statuses()[out2.event_ids[0]] == "applied" and seeker.value("city") == "Lyon"


def test_05_stale_form_token_two_tabs(w):
    mac, pid, seeker = w.mac, w.profile_id, w.seeker
    mac.sync_one(pid)
    tab_a = seeker.view().fields["city"].token
    tab_b = seeker.view().fields["city"].token
    assert seeker.edit("city", "Lyon", token=tab_a).stored == 1
    stale = seeker.edit("city", "Nice", token=tab_b)
    assert stale.stored == 0 and len(stale.stale) == 1 and not stale.ok
    assert [e.kind for e in live_events(w)] == ["answer"]  # only the first one was stored
    # even after the Mac took the first edit, the old tab's token stays stale
    mac.sync_one(pid)
    again = seeker.edit("city", "Nice", token=tab_b)
    assert again.stored == 0 and len(again.stale) == 1
    assert stored(pid, "city") == "Lyon"


def live_events(w: Setup) -> list:
    return w.live.pending(w.key)


# ================================================================ 6. two pending edits of one field
def test_06_two_edits_before_any_sync_last_value_wins_both_reported(w):
    mac, pid, seeker = w.mac, w.profile_id, w.seeker
    mac.sync_one(pid)
    o1, o2 = seeker.edit("city", "Lyon"), seeker.edit("city", "Nice")
    e1, e2 = o1.event_ids[0], o2.event_ids[0]
    evs = live_events(w)
    assert evs[1].after == e1 and evs[1].base_ver == evs[0].base_ver  # chained through `after`
    rep = mac.sync_one(pid)
    assert rep.ok and stored(pid, "city") == "Nice"
    assert seeker.statuses()[e1] == "applied" and seeker.statuses()[e2] == "applied"
    assert seeker.value("city") == "Nice" and srv_snap(w).answers["city"].ver == mac.journal.get_event(e2).ver_after
    # three in a chain, with a Mac edit of ANOTHER field in between
    seeker.edit("city", "A"), seeker.edit("city", "B"), seeker.edit("city", "C")
    facts.save_answers(pid, {"department": "Rhône"})
    mac.sync_one(pid)
    assert stored(pid, "city") == "C" and stored(pid, "department") == "Rhône"


# ================================================================ 7. write-only
def test_07_write_only_value_never_reaches_the_server(w):
    live, mac, pid, seeker = w.live, w.mac, w.profile_id, w.seeker
    facts.save_answers(pid, {"disability": "Oui (RQTH)"})
    facts.save_answers(pid, {"criminal_record": "Casier judiciaire vierge"})
    facts.save_custom_answers(pid, [("Avez-vous la RQTH ? (reconnaissance handicap)", "Reconnue en 2019 secret-xyz-4242")])
    mac.sync_one(pid)
    snap, pid_row = srv_snap(w), live.row_id(w.key)
    for path in ("disability", "criminal_record"):
        a = snap.answers[path]
        assert a.value is None and a.is_set and a.legacy == ""
        for text in ("Oui (RQTH)", "Casier judiciaire vierge"):  # nothing derived from the value either
            for h in (hashlib.sha256, hashlib.md5, hashlib.sha1):
                assert a.ver != h(text.encode()).hexdigest()[:32]
    dump = snap.model_dump_json()
    masked = [c for c in snap.custom if c.masked]
    assert len(masked) == 1 and masked[0].answer == "" and "secret-xyz-4242" not in dump
    # the stored (decrypted) snapshot and the raw database bytes
    row = live.conn.execute("SELECT key_id, nonce, ciphertext FROM snapshots WHERE profile_id=? AND purpose='snapshot'", (pid_row,)).fetchone()
    plain = live.portal.cipher.open("snapshot", pid_row, "0", ops._sealed(row)).decode()
    raw = live.raw_bytes()
    for needle in ("secret-xyz-4242", "Reconnue en 2019"):
        assert needle not in plain and needle.encode() not in raw
    v = seeker.view()
    assert v.fields["disability"].value is None and v.fields["disability"].is_set
    assert [c.answer for c in v.custom if c.masked] == [""]

    # he replaces the write-only value and the masked custom answer
    o = seeker.edit("disability", "Non")
    oc = seeker.edit_custom(masked[0].custom_id, "Non concerné secret-abc-7777")
    assert o.stored == 1 and oc.stored == 1
    assert seeker.edit("disability", "").stored == 0  # empty = "no change" for a write-only field unless he clears it
    ev_ids = [o.event_ids[0], oc.event_ids[0]]
    mac.sync_one(pid)
    assert stored(pid, "disability") == "Non"
    assert [c for c in facts.custom_answers(pid) if "RQTH" in c["question"]][0]["answer"] == "Non concerné secret-abc-7777"
    assert [seeker.statuses()[i] for i in ev_ids] == ["applied", "applied"] and live.pending(w.key) == []
    snap = srv_snap(w)
    assert snap.answers["disability"].value is None and "secret-abc-7777" not in snap.model_dump_json()
    assert all("secret-abc-7777" not in (r.message + r.label) and "Non" != r.message for r in live.stored_results(w.key))
    assert b"secret-abc-7777" not in live.raw_bytes()

    # clearing removes it on the Mac
    oclear = seeker.edit("disability", "", clear=True)
    assert oclear.stored == 1
    mac.sync_one(pid)
    assert stored(pid, "disability") == "" and "disability" not in facts.load_answers(pid)
    snap = srv_snap(w)
    assert snap.answers["disability"].value is None and not snap.answers["disability"].is_set
    assert seeker.statuses()[oclear.event_ids[0]] == "applied"


# ================================================================ 8. send approval
def awaiting(w: Setup):
    return srv_snap(w).awaiting


@pytest.mark.parametrize("sent", [True, False])
def test_supervised_approval_and_exact_attempt_settlement(w, profile, master, tmp_path, monkeypatch, run_spy, engine, sent):
    monkeypatch.setattr(service, "registry", Registry())
    job, draft = new_app(profile, master, tmp_path)
    assert w.mac.sync_one(profile.id).ok
    approval = awaiting(w)[0]
    event = w.seeker.request(approval.grant).event_ids[0]
    assert w.mac.sync_one(profile.id).ok
    app = cvstore.get_application(draft.id)
    assert app.approved_rev == draft.current_rev and app.approved_by == "user"
    assert not run_spy and not engine and not apply_store.list_runs(app.id)
    assert w.mac.journal.sends_today(profile.id, w.mac.now) == 0
    assert w.seeker.statuses()[event] == "done" and "Accord enregistré" in w.seeker.messages()[event]
    assert [item.kind for item in srv_snap(w).info] == ["supervised"]

    def reserve_without_click():
        live_job = db.get_job(job.id)
        run = apply_store.start_run(app.id, app.current_rev, "supervised", "user", app_dir,
                                    origin_sha=service.origin_digest(live_job.origin_url))
        service._reserve(run, live_job, "supervised", "user")
        apply_store.finish_and_settle(run.id, RunResult(status="failed", message="Fenêtre fermée."))
        return run.id

    first = reserve_without_click()
    assert w.mac.sync_one(profile.id).ok
    stale = srv_snap(w).unconfirmed[0].grant_sent
    service.resolve_unconfirmed(app.id, sent=False, run_id=first)
    cvs.approve(app.id, app.current_rev)
    second = reserve_without_click()
    stale_event = w.seeker.request(stale).event_ids[0]
    assert w.mac.sync_one(profile.id).ok
    assert w.seeker.statuses()[stale_event] == "refused" and apply_store.outstanding_attempt(app.id).id == second
    verification = srv_snap(w).unconfirmed[0]
    decision = w.seeker.request(verification.grant_sent if sent else verification.grant_not_sent).event_ids[0]
    assert w.mac.sync_one(profile.id).ok
    assert w.seeker.statuses()[decision] == "done" and apply_store.outstanding_attempt(app.id) is None
    if sent:
        assert apply_store.sent_run(app.id).id == second
    else:
        assert apply_store.requires_manual_retry(app.id) and cvstore.get_application(app.id).status == "draft"
    assert not run_spy and not engine


def test_registry_change_does_not_dispatch_during_sync(w, profile, master, tmp_path, monkeypatch, engine):
    registry = service.registry
    monkeypatch.setattr(service, "registry", Registry())
    _, app = new_app(profile, master, tmp_path)
    assert w.mac.sync_one(profile.id).ok
    grant = awaiting(w)[0].grant
    event = w.seeker.request(grant).event_ids[0]
    monkeypatch.setattr(service, "registry", registry)
    assert w.mac.sync_one(profile.id).ok
    assert w.seeker.statuses()[event] == "done"
    assert cvstore.get_application(app.id).approved_by == "user" and not engine and not apply_store.list_runs(app.id)


def test_08_send_approval_end_to_end(w, profile, master, tmp_path, engine, run_spy, monkeypatch):
    live, mac, pid, seeker = w.live, w.mac, w.profile_id, w.seeker
    job, app = new_app(profile, master, tmp_path, 1)  # a draft: bucket to_validate
    rep = mac.sync_one(pid, execute=False)
    assert rep.ok and rep.uploaded == 1
    (aw,) = awaiting(w)
    assert aw.company == "Société 1" and aw.doc_id and [d.doc_id for d in srv_snap(w).documents] == [aw.doc_id]
    pdf = cvs.safe_pdf(app).read_bytes()
    assert seeker.document(aw.doc_id) == pdf  # the exact bytes, through the seeker's own access path
    assert live.conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 1
    assert mac.sync_one(pid, execute=False).uploaded == 0 and live.conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 1

    out = seeker.request(aw.grant)
    assert out.stored == 1
    dup = seeker.request(aw.grant)
    assert dup.stored == 0 and len(dup.refused) == 1  # one live intent per grant
    ev = out.event_ids[0]
    assert cvstore.get_application(app.id).status == "draft"  # nothing is approved until the Mac executes

    rep = mac.sync_one(pid)
    assert rep.ok and rep.executed == 1 and not engine and not run_spy
    approved = cvstore.get_application(app.id)
    assert approved.approved_by == "user" and approved.approved_rev == app.current_rev
    assert seeker.statuses()[ev] == "done" and "Accord enregistré" in seeker.messages()[ev]
    snap = srv_snap(w)
    assert not snap.awaiting and not snap.sent and live.pending(w.key) == []
    assert mac.journal.get_grant(aw.grant).state == "used" and mac.journal.sends_today(pid, mac.now) == 0
    assert seeker.request(aw.grant).stored == 0

    forged = live.forge_event(w.key, IntentEvent, grant=aw.grant)
    assert mac.sync_one(pid).ok
    assert seeker.statuses()[forged.event_id] == "refused" and not apply_store.list_runs(app.id)
    assert not engine and not run_spy

    monkeypatch.setattr(get_settings(), "apply_auto", True)
    monkeypatch.setattr(get_settings(), "apply_auto_min_score", 60)
    summary = asyncio.run(service.daily_apply(pid, emit=lambda _: None))
    assert summary.sent == 1 and len(engine) == len(run_spy) == 1
    assert run_spy[0]["trigger"] == "auto" and run_spy[0]["approved_rev"] == app.current_rev
    assert mac.sync_one(pid).ok and len(srv_snap(w).sent) == 1


@pytest.mark.parametrize("change", ["rev", "pdf", "origin"])
def test_09_binding_drift_after_the_approval_is_refused_and_re_offered(w, profile, master, tmp_path, engine, change):
    from apply_cv_helpers import make_app

    live, mac, pid, seeker = w.live, w.mac, w.profile_id, w.seeker
    job, app = new_app(profile, master, tmp_path, 1)
    mac.sync_one(pid, execute=False)
    (aw,) = awaiting(w)
    out = seeker.request(aw.grant)
    if change == "rev":
        (tmp_path / "rev2").mkdir()
        make_app(profile, job, tmp_path / "rev2", master_hash=master.content_hash)
        assert cvstore.get_application(app.id).current_rev == 2
    elif change == "pdf":
        set_pdf(cvstore.get_application(app.id), b"%PDF-1.4 other bytes")
    else:
        with db.connect() as c:
            c.execute("UPDATE jobs SET origin_url=? WHERE id=?", ("https://careers.exemple.test/jobs/elsewhere", job.id))
    rep = mac.sync_one(pid)
    assert rep.ok and engine == [] and apply_store.list_runs(app.id) == []  # no run row at all
    assert seeker.statuses()[out.event_ids[0]] == "refused" and "a changé" in seeker.messages()[out.event_ids[0]]
    assert mac.journal.get_grant(aw.grant).state == "void" and mac.journal.sends_today(pid, mac.now) == 0
    assert cvstore.get_application(app.id).status != "submitted"
    (aw2,) = awaiting(w)  # re-offered with a NEW grant
    assert aw2.grant != aw.grant and mac.journal.get_grant(aw2.grant).state == "offered"
    assert seeker.request(aw.grant).stored == 0  # the old confirmation page is dead on the server too
    forged = live.forge_event(w.key, IntentEvent, grant=aw.grant)
    mac.sync_one(pid)
    assert seeker.statuses()[forged.event_id] == "refused" and engine == []
    # the new grant works
    out2 = seeker.request(aw2.grant)
    mac.sync_one(pid)
    assert seeker.statuses()[out2.event_ids[0]] == "done" and not engine


# ================================================================ 10. locks, spacing, budget
def two_apps(w, profile, master, tmp_path, n=2):
    apps = [new_app(profile, master, tmp_path, i)[1] for i in range(1, n + 1)]
    w.mac.sync_one(w.profile_id, execute=False)
    by_company = {a.company: a.grant for a in awaiting(w)}
    return apps, [by_company[f"Société {i}"] for i in range(1, n + 1)]


def test_10_send_lock_held_by_another_process_leaves_the_intent_queued(w, profile, master, tmp_path, engine):
    mac, pid, seeker = w.mac, w.profile_id, w.seeker
    (app,), (g,) = two_apps(w, profile, master, tmp_path, 1)
    ev = seeker.request(g).event_ids[0]
    with service.send_lock():  # "another process"
        rep = mac.sync_one(pid)
    assert rep.ok and rep.executed == 0 and "autre envoi" in rep.message and engine == []
    assert apply_store.list_runs(app.id) == [] and mac.journal.get_event(ev).state == "queued"
    assert mac.journal.get_grant(g).state == "offered" and mac.journal.sends_today(pid, mac.now) == 0
    assert seeker.statuses()[ev] == "queued" and w.live.pending(w.key) == []  # custody taken, status visible
    rep = mac.sync_one(pid)
    assert rep.executed == 1 and not engine and seeker.statuses()[ev] == "done"


def test_10_queued_intent_requested_twice_approves_once(w, profile, master, tmp_path, engine):
    mac, pid, seeker = w.mac, w.profile_id, w.seeker
    (app,), (g,) = two_apps(w, profile, master, tmp_path, 1)
    first = seeker.request(g).event_ids[0]
    with service.send_lock():
        mac.sync_one(pid)
    again = seeker.request(g)
    assert again.stored == 0
    mac.sync_one(pid)
    st = seeker.statuses()
    assert st[first] == "done" and not engine and not apply_store.list_runs(app.id)


def test_10_daily_run_holding_its_lock_blocks_the_batch(w, profile, master, tmp_path, engine):
    from jobhunt.config import get_settings

    mac, pid, seeker = w.mac, w.profile_id, w.seeker
    (app,), (g,) = two_apps(w, profile, master, tmp_path, 1)
    ev = seeker.request(g).event_ids[0]
    with daily.daily_lock(get_settings().data_dir):
        assert schedule.is_running(data_dir=get_settings().data_dir)
        rep = mac.sync_one(pid)
    assert rep.ok and rep.executed == 0 and engine == [] and apply_store.list_runs(app.id) == []
    assert mac.journal.get_event(ev).state == "queued" and mac.journal.get_grant(g).state == "offered"
    rep = mac.sync_one(pid)
    assert rep.executed == 1 and seeker.statuses()[ev] == "done"


# ================================================================ 11. unconfirmed sends
def unconfirmed_setup(w, profile, master, tmp_path):
    job, app = new_app(profile, master, tmp_path, 1, approve=True)
    make_outstanding(app, job)
    w.mac.sync_one(w.profile_id, execute=False)
    (u,) = srv_snap(w).unconfirmed
    return app, u


def test_11_unconfirmed_sent_voids_the_sibling_grant(w, profile, master, tmp_path, engine):
    mac, pid, seeker = w.mac, w.profile_id, w.seeker
    app, u = unconfirmed_setup(w, profile, master, tmp_path)
    assert cvstore.get_application(app.id).status == "failed" and u.grant_sent != u.grant_not_sent
    out = seeker.request(u.grant_sent)
    assert out.stored == 1
    assert seeker.request(u.grant_not_sent).stored == 0  # the server refuses both at once (sibling has a live intent)
    rep = mac.sync_one(pid)
    ev = out.event_ids[0]
    assert seeker.statuses()[ev] == "done" and cvstore.get_application(app.id).status == "submitted" and engine == []
    assert srv_snap(w).unconfirmed == [] and [s.company for s in srv_snap(w).sent] == ["Société 1"]
    assert mac.journal.get_grant(u.grant_sent).state == "used" and mac.journal.get_grant(u.grant_not_sent).state == "void"
    forged = w.live.forge_event(w.key, IntentEvent, grant=u.grant_not_sent)
    mac.sync_one(pid)
    assert seeker.statuses()[forged.event_id] == "refused" and cvstore.get_application(app.id).status == "submitted"
    assert seeker.request(u.grant_not_sent).stored == 0


def test_11_unconfirmed_not_sent_goes_back_to_the_queue(w, profile, master, tmp_path, engine):
    mac, pid, seeker = w.mac, w.profile_id, w.seeker
    app, u = unconfirmed_setup(w, profile, master, tmp_path)
    out = seeker.request(u.grant_not_sent)
    mac.sync_one(pid)
    assert seeker.statuses()[out.event_ids[0]] == "done" and "manuelle uniquement" in seeker.messages()[out.event_ids[0]]
    assert cvstore.get_application(app.id).status in ("draft", "approved") and apply_store.outstanding_attempt(app.id) is None
    assert mac.journal.get_grant(u.grant_sent).state == "void" and engine == []
    snap = srv_snap(w)
    (item,) = service.queue(pid)
    assert item.bucket == "to_validate" and item.outstanding is None and item.selectable
    assert apply_store.requires_manual_retry(app.id)
    snap = srv_snap(w)
    assert snap.unconfirmed == [] and snap.awaiting == []
    assert seeker.request(u.grant_sent).stored == 0
    forged = w.live.forge_event(w.key, IntentEvent, grant=u.grant_sent)
    mac.sync_one(pid)
    assert seeker.statuses()[forged.event_id] == "refused" and cvstore.get_application(app.id).status != "submitted"


# ================================================================ 12. e-mail pause and auto-resume
def test_12_answering_the_last_question_does_not_resume_a_paused_submit_run(w, profile, master, tmp_path, engine):
    mac, pid, seeker = w.mac, w.profile_id, w.seeker
    _, app = new_app(profile, master, tmp_path, 1, approve=True)
    make_run(app, tmp_path, status="needs_input", questions=[Question(label="Ville ?")])
    mac.sync_one(pid, execute=False)
    (q,) = srv_snap(w).questions
    out = seeker.answer_question(q.qid, "Lyon")
    rep = mac.sync_one(pid)
    assert rep.executed == 0 and not engine and cvstore.get_application(app.id).status == "approved"
    assert seeker.statuses()[out.event_ids[0]] == "applied" and mac.journal.sends_today(pid, mac.now) == 0
    assert not srv_snap(w).questions and not srv_snap(w).sent


def test_12_no_auto_resume_in_review_mode(w, profile, master, tmp_path, engine):
    mac, pid, seeker = w.mac, w.profile_id, w.seeker
    manage.set_mode(pid, "review")
    _, app = new_app(profile, master, tmp_path, 1, approve=True)
    make_run(app, tmp_path, status="needs_input", questions=[Question(label="Ville ?")])
    mac.sync_one(pid, execute=False)
    (q,) = srv_snap(w).questions
    assert srv_snap(w).review_mode
    out = seeker.answer_question(q.qid, "Lyon")
    mac.sync_one(pid)
    assert engine == [] and seeker.statuses()[out.event_ids[0]] == "awaiting_review"
    assert facts.get_answer(facts.load_answers(pid), "city") is None
    manage.review_apply(pid)  # the operator approves the answer: it is saved, and still nothing is started by the portal pass
    mac.sync_one(pid)
    assert facts.get_answer(facts.load_answers(pid), "city") == "Lyon"
    assert engine == [] and seeker.statuses()[out.event_ids[0]] == "applied"


# ================================================================ 13. cancel
def test_13_cancel_before_the_pass_nothing_runs(w, profile, master, tmp_path, engine):
    mac, pid, seeker = w.mac, w.profile_id, w.seeker
    (app,), (g,) = two_apps(w, profile, master, tmp_path, 1)
    out = seeker.request(g)
    ev = out.event_ids[0]
    c = seeker.cancel(ev)
    assert c.stored == 1 and [e.kind for e in w.live.pending(w.key)] == ["intent", "cancel"]
    rep = mac.sync_one(pid)
    assert rep.ok and engine == [] and apply_store.list_runs(app.id) == []
    st = seeker.statuses()
    assert st[ev] == "cancelled" and st[c.event_ids[0]] == "done"
    assert mac.journal.get_grant(g).state == "offered"  # cancelling does not burn the grant: he can decide again
    again = seeker.request(g)
    assert again.stored == 1
    mac.sync_one(pid)
    assert seeker.statuses()[again.event_ids[0]] == "done" and not engine


def test_13_cancel_after_execution_is_too_late(w, profile, master, tmp_path, engine):
    mac, pid, seeker = w.mac, w.profile_id, w.seeker
    (app,), (g,) = two_apps(w, profile, master, tmp_path, 1)
    ev = seeker.request(g).event_ids[0]
    mac.sync_one(pid)
    assert seeker.statuses()[ev] == "done"
    late = seeker.cancel(ev)
    assert late.stored == 0 and "Trop tard" in " ".join(late.messages())
    # a cancel forged into the inbox for a finished intent is refused by the Mac as well
    forged = w.live.forge_event(w.key, CancelEvent, target=ev)
    mac.sync_one(pid)
    assert seeker.statuses()[forged.event_id] == "refused" and "Trop tard" in seeker.messages()[forged.event_id]
    assert seeker.statuses()[ev] == "done" and not engine


def test_13_cancel_of_a_queued_intent(w, profile, master, tmp_path, engine):
    mac, pid, seeker = w.mac, w.profile_id, w.seeker
    (app,), (g,) = two_apps(w, profile, master, tmp_path, 1)
    ev = seeker.request(g).event_ids[0]
    with service.send_lock():
        mac.sync_one(pid)
    assert mac.journal.get_event(ev).state == "queued"
    assert seeker.request(g).stored == 0
    cancellation = seeker.cancel(ev)
    assert cancellation.stored == 1
    assert seeker.cancel(ev).stored == 0
    assert seeker.request(g).stored == 0
    mac.sync_one(pid)
    st = seeker.statuses()
    assert st[ev] == "cancelled" and st[cancellation.event_ids[0]] == "done" and engine == []


# ================================================================ 14. offline
def snapshot_of_mac_state(w: Setup):
    row = w.mac.journal.get_profile(w.profile_id)
    return (row.cursor_seq, row.snapshot_rev, row.last_sync_at, row.stream_id, row.epoch, len(w.mac.journal.events(w.profile_id)),
            answers_bytes(w.profile_id))


def test_14_server_down_reports_an_error_and_changes_nothing_locally(w):
    live, mac, pid, seeker = w.live, w.mac, w.profile_id, w.seeker
    mac.sync_one(pid)
    out = seeker.edit("city", "Lyon")
    before = snapshot_of_mac_state(w)
    live.stop()
    assert not live.running
    rep = mac.sync_one(pid)
    assert not rep.ok and "injoignable" in rep.error
    row = mac.journal.get_profile(pid)
    assert row.last_error and "injoignable" in row.last_error
    assert snapshot_of_mac_state(w) == before and mac.journal.get_profile(pid).enabled
    reports = mac.sync()  # the whole pass, all profiles
    assert [r.ok for r in reports] == [False]
    live.start()
    rep = mac.sync_one(pid)  # same port, same database file
    assert rep.ok and stored(pid, "city") == "Lyon" and mac.journal.get_profile(pid).last_error is None
    assert seeker.alive() and seeker.statuses()[out.event_ids[0]] == "applied"  # sessions survive a restart


def test_14_mac_off_for_days_thirty_events_apply_in_order_in_one_pass(w):
    mac, pid, seeker = w.mac, w.profile_id, w.seeker
    mac.sync_one(pid)
    paths = ["first_name", "last_name", "birth_place", "nationality", "address", "postal_code", "city", "department", "country",
             "stage.duration", "alternance.duration", "alternance.rhythm", "notice_period", "current_salary", "mobility"]
    ids = []
    for rnd in (1, 2):
        for p in paths:
            out = seeker.edit(p, f"{p}-{rnd}")
            assert out.stored == 1
            ids.append(out.event_ids[0])
    assert len(w.live.pending(w.key)) == 30
    rep = mac.sync_one(pid, step=timedelta(days=5))  # the Mac was off for days
    assert rep.ok and rep.pulled == 30 and rep.applied.states == {"applied": 30}
    assert [stored(pid, p) for p in paths] == [f"{p}-2" for p in paths]  # the later edit of each field won, in seq order
    assert [mac.journal.get_event(e).seq for e in ids] == list(range(1, 31))
    assert {seeker.statuses()[e] for e in ids} == {"applied"} and w.live.pending(w.key) == []
    assert mac.journal.get_profile(pid).cursor_seq == 30 == w.live.profile_row(w.key)["acked_seq"]
    # nothing was lost to the 100-result cap here, and the snapshot shows the final values
    assert srv_snap(w).answers["city"].value == "city-2"


def test_14_inbox_cap_refuses_further_input_and_the_mac_drains_in_pages_of_200(w, profile, master, tmp_path):
    live, mac, pid, seeker = w.live, w.mac, w.profile_id, w.seeker
    (app,), (g,) = two_apps(w, profile, master, tmp_path, 1)
    base = srv_ver(w, "city")
    live.forge_answers(w.key, protocol.MAX_PENDING_EVENTS, "city", base)
    assert len(live.pending(w.key)) == protocol.MAX_PENDING_EVENTS
    v = seeker.view()
    assert v.inbox_full
    for out in (seeker.edit("postal_code", "69001"), seeker.request(g), seeker.answer_question("a" * 16, "x")):
        assert out.stored == 0 and not out.ok and (out.inbox_full or out.problems)
    assert seeker.edit("postal_code", "69001").inbox_full and ops.MSG_FULL in seeker.edit("postal_code", "69001").messages()
    assert len(live.pending(w.key)) == protocol.MAX_PENDING_EVENTS  # nothing was stored
    rep = mac.sync_one(pid, execute=False)
    assert rep.ok and rep.pulled == protocol.MAX_PENDING_EVENTS
    assert live.profile_row(w.key)["acked_seq"] == mac.journal.get_profile(pid).cursor_seq == protocol.MAX_PENDING_EVENTS
    assert live.pending(w.key) == [] and stored(pid, "city") == "valeur-499"
    assert mac.journal.event_counts(pid) == {"applied": 500}
    assert len(w.live.stored_results(w.key)) == protocol.MAX_RESULTS  # only the latest 100 outcomes are kept
    assert not seeker.view().inbox_full and seeker.edit("postal_code", "69001").stored == 1  # input is accepted again


# ================================================================ 15. crash safety
class Counting:
    """Counts the real writes of save_answers."""

    def __init__(self, monkeypatch):
        self.n = 0
        real = facts.save_answers

        def spy(pid, values):
            self.n += 1
            return real(pid, values)

        monkeypatch.setattr(facts, "save_answers", spy)


def test_15_kill_after_journaling_the_page_before_applying(w, monkeypatch):
    live, mac, pid, seeker = w.live, w.mac, w.profile_id, w.seeker
    mac.sync_one(pid)
    ev = seeker.edit("city", "Lyon").event_ids[0]
    writes = Counting(monkeypatch)
    real = apply_events.apply_pending

    def die(*a, **k):
        raise Kill

    monkeypatch.setattr(apply_events, "apply_pending", die)
    with pytest.raises(Kill):
        mac.sync_one(pid)
    j = mac.journal
    assert j.get_event(ev).state == "received" and j.get_profile(pid).cursor_seq == 1 and stored(pid, "city") == ""
    assert len(live.pending(w.key)) == 1  # custody taken, not acked yet
    from jobhunt.portal.journal import pass_lock
    with pass_lock("sync.lock") as got:
        assert got  # the dying pass released the file lock
    monkeypatch.setattr(apply_events, "apply_pending", real)
    rep = mac.sync_one(pid)
    assert rep.ok and rep.pulled == 0 and stored(pid, "city") == "Lyon" and writes.n == 1
    assert seeker.statuses()[ev] == "applied" and live.pending(w.key) == []
    mac.sync_one(pid)
    assert writes.n == 1


def test_15_kill_after_save_answers_before_the_journal_records_it(w, monkeypatch):
    live, mac, pid, seeker = w.live, w.mac, w.profile_id, w.seeker
    mac.sync_one(pid)
    ev = seeker.edit("city", "Lyon").event_ids[0]
    writes = Counting(monkeypatch)
    real = Journal.finalize_write

    def die(self, *a, **k):
        raise Kill

    monkeypatch.setattr(Journal, "finalize_write", die)
    with pytest.raises(Kill):
        mac.sync_one(pid)
    j = mac.journal
    assert stored(pid, "city") == "Lyon" and writes.n == 1  # the file was written ...
    e = j.get_event(ev)
    assert e.state == "received" and e.write_ver and e.ver_after is None  # ... the outcome was not
    assert len(live.pending(w.key)) == 1
    monkeypatch.setattr(Journal, "finalize_write", real)
    rep = mac.sync_one(pid)
    assert rep.ok and writes.n == 1  # recognised as its own write: applied once
    e = j.get_event(ev)
    assert e.state == "applied" and e.ver_after == e.write_ver == srv_ver(w, "city")
    assert seeker.statuses()[ev] == "applied" and live.pending(w.key) == []


@pytest.mark.parametrize("after_approval", [False, True])
def test_15_kill_after_claim_never_reprocesses_approval(w, profile, master, tmp_path, engine, monkeypatch, after_approval):
    mac, pid, seeker = w.mac, w.profile_id, w.seeker
    (app,), (grant,) = two_apps(w, profile, master, tmp_path, 1)
    event = seeker.request(grant).event_ids[0]
    approve = cvs.approve

    def dying(*args, **kwargs):
        if after_approval:
            approve(*args, **kwargs)
        raise Kill

    monkeypatch.setattr(cvs, "approve", dying)
    with pytest.raises(Kill):
        mac.sync_one(pid)
    journal = mac.journal
    assert journal.get_event(event).state == "running" and journal.get_grant(grant).state == "used"
    assert journal.sends_today(pid, mac.now) == 0 and w.live.pending(w.key) == []
    with service.send_lock():
        pass
    for _ in range(3):
        report = mac.sync_one(pid)
        assert report.ok and report.executed == 0
    assert seeker.statuses()[event] == "interrupted" and not engine and not apply_store.list_runs(app.id)
    assert cvstore.get_application(app.id).status == ("approved" if after_approval else "draft")

def test_15_kill_after_applying_before_the_snapshot_push(w, monkeypatch):
    live, mac, pid, seeker = w.live, w.mac, w.profile_id, w.seeker
    mac.sync_one(pid)
    ev = seeker.edit("city", "Lyon").event_ids[0]
    writes = Counting(monkeypatch)
    real = sync._push

    def die(*a, **k):
        raise Kill

    monkeypatch.setattr(sync, "_push", die)
    with pytest.raises(Kill):
        mac.sync_one(pid)
    assert mac.journal.get_event(ev).state == "applied" and writes.n == 1
    assert len(live.pending(w.key)) == 1 and live.profile_row(w.key)["acked_seq"] == 0  # the server still holds it, unacked
    assert seeker.statuses().get(ev) is None
    monkeypatch.setattr(sync, "_push", real)
    rep = mac.sync_one(pid)
    assert rep.ok and rep.pulled == 0 and writes.n == 1
    assert live.pending(w.key) == [] and seeker.statuses()[ev] == "applied" and srv_snap(w).answers["city"].value == "Lyon"


def test_15_kill_after_the_server_stored_the_snapshot_before_the_mac_recorded_it(w, monkeypatch):
    live, mac, pid, seeker = w.live, w.mac, w.profile_id, w.seeker
    mac.sync_one(pid)
    ev = seeker.edit("city", "Lyon").event_ids[0]
    real = Journal.update_profile
    state = {"n": 0}

    def flaky(self, profile_id, **kw):
        if "snapshot_rev" in kw and state["n"] == 0:
            state["n"] += 1
            raise Kill
        return real(self, profile_id, **kw)

    monkeypatch.setattr(Journal, "update_profile", flaky)
    with pytest.raises(Kill):
        mac.sync_one(pid)
    assert live.profile_row(w.key)["snapshot_rev"] == 2 and mac.journal.get_profile(pid).snapshot_rev == 1
    assert live.pending(w.key) == [] and seeker.statuses()[ev] == "applied"
    rep = mac.sync_one(pid)  # the server says 409 rev: the client adopts current + 1
    assert rep.ok and live.profile_row(w.key)["snapshot_rev"] == 3 == mac.journal.get_profile(pid).snapshot_rev


# ================================================================ 16. replay and ordering over HTTP
def test_16_snapshot_replay_and_ordering_rules(w):
    live, mac, pid, seeker = w.live, w.mac, w.profile_id, w.seeker
    mac.sync_one(pid)
    seeker.edit("city", "Lyon")
    mac.sync_one(pid)
    key, token = w.key, mac.token()
    http = httpx.Client(base_url=live.base_url + protocol.API_PREFIX, headers={"Authorization": f"Bearer {token}"}, trust_env=False)

    def state():
        r = live.profile_row(key)
        return (r["snapshot_rev"], r["acked_seq"], r["last_seq"], r["snapshot_digest"], srv_snap(w).model_dump_json(), len(live.pending(key)))

    row = live.profile_row(key)
    rev, stream, cur = row["snapshot_rev"], row["stream_id"], srv_snap(w)
    assert (rev, row["acked_seq"], row["last_seq"]) == (3 - 1, 1, 1)

    def push(**kw):
        body = {"stream_id": stream, "sessions_epoch": row["sessions_epoch"], "revoked_through_seq": row["revoked_through_seq"],
                "rev": rev + 1, "acked_seq": 1, "results": [], "snapshot": cur.model_dump(mode="json")}
        body.update(kw)
        return http.put(f"/profiles/{key}/snapshot", json=body)

    start = state()
    r = push(rev=rev - 1)  # an old snapshot (lower rev)
    assert r.status_code == 409 and r.json() == {"error": "rev", "current": rev} and state() == start
    r = push(rev=rev)  # same rev, different body (not what is stored)
    assert r.status_code == 409 and state() == start
    r = push(rev=rev + 1, acked_seq=2)  # beyond last_seq
    assert r.status_code == 409 and r.json()["error"] == "ack" and state() == start
    r = push(rev=rev + 1, acked_seq=0)  # going backwards
    assert r.status_code == 409 and r.json() == {"error": "ack", "current": 1} and state() == start
    r = push(rev=rev + 1, stream_id=protocol.new_id())  # wrong stream
    assert r.status_code == 409 and r.json()["error"] == "stream" and state() == start
    r = push(rev=rev + 1, snapshot={**cur.model_dump(mode="json"), "display_name": "Autre"}, results=[])  # a valid new revision
    assert r.status_code == 200 and r.json()["rev"] == rev + 1
    after_ok = state()
    assert after_ok[0] == rev + 1 and srv_snap(w).display_name == "Autre"
    mine = {"snapshot": {**cur.model_dump(mode="json"), "display_name": "Autre"}}
    r = push(rev=rev + 1, **mine)  # a retry of the same body: 200, no change
    assert r.status_code == 200 and state() == after_ok
    r = push(rev=rev + 1, snapshot={**cur.model_dump(mode="json"), "display_name": "Encore autre"})  # same rev, other body
    assert r.status_code == 409 and state() == after_ok
    assert push(rev=rev + 1, acked_seq=1, results=[], **mine).status_code == 200  # the digest includes everything: still identical
    assert http.get(f"/profiles/{key}/inbox?after=0").json()["events"] == []
    # unknown / malformed input
    assert http.put(f"/profiles/{key}/snapshot", content=b"{}", headers={"Content-Type": "application/json"}).status_code == 422
    assert http.put(f"/profiles/{key}/snapshot", content=b"{}", headers={"Content-Type": "text/plain"}).status_code == 415
    # the Mac's next pass recovers by itself (409 rev -> adopt current + 1) and restores its own view
    rep = mac.sync_one(pid)
    assert rep.ok and srv_snap(w).display_name == "Léa" and live.profile_row(key)["snapshot_rev"] == rev + 2
    http.close()


# ================================================================ 17. streams
def test_17_resync_drops_pending_events_as_lost_and_old_grants_are_refused(w, profile, master, tmp_path, engine):
    live, mac, pid, seeker = w.live, w.mac, w.profile_id, w.seeker
    (app,), (g,) = two_apps(w, profile, master, tmp_path, 1)
    old_stream = live.profile_row(w.key)["stream_id"]
    e_ans = seeker.edit("city", "Lyon").event_ids[0]
    e_int = seeker.request(g).event_ids[0]
    assert len(live.pending(w.key)) == 2
    manage.resync(pid)
    row = live.profile_row(w.key)
    j = mac.journal.get_profile(pid)
    assert row["stream_id"] == j.stream_id != old_stream and row["last_seq"] == 0 == row["acked_seq"] == j.cursor_seq
    assert live.pending(w.key) == []
    st = seeker.statuses()
    assert st[e_ans] == "lost" and st[e_int] == "lost" and "à refaire" in seeker.messages()[e_int]
    assert mac.journal.get_grant(g).state == "void"
    stale = seeker.request(g)
    assert not seeker.view().has_snapshot and stale.stored == 0
    rep = mac.sync_one(pid)
    assert rep.ok and engine == []
    assert seeker.statuses()[e_ans] == "lost"  # the server's own `lost` results survive the Mac's next results
    (aw,) = awaiting(w)
    assert aw.grant != g
    # new work flows on the new stream (seq restarted at 0)
    new = seeker.edit("city", "Nice")
    assert [e.seq for e in live.pending(w.key)] == [1]
    mac.sync_one(pid)
    assert stored(pid, "city") == "Nice" and seeker.statuses()[new.event_ids[0]] == "applied"
    out = seeker.request(aw.grant)
    mac.sync_one(pid)
    assert seeker.statuses()[out.event_ids[0]] == "done" and not engine


def test_17_server_restored_from_an_older_copy_fails_closed_until_resync(w, tmp_path):
    live, mac, pid, seeker = w.live, w.mac, w.profile_id, w.seeker
    mac.sync_one(pid)
    backup = tmp_path / "backup" / "portal.sqlite3"
    live.backup_to(backup)
    seeker.edit("city", "Lyon")
    seeker.edit("postal_code", "69001")
    mac.sync_one(pid)
    assert mac.journal.get_profile(pid).cursor_seq == 2 and stored(pid, "city") == "Lyon"
    live.restore_from(backup)
    assert live.profile_row(w.key)["last_seq"] == 0
    seeker.edit("department", "Rhône")  # lands in the restored (older) database, seq 1
    file_before, j_before = answers_bytes(pid), len(mac.journal.events(pid))
    for _ in range(2):
        rep = mac.sync_one(pid)
        assert not rep.ok and rep.error == sync.STREAM_MSG
        assert mac.journal.get_profile(pid).last_error == sync.STREAM_MSG
        assert answers_bytes(pid) == file_before and len(mac.journal.events(pid)) == j_before and stored(pid, "department") == ""
        assert live.profile_row(w.key)["snapshot_rev"] == 1  # nothing was pushed either
    # the operator's procedure: hold, resync, release
    manage.resync(pid)
    rep = mac.sync_one(pid)
    assert rep.ok and mac.journal.get_profile(pid).last_error is None
    new = seeker.edit("department", "Isère")
    mac.sync_one(pid)
    assert stored(pid, "department") == "Isère" and seeker.statuses()[new.event_ids[0]] == "applied"


def test_17_restore_is_detected_even_once_seq_catches_up(w, tmp_path):
    live, mac, pid, seeker = w.live, w.mac, w.profile_id, w.seeker
    mac.sync_one(pid)
    backup = tmp_path / "backup" / "portal.sqlite3"
    live.backup_to(backup)
    seeker.edit("city", "Lyon")
    mac.sync_one(pid)  # cursor 1
    live.restore_from(backup)
    first = seeker.edit("department", "Rhône").event_ids[0]
    second = seeker.edit("country", "France").event_ids[0]
    rep = mac.sync_one(pid)
    assert not rep.ok and rep.error == sync.STREAM_MSG and rep.pulled == 0
    assert mac.journal.get_event(first) is None and mac.journal.get_event(second) is None
    assert stored(pid, "department") == "" and stored(pid, "country") == ""
    assert [e.event_id for e in live.pending(w.key)] == [first, second]


# ================================================================ 18. scope
def test_18_a_second_device_cannot_touch_the_first_devices_profile(w, profile, master, tmp_path):
    live, mac, pid, seeker = w.live, w.mac, w.profile_id, w.seeker
    new_app(profile, master, tmp_path, 1)
    mac.sync_one(pid, execute=False)
    key = w.key
    (aw,) = awaiting(w)
    pdf = cvs.safe_pdf(cvstore.get_application(1)).read_bytes()
    row = live.profile_row(key)
    seeker.edit("city", "Lyon")
    first_state = (row["config_rev"], row["handle_sha256"], bytes(row["passcode_hash"]), srv_snap(w).model_dump_json(), len(live.pending(key)))
    second_code = "second-pairing-" + protocol.new_token()
    live.restart(PORTAL_MAX_DEVICES="2", PORTAL_PAIRING_CODE=second_code)
    token2 = protocol.new_token()
    with PortalClient(live.base_url) as anon:
        dev2 = anon.pair(second_code, "Autre Mac", protocol.sha256_hex(token2)).device_id
    assert dev2 != Journal().get_setting("device_id")
    snap = srv_snap(w)
    push = protocol.SnapshotPush(stream_id=row["stream_id"], sessions_epoch=row["sessions_epoch"],
                                 revoked_through_seq=row["revoked_through_seq"], rev=99, acked_seq=0, snapshot=snap)
    cfg = protocol.ProfileConfig(config_rev=50, enabled=False, handle_sha256=row["handle_sha256"], sessions_epoch=9, stream_id=protocol.new_id(),
                                 passcode=None)
    with PortalClient(live.base_url, token2) as c2:
        assert c2.status().profiles == []
        for call in (lambda: c2.get_inbox(key, 0), lambda: c2.put_snapshot(key, push), lambda: c2.put_document(key, aw.doc_id, pdf),
                     lambda: c2.delete_profile(key)):
            with pytest.raises(PortalError) as e:
                call()
            assert e.value.status == 404 and e.value.code == "not_found"
        # nor can it take the profile over through its configuration (the identifiant is bound to the first device)
        with pytest.raises(PortalError) as e:
            c2.put_profile(key, cfg)
        assert e.value.status == 404 and e.value.code == "not_found"
        own = protocol.ProfileConfig(config_rev=1, enabled=True, handle_sha256=protocol.handle_digest("secondhandle"), stream_id=protocol.new_id(),
                                     sessions_epoch=0, passcode=protocol.PasscodeVerifier(salt="00" * 16, hash="11" * 32))
        with pytest.raises(PortalError) as e:
            c2.put_profile(key, own)
        assert e.value.status == 404 and e.value.code == "not_found"
        own_key = protocol.new_id()
        c2.put_profile(own_key, own)
        assert [p.profile_key for p in c2.status().profiles] == [own_key]
        with pytest.raises(PortalError) as e:
            c2.get_inbox("f" * 32, 0)
        assert e.value.status == 404
    row2 = live.conn.execute("SELECT * FROM profiles WHERE profile_key=? AND device_id=?", (key, mac.journal.get_setting("device_id"))).fetchone()
    assert (row2["config_rev"], row2["handle_sha256"], bytes(row2["passcode_hash"]), len(live.pending(key))) == (
        first_state[0], first_state[1], first_state[2], first_state[4])
    assert srv_snap(w).model_dump_json() == first_state[3] and seeker.document(aw.doc_id) == pdf and seeker.alive()
    other = Seeker(live, "secondhandle", "whatever-code")
    assert other.login().kind == "refused"  # (its verifier is a dummy) and never the first profile's data
    assert mac.sync_one(pid).ok and stored(pid, "city") == "Lyon"
    # the first device keeps working while a revoked device does not
    admin.revoke_device(live.portal.db, dev2[:8])
    with PortalClient(live.base_url, token2) as c2, pytest.raises(PortalError) as e:
        c2.status()
    assert e.value.status == 401
    assert mac.sync_one(pid).ok


def test_18_revoked_device_the_mac_reports_it_and_applies_nothing(w):
    live, mac, pid, seeker = w.live, w.mac, w.profile_id, w.seeker
    mac.sync_one(pid)
    seeker.edit("city", "Lyon")
    admin.revoke_device(live.portal.db, Journal().get_setting("device_id"))
    rep = mac.sync_one(pid)
    assert not rep.ok and "refuse cet appareil" in rep.error and mac.journal.get_profile(pid).last_error
    assert stored(pid, "city") == "" and mac.journal.get_profile(pid).cursor_seq == 0
    assert len(live.pending(w.key)) == 1  # the event waits (the server did not lose it)


def test_18_admin_hold_kills_sessions_blocks_login_the_mac_starts_nothing_release_restores(w, profile, master, tmp_path, engine):
    live, mac, pid, seeker = w.live, w.mac, w.profile_id, w.seeker
    (app,), (g,) = two_apps(w, profile, master, tmp_path, 1)
    ev = seeker.request(g).event_ids[0]
    mac.sync_one(pid, execute=False)
    assert mac.journal.get_event(ev).state == "queued"
    wrong = live.login(w.creds.handle, "wrongcode1234")
    admin.hold(live.portal.db, w.key[:10])
    assert not seeker.alive() and live.conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0
    held_login = live.login(w.creds.handle, w.creds.passcode)
    assert held_login == wrong and held_login.kind == "refused" and held_login.session_id is None  # like any wrong code
    rep = mac.sync_one(pid)
    assert rep.ok and mac.journal.get_profile(pid).held and manage.status().profiles[0].held
    assert rep.executed == 0 and engine == [] and apply_store.list_runs(app.id) == [] and mac.journal.get_event(ev).state == "queued"
    mac.sync_one(pid)
    assert engine == [] and mac.journal.get_profile(pid).held
    admin.release(live.portal.db, w.key[:10])
    assert not seeker.alive() and seeker.login().kind == "ok"  # the sessions did not come back by themselves
    rep = mac.sync_one(pid)
    assert not mac.journal.get_profile(pid).held and rep.executed == 1 and not engine and seeker.statuses()[ev] == "done"


def test_18_no_write_of_the_mac_clears_an_admin_hold(w):
    live, mac, pid = w.live, w.mac, w.profile_id
    mac.sync_one(pid)
    admin.hold(live.portal.db, w.key)
    manage.revoke(pid)
    manage.rotate_passcode(pid)
    manage.resync(pid)
    manage.set_mode(pid, "review")
    assert live.profile_row(w.key)["held"] == 1
    assert mac.sync_one(pid).ok and mac.journal.get_profile(pid).held
    assert live.profile_row(w.key)["held"] == 1


def test_18_events_that_arrive_while_held_wait_and_apply_after_the_release(w):
    live, mac, pid, seeker = w.live, w.mac, w.profile_id, w.seeker
    mac.sync_one(pid)
    out = seeker.edit("city", "Lyon")
    admin.hold(live.portal.db, w.key)
    mac.sync_one(pid)
    assert mac.journal.get_event(out.event_ids[0]).state == "received" and stored(pid, "city") == ""
    admin.release(live.portal.db, w.key)
    mac.sync_one(pid)
    assert stored(pid, "city") == "Lyon" and seeker.alive() is False
    seeker.login()
    assert seeker.statuses()[out.event_ids[0]] == "applied"


# ================================================================ 19. review mode
def test_19_review_mode_holds_everything_until_the_operator_decides(profile, master, live, tmp_path, engine):
    w = _setup(live, profile, master, review=True)
    mac, pid, seeker = w.mac, w.profile_id, w.seeker
    (app,), (g,) = two_apps(w, profile, master, tmp_path, 1)
    assert srv_snap(w).review_mode
    file_before = answers_bytes(pid)
    e_city = seeker.edit("city", "Lyon").event_ids[0]
    e_post = seeker.edit("postal_code", "69001").event_ids[0]
    e_int = seeker.request(g).event_ids[0]
    rep = mac.sync_one(pid)
    assert rep.ok and rep.executed == 0 and engine == [] and answers_bytes(pid) == file_before
    st = seeker.statuses()
    assert {st[e] for e in (e_city, e_post, e_int)} == {"awaiting_review"}
    assert {e.event_id for e in manage.review_list(pid)} == {e_city, e_post, e_int}
    assert live.pending(w.key) == []  # custody is the Mac's: the server has let go
    mac.sync_one(pid)
    assert answers_bytes(pid) == file_before and engine == []  # a second pass changes nothing
    # approve one, reject another
    manage.review_apply(pid, [e_city])
    assert stored(pid, "city") == "Lyon" and stored(pid, "postal_code") == ""
    assert manage.review_reject(pid, [e_post]) == 1
    mac.sync_one(pid)
    st = seeker.statuses()
    assert st[e_city] == "applied" and st[e_post] == "rejected" and st[e_int] == "awaiting_review"
    assert stored(pid, "postal_code") == "" and srv_snap(w).answers["city"].value == "Lyon"
    # switching to direct does NOT release the backlog
    manage.set_mode(pid, "direct")
    mac.sync_one(pid)
    assert not srv_snap(w).review_mode
    assert mac.journal.get_event(e_int).state == "awaiting_review" and engine == []
    fresh = seeker.edit("country", "France").event_ids[0]
    mac.sync_one(pid)
    assert seeker.statuses()[fresh] == "applied" and stored(pid, "country") == "France" and mac.journal.get_event(e_int).state == "awaiting_review"
    # an approved intent is re-validated like any other: the mode change voided its grant
    manage.review_apply(pid, [e_int])
    mac.sync_one(pid)
    assert seeker.statuses()[e_int] == "refused" and engine == []


def test_19_review_approved_intent_executes_on_the_next_pass(profile, master, live, tmp_path, engine, monkeypatch):
    w = _setup(live, profile, master, review=True)
    mac, pid, seeker = w.mac, w.profile_id, w.seeker
    # (the review is applied on the real clock, and an agreement lasts a week: the clock is the simulated Mac's here)
    monkeypatch.setattr(db, "now", lambda: mac.now)
    (app,), (g,) = two_apps(w, profile, master, tmp_path, 1)
    e_int = seeker.request(g).event_ids[0]
    mac.sync_one(pid)
    assert engine == [] and seeker.statuses()[e_int] == "awaiting_review"
    manage.review_apply(pid)
    assert mac.journal.get_event(e_int).state == "queued"
    mac.sync_one(pid)
    assert not engine and seeker.statuses()[e_int] == "done"


# ================================================================ 20. local pause
def test_20_pause_exchanges_data_but_applies_and_executes_nothing(w, profile, master, tmp_path, engine):
    live, mac, pid, seeker = w.live, w.mac, w.profile_id, w.seeker
    (app,), (g,) = two_apps(w, profile, master, tmp_path, 1)
    manage.pause()
    assert manage.status().paused
    file_before = answers_bytes(pid)
    e_city = seeker.edit("city", "Lyon").event_ids[0]
    e_int = seeker.request(g).event_ids[0]
    rep = mac.sync_one(pid)
    assert rep.ok and rep.pulled == 2 and rep.pushed
    assert answers_bytes(pid) == file_before and engine == [] and apply_store.list_runs(app.id) == []
    assert {mac.journal.get_event(e).state for e in (e_city, e_int)} == {"received"}
    assert live.pending(w.key) == [] and srv_snap(w).paused
    assert seeker.value("city") == "" and seeker.statuses() == {e_city: "received", e_int: "received"}
    manage.resume()
    rep = mac.sync_one(pid)
    assert rep.ok and stored(pid, "city") == "Lyon" and not engine
    assert seeker.statuses() == {e_city: "applied", e_int: "refused"} and not srv_snap(w).paused
    (refreshed,) = awaiting(w)
    assert refreshed.grant != g
    approved = seeker.request(refreshed.grant)
    assert approved.stored == 1
    assert mac.sync_one(pid).executed == 1 and seeker.statuses()[approved.event_ids[0]] == "done"


# ================================================================ 21. two profiles on one device
def test_21_two_profiles_one_pass_data_never_crosses(w, profile, master, tmp_path, engine):
    from cv_fakes import save_confirmed

    live, mac = w.live, w.mac
    other = db.save_profile(profile.model_copy(update={"id": None, "name": "Théo Martin"}))
    m2 = save_confirmed(other.id)
    creds2 = mac.enable(other.id)
    sa, sb = w.seeker, Seeker.of(live, creds2)
    key_a, key_b = w.key, mac.journal.get_profile(other.id).profile_key
    assert key_a != key_b and creds2.handle != w.creds.handle
    _, app_a = new_app(profile, master, tmp_path, 1)
    _, app_b = new_app(other, m2, tmp_path, 2)
    mac.sync(execute=False)
    assert srv_snap(w).display_name == "Léa" and live.snapshot(key_b).display_name == "Théo"
    assert sa.pid != sb.pid
    (aw_a,), (aw_b,) = awaiting(w), live.snapshot(key_b).awaiting
    assert (aw_a.company, aw_b.company) == ("Société 1", "Société 2")
    # each seeker acts; ONE pass handles both
    oa, ob = sa.edit("city", "Lyon"), sb.edit("city", "Nice")
    ia, ib = sa.request(aw_a.grant), sb.request(aw_b.grant)
    assert sb.request(aw_a.grant).stored == 0 and sa.request(aw_b.grant).stored == 0  # a grant of the other profile is unknown here
    forged = live.forge_event(key_b, IntentEvent, grant=aw_a.grant)  # even forged into the other inbox, the Mac refuses it
    reports = mac.sync()
    assert [r.ok for r in reports] == [True, True] and [r.executed for r in reports] == [1, 1]
    assert stored(w.profile_id, "city") == "Lyon" and stored(other.id, "city") == "Nice"
    assert sa.statuses()[oa.event_ids[0]] == "applied" and sb.statuses()[ob.event_ids[0]] == "applied"
    assert sa.statuses()[ia.event_ids[0]] == "done" and sb.statuses()[ib.event_ids[0]] == "done"
    assert sb.statuses()[forged.event_id] == "refused"
    assert not (set(sa.statuses()) & set(sb.statuses()))
    assert cvstore.get_application(app_a.id).approved_by == cvstore.get_application(app_b.id).approved_by == "user"
    assert not srv_snap(w).sent and not live.snapshot(key_b).sent
    assert srv_snap(w).answers["city"].value == "Lyon" and live.snapshot(key_b).answers["city"].value == "Nice"
    assert sa.view().fields["city"].value == "Lyon" and sb.view().fields["city"].value == "Nice"
    assert not engine


def test_21_documents_of_one_profile_are_not_readable_by_the_other(w, profile, master, tmp_path):
    from cv_fakes import save_confirmed

    live, mac = w.live, w.mac
    other = db.save_profile(profile.model_copy(update={"id": None, "name": "Théo Martin"}))
    m2 = save_confirmed(other.id)
    sb = Seeker.of(live, mac.enable(other.id))
    new_app(profile, master, tmp_path, 1)
    new_app(other, m2, tmp_path, 2)
    mac.sync(execute=False)
    (aw_a,), (aw_b,) = awaiting(w), live.snapshot(mac.journal.get_profile(other.id).profile_key).awaiting
    assert w.seeker.document(aw_a.doc_id) and sb.document(aw_b.doc_id)
    assert w.seeker.document(aw_b.doc_id) is None and sb.document(aw_a.doc_id) is None


# ================================================================ 22. limits
def test_22_oversized_snapshot_clean_error_on_both_sides(w, monkeypatch):
    from jobhunt.portal.protocol import Prep, SentItem

    live, mac, pid, seeker = w.live, w.mac, w.profile_id, w.seeker
    mac.sync_one(pid)
    row_before = live.profile_row(w.key)["snapshot_rev"]
    real = sync.build_snapshot
    big = SentItem(ref="e1" * 16, company="Société", title="Poste", prep=[Prep(message="x" * 1000, hint="y" * 1000)] * 40)

    def inflate(j, p, now):
        snap, docs = real(j, p, now)
        return snap.model_copy(update={"sent": [big] * 60}), docs

    monkeypatch.setattr(sync, "build_snapshot", inflate)
    out = seeker.edit("city", "Lyon")
    rep = mac.sync_one(pid)
    assert not rep.ok and rep.error == "Le portail a refusé ou renvoyé des données trop volumineuses."
    assert mac.journal.get_profile(pid).last_error == rep.error
    assert live.profile_row(w.key)["snapshot_rev"] == row_before  # nothing stored
    # the applied answer is kept (the push, not the apply, failed) and a normal pass recovers
    assert stored(pid, "city") == "Lyon"
    monkeypatch.setattr(sync, "build_snapshot", real)
    rep = mac.sync_one(pid)
    assert rep.ok and seeker.statuses()[out.event_ids[0]] == "applied"


def test_22_the_server_itself_refuses_oversized_bodies(w):
    live, mac, pid = w.live, w.mac, w.profile_id
    mac.sync_one(pid)
    key = w.key
    http = httpx.Client(base_url=live.base_url + protocol.API_PREFIX, headers={"Authorization": f"Bearer {mac.token()}"}, trust_env=False)
    before = live.profile_row(key)["snapshot_rev"]
    r = http.put(f"/profiles/{key}/snapshot", content=b" " * (protocol.MAX_SNAPSHOT_BODY + 1), headers={"Content-Type": "application/json"})
    assert r.status_code == 413 and r.json() == {"error": "too_large"}
    def chunks():  # no Content-Length: the stream itself is counted
        for _ in range(protocol.MAX_SNAPSHOT_BODY // 65536 + 2):
            yield b" " * 65536
    r = http.put(f"/profiles/{key}/snapshot", content=chunks(), headers={"Content-Type": "application/json"})
    assert r.status_code == 413 and r.json() == {"error": "too_large"}
    r = http.put(f"/profiles/{key}", content=b" " * (protocol.MAX_JSON_BODY + 1), headers={"Content-Type": "application/json"})
    assert r.status_code == 413
    assert live.profile_row(key)["snapshot_rev"] == before and mac.sync_one(pid).ok
    http.close()


def test_22_a_pdf_over_1_5_mb_is_not_offered_and_the_limit_itself_is(w, profile, master, tmp_path):
    live, mac, pid, seeker = w.live, w.mac, w.profile_id, w.seeker
    _, big = new_app(profile, master, tmp_path, 1)
    _, edge = new_app(profile, master, tmp_path, 2)
    set_pdf(big, b"%PDF-1.4 " + b"0" * (protocol.MAX_DOCUMENT_BYTES - 8))  # one byte too many
    assert len(cvs.safe_pdf(cvstore.get_application(big.id)).read_bytes()) == protocol.MAX_DOCUMENT_BYTES + 1
    set_pdf(edge, b"%PDF-1.4 " + b"1" * (protocol.MAX_DOCUMENT_BYTES - 9))  # exactly the limit
    rep = mac.sync_one(pid, execute=False)
    assert rep.ok and rep.uploaded == 1
    snap = srv_snap(w)
    by = {a.company: a for a in snap.awaiting}
    assert by["Société 1"].doc_id is None and by["Société 2"].doc_id  # "CV non disponible sur le portail" vs the exact-limit file
    assert [d.doc_id for d in snap.documents] == [by["Société 2"].doc_id] and snap.documents[0].size == protocol.MAX_DOCUMENT_BYTES
    assert seeker.document(by["Société 2"].doc_id) == cvs.safe_pdf(cvstore.get_application(edge.id)).read_bytes()
    assert live.conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 1
    # both are still approvable
    assert all(a.grant for a in snap.awaiting)


# ================================================================ extra: lifecycle of a profile (disable / purge / re-enable / recover)
def test_23_disable_then_enable_again_new_identifiant_new_stream(w, profile, master, tmp_path, engine):
    live, mac, pid, seeker = w.live, w.mac, w.profile_id, w.seeker
    (app,), (g,) = two_apps(w, profile, master, tmp_path, 1)
    out = seeker.edit("city", "Lyon")
    res = manage.disable(pid)
    assert res.server_updated and not mac.journal.get_profile(pid).enabled
    row = live.profile_row(w.key)
    assert row["enabled"] == 0 and not seeker.alive()
    assert live.login(w.creds.handle, w.creds.passcode).kind == "refused"  # behaves like an unknown identifiant
    assert mac.sync() == [] and stored(pid, "city") == ""  # a disabled profile is not synced, nothing is applied
    assert len(live.pending(w.key)) == 1
    creds = manage.enable(pid)
    assert creds.handle != w.creds.handle and creds.passcode != w.creds.passcode
    row2 = live.profile_row(w.key)
    assert row2["enabled"] == 1 and row2["stream_id"] != row["stream_id"] and live.pending(w.key) == []
    assert live.login(w.creds.handle, w.creds.passcode).kind == "refused"
    s2 = Seeker.of(live, creds)
    assert s2.statuses()[out.event_ids[0]] == "lost"  # his edit of before is shown as "à refaire"
    assert mac.sync_one(pid).ok and s2.view().has_snapshot and stored(pid, "city") == ""
    assert s2.request(awaiting(w)[0].grant).stored == 1


def test_23_disable_with_purge_removes_every_trace_on_the_server(w, profile, master, tmp_path):
    live, mac, pid, seeker = w.live, w.mac, w.profile_id, w.seeker
    two_apps(w, profile, master, tmp_path, 1)
    seeker.edit("city", "Lyon")
    assert live.conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 1
    assert manage.disable(pid, purge=True).server_updated
    for table in ("profiles", "snapshots", "manifest", "documents", "events", "sessions", "known_devices"):
        assert live.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0, table  # noqa: S608
    assert live.login(w.creds.handle, w.creds.passcode).kind == "refused"
    creds = manage.enable(pid)  # and the profile can be enabled again from scratch
    assert mac.sync_one(pid).ok and Seeker.of(live, creds).view().has_snapshot


def test_23_recover_after_losing_the_journal(w, profile, master, tmp_path):
    live, mac, pid, seeker = w.live, w.mac, w.profile_id, w.seeker
    two_apps(w, profile, master, tmp_path, 1)
    manage.rotate_passcode(pid)  # the server's config revision is now 2
    mac.sync_one(pid)
    key = w.key
    journal_dir = mac.journal.path.parent
    for f in journal_dir.glob("portal.sqlite3*"):
        f.unlink()  # the SSD lost data/portal/ (the device token file is in the keyring in real life: here it is kept)
    assert not manage.status().paired
    found = manage.recover(live.base_url, "file")
    assert [p.profile_key for p in found] == [key] and found[0].config_rev == 3 - 1 and manage.status().paired
    creds = manage.enable(pid, profile_key=key)  # reuses the server's key; adopts its configuration revision
    assert live.profile_row(key)["config_rev"] > found[0].config_rev
    assert live.conn.execute("SELECT COUNT(*) FROM profiles").fetchone()[0] == 1
    assert mac.sync_one(pid).ok
    s2 = Seeker.of(live, creds)
    assert s2.view().has_snapshot and s2.value("city") == ""
    assert not seeker.alive()  # the old code and sessions are dead
    assert live.login(w.creds.handle, w.creds.passcode).kind == "refused"


# ================================================================ extra: the server's 120 requests per minute against a first big sync
def test_24_first_sync_with_many_documents_stays_under_the_server_rate_limit(w, profile, master, tmp_path):
    mac, pid = w.mac, w.profile_id
    for n in range(1, 101):
        new_app(profile, master, tmp_path, n, score=100.0 - n * 0.1)
    for n in range(101, 131):
        job, app = new_app(profile, master, tmp_path, n, approve=True)
        make_sent(app, job)
    rep = mac.sync_one(pid, execute=False)
    snap = srv_snap(w)
    wanted = len(snap.documents)
    assert wanted == protocol.AWAITING_DOCUMENTS + 30  # 100 waiting + 30 sent
    count = lambda: w.live.conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]  # noqa: E731
    # one pass would make 2 + 130 requests; the server allows 120 per minute: the Mac stops uploading when told to wait, without an
    # error for the operator, and the next pass (a minute later) finishes the job
    assert rep.ok and rep.error is None and 100 <= rep.uploaded < wanted and count() == rep.uploaded
    assert mac.journal.get_profile(pid).last_error is None
    w.live.clock.advance(61)
    rep2 = mac.sync_one(pid, execute=False)
    assert rep2.ok and rep.uploaded + rep2.uploaded == wanted and count() == wanted
    assert mac.sync_one(pid, execute=False).uploaded == 0


# ================================================================ extra: the volume loop (send -> question -> answer -> resume -> sent)
# ================================================================ extra: clearing, custom answers, legacy dates
def test_26_clearing_a_standing_answer_and_custom_answers_edit_delete_conflict(w):
    mac, pid, seeker = w.mac, w.profile_id, w.seeker
    facts.save_answers(pid, {"city": "Toulouse"})
    facts.save_custom_answers(pid, [("Prétentions salariales ?", "Selon grille"), ("Pourquoi nous ?", "Passion"), ("Mobilité ?", "France")])
    mac.sync_one(pid)
    custom = {c.question: c for c in srv_snap(w).custom}
    assert custom["Prétentions salariales ?"].answer == "Selon grille" and not custom["Prétentions salariales ?"].masked
    cid = {q: c.custom_id for q, c in custom.items()}
    # clearing a standing answer removes it on the Mac
    o_clear = seeker.edit("city", "")
    assert o_clear.stored == 1
    # edit one custom answer, delete another, and chain edit + delete of a third
    o_edit = seeker.edit_custom(cid["Prétentions salariales ?"], "  À discuter  ")
    o_del = seeker.delete_custom(cid["Pourquoi nous ?"])
    o_e1 = seeker.edit_custom(cid["Mobilité ?"], "Toute la France")
    o_e2 = seeker.delete_custom(cid["Mobilité ?"])
    v = seeker.view()
    assert [c.deleted for c in v.custom if c.custom_id == cid["Pourquoi nous ?"]] == [True]
    assert [c.answer for c in v.custom if c.custom_id == cid["Prétentions salariales ?"]] == ["À discuter"]
    assert seeker.edit_custom(cid["Mobilité ?"], "x").stored == 0  # editing something he already deleted is refused
    facts.save_custom_answers(pid, [("Autre question ?", "Autre")])  # the Mac adds one in the meantime
    rep = mac.sync_one(pid)
    assert rep.ok and "city" not in facts.load_answers(pid) and stored(pid, "city") == ""
    left = {c["question"]: c["answer"] for c in facts.custom_answers(pid)}
    assert left == {"Prétentions salariales ?": "À discuter", "Autre question ?": "Autre"}
    st = seeker.statuses()
    assert [st[o.event_ids[0]] for o in (o_clear, o_edit, o_del, o_e1, o_e2)] == ["applied"] * 5
    assert {c.question for c in srv_snap(w).custom} == set(left)
    # conflict on a custom answer: the Mac changes it after the page he saw
    page_token = next(c.token for c in seeker.view().custom if c.question == "Autre question ?")
    cid_other = facts.custom_id("Autre question ?")
    facts.update_custom_answer(pid, cid_other, "Changé sur le Mac", "Autre")
    stale_ok = seeker.edit_custom(cid_other, "Changé sur le portail", token=page_token)
    assert stale_ok.stored == 1  # the portal still showed the old snapshot: the Mac decides
    mac.sync_one(pid)
    assert seeker.statuses()[stale_ok.event_ids[0]] == "conflict" and left["Autre question ?"] == "Autre"
    assert {c["question"]: c["answer"] for c in facts.custom_answers(pid)}["Autre question ?"] == "Changé sur le Mac"
    assert [c.answer for c in seeker.view().custom if c.question == "Autre question ?"] == ["Changé sur le Mac"]


def test_26_legacy_free_text_date_is_shown_as_a_note_and_a_picked_date_replaces_it(w):
    mac, pid, seeker = w.mac, w.profile_id, w.seeker
    with facts.answers_lock(pid):  # written by hand into answers.yaml: not a complete date, the date widget shows nothing
        data = facts.load_answers(pid)
        data["graduation_date"] = "juin 2027"
        facts._write_answers(pid, data)
    mac.sync_one(pid)
    a = srv_snap(w).answers["graduation_date"]
    assert a.value == "" and a.legacy == "juin 2027" and a.is_set
    o = seeker.edit("graduation_date", "2027-06-30")
    mac.sync_one(pid)
    assert stored(pid, "graduation_date") == "30 juin 2027" and seeker.statuses()[o.event_ids[0]] == "applied"
    a = srv_snap(w).answers["graduation_date"]
    assert a.value == "2027-06-30" and a.legacy == ""


# ================================================================ extra: a hostile or buggy server cannot make the Mac do anything it did not offer
def test_27_forged_events_are_refused_by_the_mac(w, profile, master, tmp_path, engine):
    from jobhunt.portal.protocol import AnswerEvent, CustomDeleteEvent, CustomEditEvent, QuestionAnswerEvent

    live, mac, pid = w.live, w.mac, w.profile_id
    (app,), (g,) = two_apps(w, profile, master, tmp_path, 1)
    base = srv_ver(w, "city")
    ver0 = "0" * 32
    forged = [
        live.forge_event(w.key, AnswerEvent, path="bank.iban", value="FR76 0000", base_ver=ver0),            # not an answer field
        live.forge_event(w.key, AnswerEvent, path="trusted_hosts", value="evil.example", base_ver=ver0),     # engine-owned key
        live.forge_event(w.key, AnswerEvent, path="custom", value="x", base_ver=ver0),
        live.forge_event(w.key, AnswerEvent, path="driving_licence", value="Peut-être", base_ver=srv_ver(w, "driving_licence")),
        live.forge_event(w.key, AnswerEvent, path="stage.available_from", value="2027-13-45", base_ver=srv_ver(w, "stage.available_from")),
        live.forge_event(w.key, AnswerEvent, path="city", value="Lyon", base_ver=ver0),                      # a version the Mac never issued
        live.forge_event(w.key, QuestionAnswerEvent, qid="ab" * 8, answer="oui"),                           # nothing is waiting on it
        live.forge_event(w.key, CustomEditEvent, custom_id="cd" * 8, answer="x", base_ver=ver0),
        live.forge_event(w.key, CustomDeleteEvent, custom_id="cd" * 8, base_ver=ver0),
        live.forge_event(w.key, IntentEvent, grant="ef" * 16),                                              # a grant the Mac never issued
        live.forge_event(w.key, CancelEvent, target="12" * 16),
    ]
    file_before = answers_bytes(pid)
    rep = mac.sync_one(pid)
    assert rep.ok and rep.pulled == len(forged)
    st = w.seeker.statuses()
    assert [st[e.event_id] for e in forged] == ["rejected", "rejected", "rejected", "rejected", "rejected", "conflict", "rejected",
                                                "conflict", "conflict", "refused", "refused"]
    assert answers_bytes(pid) == file_before and engine == [] and apply_store.list_runs(app.id) == []
    assert facts.trusted_hosts(pid) == [] and live.pending(w.key) == []
    assert mac.journal.get_grant(g).state == "offered" and mac.journal.sends_today(pid, mac.now) == 0


def test_27_a_grant_expires_after_seven_days_on_the_mac_clock(w, profile, master, tmp_path, engine):
    mac, pid, seeker = w.mac, w.profile_id, w.seeker
    (app,), (g,) = two_apps(w, profile, master, tmp_path, 1)
    ev = seeker.request(g).event_ids[0]
    mac.sync_one(pid, step=timedelta(days=8))  # the Mac was off for 8 days: the page he used is older than the grant
    assert seeker.statuses()[ev] == "refused" and engine == []
    (aw,) = awaiting(w)
    assert aw.grant != g and mac.journal.get_grant(g).expires_at < mac.now
    out = seeker.request(aw.grant)
    mac.sync_one(pid)
    assert seeker.statuses()[out.event_ids[0]] == "done" and not engine


# ================================================================ extra: the seeker acts while a pass is running
def test_28_edits_made_between_the_pull_and_the_push_are_neither_lost_nor_conflicting(w, monkeypatch):
    mac, pid, seeker = w.mac, w.profile_id, w.seeker
    mac.sync_one(pid)
    first = seeker.edit("city", "Lyon").event_ids[0]
    real = apply_events.apply_pending
    during: list = []

    def apply_then_he_types(journal, profile_id, now):
        report = real(journal, profile_id, now)  # the Mac took custody and applied "Lyon" ...
        during.append(seeker.edit("city", "Nice"))  # ... and now, before its push, he edits the same field again
        during.append(seeker.edit("postal_code", "69001"))
        return report

    monkeypatch.setattr(apply_events, "apply_pending", apply_then_he_types)
    assert mac.sync_one(pid).ok and stored(pid, "city") == "Lyon"
    monkeypatch.setattr(apply_events, "apply_pending", real)
    assert [o.stored for o in during] == [1, 1]
    assert [e.seq for e in w.live.pending(w.key)] == [2, 3]  # only what the Mac had not taken is left, acked 1
    assert w.live.profile_row(w.key)["acked_seq"] == 1 and seeker.statuses()[first] == "applied"
    assert mac.sync_one(pid).ok
    assert stored(pid, "city") == "Nice" and stored(pid, "postal_code") == "69001"
    assert {seeker.statuses()[o.event_ids[0]] for o in during} == {"applied"} and w.live.pending(w.key) == []


def test_28_a_page_loaded_before_the_push_is_stale_after_it(w):
    mac, pid, seeker = w.mac, w.profile_id, w.seeker
    mac.sync_one(pid)
    seeker.edit("city", "Lyon")
    token_with_pending = seeker.view().fields["city"].token  # = the id of his pending event
    mac.sync_one(pid)  # taken, applied, acked, shown in the snapshot
    late = seeker.edit("city", "Nice", token=token_with_pending)
    assert late.stored == 0 and len(late.stale) == 1  # reload the page, nothing is applied on a guess
    assert seeker.edit("city", "Nice").stored == 1


# ================================================================ extra: documented gaps (pinned so they stay visible)
def test_29_a_second_edit_during_a_pause_chains_to_the_first(w):
    mac, pid, seeker = w.mac, w.profile_id, w.seeker
    mac.sync_one(pid)
    manage.pause()
    first = seeker.edit("city", "Lyon").event_ids[0]
    mac.sync_one(pid)
    assert w.live.pending(w.key) == [] and seeker.value("city") == ""
    second = seeker.edit("city", "Nice")
    assert second.stored == 1 and w.live.pending(w.key)[0].after == first
    mac.sync_one(pid)
    manage.resume()
    mac.sync_one(pid)
    st = seeker.statuses()
    assert st[first] == "applied" and st[second.event_ids[0]] == "applied" and stored(pid, "city") == "Nice"


@pytest.mark.parametrize("action", ["rotate", "revoke", "mode", "resync", "disable"])
def test_30_settings_changes_void_grants_and_queued_intents(w, profile, master, tmp_path, engine, action):
    mac, pid, seeker = w.mac, w.profile_id, w.seeker
    (app,), (g,) = two_apps(w, profile, master, tmp_path, 1)
    ev = seeker.request(g).event_ids[0]
    with service.send_lock():
        mac.sync_one(pid)
    assert mac.journal.get_event(ev).state == "queued"
    {"rotate": lambda: manage.rotate_passcode(pid), "revoke": lambda: manage.revoke(pid), "mode": lambda: manage.set_mode(pid, "review"),
     "resync": lambda: manage.resync(pid), "disable": lambda: manage.disable(pid)}[action]()
    j = mac.journal
    assert j.get_grant(g).state == "void" and j.get_event(ev).state in ("cancelled", "lost")
    if action != "disable":
        assert mac.sync_one(pid).ok
        assert engine == [] and apply_store.list_runs(app.id) == [] and seeker_status_of(w, ev) in ("cancelled", "lost")
        (aw,) = awaiting(w)
        assert aw.grant != g  # a still actionable item gets a NEW grant
    else:
        assert engine == [] and mac.sync() == []


def seeker_status_of(w: Setup, event_id: str) -> str | None:
    fresh = Seeker(w.live, w.creds.handle, w.creds.passcode)
    if fresh.login().kind != "ok":  # after a rotation the printed code is not the current one: read the stored results directly
        return {r.event_id: r.status for r in w.live.stored_results(w.key)}.get(event_id)
    return fresh.statuses().get(event_id)
