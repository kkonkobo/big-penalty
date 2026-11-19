"""Operator commands: list, revoke-device, hold / release, purge-profile, rotate-key (functions and the CLI entry point)."""

from __future__ import annotations

import pytest
from portal_server_fakes import KEY_HEX, OLD_KEY_HEX, PDF_A, QID, Harness, fast_scrypt, make_env

from jobhunt.portal import protocol
from jobhunt.portal.server import admin, auth, ops
from jobhunt.portal.server.crypto import Cipher, CryptoError


@pytest.fixture
def h(tmp_path, monkeypatch):
    fast_scrypt(monkeypatch)
    harness = Harness.create(tmp_path)
    yield harness
    harness.portal.db.close()


def test_list_has_no_secrets(h):
    dev = h.pair()
    prof = h.enable(dev)
    ops.answer_questions(h.portal, prof.row_id, [(QID, "Oui")], h.clock())
    text = admin.list_state(h.portal.db)
    assert dev.device_id in text and prof.key in text and "pending=1" in text and "enabled=True" in text and "held=False" in text
    for secret in (dev.token, dev.token_sha256, protocol.handle_digest(prof.handle), prof.handle, prof.passcode, KEY_HEX, "Toulouse"):
        assert secret not in text


def test_revoke_device_by_prefix(h):
    dev = h.pair()
    profile = h.enable(dev)
    assert h.login(profile).kind == "ok"
    assert admin.revoke_device(h.portal.db, dev.device_id[:6], h.clock()) == dev.device_id
    assert h.client.get(f"{h.api}/status", headers=dev.headers).status_code == 401
    assert h.portal.db.conn.execute("SELECT held FROM profiles WHERE id=?", (profile.row_id,)).fetchone()[0] == 1
    for table in ("sessions", "known_devices"):
        assert h.portal.db.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    assert h.login(profile).kind == "refused"


def test_prefix_resolution_errors_change_nothing(h):
    dev = h.add_device()
    other = h.add_device()
    with h.portal.db.transaction() as c:
        c.execute("UPDATE devices SET id=? WHERE id=?", ("ab" + "1" * 30, dev.device_id))
        c.execute("UPDATE devices SET id=? WHERE id=?", ("ab" + "2" * 30, other.device_id))
    for bad in ("ab", "", "ff00ff"):
        with pytest.raises(admin.AdminError):
            admin.revoke_device(h.portal.db, bad, h.clock())
    assert h.portal.db.conn.execute("SELECT COUNT(*) FROM devices WHERE revoked_at IS NOT NULL").fetchone()[0] == 0
    assert admin.revoke_device(h.portal.db, "ab2", h.clock()).startswith("ab2")
    assert admin.revoke_device(h.portal.db, "ab" + "1" * 30, h.clock())  # an exact id works
    with pytest.raises(admin.AdminError):
        admin.hold(h.portal.db, "zz")


def test_hold_and_release(h):
    dev = h.pair()
    prof = h.enable(dev)
    out = h.login(prof)
    assert admin.hold(h.portal.db, prof.key[:5]) == prof.key
    assert auth.resolve_session(h.portal, [out.session_id], h.clock()) is None
    assert h.portal.db.conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0
    assert h.login(prof).kind == "refused"
    prof.config_rev = 2
    assert h.put_profile(dev, prof).status_code == 200  # the Mac cannot clear it
    assert h.client.get(f"{h.api}/profiles/{prof.key}/inbox", headers=dev.headers).json()["held"] is True
    assert admin.release(h.portal.db, prof.key) == prof.key
    assert h.login(prof).kind == "ok"


def test_purge_profile_requires_confirmation(h):
    dev = h.pair()
    prof = h.enable(dev)
    other = h.enable(dev)
    for _ in range(2):
        h.login(prof, code="wrong")
    with pytest.raises(admin.AdminError):
        admin.purge_profile(h.portal.db, h.portal.cipher, prof.key, False)
    assert h.portal.db.conn.execute("SELECT COUNT(*) FROM profiles").fetchone()[0] == 2
    admin.hold(h.portal.db, prof.key)
    assert admin.purge_profile(h.portal.db, h.portal.cipher, prof.key[:8], True) == prof.key
    c = h.portal.db.conn
    assert c.execute("SELECT COUNT(*) FROM profiles").fetchone()[0] == 1
    assert c.execute("SELECT COUNT(*) FROM snapshots WHERE profile_id=?", (prof.row_id,)).fetchone()[0] == 0
    assert c.execute("SELECT COUNT(*) FROM throttle WHERE bucket LIKE 'ah:%' OR bucket LIKE 'h:%'").fetchone()[0] == 0
    assert c.execute("SELECT COUNT(*) FROM snapshots WHERE profile_id=?", (other.row_id,)).fetchone()[0] == 2
    assert tuple(c.execute("SELECT device_id, config_rev, held FROM profile_tombstones WHERE profile_key=?", (prof.key,)).fetchone()) == (dev.device_id, 1, 1)


def test_rotate_key(tmp_path, monkeypatch):
    fast_scrypt(monkeypatch)
    old_harness = Harness.create(tmp_path, PORTAL_DATA_KEY=OLD_KEY_HEX)  # everything is sealed with the OLD key
    dev = old_harness.pair()
    prof = old_harness.enable(dev)
    ops.answer_questions(old_harness.portal, prof.row_id, [(QID, "Oui")], old_harness.clock())
    old_id = old_harness.portal.cipher.key_id
    db = old_harness.portal.db
    cipher = Cipher(bytes.fromhex(KEY_HEX), bytes.fromhex(OLD_KEY_HEX))
    counts = admin.rotate_key(db, cipher)
    assert counts == {"snapshots": 2, "events": 1, "documents": 2}
    for table in ("snapshots", "events", "documents"):
        assert {r[0] for r in db.conn.execute(f"SELECT key_id FROM {table}")} == {cipher.key_id}
    assert cipher.key_id != old_id
    old_harness.portal.cipher = Cipher(bytes.fromhex(KEY_HEX))  # the new key alone reads everything
    v = ops.load_view(old_harness.portal, prof.row_id, old_harness.clock())
    assert v.snapshot.display_name == "Léa" and len(v.pending) == 1
    assert ops.open_document(old_harness.portal, prof.row_id, v.snapshot.documents[0].doc_id) == PDF_A
    assert admin.rotate_key(db, cipher) == {"snapshots": 0, "events": 0, "documents": 0}  # idempotent


def test_rotate_key_failure_rolls_back_everything(tmp_path, monkeypatch):
    fast_scrypt(monkeypatch)
    harness = Harness.create(tmp_path, PORTAL_DATA_KEY=OLD_KEY_HEX)
    prof = harness.enable()
    db = harness.portal.db
    with db.transaction() as c:  # corrupt one row
        c.execute("UPDATE documents SET ciphertext = zeroblob(40) WHERE rowid = (SELECT MAX(rowid) FROM documents)")
    before = [tuple(r) for r in db.conn.execute("SELECT key_id, nonce FROM snapshots ORDER BY purpose")]
    with pytest.raises(CryptoError):
        admin.rotate_key(db, Cipher(bytes.fromhex(KEY_HEX), bytes.fromhex(OLD_KEY_HEX)))
    assert [tuple(r) for r in db.conn.execute("SELECT key_id, nonce FROM snapshots ORDER BY purpose")] == before
    assert prof.row_id


def test_cli_main(tmp_path, monkeypatch, capsys):
    fast_scrypt(monkeypatch)
    harness = Harness.create(tmp_path)
    dev = harness.pair()
    prof = harness.enable(dev)
    harness.portal.db.close()
    env = make_env(tmp_path)
    lines: list[str] = []
    assert admin.main(["list"], env, lines.append) == 0 and prof.key in lines[0]
    assert admin.main(["hold", prof.key[:6]], env, lines.append) == 0
    assert admin.main(["release", prof.key], env, lines.append) == 0
    assert admin.main(["purge-profile", prof.key], env, lines.append) == 1  # no --yes
    assert "--yes" in capsys.readouterr().err
    assert admin.main(["rotate-key"], env, lines.append) == 1 and "PORTAL_DATA_KEY_OLD" in capsys.readouterr().err
    assert admin.main(["revoke-device", "nope"], env, lines.append) == 1
    assert admin.main(["list"], {**env, "PORTAL_DATA_KEY": "bad-SECRETVALUE"}, lines.append) == 1
    assert "SECRETVALUE" not in capsys.readouterr().err
    env_rot = make_env(tmp_path, PORTAL_DATA_KEY_OLD=OLD_KEY_HEX)
    assert admin.main(["rotate-key"], env_rot, lines.append) == 0
    assert admin.main(["purge-profile", prof.key, "--yes"], env, lines.append) == 0
