"""Job seeker authentication: client address, login and its throttling, sessions, cookies, CSRF, same-origin, throttle races."""

from __future__ import annotations

import threading
import time

import pytest
from portal_server_fakes import ORIGIN, Harness, fast_scrypt, make_env

from jobhunt.portal import protocol
from jobhunt.portal.server import auth, throttle
from jobhunt.portal.server.config import Config
from jobhunt.portal.server.db import Database


@pytest.fixture
def h(tmp_path, monkeypatch):
    fast_scrypt(monkeypatch)
    harness = Harness.create(tmp_path)
    yield harness
    harness.portal.db.close()


def cfg(tmp_path, **env):
    return Config.from_env(make_env(tmp_path, **env))


# ---------------------------------------------------------------- client address
def test_client_address_without_proxy_ignores_forwarded_for(tmp_path):
    c = cfg(tmp_path)
    assert auth.client_address(("203.0.113.9", 1), {"x-forwarded-for": "192.0.2.4"}, c) == "203.0.113.9"
    assert auth.client_address(None, {}, c) == "unknown"
    assert auth.client_address(("testclient", 5), {}, c) == "testclient"


def test_client_address_through_trusted_proxy(tmp_path):
    c = cfg(tmp_path, PORTAL_TRUSTED_PROXIES="192.0.2.0/24, 203.0.113.2")
    peer = ("203.0.113.2", 5)
    assert auth.client_address(peer, {"x-forwarded-for": "198.51.100.5"}, c) == "198.51.100.5"
    # spoofed prefix: the right-most entry that is not a proxy wins
    assert auth.client_address(peer, {"x-forwarded-for": "203.0.113.6, 198.51.100.5, 192.0.2.11"}, c) == "198.51.100.5"
    raw = [(b"x-forwarded-for", b"203.0.113.6"), (b"X-Forwarded-For", b"198.51.100.5, 192.0.2.9")]
    assert auth.client_address(peer, raw, c) == "203.0.113.2"
    # malformed -> peer; none -> peer; all trusted -> peer
    assert auth.client_address(peer, {"x-forwarded-for": "198.51.100.5, garbage"}, c) == "203.0.113.2"
    assert auth.client_address(peer, {}, c) == "203.0.113.2"
    assert auth.client_address(peer, {"x-forwarded-for": "192.0.2.1"}, c) == "203.0.113.2"
    # an untrusted peer cannot borrow the header
    assert auth.client_address(("198.51.100.8", 1), {"x-forwarded-for": "203.0.113.1"}, c) == "198.51.100.8"


def test_ipv6_addresses_share_their_slash_64(tmp_path):
    c = cfg(tmp_path)
    assert auth.client_address(("2001:db8:1:2::1", 1), {}, c) == auth.client_address(("2001:db8:1:2:ffff::9", 1), {}, c)
    assert auth.client_address(("::ffff:198.51.100.4", 1), {}, c) == "198.51.100.4"


# ---------------------------------------------------------------- cookies, csrf, same origin
def test_cookie_helpers(h):
    kw = auth.cookie_kwargs(h.config, auth.SESSION_COOKIE, "v", 100)
    assert kw["secure"] and kw["httponly"] and kw["samesite"] == "strict" and kw["path"] == "/app/" and "domain" not in kw
    header = auth.cookie_header(h.config, auth.DEVICE_COOKIE, "v", 5)
    assert header == "__Secure-bp_dev=v; Max-Age=5; Path=/app/; HttpOnly; Secure; SameSite=Strict"
    assert "Max-Age=0" in auth.clear_cookie_header(h.config, auth.LOGIN_COOKIE)
    raw = [(b"cookie", b"a=1; __Secure-bp_session=one"), (b"Cookie", b"__Secure-bp_session=two; b=2")]
    assert auth.cookie_values(raw, auth.SESSION_COOKIE) == ["one", "two"]


def test_csrf_token(h):
    t = auth.csrf_token(h.portal, "sess-1")
    assert auth.csrf_ok(h.portal, "sess-1", t)
    assert not auth.csrf_ok(h.portal, "sess-2", t) and not auth.csrf_ok(h.portal, "sess-1", t[:-1] + "0") and not auth.csrf_ok(h.portal, "sess-1", None)
    assert not auth.csrf_ok(h.portal, "", t) and not auth.csrf_ok(h.portal, "sess-1", "é")


def test_login_nonce(h):
    n = auth.new_login_nonce(h.portal, h.clock())
    assert auth.login_nonce_ok(h.portal, n, n, h.clock() + 899)
    assert not auth.login_nonce_ok(h.portal, n, n, h.clock() + 901)
    assert not auth.login_nonce_ok(h.portal, n, n, h.clock() - 5)
    other = auth.new_login_nonce(h.portal, h.clock())
    assert not auth.login_nonce_ok(h.portal, n, other, h.clock())
    assert not auth.login_nonce_ok(h.portal, None, None, h.clock()) and not auth.login_nonce_ok(h.portal, n, "", h.clock())
    forged = f"{int(h.clock())}.abc.{'0' * 32}"
    assert not auth.login_nonce_ok(h.portal, forged, forged, h.clock())
    assert auth.consume_login_nonce(h.portal, n, n, h.clock())
    assert not auth.consume_login_nonce(h.portal, n, n, h.clock())


@pytest.mark.parametrize("method,headers,expected", [
    ("GET", {}, True), ("HEAD", {}, True),
    ("POST", {}, False),
    ("POST", {"Origin": ORIGIN}, True),
    ("POST", {"Origin": "https://evil.example"}, False),
    ("POST", {"Origin": "null"}, False),
    ("POST", {"Origin": ORIGIN + "/"}, False),
    ("POST", {"Sec-Fetch-Site": "same-origin"}, True),
    ("POST", {"Sec-Fetch-Site": "same-site"}, False),
    ("POST", {"Sec-Fetch-Site": "cross-site"}, False),
    ("POST", {"Origin": ORIGIN, "Sec-Fetch-Site": "cross-site"}, False),
    ("POST", {"Origin": "https://evil.example", "Sec-Fetch-Site": "same-origin"}, False),
    ("POST", {"Origin": ORIGIN, "Sec-Fetch-Site": "same-origin"}, True),
])
def test_same_origin(h, method, headers, expected):
    assert auth.same_origin_ok(method, headers, h.config) is expected


def test_origin_default_port_and_duplicate_headers(h):
    assert auth.same_origin_ok("POST", [(b"origin", (ORIGIN + ":443").encode())], h.config)
    for name, value in [(b"origin", ORIGIN.encode()), (b"sec-fetch-site", b"same-origin")]:
        assert not auth.same_origin_ok("POST", [(name, value), (name, value)], h.config)


# ---------------------------------------------------------------- login
def test_login_success_issues_session_and_known_device(h):
    prof = h.enable()
    out = h.login(prof)
    assert out.kind == "ok" and out.profile_id == prof.row_id and out.session_id and out.known_device_id
    s = auth.resolve_session(h.portal, [out.session_id], h.clock())
    assert s and s.profile_id == prof.row_id
    stored = h.portal.db.conn.execute("SELECT id_sha256 FROM sessions").fetchone()[0]
    assert stored != out.session_id and len(stored) == 64  # only the hash is stored


def test_login_normalises_handle_and_code(h):
    prof = h.enable(handle="abcde-fghjk")
    assert h.login(prof.__class__(**{**prof.__dict__, "handle": "  ABCDE fghjk "}), code=" ABCD-EFGH-JKMN ").kind == "ok"


def test_login_refusals_are_indistinguishable(h, monkeypatch):
    dev = h.pair()
    ok = h.enable(dev)
    disabled = h.enable(dev)
    held = h.enable(dev)
    disabled.config_rev = 2
    h.put_profile(dev, disabled, enabled=False)
    with h.portal.db.transaction() as c:
        c.execute("UPDATE profiles SET held=1 WHERE id=?", (held.row_id,))
    calls = []
    real = protocol.passcode_hash
    monkeypatch.setattr(protocol, "passcode_hash", lambda code, salt: (calls.append(salt), real(code, salt))[1])
    attempts = [h.login(ok, code="wrong-code-zzzz"), h.login(disabled), h.login(held),
                h.login(h.new_profile(handle="nosuchhandle")), h.login(ok, code="0")]
    assert {a for a in attempts} == {auth.LoginOutcome("refused")}
    assert len(calls) == 5  # unknown / disabled / held ran the same scrypt as a wrong code
    assert h.login(ok).kind == "ok"


def test_oversized_input_is_refused_before_anything(h, monkeypatch):
    prof = h.enable()
    monkeypatch.setattr(protocol, "passcode_hash", lambda *a: pytest.fail("must not hash"))
    assert h.login(prof, code="x" * (protocol.SECRET_MAX_INPUT + 1)).kind == "refused"


def test_per_address_and_pair_budgets(h):
    prof = h.enable()
    for _ in range(5):
        assert h.login(prof, code="wrong").kind == "refused"
    assert h.login(prof, code="wrong").kind == "throttled"
    assert h.login(prof).kind == "throttled"  # the right code is refused too once the pair budget is spent
    assert h.login(prof, addr="198.51.100.77").kind == "ok"  # another address is not affected
    h.clock.advance(901)
    assert h.login(prof).kind == "ok"


def test_address_budget_across_handles(h):
    profs = [h.enable() for _ in range(3)]
    for i in range(10):
        assert h.login(profs[i % 3], code="wrong").kind == "refused"
    assert h.login(profs[0]).kind == "throttled"


def test_success_resets_pair_bucket_and_refunds(h):
    prof = h.enable()
    for _ in range(4):
        h.login(prof, code="wrong")
    assert h.login(prof).kind == "ok"
    for _ in range(5):
        assert h.login(prof, code="wrong").kind == "refused"  # the pair bucket started again from zero


def test_anonymous_lane_stranger_does_not_lock_known_browser(h):
    prof = h.enable()
    first = h.login(prof)
    assert first.kind == "ok"
    for i in range(20):  # strangers from 20 addresses exhaust the anonymous per-handle lane (20 / hour)
        assert h.login(prof, addr=f"203.0.113.{i + 1}", code="wrong").kind == "refused"
    assert h.login(prof, addr="203.0.113.99", code="wrong").kind == "throttled"
    assert h.login(prof, addr="203.0.113.98").kind == "throttled"  # a new browser is locked out until the hour passes
    again = h.login(prof, addr="198.51.100.10", device_cookie=first.known_device_id)  # the returning browser still gets in
    assert again.kind == "ok" and again.known_device_id == first.known_device_id
    h.clock.advance(3601)
    assert h.login(prof, addr="203.0.113.98").kind == "ok"


def test_known_device_lane_has_its_own_budget(h):
    prof = h.enable()
    dev = h.login(prof).known_device_id
    for i in range(10):
        assert h.login(prof, addr=f"192.0.2.{i + 1}", device_cookie=dev, code="wrong").kind == "refused"
    assert h.login(prof, addr="192.0.2.200", device_cookie=dev).kind == "throttled"
    h.clock.advance(901)
    assert h.login(prof, addr="192.0.2.200", device_cookie=dev).kind == "ok"


def test_known_device_cookie_of_another_profile_or_epoch_only_selects_the_anonymous_lane(h):
    dev = h.pair()
    a, b = h.enable(dev), h.enable(dev)
    cookie_b = h.login(b).known_device_id
    for i in range(20):
        h.login(a, addr=f"203.0.113.{i + 1}", code="wrong")
    assert h.login(a, addr="203.0.113.99", device_cookie=cookie_b).kind == "throttled"  # b's cookie does not open a's lane
    cookie_a = h.login(b).known_device_id
    b.config_rev, b.epoch = 2, 1
    h.put_profile(dev, b)
    assert h.portal.db.conn.execute("SELECT COUNT(*) FROM known_devices WHERE profile_id=?", (b.row_id,)).fetchone()[0] == 0
    assert cookie_a  # stale after the epoch bump: no row, anonymous lane


@pytest.mark.parametrize("change", [
    "UPDATE profiles SET sessions_epoch = sessions_epoch + 1", "UPDATE profiles SET held = 1", "UPDATE profiles SET enabled = 0",
    "UPDATE profiles SET passcode_hash = zeroblob(32)", "UPDATE profiles SET handle_sha256 = hex(zeroblob(32))",
    "DELETE FROM profiles"])
def test_credentials_changing_during_the_hash_fails_the_login(h, monkeypatch, change):
    prof = h.enable()
    real = protocol.passcode_hash

    def hashing(code, salt):
        out = real(code, salt)
        with h.portal.db.transaction() as c:  # the operator rotates the credentials while scrypt runs
            c.execute(change)
        return out

    monkeypatch.setattr(protocol, "passcode_hash", hashing)
    assert h.login(prof).kind == "refused"
    assert h.portal.db.conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0


def test_login_deletes_the_presented_session(h):
    prof = h.enable()
    first = h.login(prof)
    second = h.login(prof, presented=first.session_id, device_cookie=first.known_device_id)
    assert second.kind == "ok" and second.session_id != first.session_id
    assert auth.resolve_session(h.portal, [first.session_id], h.clock()) is None
    assert auth.resolve_session(h.portal, [second.session_id], h.clock())


def test_scrypt_gate_busy_beyond_two_running_and_eight_waiting(h, monkeypatch):
    prof = h.enable()
    release, started = threading.Event(), threading.Semaphore(0)
    real = protocol.passcode_hash

    def slow(code, salt):
        started.release()
        release.wait(10)
        return real(code, salt)

    monkeypatch.setattr(protocol, "passcode_hash", slow)
    results: list[str] = []
    threads = [threading.Thread(target=lambda i=i: results.append(h.login(prof, addr=f"192.0.2.{i + 1}").kind)) for i in range(10)]
    for t in threads:
        t.start()
    deadline = time.time() + 10
    while time.time() < deadline and not (h.portal.gate._waiting == 8):
        time.sleep(0.01)
    assert h.portal.gate._waiting == 8
    started.acquire(timeout=5)
    assert h.login(prof, addr="192.0.2.99").kind == "busy"
    release.set()
    for t in threads:
        t.join(10)
    assert results == ["ok"] * 10


def test_real_scrypt_parameters_round_trip(tmp_path):  # no fast_scrypt here: N = 2**15
    harness = Harness.create(tmp_path)
    dev = harness.pair()
    prof = harness.enable(dev, with_snapshot=False)
    t = time.time()
    assert harness.login(prof).kind == "ok" and harness.login(prof, code="wrong").kind == "refused"
    assert time.time() - t < 20
    assert protocol.SCRYPT_N == 2 ** 15
    harness.portal.db.close()


# ---------------------------------------------------------------- sessions
def test_session_idle_and_absolute_expiry(h):
    prof = h.enable()
    out = h.login(prof)
    sid = [out.session_id]
    h.clock.advance(6 * 86400)
    assert auth.resolve_session(h.portal, sid, h.clock())  # touches last_seen
    h.clock.advance(6 * 86400)
    assert auth.resolve_session(h.portal, sid, h.clock())  # still inside the idle window thanks to the touch
    for _ in range(3):
        h.clock.advance(6 * 86400)
        s = auth.resolve_session(h.portal, sid, h.clock())
    assert s is None  # 30 days after creation: absolute limit even though it was used
    out2 = h.login(prof)
    h.clock.advance(7 * 86400 + 1)
    assert auth.resolve_session(h.portal, [out2.session_id], h.clock()) is None  # idle


def test_session_touch_at_most_once_a_minute(h):
    prof = h.enable()
    out = h.login(prof)
    seen = lambda: h.portal.db.conn.execute("SELECT last_seen_at FROM sessions").fetchone()[0]  # noqa: E731
    start = seen()
    h.clock.advance(30)
    auth.resolve_session(h.portal, [out.session_id], h.clock())
    assert seen() == start
    h.clock.advance(31)
    auth.resolve_session(h.portal, [out.session_id], h.clock())
    assert seen() == start + 61


def test_session_dies_with_epoch_disable_hold_and_duplicates(h):
    dev = h.pair()
    prof = h.enable(dev)
    out = h.login(prof)
    sid = out.session_id
    assert auth.resolve_session(h.portal, [sid, sid], h.clock()) is None  # two cookies: none, even equal
    assert auth.resolve_session(h.portal, [], h.clock()) is None and auth.resolve_session(h.portal, [""], h.clock()) is None
    assert auth.resolve_session(h.portal, ["x" * 500], h.clock()) is None
    assert auth.resolve_session(h.portal, [sid], h.clock())
    with h.portal.db.transaction() as c:
        c.execute("UPDATE profiles SET held=1")
    assert auth.resolve_session(h.portal, [sid], h.clock()) is None
    with h.portal.db.transaction() as c:
        c.execute("UPDATE profiles SET held=0, sessions_epoch=sessions_epoch+1")
    assert auth.resolve_session(h.portal, [sid], h.clock()) is None  # the epoch no longer matches
    with h.portal.db.transaction() as c:
        c.execute("UPDATE profiles SET sessions_epoch=sessions_epoch-1, enabled=0")
    assert auth.resolve_session(h.portal, [sid], h.clock()) is None


def test_delete_session_and_profile_sessions(h):
    prof = h.enable()
    a, b = h.login(prof), h.login(prof)
    auth.delete_session(h.portal, a.session_id)
    assert auth.resolve_session(h.portal, [a.session_id], h.clock()) is None and auth.resolve_session(h.portal, [b.session_id], h.clock())
    with h.portal.db.transaction() as c:
        auth.delete_profile_sessions(c, prof.row_id)
    assert auth.resolve_session(h.portal, [b.session_id], h.clock()) is None
    assert h.portal.db.conn.execute("SELECT COUNT(*) FROM known_devices").fetchone()[0] == 0


def test_purge_expired(h):
    from jobhunt.portal.server import db as dbmod
    prof = h.enable()
    h.login(prof)
    throttle.reserve(h.portal.db, [("x", 5, 60)], h.clock())
    h.clock.advance(8 * 86400)
    out = dbmod.purge_expired(h.portal.db, h.clock())
    assert out["sessions"] == 1 and out["known_devices"] == 0
    h.clock.advance(366 * 86400)
    assert dbmod.purge_expired(h.portal.db, h.clock())["known_devices"] == 1
    assert h.portal.db.conn.execute("SELECT COUNT(*) FROM throttle").fetchone()[0] == 0
    h.portal.db.last_purge = h.clock()
    assert dbmod.purge_if_due(h.portal.db, h.clock() + 10) is False and dbmod.purge_if_due(h.portal.db, h.clock() + 3601) is True


# ---------------------------------------------------------------- throttle
def test_throttle_cap_rejects_new_rows_atomically(h, monkeypatch):
    monkeypatch.setattr(throttle, "MAX_ROWS", 1)
    db = h.portal.db
    assert throttle.reserve(db, [("existing", 5, 60)], 1000)
    assert not throttle.reserve(db, [("existing", 5, 60), ("new", 5, 60)], 1000)
    assert throttle.count(db, "existing", 60, 1000) == 1
    assert throttle.reserve(db, [("existing", 5, 60)], 1000)


def test_reserve_is_atomic_across_buckets(h):
    db = h.portal.db
    assert throttle.reserve(db, [("a", 1, 60), ("b", 5, 60)], 1000)
    assert not throttle.reserve(db, [("b", 5, 60), ("a", 1, 60)], 1000)  # a is full: b must NOT have been incremented
    assert throttle.count(db, "b", 60, 1000) == 1
    assert throttle.reserve(db, [("a", 1, 60)], 1060)  # next window
    throttle.refund(db, [("a", 60)], 1060)
    assert throttle.count(db, "a", 60, 1060) == 0
    throttle.refund(db, [("a", 60)], 1060)
    assert throttle.count(db, "a", 60, 1060) == 0
    throttle.reset(db, ["b"])
    assert throttle.count(db, "b", 60, 1000) == 0


def test_concurrent_reserves_never_exceed_the_limit(h, tmp_path):
    db = h.portal.db
    wins: list[bool] = []
    barrier = threading.Barrier(16)

    def worker():
        barrier.wait()
        for _ in range(5):
            wins.append(throttle.reserve(db, [("race", 20, 3600), ("race2", 40, 3600)], 5000))

    threads = [threading.Thread(target=worker) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert sum(wins) == 20 and len(wins) == 80
    assert throttle.count(db, "race", 3600, 5000) == 20 and throttle.count(db, "race2", 3600, 5000) == 20
    assert isinstance(db, Database)
