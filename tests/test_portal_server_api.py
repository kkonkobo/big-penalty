"""The Mac API: pairing, bearer auth, scoping, configuration, snapshot, inbox, documents, limits, headers."""

from __future__ import annotations

import hashlib
import logging
from datetime import UTC, datetime

import pytest
from portal_server_fakes import (DOC_A, DOC_B, GRANT_SEND_1, PAIRING_CODE, PDF_A, PDF_B, QID, Harness, doc_ref, fast_scrypt,
                                 make_pdf, make_snapshot)

from jobhunt.portal import protocol
from jobhunt.portal.protocol import EventResult
from jobhunt.portal.server import admin, api, auth, ops, throttle
from jobhunt.portal.server.app import SECURITY_HEADERS


@pytest.fixture
def h(tmp_path, monkeypatch):
    fast_scrypt(monkeypatch)
    harness = Harness.create(tmp_path)
    yield harness
    harness.portal.db.close()


def assert_headers(r, cache=True):
    for k, v in SECURITY_HEADERS.items():
        assert r.headers.get(k) == v, k
    if cache:
        assert r.headers.get("cache-control") == "no-store"


# ---------------------------------------------------------------- pairing
def pair_body(token_sha=None, code=PAIRING_CODE):
    return {"pairing_code": code, "device_name": "Mac", "token_sha256": token_sha or hashlib.sha256(b"t").hexdigest()}


def test_pairing_success_and_idempotent_retry(h):
    r = h.client.post(f"{h.api}/pair", json=pair_body())
    assert r.status_code == 200 and len(r.json()["device_id"]) == 32
    again = h.client.post(f"{h.api}/pair", json=pair_body())
    assert again.status_code == 200 and again.json() == r.json()
    other = h.client.post(f"{h.api}/pair", json=pair_body(hashlib.sha256(b"other").hexdigest()))
    assert other.status_code == 403 and other.json() == {"error": "refused"}  # a consumed code serves only its device


def test_pairing_wrong_code_expired_and_off(h, tmp_path):
    assert h.client.post(f"{h.api}/pair", json=pair_body(code="wrong")).status_code == 403
    h.clock.now = 1_900_000_000 + 86400 * 365 * 3  # after 2030-01-01
    assert h.client.post(f"{h.api}/pair", json=pair_body()).status_code == 403
    off = Harness.create(tmp_path / "off", PORTAL_PAIRING_CODE=None)
    for _ in range(6):
        assert off.client.post(f"{off.api}/pair", json=pair_body()).status_code == 403
    assert off.portal.db.conn.execute("SELECT COUNT(*) FROM throttle").fetchone()[0] == 0


def test_pairing_max_devices(tmp_path, h):
    h.pair()
    second = Harness.create(tmp_path, PORTAL_PAIRING_CODE="z" * 40)  # same database file, another code
    assert second.client.post(f"{second.api}/pair", json=pair_body(code="z" * 40)).json() == {"error": "max_devices"}
    third = Harness.create(tmp_path, PORTAL_PAIRING_CODE="y" * 40, PORTAL_MAX_DEVICES="2")
    assert third.client.post(f"{third.api}/pair", json=pair_body(hashlib.sha256(b"3").hexdigest(), "y" * 40)).status_code == 200


def test_pairing_throttle(h):
    for _ in range(5):
        assert h.client.post(f"{h.api}/pair", json=pair_body(code="wrong")).status_code == 403
    assert h.client.post(f"{h.api}/pair", json=pair_body(code="wrong")).status_code == 429
    assert h.client.post(f"{h.api}/pair", json=pair_body()).status_code == 200


def test_pairing_global_throttle(h):
    for _ in range(20):
        assert throttle.reserve(h.portal.db, [("pair:all", *api.PAIR_GLOBAL)], h.clock())
    assert h.client.post(f"{h.api}/pair", json=pair_body(code="wrong")).status_code == 429
    assert h.client.post(f"{h.api}/pair", json=pair_body()).status_code == 200


def test_pairing_code_never_logged(h, caplog):
    caplog.set_level(logging.DEBUG)
    h.client.post(f"{h.api}/pair", json=pair_body())
    assert PAIRING_CODE not in caplog.text


# ---------------------------------------------------------------- auth and scoping
def test_bearer_auth(h):
    dev = h.pair()
    assert h.client.get(f"{h.api}/status", headers=dev.headers).status_code == 200
    for headers in ({}, {"Authorization": "Bearer nope"}, {"Authorization": "Basic abc"}, {"Authorization": "Bearer "}):
        r = h.client.get(f"{h.api}/status", headers=headers)
        assert r.status_code == 401 and r.json() == {"error": "unauthorized"}
    with h.portal.db.transaction() as c:
        c.execute("UPDATE devices SET revoked_at=1")
    assert h.client.get(f"{h.api}/status", headers=dev.headers).status_code == 401


def test_device_rate_limit(h):
    dev = h.pair()
    codes = [h.client.get(f"{h.api}/status", headers=dev.headers).status_code for _ in range(122)]
    assert codes.count(200) == 120 and codes[-1] == 429
    h.clock.advance(61)
    assert h.client.get(f"{h.api}/status", headers=dev.headers).status_code == 200


def test_every_profile_route_is_scoped_to_the_device(h):
    owner = h.pair()
    prof = h.enable(owner)
    other = h.add_device()
    base = f"{h.api}/profiles/{prof.key}"
    unknown = f"{h.api}/profiles/{'f' * 32}"
    snap = {"stream_id": prof.stream_id, "sessions_epoch": prof.epoch, "revoked_through_seq": 0,
            "rev": 99, "acked_seq": 0, "snapshot": make_snapshot().model_dump(mode="json")}
    for root in (base, unknown):
        for method, suffix, kwargs in (("GET", "/inbox", {}), ("PUT", "/snapshot", {"json": snap}),
                                       ("PUT", f"/documents/{DOC_A}", {"content": PDF_A, "headers_extra": {"Content-Type": "application/pdf"}}),
                                       ("DELETE", "", {})):
            extra = kwargs.pop("headers_extra", {})
            r = h.client.request(method, root + suffix, headers={**other.headers, **extra}, **kwargs)
            assert r.status_code == 404 and r.json() == {"error": "not_found"}, (method, suffix, r.text)
    assert h.client.get(f"{base}/inbox", headers=owner.headers).status_code == 200
    taken = h.client.put(base, headers=other.headers, json=prof.config_body(config_rev=5))
    assert taken.status_code == 404 and taken.json() == {"error": "not_found"}
    assert h.client.get(f"{h.api}/status", headers=other.headers).json()["profiles"] == []
    assert len(h.client.get(f"{h.api}/status", headers=owner.headers).json()["profiles"]) == 1


# ---------------------------------------------------------------- configuration
def test_config_creation_and_revision_rules(h):
    dev = h.pair()
    prof = h.new_profile()
    no_pass = prof.config_body()
    del no_pass["passcode"]
    assert h.client.put(f"{h.api}/profiles/{prof.key}", headers=dev.headers, json=no_pass).status_code == 422
    assert h.put_profile(dev, prof).json() == {"config_rev": 1}
    assert h.put_profile(dev, prof).status_code == 200  # identical retry
    r = h.put_profile(dev, prof, sessions_epoch=3)
    assert r.status_code == 409 and r.json() == {"error": "config_rev", "current": 1}
    prof.config_rev = 2
    assert h.put_profile(dev, prof, sessions_epoch=3).status_code == 200
    prof.config_rev = 1
    assert h.put_profile(dev, prof).json() == {"error": "config_rev", "current": 2}
    prof.config_rev = 3
    no_pass = prof.config_body(sessions_epoch=3)
    del no_pass["passcode"]  # keeps the stored verifier
    assert h.client.put(f"{h.api}/profiles/{prof.key}", headers=dev.headers, json=no_pass).status_code == 200


def test_config_validation(h):
    dev = h.pair()
    prof = h.new_profile()
    url = f"{h.api}/profiles/{prof.key}"
    assert h.client.put(url, headers=dev.headers, json={**prof.config_body(), "extra": 1}).json() == {"error": "invalid"}
    assert h.client.put(f"{h.api}/profiles/not-a-key", headers=dev.headers, json=prof.config_body()).status_code == 422
    assert h.client.put(url, headers=dev.headers, content=b"{", ).status_code == 415
    assert h.client.put(url, headers={**dev.headers, "Content-Type": "application/json"}, content=b"{").status_code == 422
    assert h.client.put(url, headers=dev.headers, json=prof.config_body(handle_sha256="zz")).status_code == 422


def test_handle_taken_and_profile_limit(h):
    dev = h.pair()
    a, b = h.enable(dev, with_snapshot=False), h.new_profile()
    assert h.put_profile(dev, b, handle_sha256=protocol.handle_digest(a.handle)).json() == {"error": "handle_taken"}
    for _ in range(protocol.MAX_PROFILES_PER_DEVICE - 1):
        assert h.put_profile(dev, h.new_profile()).status_code == 200
    r = h.put_profile(dev, h.new_profile())
    assert r.status_code == 409 and r.json() == {"error": "profile_limit"}


def test_new_passcode_epoch_and_disable_side_effects(h):
    dev = h.pair()
    prof = h.enable(dev)
    out = h.login(prof)
    assert out.kind == "ok" and auth.resolve_session(h.portal, [out.session_id], h.clock())
    throttle.reserve(h.portal.db, [(f"h:{h.portal.cipher.mac('login', protocol.handle_digest(prof.handle))}", 20, 3600)], h.clock())
    prof.config_rev = 2
    assert h.put_profile(dev, prof, enabled=False).status_code == 200
    assert auth.resolve_session(h.portal, [out.session_id], h.clock()) is None
    n_known = lambda: h.portal.db.conn.execute("SELECT COUNT(*) FROM known_devices").fetchone()[0]  # noqa: E731
    assert n_known() == 0
    prof.config_rev = 3
    assert h.put_profile(dev, prof).status_code == 200
    out2 = h.login(prof)
    assert out2.kind == "ok"
    # a new passcode wipes sessions, known browsers and the login throttle
    prof.config_rev, prof.epoch, prof.passcode = 4, 1, "new-passcode-123"
    assert h.put_profile(dev, prof).status_code == 200
    assert auth.resolve_session(h.portal, [out2.session_id], h.clock()) is None and n_known() == 0
    assert h.portal.db.conn.execute("SELECT COUNT(*) FROM throttle WHERE bucket LIKE 'h:%'").fetchone()[0] == 0
    assert h.login(prof).kind == "ok" and h.login(prof, code="abcd-efgh-jkmn").kind == "refused"
    # an epoch bump alone also kills sessions
    out3 = h.login(prof)
    prof.config_rev = 5
    assert h.put_profile(dev, prof, sessions_epoch=9).status_code == 200
    assert auth.resolve_session(h.portal, [out3.session_id], h.clock()) is None


def test_put_never_changes_the_admin_hold(h):
    dev = h.pair()
    prof = h.enable(dev)
    with h.portal.db.transaction() as c:
        c.execute("UPDATE profiles SET held=1")
    prof.config_rev = 2
    h.put_profile(dev, prof, enabled=True)
    assert h.client.get(f"{h.api}/profiles/{prof.key}/inbox", headers=dev.headers).json()["held"] is True
    assert h.client.get(f"{h.api}/status", headers=dev.headers).json()["profiles"][0]["held"] is True
    assert h.login(prof).kind == "refused"


def test_stream_change_converts_pending_work_to_lost_results(h):
    dev = h.pair()
    prof = h.enable(dev)
    queued = EventResult(event_id="1" * 32, kind="intent", status="queued", label="Société Exemple — Poste", at=datetime(2027, 1, 1, tzinfo=UTC))
    final = EventResult(event_id="2" * 32, kind="answer", status="applied", label="Ville", at=queued.at)
    assert h.push_snapshot(dev, prof, results=[queued, final]).status_code == 200
    assert ops.answer_questions(h.portal, prof.row_id, [(QID, "Oui")], h.clock()).stored == 1
    assert ops.request_intents(h.portal, prof.row_id, [GRANT_SEND_1], h.clock()).stored == 1
    old_stream = prof.stream_id
    prof.config_rev, prof.stream_id = 2, protocol.new_id()
    assert h.put_profile(dev, prof).status_code == 200
    inbox = h.client.get(f"{h.api}/profiles/{prof.key}/inbox", headers=dev.headers).json()
    assert inbox["stream_id"] == prof.stream_id != old_stream and inbox["last_seq"] == 0 and inbox["events"] == []
    view = ops.load_view(h.portal, prof.row_id, h.clock())
    assert not view.has_snapshot and ops.open_document(h.portal, prof.row_id, DOC_A) is None
    assert ops.request_intents(h.portal, prof.row_id, [GRANT_SEND_1], h.clock()).stored == 0
    results = {r.event_id: r for r in view.results}
    assert len(results) == 4
    assert results["1" * 32].status == "lost" and results["1" * 32].label == "Société Exemple — Poste"
    assert results["2" * 32].status == "applied"
    assert [r.message for r in results.values() if r.status == "lost"] == ["Perdu lors d'une réinitialisation : à refaire."] * 3
    assert {r.label for r in results.values() if r.kind == "intent" and r.event_id not in ("1" * 32,)} == {"Société Exemple — Technicien mécatronique"}
    # old stream is refused, the new one restarts at seq 1, and the lost results survive the next push
    assert h.client.put(f"{h.api}/profiles/{prof.key}/snapshot", headers=dev.headers, json={
        "stream_id": old_stream, "sessions_epoch": prof.epoch, "revoked_through_seq": 0,
        "rev": 1, "acked_seq": 0, "snapshot": make_snapshot().model_dump(mode="json")}).json() == {"error": "stream"}
    assert h.push_snapshot(dev, prof, rev=1).status_code == 200
    assert sum(r.status == "lost" for r in ops.load_view(h.portal, prof.row_id, h.clock()).results) == 3
    assert ops.answer_questions(h.portal, prof.row_id, [(QID, "Non")], h.clock()).stored == 1
    assert h.client.get(f"{h.api}/profiles/{prof.key}/inbox", headers=dev.headers).json()["events"][0]["seq"] == 1


def test_stream_change_keeps_at_most_max_results(h, monkeypatch):
    dev = h.pair()
    prof = h.enable(dev)
    monkeypatch.setattr(protocol, "MAX_RESULTS", 2)
    for i in range(3):
        ops.request_intents(h.portal, prof.row_id, [[GRANT_SEND_1, "a2" * 16, "a3" * 16][i]], h.clock())
    prof.config_rev, prof.stream_id = 2, protocol.new_id()
    h.put_profile(dev, prof)
    assert len(ops.load_view(h.portal, prof.row_id, h.clock()).results) == 2


# ---------------------------------------------------------------- snapshot
def test_snapshot_rev_digest_and_replay(h):
    dev = h.pair()
    prof = h.enable(dev, with_snapshot=False)
    first = h.push_snapshot(dev, prof, rev=1, upload=False)
    assert first.status_code == 200 and sorted(first.json()["missing_documents"]) == sorted([DOC_A, DOC_B])
    assert ops.answer_questions(h.portal, prof.row_id, [(QID, "Oui")], h.clock()).stored == 1
    replay = h.push_snapshot(dev, prof, rev=1, upload=False)
    assert replay.status_code == 200 and replay.json() == first.json()
    assert len(h.client.get(f"{h.api}/profiles/{prof.key}/inbox", headers=dev.headers).json()["events"]) == 1  # replay changed nothing
    different = h.push_snapshot(dev, prof, make_snapshot(display_name="Autre"), rev=1, upload=False)
    assert different.status_code == 409 and different.json() == {"error": "rev", "current": 1}
    assert h.push_snapshot(dev, prof, rev=0 + 1, upload=False).status_code == 200
    assert h.push_snapshot(dev, prof, make_snapshot(display_name="Léa 2"), rev=2, upload=False).status_code == 200
    assert h.push_snapshot(dev, prof, rev=1, upload=False).json() == {"error": "rev", "current": 2}
    assert ops.load_view(h.portal, prof.row_id, h.clock()).snapshot.display_name == "Léa 2"


def test_snapshot_ack_rules_and_event_deletion(h):
    dev = h.pair()
    prof = h.enable(dev)
    for qid_answer in ("Oui",):
        ops.answer_questions(h.portal, prof.row_id, [(QID, qid_answer)], h.clock())
    ops.request_intents(h.portal, prof.row_id, [GRANT_SEND_1], h.clock())
    assert h.push_snapshot(dev, prof, acked_seq=3).json() == {"error": "ack", "current": 0}
    assert h.push_snapshot(dev, prof, acked_seq=1).status_code == 200
    inbox = h.client.get(f"{h.api}/profiles/{prof.key}/inbox", headers=dev.headers).json()
    assert [e["seq"] for e in inbox["events"]] == [2] and inbox["last_seq"] == 2 and inbox["acked_seq"] == 1
    assert h.push_snapshot(dev, prof, acked_seq=0).json() == {"error": "ack", "current": 1}  # never backwards
    assert h.push_snapshot(dev, prof, acked_seq=2).status_code == 200
    assert h.client.get(f"{h.api}/profiles/{prof.key}/inbox?after=0", headers=dev.headers).json()["events"] == []


def test_snapshot_control_fence_precedes_revision_replay_and_ack(h):
    dev = h.pair()
    prof = h.enable(dev)
    url = f"{h.api}/profiles/{prof.key}/snapshot"
    body = {"stream_id": prof.stream_id, "sessions_epoch": prof.epoch, "revoked_through_seq": 0,
            "rev": prof.rev + 1, "acked_seq": 0, "snapshot": make_snapshot().model_dump(mode="json")}
    assert h.client.put(url, headers=dev.headers, json=body).status_code == 200
    assert ops.answer_questions(h.portal, prof.row_id, [(QID, "Oui")], h.clock()).stored == 1
    prof.config_rev += 1
    prof.epoch += 1
    assert h.put_profile(dev, prof).status_code == 200
    assert h.client.put(url, headers=dev.headers, json=body).json() == {"error": "controls"}
    current = {**body, "sessions_epoch": prof.epoch, "rev": body["rev"] + 1, "acked_seq": 1}
    assert h.client.put(url, headers=dev.headers, json=current).json() == {"error": "controls"}
    inbox = h.client.get(f"{h.api}/profiles/{prof.key}/inbox", headers=dev.headers).json()
    assert inbox["acked_seq"] == 0 and [event["seq"] for event in inbox["events"]] == [1]
    assert h.client.put(url, headers=dev.headers, json={**current, "revoked_through_seq": 1}).status_code == 200


def test_snapshot_documents_missing_and_dropped(h):
    dev = h.pair()
    prof = h.enable(dev, with_snapshot=False)
    assert sorted(h.push_snapshot(dev, prof, upload=False).json()["missing_documents"]) == sorted([DOC_A, DOC_B])
    assert h.upload(dev, prof, DOC_A, PDF_A).status_code == 200
    only_b = make_snapshot(awaiting=[], documents=[doc_ref(PDF_B)])
    r = h.push_snapshot(dev, prof, only_b, upload=False)
    assert r.json()["missing_documents"] == [DOC_B]
    assert h.portal.db.conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0  # DOC_A dropped
    assert ops.open_document(h.portal, prof.row_id, DOC_A) is None


def test_snapshot_rejects_unlisted_document_reference_and_oversized_manifest(h, monkeypatch):
    dev = h.pair()
    prof = h.enable(dev, with_snapshot=False)
    bad = make_snapshot(documents=[doc_ref(PDF_B)])  # awaiting references DOC_A, absent from the manifest
    assert h.push_snapshot(dev, prof, bad).json() == {"error": "invalid"}
    monkeypatch.setattr(protocol, "MAX_DOCUMENTS_BYTES", len(PDF_A) + 1)
    assert h.push_snapshot(dev, prof, make_snapshot()).status_code == 422
    dup = make_snapshot(documents=[doc_ref(PDF_A), doc_ref(PDF_A), doc_ref(PDF_B)])
    monkeypatch.undo()
    assert h.push_snapshot(dev, prof, dup).status_code == 422


@pytest.mark.parametrize("leak", ["value", "legacy", "custom"])
def test_snapshot_refuses_masked_values(h, leak):
    dev = h.pair()
    prof = h.enable(dev, with_snapshot=False)
    snapshot = make_snapshot()
    if leak == "custom":
        next(answer for answer in snapshot.custom if answer.masked).answer = "private"
    else:
        setattr(snapshot.answers["misc.health"], leak, "private")
    assert h.push_snapshot(dev, prof, snapshot, upload=False).status_code == 422
    assert not ops.load_view(h.portal, prof.row_id, h.clock()).has_snapshot


def test_snapshot_refuses_rebinding_a_known_document(h):
    dev = h.pair()
    prof = h.enable(dev)
    snapshot = make_snapshot()
    snapshot.documents[0].sha256 = "0" * 64
    assert h.push_snapshot(dev, prof, snapshot, upload=False).status_code == 422


def test_snapshot_is_stored_encrypted(h, tmp_path):
    dev = h.pair()
    prof = h.enable(dev)
    ops.answer_questions(h.portal, prof.row_id, [(QID, "Oui")], h.clock())
    h.portal.db.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    raw = b"".join(p.read_bytes() for p in (tmp_path / "data").iterdir() if p.is_file())
    for needle in ("Toulouse", "Société Exemple", "06 00 00 00 00", "Léa", "Permis de conduire"):
        assert needle.encode() not in raw
    assert PDF_A not in raw and b"%PDF-" not in raw
    assert (tmp_path / "data" / "portal.sqlite3").stat().st_mode & 0o777 == 0o600


def test_snapshot_row_moved_to_another_profile_fails(h):
    dev = h.pair()
    a, b = h.enable(dev), h.enable(dev)
    with h.portal.db.transaction() as c:
        row = c.execute("SELECT key_id, nonce, ciphertext FROM snapshots WHERE profile_id=? AND purpose='snapshot'", (a.row_id,)).fetchone()
        c.execute("UPDATE snapshots SET key_id=?, nonce=?, ciphertext=? WHERE profile_id=? AND purpose='snapshot'",
                  (row[0], row[1], row[2], b.row_id))
    with pytest.raises(Exception, match="cannot open"):
        ops.load_view(h.portal, b.row_id, h.clock())


# ---------------------------------------------------------------- inbox
def test_inbox_paging_and_contiguity(h):
    dev = h.pair()
    prof = h.enable(dev)
    for i in range(protocol.INBOX_PAGE + 30):
        view = ops.load_view(h.portal, prof.row_id, h.clock())
        field = view.fields["identity.city"]
        assert ops.submit_answers(h.portal, prof.row_id, [ops.AnswerEdit("identity.city", f"Ville {i}", field.token)], h.clock()).stored == 1
    url = f"{h.api}/profiles/{prof.key}/inbox"
    page1 = h.client.get(url, headers=dev.headers).json()
    assert len(page1["events"]) == protocol.INBOX_PAGE and page1["last_seq"] == protocol.INBOX_PAGE + 30
    assert [e["seq"] for e in page1["events"]] == list(range(1, protocol.INBOX_PAGE + 1))
    page2 = h.client.get(url, params={"after": protocol.INBOX_PAGE}, headers=dev.headers).json()
    assert [e["seq"] for e in page2["events"]] == list(range(protocol.INBOX_PAGE + 1, protocol.INBOX_PAGE + 31))
    assert page2["events"][0]["kind"] == "answer" and page2["events"][0]["after"] == page1["events"][-1]["event_id"]
    assert h.client.get(url, params={"after": -1}, headers=dev.headers).status_code == 422


# ---------------------------------------------------------------- documents
def test_document_upload_validation_order(h):
    dev = h.pair()
    prof = h.enable(dev, with_snapshot=False)
    h.push_snapshot(dev, prof, upload=False)
    url = f"{h.api}/profiles/{prof.key}/documents/{DOC_A}"
    pdf = {"Content-Type": "application/pdf"}
    assert h.client.put(f"{h.api}/profiles/{prof.key}/documents/{'9' * 32}", content=PDF_A, headers={**dev.headers, **pdf}).status_code == 404
    assert h.client.put(url, content=PDF_A, headers={**dev.headers, "Content-Type": "text/plain"}).status_code == 415
    assert h.client.put(url, content=(b for b in [PDF_A]), headers={**dev.headers, **pdf}).status_code == 411  # chunked: no length
    assert h.client.put(url, content=PDF_A + b"x", headers={**dev.headers, **pdf}).status_code == 422  # size differs
    same_size = b"%PDF-" + b"\x00" * (len(PDF_A) - 5)
    assert h.client.put(url, content=same_size, headers={**dev.headers, **pdf}).status_code == 422  # sha256 differs
    no_magic = b"X" * len(PDF_A)
    assert h.client.put(url, content=no_magic, headers={**dev.headers, **pdf}).status_code == 422
    assert h.client.put(url, content=PDF_A, headers={**dev.headers, "Content-Type": "application/pdf; charset=binary"}).status_code == 200
    assert h.client.put(url, content=PDF_A, headers={**dev.headers, **pdf}).status_code == 200  # idempotent
    assert ops.open_document(h.portal, prof.row_id, DOC_A) == PDF_A
    assert ops.open_document(h.portal, prof.row_id, DOC_B) is None  # listed but not stored
    assert h.client.put(url, content=PDF_A, headers=pdf).status_code == 401


def test_document_quota_rechecked_when_storing(h, monkeypatch):
    dev = h.pair()
    prof = h.enable(dev, with_snapshot=False)
    h.push_snapshot(dev, prof, upload=False)
    monkeypatch.setattr(protocol, "MAX_DOCUMENTS_BYTES", len(PDF_A) + 3)
    assert h.upload(dev, prof, DOC_A, PDF_A).status_code == 200
    r = h.upload(dev, prof, DOC_B, PDF_B)
    assert r.status_code == 413 and r.json() == {"error": "quota"}


def test_total_document_quota_spans_profiles(tmp_path):
    harness = Harness.create(tmp_path, PORTAL_MAX_TOTAL_BYTES=str(len(PDF_A)))
    device = harness.pair()
    first = harness.enable(device, with_snapshot=False)
    second = harness.enable(device, with_snapshot=False, handle="secondhandle")
    for profile in (first, second):
        assert harness.push_snapshot(device, profile, upload=False).status_code == 200
    assert harness.upload(device, first, DOC_A, PDF_A).status_code == 200
    assert harness.upload(device, second, DOC_A, PDF_A).json() == {"error": "quota"}
    assert harness.upload(device, first, DOC_A, PDF_A).status_code == 200


def test_document_replaced_when_content_changes(h):
    dev = h.pair()
    prof = h.enable(dev)
    new_pdf = make_pdf("a-longer-revision")
    snap = make_snapshot(awaiting=[], sent=[], documents=[doc_ref(PDF_A)])
    assert h.push_snapshot(dev, prof, snap).status_code == 200
    changed = snap.model_copy(update={"documents": [doc_ref(new_pdf)]})
    assert h.push_snapshot(dev, prof, changed, upload=False).json()["missing_documents"] == [doc_ref(new_pdf).doc_id]


# ---------------------------------------------------------------- delete, status
def test_delete_profile_removes_everything(h):
    dev = h.pair()
    prof = h.enable(dev)
    ops.answer_questions(h.portal, prof.row_id, [(QID, "Oui")], h.clock())
    out = h.login(prof)
    assert h.client.delete(f"{h.api}/profiles/{prof.key}", headers=dev.headers).status_code == 200
    c = h.portal.db.conn
    for table in ("profiles", "snapshots", "events", "documents", "manifest", "sessions", "known_devices"):
        assert c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0, table
    assert auth.resolve_session(h.portal, [out.session_id], h.clock()) is None
    assert h.client.delete(f"{h.api}/profiles/{prof.key}", headers=dev.headers).status_code == 404


def test_purged_profile_cannot_change_owner_reuse_revision_or_clear_hold(h):
    owner = h.pair()
    other = h.add_device()
    profile = h.enable(owner)
    admin.hold(h.portal.db, profile.key)
    assert h.client.delete(f"{h.api}/profiles/{profile.key}", headers=owner.headers).status_code == 200
    retired_stream = profile.stream_id
    profile.stream_id = protocol.new_id()
    assert h.put_profile(other, profile).status_code == 404
    assert h.put_profile(owner, profile).json() == {"error": "config_rev", "current": 1}
    profile.config_rev = 2
    assert h.put_profile(owner, profile, stream_id=retired_stream).json() == {"error": "stream"}
    assert h.put_profile(owner, profile).status_code == 200
    assert h.client.get(f"{h.api}/status", headers=owner.headers).json()["profiles"][0]["held"] is True
    assert h.login(profile).kind == "refused"


def test_status_lists_only_own_profiles(h):
    dev = h.pair()
    prof = h.enable(dev)
    other = h.add_device()
    h.enable(other)
    body = h.client.get(f"{h.api}/status", headers=dev.headers).json()
    assert body["version"] == "abc123" and [p["profile_key"] for p in body["profiles"]] == [prof.key]
    assert body["profiles"][0]["snapshot_rev"] == 1


# ---------------------------------------------------------------- limits, headers, errors
def test_body_limits_declared_and_streamed(h):
    dev = h.pair()
    prof = h.new_profile()
    url = f"{h.api}/profiles/{prof.key}"
    big = b" " * (protocol.MAX_JSON_BODY + 1)
    headers = {**dev.headers, "Content-Type": "application/json"}
    declared = h.client.put(url, content=big, headers=headers)
    assert declared.status_code == 413 and declared.json() == {"error": "too_large"}
    streamed = h.client.put(url, content=(big[i:i + 4096] for i in range(0, len(big), 4096)), headers=headers)
    assert streamed.status_code == 413
    assert_headers(declared)
    assert h.client.put(f"{url}/snapshot", content=b" " * (protocol.MAX_SNAPSHOT_BODY + 1), headers=headers).status_code == 413
    assert h.client.put(f"{url}/documents/{DOC_A}", content=b"x" * (protocol.MAX_DOCUMENT_BYTES + 1),
                        headers={**dev.headers, "Content-Type": "application/pdf"}).status_code == 413
    assert h.client.post(f"{h.config.base_path}/anything", content=b"x" * (64 * 1024 + 1)).status_code == 413
    assert h.client.put(url, content=b"", headers={**headers, "Content-Length": "abc"}).status_code in (400, 422)


def test_security_headers_everywhere(h):
    dev = h.pair()
    ok = h.client.get(f"{h.api}/status", headers=dev.headers)
    assert ok.status_code == 200
    assert_headers(ok)
    assert_headers(h.client.get(f"{h.config.base_path}/healthz"))
    assert_headers(h.client.get("/nowhere"))
    assert_headers(h.client.get(f"{h.api}/unknown"))
    assert_headers(h.client.put(f"{h.api}/profiles/{'a' * 32}", headers=dev.headers, json={}))  # 422
    assert_headers(h.client.get(f"{h.api}/profiles/{'a' * 32}/inbox"))  # 401


def test_unexpected_exception_is_generic_and_leaks_nothing(h, caplog):
    secret = "secret-input-9f8e7d"

    @h.app.get(h.api + "/boom")
    async def boom():
        raise RuntimeError(f"details {secret}")

    @h.app.get(h.config.base_path + "/boom")
    async def boom_page():
        raise RuntimeError(f"details {secret}")

    caplog.set_level(logging.DEBUG)
    r = h.client.get(f"{h.api}/boom?x={secret}")
    assert r.status_code == 500 and r.json() == {"error": "server"} and secret not in r.text
    assert_headers(r)
    page = h.client.get(f"{h.config.base_path}/boom?x={secret}")
    assert page.status_code == 500 and secret not in page.text and "text/html" in page.headers["content-type"] and "erreur" in page.text.lower()
    assert_headers(page)
    ours = "\n".join(r.getMessage() for r in caplog.records if r.name == "bigpenalty.portal")
    assert secret not in ours and "RuntimeError" in ours
    assert "GET /app/api/v3/boom 500" in ours  # the route template, never the raw path / query


def test_validation_errors_never_echo_input(h):
    dev = h.pair()
    r = h.client.put(f"{h.api}/profiles/{'b' * 32}", headers=dev.headers, json={"leaked": "SECRET-VALUE"})
    assert r.status_code == 422 and r.json() == {"error": "invalid"} and "SECRET-VALUE" not in r.text
    unknown_key = h.client.post(f"{h.api}/pair", json={**pair_body(), "extra": "SECRET-VALUE"})
    assert unknown_key.json() == {"error": "invalid"}
    assert h.client.post(f"{h.api}/pair", json={**pair_body(), "token_sha256": "SECRET-VALUE"}).json() == {"error": "invalid"}
