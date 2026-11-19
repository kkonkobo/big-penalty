"""Crypto (AES-GCM sealing, key rotation) and configuration of the portal server."""

from __future__ import annotations

import base64

import pytest
from portal_server_fakes import KEY_HEX, OLD_KEY_HEX, make_env

from jobhunt.portal.server.config import Config, ConfigError, parse_key
from jobhunt.portal.server.crypto import Cipher, CryptoError, Sealed

KEY, OLD = bytes.fromhex(KEY_HEX), bytes.fromhex(OLD_KEY_HEX)


def test_round_trip_and_fresh_nonces():
    c = Cipher(KEY)
    a = c.seal("snapshot", 1, "0", "hello Léa".encode())
    b = c.seal("snapshot", 1, "0", "hello Léa".encode())
    assert c.open("snapshot", 1, "0", a) == "hello Léa".encode()
    assert a.nonce != b.nonce and a.ciphertext != b.ciphertext and len(a.nonce) == 12
    assert len({c.seal("event", 1, "x", b"same").nonce for _ in range(200)}) == 200


def test_associated_data_binds_purpose_profile_and_row():
    c = Cipher(KEY)
    s = c.seal("document", 3, "docA", b"pdf")
    for args in (("document", 4, "docA"), ("document", 3, "docB"), ("event", 3, "docA"), ("snapshot", 3, "docA")):
        with pytest.raises(CryptoError):
            c.open(*args, s)


def test_tampering_fails():
    c = Cipher(KEY)
    s = c.seal("results", 1, "0", b"data")
    flipped = Sealed(s.key_id, s.nonce, bytes([s.ciphertext[0] ^ 1]) + s.ciphertext[1:])
    with pytest.raises(CryptoError):
        c.open("results", 1, "0", flipped)
    with pytest.raises(CryptoError):
        c.open("results", 1, "0", Sealed(s.key_id, bytes(12), s.ciphertext))
    with pytest.raises(CryptoError):
        c.open("results", 1, "0", Sealed("deadbeef", s.nonce, s.ciphertext))


def test_wrong_key_fails_and_key_id_is_not_the_key():
    s = Cipher(KEY).seal("snapshot", 1, "0", b"x")
    with pytest.raises(CryptoError):
        Cipher(OLD).open("snapshot", 1, "0", s)
    assert s.key_id != KEY_HEX[:8] and len(s.key_id) == 8 and s.key_id != Cipher(OLD).key_id


def test_old_key_readable_and_reseal():
    old_sealed = Cipher(OLD).seal("event", 2, "ev1", b"payload")
    both = Cipher(KEY, OLD)
    assert both.open("event", 2, "ev1", old_sealed) == b"payload"
    assert both.needs_rotation(old_sealed)
    new = both.reseal("event", 2, "ev1", old_sealed)
    assert not both.needs_rotation(new) and Cipher(KEY).open("event", 2, "ev1", new) == b"payload"
    with pytest.raises(CryptoError):
        Cipher(KEY).open("event", 2, "ev1", old_sealed)


def test_macs_are_keyed_and_separated():
    c = Cipher(KEY)
    assert c.mac("csrf", "x") == c.mac("csrf", "x")
    assert len({c.mac(p, "x") for p in ("address", "csrf", "login")}) == 3
    assert Cipher(OLD).mac("csrf", "x") != c.mac("csrf", "x")


# ---------------------------------------------------------------- config
def test_config_defaults(tmp_path):
    cfg = Config.from_env(make_env(tmp_path))
    assert cfg.base_path == "/app" and cfg.max_devices == 1 and cfg.pairing_enabled and cfg.allowed_hosts == {"testserver"}
    assert KEY_HEX not in repr(cfg)


def test_key_formats():
    assert parse_key("K", KEY_HEX) == KEY
    assert parse_key("K", base64.urlsafe_b64encode(KEY).decode().rstrip("=")) == KEY
    assert parse_key("K", base64.urlsafe_b64encode(KEY).decode()) == KEY
    for bad in ("", "abc", "zz" * 32, "00" * 31, base64.urlsafe_b64encode(b"x" * 31).decode()):
        with pytest.raises(ConfigError):
            parse_key("K", bad)


@pytest.mark.parametrize("name,value", [
    ("PORTAL_DATA_KEY", None), ("PORTAL_DATA_KEY", "tooshort-SECRETVALUE"), ("PORTAL_DATA_KEY_OLD", KEY_HEX),
    ("PORTAL_PUBLIC_ORIGIN", None), ("PORTAL_PUBLIC_ORIGIN", "http://example.org"), ("PORTAL_PUBLIC_ORIGIN", "https://example.org/app"),
    ("PORTAL_PUBLIC_ORIGIN", "http://127.0.0.1:8000"), ("PORTAL_PUBLIC_ORIGIN", "ftp://example.org"),
    ("PORTAL_BASE_PATH", "app"), ("PORTAL_BASE_PATH", "/app/"), ("PORTAL_BASE_PATH", "/a b"),
    ("PORTAL_TRUSTED_PROXIES", "not-a-cidr"), ("PORTAL_PAIRING_EXPIRES", "2030-01-01T00:00:00"), ("PORTAL_PAIRING_EXPIRES", "soon"),
    ("PORTAL_MAX_DEVICES", "many"), ("PORTAL_MAX_DEVICES", "0")])
def test_config_errors_never_contain_values(tmp_path, name, value):
    with pytest.raises(ConfigError) as err:
        Config.from_env(make_env(tmp_path, **{name: value}))
    assert "SECRETVALUE" not in str(err.value) and KEY_HEX not in str(err.value)


def test_insecure_local_only_when_flagged(tmp_path):
    cfg = Config.from_env(make_env(tmp_path, PORTAL_PUBLIC_ORIGIN="http://127.0.0.1:8123", PORTAL_ALLOW_INSECURE_LOCAL="1"))
    assert cfg.public_origin == "http://127.0.0.1:8123" and cfg.allowed_hosts == {"127.0.0.1:8123"}
    with pytest.raises(ConfigError):
        Config.from_env(make_env(tmp_path, PORTAL_PUBLIC_ORIGIN="http://evil.example:80", PORTAL_ALLOW_INSECURE_LOCAL="1"))


def test_pairing_off_unless_complete(tmp_path):
    assert not Config.from_env(make_env(tmp_path, PORTAL_PAIRING_CODE=None)).pairing_enabled
    assert not Config.from_env(make_env(tmp_path, PORTAL_PAIRING_EXPIRES=None)).pairing_enabled
    assert not Config.from_env(make_env(tmp_path, PORTAL_PAIRING_CODE="x" * 31)).pairing_enabled
    assert Config.from_env(make_env(tmp_path, PORTAL_PAIRING_CODE="x" * 32)).pairing_enabled


def test_trusted_proxies_and_extras(tmp_path):
    cfg = Config.from_env(make_env(tmp_path, PORTAL_TRUSTED_PROXIES="10.0.0.0/8, 172.18.0.1", PORTAL_MAX_DEVICES="3",
                                   PORTAL_BASE_PATH="/portal", PORTAL_DATA_KEY_OLD=OLD_KEY_HEX))
    assert len(cfg.trusted_proxies) == 2 and cfg.max_devices == 3 and cfg.base_path == "/portal" and cfg.data_key_old == OLD
