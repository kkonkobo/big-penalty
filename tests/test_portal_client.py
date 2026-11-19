"""portal/client.py and portal/secrets.py: TLS-only rule, redirects, size cap, error mapping, no token in messages."""

from __future__ import annotations

import os
import json
import stat

import httpx
import pytest

from jobhunt.portal import protocol, secrets
from jobhunt.portal.client import MAX_RESPONSE_BYTES, PortalClient, PortalError, PortalOffline

TOKEN = "not-a-real-device-token-used-only-by-tests"
KEY = "a" * 32


def client_for(handler, base="http://127.0.0.1:1/app", token=TOKEN):
    return PortalClient(base, token, transport=httpx.MockTransport(handler))


@pytest.mark.parametrize("url", ["http://example.org/app", "ftp://x.test", "https://", "http://127.0.0.1.evil.test/app",
                                 "https://user:pw@host.test/app", "https://host.test/app?x=1", "javascript:alert(1)", "//host.test/app"])
def test_refuses_bad_base_urls(url):
    with pytest.raises(PortalError) as e:
        PortalClient(url, TOKEN)
    assert e.value.code == "bad_url"


@pytest.mark.parametrize("url", ["https://host.test/app", "http://127.0.0.1:8000/app"])
def test_accepts_https_and_loopback(url):
    PortalClient(url, TOKEN).close()


def test_sends_bearer_and_json_headers_to_the_versioned_path():
    seen = {}

    def handler(req):
        seen.update(path=req.url.path, auth=req.headers["authorization"], ct=req.headers.get("content-type"))
        return httpx.Response(200, json={"protocol": 3, "version": "x", "profiles": []})

    client_for(handler).status()
    assert seen["path"] == "/app/api/v3/status" and seen["auth"] == f"Bearer {TOKEN}"


def test_pair_sends_no_bearer():
    seen = {}

    def handler(req):
        seen["auth"] = req.headers.get("authorization")
        seen["ct"] = req.headers["content-type"]
        return httpx.Response(200, json={"device_id": "b" * 32})

    r = client_for(handler, token=None).pair("code", "Mac", "c" * 64)
    assert r.device_id == "b" * 32 and seen["auth"] is None and seen["ct"] == "application/json"


def test_does_not_follow_redirects():
    calls = []

    def handler(req):
        calls.append(req.url.path)
        return httpx.Response(302, headers={"location": "https://evil.test/steal"})

    with pytest.raises(PortalError) as e:
        client_for(handler).status()
    assert e.value.code == "redirect" and len(calls) == 1


def test_response_size_cap_declared_and_streamed():
    def declared(req):
        return httpx.Response(200, headers={"content-length": str(MAX_RESPONSE_BYTES + 1)}, content=b"{}")

    def streamed(req):
        return httpx.Response(200, content=iter([b"x" * (MAX_RESPONSE_BYTES // 2 + 1)] * 2))

    for h in (declared, streamed):
        with pytest.raises(PortalError) as e:
            client_for(h).status()
        assert e.value.code == "too_large"


def test_error_mapping_never_shows_the_body_or_the_token():
    cases = {401: ("unauthorized", None), 404: ("not_found", None), 413: ("too_large", None), 422: ("invalid", None),
             429: ("throttled", None), 500: ("http_error", None)}
    for status, (code, _) in cases.items():
        body = {"error": "whatever", "echo": TOKEN}
        with pytest.raises(PortalError) as e:
            client_for(lambda r, s=status: httpx.Response(s, json=body)).status()
        assert e.value.code == code and e.value.status == status
        assert TOKEN not in str(e.value) and "whatever" not in str(e.value) and TOKEN not in repr(e.value)


def test_409_codes_carry_current():
    for code in ("config_rev", "rev", "ack", "stream", "handle_taken"):
        with pytest.raises(PortalError) as e:
            client_for(lambda r, c=code: httpx.Response(409, json={"error": c, "current": 7})).status()
        assert (e.value.code, e.value.status, e.value.current) == (code, 409, 7)
    with pytest.raises(PortalError) as e:  # an unknown 409 code is not trusted
        client_for(lambda r: httpx.Response(409, json={"error": "weird"})).status()
    assert e.value.code == "http_error"


def test_invalid_success_body_is_an_error():
    for content in (b"not json", b'{"profiles": []}', b'{"protocol":1,"profiles":[]}', b'{"protocol":2,"extra":1}'):
        with pytest.raises(PortalError) as e:
            client_for(lambda r, c=content: httpx.Response(200, content=c)).status()
        assert e.value.code == "bad_response"


def test_network_error_is_offline_without_leaking():
    def boom(req):
        raise httpx.ConnectError(f"cannot reach {req.url} with {TOKEN}")

    with pytest.raises(PortalOffline) as e:
        client_for(boom).status()
    assert TOKEN not in str(e.value) and "127.0.0.1" not in str(e.value)
    assert isinstance(e.value, PortalError)


def test_inbox_keeps_a_valid_envelope_with_an_invalid_body():
    good = {"seq": 1, "event_id": "1" * 32, "at": "2026-11-18T10:00:00Z", "kind": "answer", "path": "city", "value": "Lyon", "base_ver": "2" * 32}
    bad = {"seq": 2, "event_id": "3" * 32, "at": "2026-11-18T10:00:00Z", "kind": "answer", "path": "NOT A PATH"}
    page = client_for(lambda r: httpx.Response(200, json={"stream_id": "4" * 32, "last_seq": 2, "acked_seq": 0,
                                                       "sessions_epoch": 0, "revoked_through_seq": 0, "config_rev": 1, "enabled": True,
                                                        "held": True, "events": [good, bad]})).get_inbox(KEY, 0)
    assert page.held and page.acked_seq == 0 and page.last_seq == 2 and page.events[0].kind == "answer"
    assert type(page.events[1]).__name__ == "InvalidEvent" and page.events[1].seq == 2
    broken = {"seq": "x"}
    with pytest.raises(PortalError):
        client_for(lambda r: httpx.Response(200, json={"stream_id": "4" * 32, "last_seq": 1, "acked_seq": 0,
                                                     "sessions_epoch": 0, "revoked_through_seq": 0, "config_rev": 1, "enabled": True,
                                                      "events": [broken]})).get_inbox(KEY, 0)


def test_profile_key_is_validated_before_it_reaches_a_url():
    c = client_for(lambda r: httpx.Response(200, json={}))
    with pytest.raises(PortalError):
        c.get_inbox("../../status", 0)


def test_oversized_request_is_not_sent():
    sent = []
    c = client_for(lambda r: sent.append(r) or httpx.Response(200, json={}))
    with pytest.raises(PortalError) as e:
        c.put_document(KEY, "b" * 32, b"%PDF-" + b"0" * protocol.MAX_DOCUMENT_BYTES)
    assert e.value.code == "too_large" and not sent


def test_document_upload_headers():
    seen = {}

    def handler(req):
        seen.update(ct=req.headers["content-type"], cl=req.headers["content-length"], path=req.url.path)
        return httpx.Response(200, json={})

    client_for(handler).put_document(KEY, "b" * 32, b"%PDF-1.4 x")
    assert seen["ct"] == "application/pdf" and seen["cl"] == "10" and seen["path"].endswith(f"/documents/{'b' * 32}")


# ------------------------------------------------------------------ secrets
def test_file_token_store_is_0600_and_bound_to_the_origin():
    secrets.store_token("https://a.test", TOKEN, "file")
    path = secrets._file()
    assert os.name != "posix" or stat.S_IMODE(path.stat().st_mode) == 0o600
    assert secrets.load_token("https://a.test", "file") == TOKEN
    assert secrets.load_token("https://other.test", "file") is None
    path.chmod(0o644)
    with pytest.raises(secrets.PortalSecretError) as e:
        secrets.load_token("https://a.test", "file")
    assert TOKEN not in str(e.value)
    path.chmod(0o600)
    secrets.delete_token("https://a.test", "file")
    assert secrets.load_token("https://a.test", "file") is None
    secrets.delete_token("https://a.test", "file")  # already gone: fine


def test_keyring_without_backend_is_a_clear_error_no_fallback(monkeypatch):
    monkeypatch.setattr(secrets, "_load_keyring", lambda: None)
    for fn in (lambda: secrets.store_token("https://a.test", TOKEN, "keyring"), lambda: secrets.load_token("https://a.test", "keyring")):
        with pytest.raises(secrets.PortalSecretError) as e:
            fn()
        assert "trousseau" in str(e.value).lower() and TOKEN not in str(e.value)
    assert not secrets._file().exists()


class FakeKeyring:
    class backend:
        priority = 5

    def __init__(self):
        self.store = {}

    def get_keyring(self):
        return self.backend()

    def set_password(self, s, a, p):
        self.store[(s, a)] = p

    def get_password(self, s, a):
        return self.store.get((s, a))

    def delete_password(self, s, a):
        if (s, a) not in self.store:
            raise RuntimeError("PasswordDeleteError")
        del self.store[(s, a)]


def test_keyring_roundtrip_uses_service_and_origin(monkeypatch):
    kr = FakeKeyring()
    monkeypatch.setattr(secrets, "_load_keyring", lambda: kr)
    secrets.store_token("https://a.test", TOKEN, "keyring")
    assert kr.store == {("big-penalty-portal", "https://a.test"): TOKEN}
    assert secrets.load_token("https://a.test", "keyring") == TOKEN
    secrets.delete_token("https://a.test", "keyring")
    assert secrets.load_token("https://a.test", "keyring") is None


def test_keyring_failure_message_has_no_token(monkeypatch):
    kr = FakeKeyring()

    def boom(*a):
        raise RuntimeError(f"locked {TOKEN}")

    kr.set_password = boom
    monkeypatch.setattr(secrets, "_load_keyring", lambda: kr)
    with pytest.raises(secrets.PortalSecretError) as e:
        secrets.store_token("https://a.test", TOKEN, "keyring")
    assert TOKEN not in str(e.value) and e.value.__cause__ is None and e.value.__context__ is None
