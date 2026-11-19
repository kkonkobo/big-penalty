"""SQLite storage (stdlib `sqlite3`): WAL, `busy_timeout`, foreign keys, `synchronous=FULL`, short write transactions.

Times are integer epoch seconds (UTC) everywhere. Tables (schema version in `PRAGMA user_version`):

- `devices(id, name, token_sha256 UNIQUE, created_at, revoked_at)`: paired Macs; only the sha256 of the token is kept.
- `pairings(code_sha256 PK, device_id, used_at)`: one row consumes a pairing code.
- `profiles(id AUTOINCREMENT, device_id, profile_key, handle_sha256 UNIQUE, enabled, held, config_rev, config_digest,
  sessions_epoch, stream_id, passcode_salt, passcode_hash, snapshot_rev, snapshot_digest, snapshot_at, acked_seq,
  last_seq, created_at)`: one job seeker; `held` is the admin's sticky hold (only the admin tool changes it).
- `snapshots(profile_id, purpose 'snapshot'|'results', key_id, nonce, ciphertext)`: sealed JSON.
- `manifest(profile_id, doc_id, sha256, size)`: documents of the current snapshot (clear: ids are keyed hashes).
- `documents(profile_id, doc_id, sha256, size, key_id, nonce, ciphertext)`: uploaded PDFs, sealed.
- `events(profile_id, seq, event_id UNIQUE, at, kind, key_id, nonce, ciphertext)`: the inbox (not yet acked by the Mac).
- `sessions(id_sha256 PK, profile_id, epoch, created_at, last_seen_at)`, `known_devices(id_sha256 PK, profile_id, epoch,
  created_at, last_used_at)`: browser sessions and "known browser" cookies, stored as sha256.
- `throttle(bucket, window_start, count)`: fixed-window counters; bucket names hold only keyed hashes.
"""

from __future__ import annotations

import contextlib
import os
import sqlite3
import threading
from collections.abc import Iterator
from pathlib import Path

SESSION_IDLE = 7 * 86400
SESSION_ABSOLUTE = 30 * 86400
KNOWN_DEVICE_TTL = 365 * 86400
THROTTLE_KEEP = 2 * 86400
PURGE_EVERY = 3600

_SCHEMA_V1 = """
CREATE TABLE devices (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, token_sha256 TEXT NOT NULL UNIQUE,
    created_at INTEGER NOT NULL, revoked_at INTEGER);
CREATE TABLE pairings (
    code_sha256 TEXT PRIMARY KEY, device_id TEXT NOT NULL REFERENCES devices(id) ON DELETE CASCADE, used_at INTEGER NOT NULL);
CREATE TABLE profiles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id TEXT NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    profile_key TEXT NOT NULL, handle_sha256 TEXT NOT NULL UNIQUE,
    enabled INTEGER NOT NULL, held INTEGER NOT NULL DEFAULT 0,
    config_rev INTEGER NOT NULL, config_digest TEXT NOT NULL, sessions_epoch INTEGER NOT NULL, stream_id TEXT NOT NULL,
    passcode_salt BLOB NOT NULL, passcode_hash BLOB NOT NULL,
    snapshot_rev INTEGER NOT NULL DEFAULT 0, snapshot_digest TEXT, snapshot_at INTEGER,
    acked_seq INTEGER NOT NULL DEFAULT 0, last_seq INTEGER NOT NULL DEFAULT 0, created_at INTEGER NOT NULL,
    UNIQUE (device_id, profile_key));
CREATE TABLE snapshots (
    profile_id INTEGER NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    purpose TEXT NOT NULL CHECK (purpose IN ('snapshot', 'results')),
    key_id TEXT NOT NULL, nonce BLOB NOT NULL, ciphertext BLOB NOT NULL, PRIMARY KEY (profile_id, purpose));
CREATE TABLE manifest (
    profile_id INTEGER NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    doc_id TEXT NOT NULL, sha256 TEXT NOT NULL, size INTEGER NOT NULL, PRIMARY KEY (profile_id, doc_id));
CREATE TABLE documents (
    profile_id INTEGER NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    doc_id TEXT NOT NULL, sha256 TEXT NOT NULL, size INTEGER NOT NULL,
    key_id TEXT NOT NULL, nonce BLOB NOT NULL, ciphertext BLOB NOT NULL, PRIMARY KEY (profile_id, doc_id));
CREATE TABLE events (
    profile_id INTEGER NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    seq INTEGER NOT NULL, event_id TEXT NOT NULL UNIQUE, at INTEGER NOT NULL, kind TEXT NOT NULL,
    key_id TEXT NOT NULL, nonce BLOB NOT NULL, ciphertext BLOB NOT NULL, PRIMARY KEY (profile_id, seq));
CREATE TABLE sessions (
    id_sha256 TEXT PRIMARY KEY, profile_id INTEGER NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    epoch INTEGER NOT NULL, created_at INTEGER NOT NULL, last_seen_at INTEGER NOT NULL);
CREATE INDEX sessions_profile ON sessions(profile_id);
CREATE TABLE known_devices (
    id_sha256 TEXT PRIMARY KEY, profile_id INTEGER NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    epoch INTEGER NOT NULL, created_at INTEGER NOT NULL, last_used_at INTEGER NOT NULL);
CREATE INDEX known_devices_profile ON known_devices(profile_id);
CREATE TABLE throttle (
    bucket TEXT NOT NULL, window_start INTEGER NOT NULL, count INTEGER NOT NULL, PRIMARY KEY (bucket, window_start));
"""
MIGRATIONS = [_SCHEMA_V1, """
CREATE UNIQUE INDEX profiles_global_key ON profiles(profile_key);
CREATE TABLE profile_tombstones (
    profile_key TEXT PRIMARY KEY, device_id TEXT NOT NULL, config_rev INTEGER NOT NULL, held INTEGER NOT NULL);
CREATE TABLE retired_streams (
    profile_key TEXT NOT NULL, stream_id TEXT NOT NULL, PRIMARY KEY (profile_key, stream_id));
""", """
CREATE TABLE used_login_nonces (digest TEXT PRIMARY KEY, expires_at INTEGER NOT NULL);
CREATE INDEX used_login_nonces_expiry ON used_login_nonces(expires_at);
""", """
ALTER TABLE profiles ADD COLUMN revoked_through_seq INTEGER NOT NULL DEFAULT 0;
UPDATE profiles SET revoked_through_seq=last_seq;
""", """
UPDATE profiles SET revoked_through_seq=last_seq, snapshot_digest=NULL, snapshot_at=NULL;
DELETE FROM snapshots;
DELETE FROM manifest;
DELETE FROM documents;
"""]


def _prepare_file(path: Path) -> None:
    """Create the parent directory (0700) and the file (0600) when missing; tighten modes where the process may."""
    created_dir = not path.parent.exists()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if created_dir:
        with contextlib.suppress(OSError):
            os.chmod(path.parent, 0o700)
    if not path.exists():
        os.close(os.open(path, os.O_CREAT | os.O_RDWR, 0o600))
    os.chmod(path, 0o600)


class Database:
    """One connection per thread (autocommit mode; transactions are explicit and short)."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        _prepare_file(self.path)
        self._local = threading.local()
        self._all: list[sqlite3.Connection] = []
        self._lock = threading.Lock()
        self.last_purge = 0.0
        self._migrate()

    def _open(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("PRAGMA foreign_keys=ON")
        with self._lock:
            self._all.append(conn)
        return conn

    @property
    def conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._local.conn = self._open()
        return conn

    def _migrate(self) -> None:
        with self.transaction() as c:  # the version is read under the write lock: two starters cannot both migrate
            version = c.execute("PRAGMA user_version").fetchone()[0]
            if version > len(MIGRATIONS):
                raise RuntimeError("database schema is newer than this server")
            for i in range(version, len(MIGRATIONS)):
                for statement in MIGRATIONS[i].split(";\n"):
                    if statement.strip():
                        c.execute(statement)
                c.execute(f"PRAGMA user_version={i + 1}")

    @contextlib.contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """BEGIN IMMEDIATE ... COMMIT (ROLLBACK on any exception). Never nest, never hold across slow work."""
        conn = self.conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")

    def close(self) -> None:
        with self._lock:
            conns, self._all = self._all, []
        for conn in conns:
            with contextlib.suppress(sqlite3.Error):
                conn.close()
        self._local = threading.local()


def purge_profile_data(conn: sqlite3.Connection, profile: sqlite3.Row) -> None:
    """Delete profile data in the caller's transaction, keeping only ownership, revision and hold metadata."""
    key = profile["profile_key"]
    conn.execute("INSERT INTO profile_tombstones VALUES (?,?,?,?)",
                 (key, profile["device_id"], profile["config_rev"], profile["held"]))
    conn.execute("INSERT INTO retired_streams VALUES (?,?)", (key, profile["stream_id"]))
    conn.execute("DELETE FROM profiles WHERE id=?", (profile["id"],))


def purge_expired(db: Database, now: float) -> dict[str, int]:
    """Delete expired sessions (idle 7 days / absolute 30 days), known devices unused for a year and old throttle rows."""
    now = int(now)
    with db.transaction() as c:
        s = c.execute("DELETE FROM sessions WHERE last_seen_at < ? OR created_at < ?",
                      (now - SESSION_IDLE, now - SESSION_ABSOLUTE)).rowcount
        k = c.execute("DELETE FROM known_devices WHERE last_used_at < ?", (now - KNOWN_DEVICE_TTL,)).rowcount
        t = c.execute("DELETE FROM throttle WHERE window_start < ?", (now - THROTTLE_KEEP,)).rowcount
        c.execute("DELETE FROM used_login_nonces WHERE expires_at < ?", (now,))
    db.last_purge = float(now)
    return {"sessions": s, "known_devices": k, "throttle": t}


def purge_if_due(db: Database, now: float) -> bool:
    if now - db.last_purge < PURGE_EVERY:
        return False
    db.last_purge = float(now)  # claim first: concurrent requests do not all purge
    purge_expired(db, now)
    return True
