"""End-to-end harness: the REAL portal server (uvicorn on 127.0.0.1, in a background thread) against the REAL Mac-side client
(`manage` / `sync` / `PortalClient`, journal and token file in the test's temp data dir). Fictional data only.

The job seeker is simulated through the server's own `auth.*` / `ops.*` functions (the HTML pages are not involved):
`Seeker` wraps them and, like a real request, resolves the session on every call.
"""

from __future__ import annotations

import secrets
import socket
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import uvicorn

from jobhunt.portal import manage, protocol, sync
from jobhunt.portal import secrets as portal_secrets
from jobhunt.portal.journal import Journal
from jobhunt.portal.server import auth, ops
from jobhunt.portal.server.app import create_app
from jobhunt.portal.server.config import Config

MAC_T0 = datetime(2026, 11, 18, 10, 0, tzinfo=UTC)
SEEKER_ADDR = "198.51.100.7"


class SessionDead(Exception):
    """The seeker's session no longer resolves (what a real page would answer with the login form)."""


class Kill(BaseException):
    """Simulates the Mac process dying at one point (BaseException: no `except Exception` of the code under test catches it)."""


class Clock:
    """The server's clock: the real one plus an offset that tests move forward (never sleeps)."""

    def __init__(self) -> None:
        self.offset = 0.0

    def __call__(self) -> float:
        return time.time() + self.offset

    def advance(self, seconds: float) -> None:
        self.offset += seconds


def _bind(port: int = 0) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", port))
    sock.listen(64)
    return sock


class LiveServer:
    """create_app(Config(...)) served by uvicorn in a thread; `stop()` / `start()` / `restart()` keep port and database file."""

    def __init__(self, tmp_path: Path, clock: Clock | None = None, **env: str) -> None:
        self.clock = clock or Clock()
        self.db_path = tmp_path / "server" / "portal.sqlite3"
        self.data_key = secrets.token_hex(32)
        self.pairing_code = "pairing-" + secrets.token_hex(24)
        self.expires = datetime.now(UTC) + timedelta(days=30)
        self._first = _bind()
        self.port = self._first.getsockname()[1]
        self.origin = f"http://127.0.0.1:{self.port}"
        self.base_url = self.origin + "/app"
        self.portal = None
        self.app = None
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None
        self.start(**env)

    # ------------------------------------------------------------ lifecycle
    def config(self, **over: str) -> Config:
        env = {"PORTAL_DATA_KEY": self.data_key, "PORTAL_PUBLIC_ORIGIN": self.origin, "PORTAL_ALLOW_INSECURE_LOCAL": "1",
               "PORTAL_DB_PATH": str(self.db_path), "PORTAL_PAIRING_CODE": self.pairing_code,
               "PORTAL_PAIRING_EXPIRES": self.expires.isoformat(), "PORTAL_VERSION": "e2e"}
        env.update(over)
        return Config.from_env({k: v for k, v in env.items() if v is not None})

    def start(self, **env: str) -> None:
        assert self._server is None, "already running"
        sock = self._first or _bind(self.port)
        self._first = None
        self.app = create_app(self.config(**env), clock=self.clock)
        self.portal = self.app.state.portal
        cfg = uvicorn.Config(self.app, log_level="warning", access_log=False, ws="none", lifespan="off")
        self._server = uvicorn.Server(cfg)
        self._thread = threading.Thread(target=self._server.run, kwargs={"sockets": [sock]}, daemon=True)
        self._thread.start()
        deadline = time.monotonic() + 15
        while not self._server.started:
            if time.monotonic() > deadline or not self._thread.is_alive():
                raise RuntimeError("the test server did not start")
            time.sleep(0.01)

    def stop(self) -> None:
        if self._server is None:
            return
        self._server.should_exit = True
        self._thread.join(15)
        assert not self._thread.is_alive(), "the test server did not stop"
        self._server, self._thread = None, None
        self.portal.db.close()

    def restart(self, **env: str) -> None:
        self.stop()
        self.start(**env)

    @property
    def running(self) -> bool:
        return self._server is not None

    # ------------------------------------------------------------ server side inspection
    @property
    def conn(self) -> sqlite3.Connection:
        return self.portal.db.conn

    def profile_row(self, profile_key: str):
        return self.conn.execute("SELECT * FROM profiles WHERE profile_key=?", (profile_key,)).fetchone()

    def row_id(self, profile_key: str) -> int:
        return self.profile_row(profile_key)["id"]

    def snapshot(self, profile_key: str) -> protocol.Snapshot | None:
        return ops._load_snapshot(self.conn, self.portal.cipher, self.row_id(profile_key))

    def stored_results(self, profile_key: str) -> list[protocol.EventResult]:
        return ops.load_results(self.conn, self.portal.cipher, self.row_id(profile_key))

    def pending(self, profile_key: str) -> list:
        return ops.load_events(self.conn, self.portal.cipher, self.row_id(profile_key))

    def raw_bytes(self) -> bytes:
        """Everything the database persists: the main file and its WAL, after a checkpoint attempt that keeps the WAL readable."""
        out = b""
        for suffix in ("", "-wal"):
            p = Path(str(self.db_path) + suffix)
            if p.exists():
                out += p.read_bytes()
        return out

    def forge_event(self, profile_key: str, model, **fields):
        """Append an event straight into the server's inbox (what a compromised or buggy server could do)."""
        pid, now = self.row_id(profile_key), self.clock()
        with self.portal.db.transaction() as c:
            ctx = ops._Ctx(c, self.portal.cipher, pid, now)
            ev = ctx.add(model, **fields)
            sealed = self.portal.cipher.seal("event", pid, ev.event_id, ev.model_dump_json().encode())
            c.execute("INSERT INTO events(profile_id, seq, event_id, at, kind, key_id, nonce, ciphertext) VALUES (?,?,?,?,?,?,?,?)",
                      (pid, ev.seq, ev.event_id, int(now), ev.kind, sealed.key_id, sealed.nonce, sealed.ciphertext))
            c.execute("UPDATE profiles SET last_seq=? WHERE id=?", (ev.seq, pid))
        return ev

    def forge_answers(self, profile_key: str, n: int, path: str, base_ver: str) -> list:
        """`n` valid chained `answer` events appended in ONE transaction (fills an inbox quickly)."""
        from jobhunt.portal.protocol import AnswerEvent

        pid, now = self.row_id(profile_key), self.clock()
        out: list = []
        with self.portal.db.transaction() as c:
            last = c.execute("SELECT last_seq FROM profiles WHERE id=?", (pid,)).fetchone()[0]
            for i in range(n):
                ev = AnswerEvent(seq=last + 1 + i, event_id=protocol.new_id(), at=datetime.fromtimestamp(now, UTC), path=path,
                                 value=f"valeur-{i}", base_ver=base_ver, after=out[-1].event_id if out else None)
                sealed = self.portal.cipher.seal("event", pid, ev.event_id, ev.model_dump_json().encode())
                c.execute("INSERT INTO events(profile_id, seq, event_id, at, kind, key_id, nonce, ciphertext) VALUES (?,?,?,?,?,?,?,?)",
                          (pid, ev.seq, ev.event_id, int(now), ev.kind, sealed.key_id, sealed.nonce, sealed.ciphertext))
                out.append(ev)
            c.execute("UPDATE profiles SET last_seq=? WHERE id=?", (last + n, pid))
        return out

    def backup_to(self, dest: Path) -> None:
        """A consistent copy of the live database (sqlite backup API)."""
        dest.parent.mkdir(parents=True, exist_ok=True)
        out = sqlite3.connect(dest)
        try:
            self.conn.backup(out)
        finally:
            out.close()

    def restore_from(self, src: Path, **env: str) -> None:
        """Stop the server, put an older copy of its database file back, start again."""
        self.stop()
        for suffix in ("", "-wal", "-shm"):
            Path(str(self.db_path) + suffix).unlink(missing_ok=True)
        self.db_path.write_bytes(src.read_bytes())
        self.start(**env)

    def login(self, handle: str, code: str, addr: str = SEEKER_ADDR, device_cookie: str | None = None) -> auth.LoginOutcome:
        return auth.login(self.portal, handle, code, addr, device_cookie, self.clock())


class Seeker:
    """One job seeker in front of the portal: logs in with identifiant + code, then every call resolves the session again."""

    def __init__(self, live: LiveServer, handle: str, code: str, addr: str = SEEKER_ADDR) -> None:
        self.live, self.handle, self.code, self.addr = live, handle, code, addr
        self.session_id: str | None = None
        self.outcome: auth.LoginOutcome | None = None

    @classmethod
    def of(cls, live: LiveServer, creds: manage.Credentials, **kw) -> Seeker:
        s = cls(live, creds.handle, creds.passcode, **kw)
        s.login()
        return s

    def login(self, handle: str | None = None, code: str | None = None) -> auth.LoginOutcome:
        out = self.live.login(handle or self.handle, code or self.code, self.addr)
        self.outcome = out
        if out.kind == "ok":
            self.session_id = out.session_id
        return out

    # ------------------------------------------------------------ session and reading
    def session(self) -> auth.Session:
        s = auth.resolve_session(self.live.portal, [self.session_id] if self.session_id else [], self.live.clock())
        if s is None:
            raise SessionDead
        return s

    @property
    def pid(self) -> int:
        return self.session().profile_id

    def alive(self) -> bool:
        try:
            self.session()
            return True
        except SessionDead:
            return False

    def view(self) -> ops.PortalView:
        return ops.load_view(self.live.portal, self.pid, self.live.clock())

    def statuses(self) -> dict[str, str]:
        return {r.event_id: r.status for r in self.view().results}

    def messages(self) -> dict[str, str]:
        return {r.event_id: r.message for r in self.view().results}

    def value(self, path: str) -> str | None:
        return self.view().fields[path].value

    # ------------------------------------------------------------ acting (each returns the SubmitOutcome)
    def edit(self, path: str, value: str, *, token: str | None = None, clear: bool = False) -> ops.SubmitOutcome:
        v = self.view()
        tok = token if token is not None else v.fields[path].token
        return ops.submit_answers(self.live.portal, self.pid, [ops.AnswerEdit(path, value, tok, clear)], self.live.clock())

    def edit_many(self, values: dict[str, str]) -> ops.SubmitOutcome:
        v = self.view()
        edits = [ops.AnswerEdit(p, val, v.fields[p].token) for p, val in values.items()]
        return ops.submit_answers(self.live.portal, self.pid, edits, self.live.clock())

    def answer_question(self, qid: str, answer: str) -> ops.SubmitOutcome:
        snapshot = self.view().snapshot
        question = next((question for question in snapshot.questions if question.qid == qid), None) if snapshot else None
        tokens = {qid: snapshot.answers[question.path].ver} if question and question.path else None
        return ops.answer_questions(self.live.portal, self.pid, [(qid, answer)], self.live.clock(), tokens=tokens)

    def edit_custom(self, custom_id: str, answer: str, *, token: str | None = None) -> ops.SubmitOutcome:
        tok = token if token is not None else next(c.token for c in self.view().custom if c.custom_id == custom_id)
        return ops.edit_custom(self.live.portal, self.pid, custom_id, answer, tok, self.live.clock())

    def delete_custom(self, custom_id: str, *, token: str | None = None) -> ops.SubmitOutcome:
        tok = token if token is not None else next(c.token for c in self.view().custom if c.custom_id == custom_id)
        return ops.delete_custom(self.live.portal, self.pid, custom_id, tok, self.live.clock())

    def request(self, *grants: str, verified: bool = True) -> ops.SubmitOutcome:
        return ops.request_intents(self.live.portal, self.pid, list(grants), self.live.clock(), verified=verified)

    def cancel(self, event_id: str) -> ops.SubmitOutcome:
        return ops.cancel_intent(self.live.portal, self.pid, event_id, self.live.clock())

    def document(self, doc_id: str) -> bytes | None:
        return ops.open_document(self.live.portal, self.pid, doc_id)


class Mac:
    """The operator's Mac: the real `manage` / `sync` functions with their defaults (journal in the temp data dir, token in the
    `file` store), and a clock the test drives."""

    def __init__(self, live: LiveServer) -> None:
        self.live = live
        self.now = MAC_T0

    @property
    def journal(self) -> Journal:
        return Journal()

    def pair(self, code: str | None = None, name: str = "Mac de test") -> manage.PairInfo:
        return manage.pair(self.live.base_url, code or self.live.pairing_code, name, "file")

    def token(self) -> str:
        return portal_secrets.load_token(self.live.origin, "file")

    def enable(self, profile_id: int, **kw) -> manage.Credentials:
        return manage.enable(profile_id, **kw)

    def sync(self, *, execute: bool = True, ids: list[int] | None = None, step: timedelta | None = timedelta(minutes=20)):
        """One real pass over HTTP. The Mac clock moves first by `step` (default: past the spacing between send batches)."""
        if step:
            self.now += step
        with patch("jobhunt.db.now", lambda: self.now):
            return sync.sync_all(ids, execute, now=self.now)

    def sync_one(self, profile_id: int, **kw):
        (rep,) = self.sync(ids=[profile_id], **kw)
        return rep


@dataclass
class Setup:
    """What a scenario usually needs: server, Mac, an enabled local profile with its seeker."""
    live: LiveServer
    mac: Mac
    profile_id: int
    creds: manage.Credentials
    seeker: Seeker

    @property
    def key(self) -> str:
        return self.mac.journal.get_profile(self.profile_id).profile_key
