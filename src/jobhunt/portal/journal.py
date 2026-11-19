"""Durable local state of the portal sync client: `<data_dir>/portal/portal.sqlite3`.

SQLite from the standard library, WAL, `synchronous=FULL`, a busy timeout; every write is ONE short explicit transaction
(`BEGIN IMMEDIATE`); the schema is migrated by `PRAGMA user_version` under `BEGIN IMMEDIATE`. The directory is 0700 and the
file 0600. Times are stored as UTC text (`2026-11-18T10:00:00.000000Z`), so they compare as strings.

Tables
- settings(key, value): server origin + base path, device id / name, token store kind, `local_secret` (32 random bytes, hex:
  HMAC key of value hashes and document ids), `paused` ("1" = global kill switch).
- profiles(profile_id PK = local jobhunt profile id, profile_key, handle, stream_id, epoch (= sessions_epoch), config_rev,
  enabled, review_mode, snapshot_rev, cursor_seq, last_sync_at, last_error, last_send_batch_at, held)
- events(profile_id, stream_id, seq, event_id UNIQUE, kind, payload_json, at, state, message, label, before_text, after_text,
  ver_after, write_ver, run_id, updated_at): one row per event taken into custody. State = a `protocol.ResultStatus`
  or `received`. `before_text` / `after_text` are for the operator's local view only (they never leave the Mac).
  `write_ver` is a version planned before a file write (crash recovery of apply_events); `ver_after` is set once the write
  is complete and recorded.
- grants(grant_id PK, profile_id, stream_id, epoch, action, app_id, rev, pdf_sha256, origin_sha, run_id, mode, issued_at,
  expires_at, state offered | used | void): binding columns use "" / 0 when not applicable, so equality finds a grant.
- versions(profile_id, key, value_hash, ver): key `a:<path>` or `c:<custom_id>`.
- refs(profile_id, kind, local_id, ref): stable opaque ids of sent items.
- budget(profile_id, day, count): portal-triggered submission attempts per profile and Paris day.
"""

from __future__ import annotations

import contextlib
import copy
import hmac
import json
import os
import secrets as _secrets
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from typing import Any, Callable, Iterator
from zoneinfo import ZoneInfo

from jobhunt.config import get_settings
from jobhunt.portal import protocol

SCHEMA_VERSION = 7
PARIS = ZoneInfo("Europe/Paris")
INTERRUPTED_MESSAGE = "Résultat inconnu : vérifiez avant de réessayer."
LOST_MESSAGE = "À refaire : la synchronisation a été réinitialisée."
VOIDED_MESSAGE = "Annulée : les réglages du portail ont changé."
REVOKED_MESSAGE = "Annulée : les accès au portail ont changé."
NON_FINAL = ("received", "awaiting_review", "queued", "running")
FINAL = protocol.FINAL_STATUSES
_STATES = ("received", "awaiting_review", "queued", "running", *protocol.FINAL_STATUSES)


class StreamError(Exception):
    """The inbox does not continue our cursor (gap, other stream, duplicated id): fail closed."""


class JournalError(Exception):
    pass


def fmt(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def parse(text: str | None) -> datetime | None:
    if not text:
        return None
    d = datetime.fromisoformat(text)
    return d if d.tzinfo else d.replace(tzinfo=UTC)


def paris_day(now: datetime) -> str:
    return now.astimezone(PARIS).date().isoformat()


def portal_dir() -> Path:
    d = get_settings().data_dir / "portal"
    d.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(d, 0o700)
    return d


@contextlib.contextmanager
def pass_lock(name: str = "sync.lock") -> Iterator[bool]:
    """Non-blocking exclusive file lock `data/portal/<name>`; yields False (nothing held) when it is busy."""
    from jobhunt.filelock import try_lock, unlock

    fd = os.open(portal_dir() / name, os.O_RDWR | os.O_CREAT, 0o600)
    got = False
    try:
        got = try_lock(fd)
        yield got
    finally:
        if got:
            unlock(fd)
        os.close(fd)


@dataclass
class ProfileRow:
    profile_id: int
    profile_key: str
    handle: str = ""
    stream_id: str = ""
    epoch: int = 0
    config_rev: int = 0
    enabled: bool = False
    review_mode: bool = False
    snapshot_rev: int = 0
    cursor_seq: int = 0
    confirmed_ack: int = 0
    last_sync_at: datetime | None = None
    last_error: str | None = None
    last_send_batch_at: datetime | None = None
    held: bool = False
    revoked_through_seq: int = 0


@dataclass
class EventRow:
    profile_id: int
    stream_id: str
    seq: int
    event_id: str
    kind: str
    payload: dict[str, Any]
    at: datetime
    state: str
    message: str = ""
    label: str = ""
    before_text: str = ""
    after_text: str = ""
    ver_after: str | None = None
    write_ver: str | None = None
    run_id: int | None = None
    updated_at: datetime | None = None
    approved: bool = False
    before_hash: str | None = None
    planned_hash: str | None = None


@dataclass
class GrantRow:
    grant_id: str
    profile_id: int
    stream_id: str
    epoch: int
    action: str
    app_id: int
    rev: int
    pdf_sha256: str
    origin_sha: str
    run_id: int
    mode: str
    issued_at: datetime
    expires_at: datetime
    state: str
    used_by: str | None = None


@dataclass
class NewEvent:
    seq: int
    event_id: str
    kind: str
    payload: dict[str, Any]
    at: datetime
    state: str = "received"
    message: str = ""


_PROFILE_COLS = ("profile_key", "handle", "stream_id", "epoch", "config_rev", "enabled", "review_mode", "snapshot_rev",
                 "cursor_seq", "confirmed_ack", "last_sync_at", "last_error", "last_send_batch_at", "held", "revoked_through_seq")
_EVENT_COLS = ("state", "message", "label", "before_text", "after_text", "ver_after", "write_ver", "run_id", "approved",
               "before_hash", "planned_hash")


def _profile(r: sqlite3.Row) -> ProfileRow:
    return ProfileRow(
        profile_id=r["profile_id"], profile_key=r["profile_key"], handle=r["handle"] or "", stream_id=r["stream_id"] or "",
        epoch=r["epoch"], config_rev=r["config_rev"], enabled=bool(r["enabled"]), review_mode=bool(r["review_mode"]),
        snapshot_rev=r["snapshot_rev"], cursor_seq=r["cursor_seq"], last_sync_at=parse(r["last_sync_at"]), last_error=r["last_error"],
        last_send_batch_at=parse(r["last_send_batch_at"]), held=bool(r["held"]), confirmed_ack=r["confirmed_ack"],
        revoked_through_seq=r["revoked_through_seq"])


def _event(r: sqlite3.Row) -> EventRow:
    return EventRow(
        profile_id=r["profile_id"], stream_id=r["stream_id"], seq=r["seq"], event_id=r["event_id"], kind=r["kind"],
        payload=json.loads(r["payload_json"]), at=parse(r["at"]), state=r["state"], message=r["message"] or "", label=r["label"] or "",
        before_text=r["before_text"] or "", after_text=r["after_text"] or "", ver_after=r["ver_after"], write_ver=r["write_ver"],
        run_id=r["run_id"], updated_at=parse(r["updated_at"]), approved=bool(r["approved"]),
        before_hash=r["before_hash"], planned_hash=r["planned_hash"])


def _grant(r: sqlite3.Row) -> GrantRow:
    return GrantRow(grant_id=r["grant_id"], profile_id=r["profile_id"], stream_id=r["stream_id"], epoch=r["epoch"], action=r["action"],
                    app_id=r["app_id"], rev=r["rev"], pdf_sha256=r["pdf_sha256"], origin_sha=r["origin_sha"], run_id=r["run_id"],
                    mode=r["mode"], issued_at=parse(r["issued_at"]), expires_at=parse(r["expires_at"]), state=r["state"], used_by=r["used_by"])


_MIGRATIONS: dict[int, str] = {1: """
CREATE TABLE settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE profiles(
  profile_id INTEGER PRIMARY KEY, profile_key TEXT NOT NULL, handle TEXT NOT NULL DEFAULT '', stream_id TEXT NOT NULL DEFAULT '',
  epoch INTEGER NOT NULL DEFAULT 0, config_rev INTEGER NOT NULL DEFAULT 0, enabled INTEGER NOT NULL DEFAULT 0,
  review_mode INTEGER NOT NULL DEFAULT 0, snapshot_rev INTEGER NOT NULL DEFAULT 0, cursor_seq INTEGER NOT NULL DEFAULT 0,
  last_sync_at TEXT, last_error TEXT, last_send_batch_at TEXT, held INTEGER NOT NULL DEFAULT 0);
CREATE TABLE events(
  profile_id INTEGER NOT NULL, stream_id TEXT NOT NULL, seq INTEGER NOT NULL, event_id TEXT NOT NULL UNIQUE, kind TEXT NOT NULL,
  payload_json TEXT NOT NULL, at TEXT NOT NULL, state TEXT NOT NULL, message TEXT NOT NULL DEFAULT '', label TEXT NOT NULL DEFAULT '',
  before_text TEXT NOT NULL DEFAULT '', after_text TEXT NOT NULL DEFAULT '', ver_after TEXT, write_ver TEXT, run_id INTEGER,
  updated_at TEXT NOT NULL, PRIMARY KEY(profile_id, stream_id, seq));
CREATE INDEX events_state ON events(profile_id, state);
CREATE TABLE grants(
  grant_id TEXT PRIMARY KEY, profile_id INTEGER NOT NULL, stream_id TEXT NOT NULL, epoch INTEGER NOT NULL, action TEXT NOT NULL,
  app_id INTEGER NOT NULL, rev INTEGER NOT NULL DEFAULT 0, pdf_sha256 TEXT NOT NULL DEFAULT '', origin_sha TEXT NOT NULL DEFAULT '',
  run_id INTEGER NOT NULL DEFAULT 0, mode TEXT NOT NULL DEFAULT '', issued_at TEXT NOT NULL, expires_at TEXT NOT NULL,
  state TEXT NOT NULL DEFAULT 'offered');
CREATE INDEX grants_binding ON grants(profile_id, stream_id, epoch, action, app_id, rev, run_id);
CREATE TABLE versions(profile_id INTEGER NOT NULL, key TEXT NOT NULL, value_hash TEXT NOT NULL, ver TEXT NOT NULL,
  PRIMARY KEY(profile_id, key));
CREATE TABLE refs(profile_id INTEGER NOT NULL, kind TEXT NOT NULL, local_id INTEGER NOT NULL, ref TEXT NOT NULL UNIQUE,
  PRIMARY KEY(profile_id, kind, local_id));
CREATE TABLE budget(profile_id INTEGER NOT NULL, day TEXT NOT NULL, count INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(profile_id, day));
""", 2: "ALTER TABLE profiles ADD COLUMN confirmed_ack INTEGER NOT NULL DEFAULT 0;",
    3: "ALTER TABLE events ADD COLUMN approved INTEGER NOT NULL DEFAULT 0;",
    4: """CREATE TABLE auto_resume(profile_id INTEGER NOT NULL, app_id INTEGER NOT NULL, run_id INTEGER NOT NULL,
          event_id TEXT NOT NULL, PRIMARY KEY(profile_id, app_id, run_id, event_id));""",
    5: "ALTER TABLE grants ADD COLUMN used_by TEXT;",
    6: "ALTER TABLE events ADD COLUMN before_hash TEXT;\nALTER TABLE events ADD COLUMN planned_hash TEXT;",
    7: "ALTER TABLE profiles ADD COLUMN revoked_through_seq INTEGER NOT NULL DEFAULT 0;"}


class Journal:
    _connection: sqlite3.Connection | None = None

    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else portal_dir() / "portal.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            os.chmod(self.path.parent, 0o700)
        if not self.path.exists():
            os.close(os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600))
        with contextlib.suppress(OSError):
            os.chmod(self.path, 0o600)
        self._migrate()

    # ------------------------------------------------------------ plumbing
    def _connect(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA busy_timeout=30000")
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=FULL")
        return c

    @contextlib.contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        if self._connection is not None:
            yield self._connection
            return
        c = self._connect()
        try:
            c.execute("BEGIN IMMEDIATE")
            try:
                yield c
            except BaseException:
                if c.in_transaction:
                    c.execute("ROLLBACK")
                raise
            c.execute("COMMIT")
        finally:
            c.close()

    @contextlib.contextmanager
    def _read(self) -> Iterator[sqlite3.Connection]:
        if self._connection is not None:
            yield self._connection
            return
        c = self._connect()
        try:
            yield c
        finally:
            c.close()

    def _migrate(self) -> None:
        with self._tx() as c:
            v = c.execute("PRAGMA user_version").fetchone()[0]
            if v > SCHEMA_VERSION:
                raise JournalError("Le journal du portail vient d'une version plus récente de l'application.")
            for n in range(v + 1, SCHEMA_VERSION + 1):
                for stmt in _MIGRATIONS[n].split(";\n"):
                    if stmt.strip():
                        c.execute(stmt)
                if n == 7:
                    now = datetime.now(UTC)
                    for row in c.execute("SELECT profile_id FROM profiles").fetchall():
                        self._retire_epoch_work(c, row[0], now)
                c.execute(f"PRAGMA user_version={n}")

    def schema_version(self) -> int:
        with self._read() as c:
            return c.execute("PRAGMA user_version").fetchone()[0]

    # ------------------------------------------------------------ settings
    def get_setting(self, key: str, default: str | None = None) -> str | None:
        with self._read() as c:
            r = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return r["value"] if r else default

    def set_setting(self, key: str, value: str) -> None:
        self.set_settings({key: value})

    def set_settings(self, values: dict[str, str]) -> None:
        with self._tx() as c:
            c.executemany("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", values.items())

    def delete_settings(self, *keys: str) -> None:
        with self._tx() as c:
            c.executemany("DELETE FROM settings WHERE key=?", [(k,) for k in keys])

    def local_secret(self) -> bytes:
        with self._tx() as c:
            r = c.execute("SELECT value FROM settings WHERE key='local_secret'").fetchone()
            if r:
                return bytes.fromhex(r["value"])
            value = _secrets.token_hex(32)
            c.execute("INSERT INTO settings(key,value) VALUES('local_secret',?)", (value,))
            return bytes.fromhex(value)

    def hmac_hex(self, text: str) -> str:
        return hmac.new(self.local_secret(), text.encode("utf-8"), sha256).hexdigest()

    @property
    def paused(self) -> bool:
        return self.get_setting("paused", "0") == "1"

    def set_paused(self, value: bool, now: datetime | None = None) -> None:
        with self._tx() as c:
            c.execute(
                "INSERT INTO settings VALUES('paused',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(int(value)),))
            if value:
                for row in c.execute("SELECT profile_id FROM profiles"):
                    stamp = now or datetime.now(UTC)
                    self._reconcile_before_control(c, row[0], stamp)
                    self._void_grants(c, row[0], stamp)

    @staticmethod
    def _config_key(profile_id: int) -> str:
        return f"pending_config:{profile_id}"

    def pending_config(self, profile_id: int) -> str | None:
        return self.get_setting(self._config_key(profile_id))

    def stage_config(self, profile_id: int, operation: str, now: datetime, **fields: Any) -> None:
        with self.transaction() as journal:
            journal.save_profile(profile_id, control_time=now, **fields)
            journal.void_grants(profile_id, now)
            journal.set_setting(self._config_key(profile_id), operation)

    def complete_config(self, profile_id: int, now: datetime, **fields: Any) -> None:
        with self.transaction() as journal:
            journal.save_profile(profile_id, control_time=now, **fields)
            journal.delete_settings(self._config_key(profile_id))

    def disable_for_unpair(self, now: datetime) -> None:
        with self.transaction() as journal:
            for profile in journal.list_profiles():
                journal.save_profile(profile.profile_id, enabled=False, control_time=now)
                journal.void_grants(profile.profile_id, now)
                journal.delete_settings(self._config_key(profile.profile_id))

    @contextlib.contextmanager
    def transaction(self) -> Iterator[Journal]:
        with self._tx() as connection:
            scoped = copy.copy(self)
            scoped._connection = connection
            yield scoped

    def controls(self, profile_id: int) -> tuple[ProfileRow | None, str]:
        profile = self.get_profile(profile_id)
        if profile is None:
            return None, "profil inconnu"
        if not profile.enabled:
            return profile, "profil désactivé"
        if self.paused:
            return profile, "pause locale"
        if profile.held:
            return profile, "profil suspendu"
        if self.pending_config(profile_id):
            return profile, "configuration en attente"
        return profile, ""

    def checkpoint(self) -> None:
        """Make an answer's write plan durable before touching the separate answers file."""
        assert self._connection is not None
        self._connection.execute("COMMIT")
        self._connection.execute("BEGIN IMMEDIATE")

    def _reconcile_before_control(self, connection: sqlite3.Connection, profile_id: int, now: datetime) -> None:
        from jobhunt.portal.apply_events import reconcile_write_plans

        scoped = copy.copy(self)
        scoped._connection = connection
        reconcile_write_plans(scoped, profile_id, now)

    def _retire_epoch_work(self, connection, profile_id: int, now: datetime, *, through: int | None = None) -> None:
        self._reconcile_before_control(connection, profile_id, now)
        self._void_grants(connection, profile_id, now, cancel_queued=through is None)
        connection.execute("UPDATE events SET state='cancelled', message=?, updated_at=? WHERE profile_id=? "
                           "AND stream_id=(SELECT stream_id FROM profiles WHERE profile_id=?) "
                           "AND seq<=? AND state IN ('received','awaiting_review','queued')",
                           (REVOKED_MESSAGE, fmt(now), profile_id, profile_id, through if through is not None else 2**63 - 1))

    # ------------------------------------------------------------ profiles
    def get_profile(self, profile_id: int) -> ProfileRow | None:
        with self._read() as c:
            r = c.execute("SELECT * FROM profiles WHERE profile_id=?", (profile_id,)).fetchone()
        return _profile(r) if r else None

    def list_profiles(self, *, enabled_only: bool = False) -> list[ProfileRow]:
        with self._read() as c:
            rows = c.execute("SELECT * FROM profiles" + (" WHERE enabled=1" if enabled_only else "") + " ORDER BY profile_id").fetchall()
        return [_profile(r) for r in rows]

    def save_profile(self, profile_id: int, *, control_time: datetime | None = None,
                     expected_stream: str | None = None, expected_epoch: int | None = None, **fields: Any) -> None:
        """Insert or update (only the given columns). `profile_key` is required for a new row."""
        cols = self._cols(fields)
        with self._tx() as c:
            current = c.execute("SELECT * FROM profiles WHERE profile_id=?", (profile_id,)).fetchone()
            if expected_stream is not None and (current is None or current["stream_id"] != expected_stream):
                raise StreamError("flux inattendu")
            if expected_epoch is not None and (current is None or current["epoch"] != expected_epoch):
                raise StreamError("version d’accès inattendue")
            if current:
                now = control_time or datetime.now(UTC)
                if "stream_id" in cols and current["stream_id"] != cols["stream_id"]:
                    self._reconcile_before_control(c, profile_id, now)
                    cols.update(cursor_seq=0, confirmed_ack=0, revoked_through_seq=0)
                    self._reset_stream_work(c, profile_id, now)
                elif "epoch" in cols and current["epoch"] != cols["epoch"]:
                    self._retire_epoch_work(c, profile_id, now)
                elif cols.get("revoked_through_seq", 0) > current["revoked_through_seq"] and current["cursor_seq"]:
                    self._retire_epoch_work(c, profile_id, now, through=cols["revoked_through_seq"])
                elif any(k in cols and current[k] != cols[k] for k in ("enabled", "review_mode")):
                    self._reconcile_before_control(c, profile_id, now)
                    self._void_grants(c, profile_id, now)
                elif "held" in cols and current["held"] != cols["held"]:
                    self._reconcile_before_control(c, profile_id, now)
                if cols:
                    c.execute("UPDATE profiles SET " + ",".join(f"{k}=?" for k in cols) + " WHERE profile_id=?", (*cols.values(), profile_id))
            else:
                if "profile_key" not in cols:
                    raise JournalError("profile_key requis")
                cols = {"profile_id": profile_id, **cols}
                c.execute(f"INSERT INTO profiles({','.join(cols)}) VALUES({','.join('?' * len(cols))})", tuple(cols.values()))

    update_profile = save_profile

    @staticmethod
    def _cols(fields: dict[str, Any]) -> dict[str, Any]:
        out = {}
        for k, v in fields.items():
            if k not in _PROFILE_COLS:
                raise JournalError(f"colonne inconnue : {k}")
            out[k] = fmt(v) if isinstance(v, datetime) else int(v) if isinstance(v, bool) else v
        return out

    # ------------------------------------------------------------ events
    def insert_page(self, profile_id: int, stream_id: str, events: list[NewEvent], now: datetime) -> int:
        """Journal a page of events and advance the cursor, in ONE transaction. The page must belong to the profile's stream and
        continue the cursor without a gap, else StreamError (nothing written). Returns the new cursor."""
        with self._tx() as c:
            p = c.execute("SELECT stream_id, cursor_seq, revoked_through_seq FROM profiles WHERE profile_id=?", (profile_id,)).fetchone()
            if p is None:
                raise StreamError("profil inconnu")
            if stream_id != p["stream_id"]:
                raise StreamError("flux inattendu")
            cursor = p["cursor_seq"]
            for ev in events:
                if ev.seq != cursor + 1:
                    raise StreamError("trou dans la séquence")
                revoked = ev.seq <= p["revoked_through_seq"]
                try:
                    c.execute("INSERT INTO events(profile_id,stream_id,seq,event_id,kind,payload_json,at,state,message,updated_at)"
                              " VALUES(?,?,?,?,?,?,?,?,?,?)",
                              (profile_id, stream_id, ev.seq, ev.event_id, ev.kind, json.dumps({} if revoked else ev.payload, ensure_ascii=False),
                               fmt(ev.at), "cancelled" if revoked else ev.state, REVOKED_MESSAGE if revoked else ev.message, fmt(now)))
                except sqlite3.IntegrityError:
                    raise StreamError("événement en double") from None
                cursor = ev.seq
            c.execute("UPDATE profiles SET cursor_seq=? WHERE profile_id=?", (cursor, profile_id))
            return cursor

    def get_event(self, event_id: str) -> EventRow | None:
        with self._read() as c:
            r = c.execute("SELECT * FROM events WHERE event_id=?", (event_id,)).fetchone()
        return _event(r) if r else None

    def events(self, profile_id: int, states: tuple[str, ...] | None = None, kinds: tuple[str, ...] | None = None) -> list[EventRow]:
        q, args = "SELECT * FROM events WHERE profile_id=?", [profile_id]
        if states:
            q += f" AND state IN ({','.join('?' * len(states))})"
            args += list(states)
        if kinds:
            q += f" AND kind IN ({','.join('?' * len(kinds))})"
            args += list(kinds)
        with self._read() as c:
            rows = c.execute(q + " ORDER BY rowid", args).fetchall()
        return [_event(r) for r in rows]

    def update_event(self, event_id: str, now: datetime, *, only_from: tuple[str, ...] | None = None, **fields: Any) -> bool:
        """Set columns of an event (state, message, label, before_text, after_text, ver_after, write_ver, run_id). With `only_from`
        the update happens only while the event is in one of those states (compare and set). True when a row changed."""
        bad = [k for k in fields if k not in _EVENT_COLS]
        if bad:
            raise JournalError(f"colonne inconnue : {bad[0]}")
        if fields.get("state") is not None and fields["state"] not in _STATES:
            raise JournalError("état inconnu")
        with self._tx() as c:
            q = "UPDATE events SET " + ",".join(f"{k}=?" for k in fields) + ",updated_at=? WHERE event_id=?"
            args = [*fields.values(), fmt(now), event_id]
            if only_from:
                q += f" AND state IN ({','.join('?' * len(only_from))})"
                args += list(only_from)
            return c.execute(q, args).rowcount == 1

    def finalize_write(self, profile_id: int, event_id: str, key: str, value_hash: str, ver: str, now: datetime, message: str = "") -> None:
        """After a successful file write, in one transaction: the field's new version and the event `applied` with that version."""
        with self._tx() as c:
            c.execute("INSERT INTO versions(profile_id,key,value_hash,ver) VALUES(?,?,?,?) ON CONFLICT(profile_id,key) DO UPDATE SET"
                      " value_hash=excluded.value_hash, ver=excluded.ver", (profile_id, key, value_hash, ver))
            c.execute("UPDATE events SET state='applied', ver_after=?, message=?, updated_at=? WHERE event_id=?",
                      (ver, message, fmt(now), event_id))

    def interrupted_sweep(self, profile_id: int, now: datetime) -> int:
        """Events found `running` when a pass starts were interrupted: finished as `interrupted`, NEVER re-executed."""
        with self._tx() as c:
            return c.execute("UPDATE events SET state='interrupted', message=?, updated_at=? WHERE profile_id=? AND state='running'",
                             (INTERRUPTED_MESSAGE, fmt(now), profile_id)).rowcount

    def latest_results(self, profile_id: int, n: int = protocol.MAX_RESULTS) -> list[protocol.EventResult]:
        with self._read() as c:
            kinds = ",".join("?" for _ in protocol.EVENT_KINDS)
            rows = c.execute(f"SELECT * FROM events WHERE profile_id=? AND kind IN ({kinds}) "
                             "ORDER BY updated_at DESC, seq DESC LIMIT ?", (profile_id, *protocol.EVENT_KINDS, n)).fetchall()
        return [protocol.EventResult(event_id=r["event_id"], kind=r["kind"], status=r["state"], message=(r["message"] or "")[:300],
                                     label=(r["label"] or "")[:500], at=parse(r["updated_at"])) for r in rows]

    def tail(self, profile_id: int, n: int = 50) -> list[EventRow]:
        with self._read() as c:
            rows = c.execute("SELECT * FROM events WHERE profile_id=? ORDER BY updated_at DESC, seq DESC LIMIT ?", (profile_id, n)).fetchall()
        return [_event(r) for r in rows]

    def event_counts(self, profile_id: int) -> dict[str, int]:
        with self._read() as c:
            rows = c.execute("SELECT state, COUNT(*) n FROM events WHERE profile_id=? GROUP BY state", (profile_id,)).fetchall()
        return {r["state"]: r["n"] for r in rows}

    # ------------------------------------------------------------ grants and the budget
    def find_or_create_grant(self, profile: ProfileRow, action: str, now: datetime, *, app_id: int, rev: int = 0, pdf_sha256: str = "",
                             origin_sha: str = "", run_id: int = 0, mode: str = "") -> str:
        """Reuse an offered, unexpired grant with the same binding; otherwise a NEW one (used, void and expired are never revived)."""
        from jobhunt.portal.settings import GRANT_LIFETIME

        with self.transaction() as journal:
            current, stop = journal.controls(profile.profile_id)
            state = "void" if stop or (current.stream_id, current.epoch, current.review_mode) != (
                profile.stream_id, profile.epoch, profile.review_mode) else "offered"
            connection = journal._connection
            r = connection.execute("SELECT grant_id FROM grants WHERE profile_id=? AND stream_id=? AND epoch=? AND action=? AND app_id=? AND rev=?"
                                   " AND pdf_sha256=? AND origin_sha=? AND run_id=? AND mode=? AND state=? AND expires_at>? LIMIT 1",
                                   (profile.profile_id, profile.stream_id, profile.epoch, action, app_id, rev, pdf_sha256, origin_sha, run_id,
                                    mode, state, fmt(now))).fetchone()
            if r:
                return r["grant_id"]
            gid = protocol.new_id()
            connection.execute("INSERT INTO grants(grant_id,profile_id,stream_id,epoch,action,app_id,rev,pdf_sha256,origin_sha,"
                               "run_id,mode,issued_at,expires_at,state) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                               (gid, profile.profile_id, profile.stream_id, profile.epoch, action, app_id, rev, pdf_sha256, origin_sha, run_id,
                                mode, fmt(now), fmt(now + GRANT_LIFETIME), state))
            return gid

    def get_grant(self, grant_id: str) -> GrantRow | None:
        with self._read() as c:
            r = c.execute("SELECT * FROM grants WHERE grant_id=?", (grant_id,)).fetchone()
        return _grant(r) if r else None

    def grants(self, profile_id: int) -> list[GrantRow]:
        with self._read() as c:
            return [_grant(r) for r in c.execute("SELECT * FROM grants WHERE profile_id=? ORDER BY issued_at", (profile_id,)).fetchall()]

    def usable_grant(self, grant_id: str, profile: ProfileRow, now: datetime) -> GrantRow | None:
        g = self.get_grant(grant_id)
        if (g is None or g.profile_id != profile.profile_id or g.stream_id != profile.stream_id or g.epoch != profile.epoch
                or g.state != "offered" or g.expires_at <= now):
            return None
        return g

    def void_grants(self, profile_id: int, now: datetime, *, cancel_queued: bool = True) -> None:
        """Void every offered grant of the profile and cancel its queued intents (`awaiting_review` events are left alone)."""
        with self._tx() as c:
            self._void_grants(c, profile_id, now, cancel_queued)

    @staticmethod
    def _void_grants(connection, profile_id, now, cancel_queued=True):
        connection.execute("UPDATE grants SET state='void' WHERE profile_id=? AND state='offered'", (profile_id,))
        connection.execute("DELETE FROM auto_resume WHERE profile_id=?", (profile_id,))
        if cancel_queued:
            connection.execute("UPDATE events SET state='cancelled', message=?, updated_at=? WHERE profile_id=? AND state='queued'",
                               (VOIDED_MESSAGE, fmt(now), profile_id))

    def use_grant_pair(self, grant_id: str, sibling_id: str | None, event_id: str, now: datetime) -> bool:
        """Mark a grant used and void its sibling in ONE transaction, only if it is still offered and unexpired."""
        with self._tx() as c:
            r = c.execute("UPDATE grants SET state='used', used_by=? WHERE grant_id=? AND state='offered' AND expires_at>?",
                          (event_id, grant_id, fmt(now)))
            if r.rowcount != 1:
                return False
            if sibling_id:
                c.execute("UPDATE grants SET state='void' WHERE grant_id=? AND state='offered'", (sibling_id,))
            return True

    def claim_intent(self, profile_id: int, event_id: str, grant_id: str, now: datetime | Callable[[], datetime], max_per_day: int,
                     **options: Any) -> str:
        return self.claim_send(profile_id, event_id, grant_id, now, max_per_day, **options)

    def record_resume_candidates(self, profile_id: int, event_id: str, candidates: list[tuple[int, int]]) -> None:
        with self._tx() as conn:
            conn.executemany("INSERT OR IGNORE INTO auto_resume VALUES (?,?,?,?)",
                             [(profile_id, app_id, run_id, event_id) for app_id, run_id in candidates])

    def resume_candidates(self, profile_id: int) -> set[tuple[int, int]]:
        with self._read() as conn:
            rows = conn.execute("SELECT a.app_id, a.run_id FROM auto_resume a JOIN events e USING(event_id) "
                                "WHERE a.profile_id=? AND e.state='applied'", (profile_id,)).fetchall()
        return {(row[0], row[1]) for row in rows}

    def forget_resume(self, profile_id: int, app_id: int, run_id: int) -> None:
        with self._tx() as conn:
            conn.execute("DELETE FROM auto_resume WHERE profile_id=? AND app_id=? AND run_id=?", (profile_id, app_id, run_id))

    def claim_send(self, profile_id: int, event_id: str | None, grant_id: str | None,
                   clock: datetime | Callable[[], datetime], max_per_day: int, *, spacing: timedelta = timedelta(),
                   batch_started: bool = False, expected: ProfileRow | None = None, validate: Callable[[], bool] | None = None,
                   refusal: str | None = None, resume_candidate: tuple[int, int] | None = None) -> str:
        """Check controls and bindings, then record the claim, budget and first batch stamp together."""
        with self.transaction() as journal:
            profile, stop = journal.controls(profile_id)
            if stop or (expected and (profile.stream_id, profile.epoch) != (expected.stream_id, expected.epoch)):
                return "controls"
            if journal.events(profile_id, ("received",)):
                return "inbox"
            event = journal.get_event(event_id) if event_id is not None else None
            if event_id is not None:
                if (event is None or event.profile_id != profile_id or event.stream_id != profile.stream_id
                        or event.state != "queued" or event.kind != "intent" or event.payload.get("grant") != grant_id):
                    return "not_queued"
            if profile.review_mode and (event is None or not event.approved):
                return "controls"
            if resume_candidate is not None and resume_candidate not in journal.resume_candidates(profile_id):
                return "not_queued"
            valid = validate is None or validate()
            now = clock() if callable(clock) else clock
            if not batch_started and profile.last_send_batch_at and now - profile.last_send_batch_at < spacing:
                return "spacing"
            grant = journal.usable_grant(grant_id, profile, now) if event is not None else None
            if event is not None and (grant is None or grant.action not in ("send", "resume") or not valid):
                if refusal is not None:
                    journal.update_event(event_id, now, state="refused", message=refusal)
                    if grant and not valid:
                        journal._connection.execute("UPDATE grants SET state='void' WHERE grant_id=?", (grant_id,))
                    return "refused"
                return "no_grant"
            if not valid:
                return "refused"
            if not journal.take_budget(profile_id, now, max_per_day):
                return "budget"
            if grant is not None:
                journal._connection.execute("UPDATE grants SET state='used' WHERE grant_id=?", (grant_id,))
                journal.update_event(event_id, now, state="running", message="")
            if resume_candidate is not None:
                journal.forget_resume(profile_id, *resume_candidate)
            if not batch_started:
                journal.save_profile(profile_id, last_send_batch_at=now)
            return "claimed"

    def take_budget(self, profile_id: int, now: datetime, max_per_day: int) -> bool:
        """Count one portal-triggered attempt that has no event (an automatic resume). False when the day's budget is used up."""
        with self._tx() as c:
            day = paris_day(now)
            row = c.execute("SELECT count FROM budget WHERE profile_id=? AND day=?", (profile_id, day)).fetchone()
            if (row["count"] if row else 0) >= max_per_day:
                return False
            c.execute("INSERT INTO budget(profile_id,day,count) VALUES(?,?,1) ON CONFLICT(profile_id,day) DO UPDATE SET count=count+1",
                      (profile_id, day))
            return True

    def sends_today(self, profile_id: int, now: datetime) -> int:
        with self._read() as c:
            r = c.execute("SELECT count FROM budget WHERE profile_id=? AND day=?", (profile_id, paris_day(now))).fetchone()
        return r["count"] if r else 0

    # ------------------------------------------------------------ versions and refs
    def version_for(self, profile_id: int, key: str, value_hash: str) -> str:
        """The version of a field: kept while its value hash is unchanged, a new random one when the value differs."""
        with self._tx() as c:
            r = c.execute("SELECT value_hash, ver FROM versions WHERE profile_id=? AND key=?", (profile_id, key)).fetchone()
            if r and r["value_hash"] == value_hash:
                return r["ver"]
            ver = protocol.new_id()
            c.execute("INSERT INTO versions(profile_id,key,value_hash,ver) VALUES(?,?,?,?) ON CONFLICT(profile_id,key) DO UPDATE SET"
                      " value_hash=excluded.value_hash, ver=excluded.ver", (profile_id, key, value_hash, ver))
            return ver

    def ref_for(self, profile_id: int, kind: str, local_id: int) -> str:
        with self._tx() as c:
            r = c.execute("SELECT ref FROM refs WHERE profile_id=? AND kind=? AND local_id=?", (profile_id, kind, local_id)).fetchone()
            if r:
                return r["ref"]
            ref = protocol.new_id()
            c.execute("INSERT INTO refs VALUES(?,?,?,?)", (profile_id, kind, local_id, ref))
            return ref

    # ------------------------------------------------------------ stream reset
    def reset_stream(self, profile_id: int, stream_id: str, now: datetime) -> None:
        """A new stream: cursor 0, every grant void, non-final events `lost`."""
        self.save_profile(profile_id, stream_id=stream_id, control_time=now)

    @staticmethod
    def _reset_stream_work(connection, profile_id, now):
        Journal._void_grants(connection, profile_id, now, cancel_queued=False)
        connection.execute(f"UPDATE events SET state='lost', message=?, updated_at=? WHERE profile_id=? AND state IN ({','.join('?' * len(NON_FINAL))})",
                           (LOST_MESSAGE, fmt(now), profile_id, *NON_FINAL))
