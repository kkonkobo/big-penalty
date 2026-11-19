"""Job seeker authentication (DESIGN §6): client address, login with throttling, sessions, cookies, CSRF.

All functions take the `Portal` container (`request.app.state.portal`: `.config`, `.db`, `.cipher`, `.gate`) and an explicit
`now` (epoch seconds) so tests use a fake clock. `login` is a plain blocking function (scrypt): call it from a sync route or
through `login_async`, never directly on the event loop."""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import os
import threading
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal
from urllib.parse import urlsplit

from starlette.concurrency import run_in_threadpool

from jobhunt.portal import protocol

from . import throttle
from .db import SESSION_ABSOLUTE, SESSION_IDLE
from .throttle import BucketSpec

if TYPE_CHECKING:
    from .app import Portal

SESSION_COOKIE = "__Secure-bp_session"
DEVICE_COOKIE = "__Secure-bp_dev"
LOGIN_COOKIE = "__Secure-bp_login"
SESSION_TOUCH = 60
LOGIN_NONCE_TTL = 15 * 60
DEVICE_COOKIE_MAX_AGE = 365 * 86400
GENERIC_LOGIN_ERROR = "Identifiant ou code incorrect, ou trop d'essais. Réessayez plus tard."

ADDR_LIMIT = (10, 900)
ADDR_HANDLE_LIMIT = (5, 900)
HANDLE_ANON_LIMIT = (20, 3600)
DEVICE_LIMIT = (10, 900)
SCRYPT_RUNNING = 2
SCRYPT_WAITING = 8

_DUMMY_SALT = bytes(range(protocol.SCRYPT_SALT_BYTES))
_DUMMY_HASH = os.urandom(protocol.SCRYPT_DKLEN)  # a random value no scrypt output equals


# ---------------------------------------------------------------- client address
def _parse_ip(text: str):
    try:
        parsed = ipaddress.ip_address(text.strip())
        return (parsed.ipv4_mapped or parsed) if parsed.version == 6 else parsed
    except ValueError:
        return None


def _trusted(ip, config) -> bool:
    return any(ip in net for net in config.trusted_proxies if net.version == ip.version)


def _canonical(ip) -> str:
    if ip.version == 6:  # one household / subscriber owns a /64: do not let a rotating suffix dodge the throttle
        return str(ipaddress.ip_network(f"{ip}/64", strict=False).network_address) + "/64"
    return str(ip)


def client_address(scope_client, headers: Mapping[str, str] | Iterable[tuple[bytes, bytes]], config) -> str:
    """The socket peer, unless it is a trusted proxy: then the right-most X-Forwarded-For entry that is not a trusted
    proxy. A malformed entry falls back to the peer. `scope_client` is the ASGI `scope["client"]`."""
    peer_text = str(scope_client[0]) if scope_client else "unknown"
    peer = _parse_ip(peer_text)
    if peer is None:
        return peer_text[:100]
    if not _trusted(peer, config):
        return _canonical(peer)
    values: list[str] = []
    items = headers.items() if isinstance(headers, Mapping) else ((k.decode("latin-1"), v.decode("latin-1")) for k, v in headers)
    for name, value in items:
        if name.lower() == "x-forwarded-for":
            values.append(value)
    if len(values) != 1:
        return _canonical(peer)
    entries = [e.strip() for v in values for e in v.split(",")]
    for entry in reversed(entries):
        ip = _parse_ip(entry)
        if ip is None:
            return _canonical(peer)
        if not _trusted(ip, config):
            return _canonical(ip)
    return _canonical(peer)


# ---------------------------------------------------------------- cookies
def cookie_values(headers: Iterable[tuple[bytes, bytes]], name: str) -> list[str]:
    """EVERY value of the cookie `name` across all Cookie headers (Starlette's `request.cookies` keeps only one)."""
    out: list[str] = []
    for key, value in headers:
        if key.lower() != b"cookie":
            continue
        for part in value.decode("latin-1").split(";"):
            k, sep, v = part.strip().partition("=")
            if sep and k == name:
                out.append(v.strip())
    return out


def cookie_kwargs(config, name: str, value: str, max_age: int) -> dict:
    """Arguments for `response.set_cookie(**...)`: HttpOnly, Secure, SameSite=Strict, Path=<base>/, no Domain."""
    return {"key": name, "value": value, "max_age": max_age, "path": config.cookie_prefix_path, "secure": True,
            "httponly": True, "samesite": "strict"}


def cookie_header(config, name: str, value: str, max_age: int) -> str:
    return f"{name}={value}; Max-Age={max_age}; Path={config.cookie_prefix_path}; HttpOnly; Secure; SameSite=Strict"


def clear_cookie_header(config, name: str) -> str:
    return cookie_header(config, name, "", 0)


# ---------------------------------------------------------------- CSRF, login nonce, same-origin
def csrf_token(portal: Portal, session_id: str) -> str:
    return portal.cipher.mac("csrf", session_id)


def csrf_ok(portal: Portal, session_id: str, token: str | None) -> bool:
    if not session_id or not token:
        return False
    return hmac.compare_digest(csrf_token(portal, session_id).encode(), token.encode("utf-8", "replace"))


def new_login_nonce(portal: Portal, now: float) -> str:
    """Value for BOTH the `__Secure-bp_login` cookie and the login form's hidden field. Valid 15 minutes."""
    body = f"{int(now)}.{protocol.new_token()}"
    return f"{body}.{portal.cipher.mac('login', 'nonce|' + body)[:32]}"


def login_nonce_ok(portal: Portal, cookie_value: str | None, form_value: str | None, now: float) -> bool:
    """Cookie and form must carry the same, authentic, unexpired nonce (constant-time compare). Clear the cookie after use."""
    if not cookie_value or not form_value or not hmac.compare_digest(cookie_value.encode(), form_value.encode("utf-8", "replace")):
        return False
    ts, _, rest = cookie_value.partition(".")
    body, _, mac = cookie_value.rpartition(".")
    if not ts.isdigit() or not rest or not hmac.compare_digest(portal.cipher.mac("login", "nonce|" + body)[:32].encode(), mac.encode()):
        return False
    return 0 <= now - int(ts) <= LOGIN_NONCE_TTL


def consume_login_nonce(portal: Portal, cookie_value: str | None, form_value: str | None, now: float) -> bool:
    if not login_nonce_ok(portal, cookie_value, form_value, now):
        return False
    digest = hashlib.sha256(cookie_value.encode()).hexdigest()
    expires_at = int(cookie_value.partition(".")[0]) + LOGIN_NONCE_TTL
    with portal.db.transaction() as conn:
        return conn.execute("INSERT OR IGNORE INTO used_login_nonces VALUES (?,?)", (digest, expires_at)).rowcount == 1


def _origin(value: str) -> tuple[str, str, int] | None:
    try:
        target = urlsplit(value)
        if (target.scheme not in {"http", "https"} or not target.hostname or target.path or "?" in value or "#" in value
                or "@" in target.netloc or any(char.isspace() for char in value)):
            return None
        port = target.port
        return target.scheme, target.hostname, port if port is not None else (443 if target.scheme == "https" else 80)
    except ValueError:
        return None


def same_origin_ok(method: str, headers: Mapping[str, str] | Iterable[tuple[bytes, bytes]], config) -> bool:
    """State-changing requests need positive same-origin evidence: Origin (if present) matches the public origin,
    Sec-Fetch-Site (if present) is `same-origin`, and at least one of the two is present. GET/HEAD are not checked."""
    if method.upper() in ("GET", "HEAD", "OPTIONS"):
        return True
    h = {}
    items = headers.items() if isinstance(headers, Mapping) else ((key.decode("latin-1"), val.decode("latin-1")) for key, val in headers)
    for key, value in items:
        key = key.lower()
        if key in {"origin", "sec-fetch-site"}:
            if key in h:
                return False
            h[key] = value
    origin, site = h.get("origin"), h.get("sec-fetch-site")
    if origin is None and site is None:
        return False
    if origin is not None and origin.strip() == "null" and site == "same-origin":
        # Browsers serialize Origin as "null" when the page's referrer policy is no-referrer (or for some privacy
        # modes); Sec-Fetch-Site is set by the browser itself and cannot be forged by another site.
        return True
    if origin is not None and ((canonical := _origin(origin)) is None or canonical != _origin(config.public_origin)):
        return False
    return site is None or site == "same-origin"


# ---------------------------------------------------------------- login
class ScryptGate:
    """At most `running` verifications at once and `waiting` queued; beyond that `run` reports busy."""

    def __init__(self, running: int = SCRYPT_RUNNING, waiting: int = SCRYPT_WAITING) -> None:
        self._sem = threading.Semaphore(running)
        self._lock = threading.Lock()
        self._max_waiting = waiting
        self._waiting = 0

    def run(self, fn: Callable[[], bytes]) -> tuple[bool, bytes | None]:
        with self._lock:
            if self._waiting >= self._max_waiting:
                return False, None
            self._waiting += 1
        self._sem.acquire()
        with self._lock:
            self._waiting -= 1
        try:
            return True, fn()
        finally:
            self._sem.release()


@dataclass(frozen=True)
class LoginOutcome:
    """`ok`: set the cookies. `refused` / `throttled` / `busy`: render the SAME generic text (GENERIC_LOGIN_ERROR);
    status 401 for refused, 429 for throttled and busy."""
    kind: Literal["ok", "refused", "throttled", "busy"]
    session_id: str | None = None        # raw value of __Secure-bp_session
    known_device_id: str | None = None   # raw value of __Secure-bp_dev
    profile_id: int | None = None


_REFUSED = LoginOutcome("refused")


def login(portal: Portal, handle: str, code: str, client_addr: str, known_device_cookie: str | None, now: float,
          presented_session: str | None = None) -> LoginOutcome:
    if len(handle or "") > protocol.SECRET_MAX_INPUT or len(code or "") > protocol.SECRET_MAX_INPUT:
        return _REFUSED
    db, cipher = portal.db, portal.cipher
    digest = protocol.handle_digest(handle or "")
    addr_key, handle_key = cipher.mac("address", client_addr), cipher.mac("login", digest)
    row = db.conn.execute("SELECT id, enabled, held, sessions_epoch, passcode_salt, passcode_hash FROM profiles "
                          "WHERE handle_sha256=?", (digest,)).fetchone()
    usable = row is not None and bool(row["enabled"]) and not row["held"]
    device_hash = hashlib.sha256(known_device_cookie.encode()).hexdigest() if known_device_cookie else None
    known = None
    if device_hash and row is not None:
        known = db.conn.execute("SELECT 1 FROM known_devices WHERE id_sha256=? AND profile_id=? AND epoch=?",
                                (device_hash, row["id"], row["sessions_epoch"])).fetchone()
    specs: list[BucketSpec] = [(f"a:{addr_key}", *ADDR_LIMIT)]
    lane = f"d:{handle_key}:{device_hash}" if known else f"h:{handle_key}"
    if row is not None:
        specs.extend([(f"ah:{addr_key}:{handle_key}", *ADDR_HANDLE_LIMIT),
                      (lane, *(DEVICE_LIMIT if known else HANDLE_ANON_LIMIT))])
    if not throttle.reserve(db, specs, now):
        return LoginOutcome("throttled")
    salt = bytes(row["passcode_salt"]) if usable else _DUMMY_SALT
    expected = bytes(row["passcode_hash"]) if usable else _DUMMY_HASH
    ran, got = portal.gate.run(lambda: protocol.passcode_hash(code or "", salt))  # blocking, outside any DB transaction
    if not ran:
        return LoginOutcome("busy")
    if not (hmac.compare_digest(got or b"", expected) and usable):
        return _REFUSED
    with db.transaction() as c:  # re-read: the credentials must be what they were when the attempt started
        fresh = c.execute("SELECT enabled, held, sessions_epoch, passcode_salt, passcode_hash, handle_sha256 FROM profiles WHERE id=?",
                          (row["id"],)).fetchone()
        if (fresh is None or not fresh["enabled"] or fresh["held"] or fresh["sessions_epoch"] != row["sessions_epoch"]
                or fresh["handle_sha256"] != digest or bytes(fresh["passcode_hash"]) != expected or bytes(fresh["passcode_salt"]) != salt):
            return _REFUSED
        if presented_session:
            c.execute("DELETE FROM sessions WHERE id_sha256=?", (hashlib.sha256(presented_session.encode()).hexdigest(),))
        session_id = protocol.new_token()
        c.execute("INSERT INTO sessions(id_sha256, profile_id, epoch, created_at, last_seen_at) VALUES (?,?,?,?,?)",
                  (hashlib.sha256(session_id.encode()).hexdigest(), row["id"], row["sessions_epoch"], int(now), int(now)))
        if known:
            device_id = known_device_cookie
            c.execute("UPDATE known_devices SET last_used_at=? WHERE id_sha256=?", (int(now), device_hash))
        else:
            device_id = protocol.new_token()
            c.execute("INSERT INTO known_devices(id_sha256, profile_id, epoch, created_at, last_used_at) VALUES (?,?,?,?,?)",
                      (hashlib.sha256(device_id.encode()).hexdigest(), row["id"], row["sessions_epoch"], int(now), int(now)))
    throttle.reset(db, [f"ah:{addr_key}:{handle_key}"])  # success clears the pair bucket and gives the shared ones back
    throttle.refund(db, [(f"a:{addr_key}", ADDR_LIMIT[1]), (lane, (DEVICE_LIMIT if known else HANDLE_ANON_LIMIT)[1])], now)
    return LoginOutcome("ok", session_id, device_id, row["id"])


async def login_async(*args, **kwargs) -> LoginOutcome:
    return await run_in_threadpool(login, *args, **kwargs)


# ---------------------------------------------------------------- sessions
@dataclass(frozen=True)
class Session:
    id: str            # raw cookie value (feeds csrf_token)
    profile_id: int
    epoch: int
    created_at: int
    last_seen_at: int


def resolve_session(portal: Portal, values: list[str], now: float) -> Session | None:
    """The session of a request, or None. More than one cookie value (even equal) means none."""
    if len(values) != 1 or not values[0] or len(values[0]) > 200:
        return None
    now = int(now)
    sid = hashlib.sha256(values[0].encode()).hexdigest()
    c = portal.db.conn
    row = _live_session(c, sid, now)
    if row is None:
        return None
    last = row["last_seen_at"]
    if now - last >= SESSION_TOUCH:
        with portal.db.transaction() as tx:
            tx.execute("UPDATE sessions SET last_seen_at=? WHERE id_sha256=? AND last_seen_at<?", (now, sid, now))
        last = now
    return Session(values[0], row["profile_id"], row["epoch"], row["created_at"], last)


def _live_session(conn, digest: str, now: float):
    return conn.execute("SELECT s.profile_id, s.epoch, s.created_at, s.last_seen_at FROM sessions s "
                        "JOIN profiles p ON p.id=s.profile_id WHERE s.id_sha256=? AND p.enabled=1 AND p.held=0 "
                        "AND s.epoch=p.sessions_epoch AND s.created_at>? AND s.last_seen_at>?",
                        (digest, now - SESSION_ABSOLUTE, now - SESSION_IDLE)).fetchone()


def session_is_live(conn, session: Session, now: float) -> bool:
    row = _live_session(conn, hashlib.sha256(session.id.encode()).hexdigest(), int(now))
    return row is not None and row["profile_id"] == session.profile_id and row["epoch"] == session.epoch


def delete_session(portal: Portal, session_id: str) -> None:
    with portal.db.transaction() as c:
        c.execute("DELETE FROM sessions WHERE id_sha256=?", (hashlib.sha256(session_id.encode()).hexdigest(),))


def delete_profile_sessions(conn, profile_id: int, known_devices: bool = True) -> None:
    """Inside the caller's transaction: kill every session (and, by default, every known browser) of a profile."""
    conn.execute("DELETE FROM sessions WHERE profile_id=?", (profile_id,))
    if known_devices:
        conn.execute("DELETE FROM known_devices WHERE profile_id=?", (profile_id,))
