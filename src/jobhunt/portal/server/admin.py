"""Operator tool: `python -m jobhunt.portal.server.admin <command>` with the server's own environment.

Commands: `list`, `revoke-device ID`, `hold PROFILE`, `release PROFILE`, `purge-profile PROFILE --yes`, `rotate-key`.
ID / PROFILE are exact ids or UNIQUE prefixes of the device id / profile key; ambiguous or unknown: error, nothing changed.
Never prints a secret, a handle or a passcode. The functions are importable (and tested) without spawning a process."""

from __future__ import annotations

import argparse
import os
import sys
import time
from collections.abc import Callable, Sequence

from . import throttle
from .auth import delete_profile_sessions
from .config import Config, ConfigError
from .crypto import Cipher, Sealed
from .db import Database, purge_profile_data


class AdminError(Exception):
    """A refusal with a message safe to print."""


def _resolve(conn, table: str, column: str, prefix: str, what: str) -> str:
    prefix = (prefix or "").strip().lower()
    if not prefix:
        raise AdminError(f"{what}: empty identifier")
    matches = [r[0] for r in conn.execute(f"SELECT {column} FROM {table}") if r[0].startswith(prefix)]  # noqa: S608 (fixed names)
    if prefix in matches:
        return prefix
    if len(matches) > 1:
        raise AdminError(f"{what}: ambiguous prefix ({len(matches)} matches)")
    if not matches:
        raise AdminError(f"{what}: not found")
    return matches[0]


def _profile_row(db: Database, prefix: str):
    key = _resolve(db.conn, "profiles", "profile_key", prefix, "profile")
    return db.conn.execute("SELECT id, profile_key, handle_sha256, device_id FROM profiles WHERE profile_key=?", (key,)).fetchone()


def list_state(db: Database, now: float | None = None) -> str:
    now = time.time() if now is None else now
    c = db.conn
    lines = ["Devices:"]
    for d in c.execute("SELECT * FROM devices ORDER BY created_at"):
        state = "revoked" if d["revoked_at"] else "active"
        lines.append(f"  {d['id']}  {d['name']!r}  {state}  created={int(d['created_at'])}")
    lines.append("Profiles:")
    for p in c.execute("SELECT p.*, (SELECT COUNT(*) FROM sessions s WHERE s.profile_id=p.id) AS n_sessions, "
                       "(SELECT COUNT(*) FROM events e WHERE e.profile_id=p.id) AS n_events FROM profiles p ORDER BY p.id"):
        lines.append(f"  {p['profile_key']}  device={p['device_id'][:8]}  enabled={bool(p['enabled'])}  held={bool(p['held'])}  "
                     f"config_rev={p['config_rev']}  snapshot_rev={p['snapshot_rev']}  seq={p['acked_seq']}/{p['last_seq']}  "
                     f"pending={p['n_events']}  sessions={p['n_sessions']}")
    return "\n".join(lines)


def revoke_device(db: Database, prefix: str, now: float | None = None) -> str:
    now = time.time() if now is None else now
    with db.transaction() as c:
        device = _resolve(c, "devices", "id", prefix, "device")
        c.execute("UPDATE devices SET revoked_at=COALESCE(revoked_at, ?) WHERE id=?", (int(now), device))
        c.execute("UPDATE profiles SET held=1 WHERE device_id=?", (device,))
        c.execute("UPDATE profile_tombstones SET held=1 WHERE device_id=?", (device,))
        for profile in c.execute("SELECT id FROM profiles WHERE device_id=?", (device,)).fetchall():
            delete_profile_sessions(c, profile["id"])
    return device


def hold(db: Database, prefix: str) -> str:
    with db.transaction() as c:
        row = _profile_row_in(c, prefix)
        c.execute("UPDATE profiles SET held=1 WHERE id=?", (row["id"],))
        delete_profile_sessions(c, row["id"])
    return row["profile_key"]


def release(db: Database, prefix: str) -> str:
    with db.transaction() as c:
        row = _profile_row_in(c, prefix)
        c.execute("UPDATE profiles SET held=0 WHERE id=?", (row["id"],))
    return row["profile_key"]


def _profile_row_in(c, prefix: str):
    key = _resolve(c, "profiles", "profile_key", prefix, "profile")
    return c.execute("SELECT * FROM profiles WHERE profile_key=?", (key,)).fetchone()


def purge_profile(db: Database, cipher: Cipher, prefix: str, confirmed: bool) -> str:
    if not confirmed:
        raise AdminError("purge-profile needs --yes")
    with db.transaction() as c:
        row = _profile_row_in(c, prefix)
        throttle.clear_login(c, cipher.mac("login", row["handle_sha256"]))
        purge_profile_data(c, row)
    return row["profile_key"]


_SEALED_TABLES = (  # table, SQL expression of the seal purpose, row id used in the associated data
    ("snapshots", "purpose", lambda r: "0"),
    ("events", "'event'", lambda r: r["event_id"]),
    ("documents", "'document'", lambda r: r["doc_id"]),
)


def rotate_key(db: Database, cipher: Cipher) -> dict[str, int]:
    """Re-encrypt every sealed row under the current key, in ONE transaction, then verify by reading each row back.
    Needs the old key configured (rows of the old key must be readable). Any failure rolls everything back."""
    done = {"snapshots": 0, "events": 0, "documents": 0}
    with db.transaction() as c:
        for table, purpose_sql, row_id in _SEALED_TABLES:
            rows = c.execute(f"SELECT rowid AS rid, *, {purpose_sql} AS purpose_sql FROM {table}").fetchall()  # noqa: S608
            for r in rows:
                purpose = r["purpose_sql"]
                sealed = Sealed(r["key_id"], bytes(r["nonce"]), bytes(r["ciphertext"]))
                rid = row_id(r)
                if not cipher.needs_rotation(sealed):
                    cipher.open(purpose, r["profile_id"], rid, sealed)  # still verify what we leave alone
                    continue
                new = cipher.reseal(purpose, r["profile_id"], rid, sealed)
                c.execute(f"UPDATE {table} SET key_id=?, nonce=?, ciphertext=? WHERE rowid=?",  # noqa: S608
                          (new.key_id, new.nonce, new.ciphertext, r["rid"]))
                back = c.execute(f"SELECT key_id, nonce, ciphertext FROM {table} WHERE rowid=?", (r["rid"],)).fetchone()  # noqa: S608
                cipher.open(purpose, r["profile_id"], rid, Sealed(back["key_id"], bytes(back["nonce"]), bytes(back["ciphertext"])))
                done[table] += 1
    return done


def main(argv: Sequence[str] | None = None, environ: dict | None = None, out: Callable[[str], None] = print) -> int:
    parser = argparse.ArgumentParser(prog="python -m jobhunt.portal.server.admin")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list")
    for name in ("revoke-device", "hold", "release", "purge-profile"):
        p = sub.add_parser(name)
        p.add_argument("target")
        if name == "purge-profile":
            p.add_argument("--yes", action="store_true")
    sub.add_parser("rotate-key")
    args = parser.parse_args(argv)
    try:
        config = Config.from_env(os.environ if environ is None else environ)
        db = Database(config.db_path)
        cipher = Cipher(config.data_key, config.data_key_old)
        if args.command == "list":
            out(list_state(db))
        elif args.command == "revoke-device":
            out(f"revoked device {revoke_device(db, args.target)}")
        elif args.command == "hold":
            out(f"held profile {hold(db, args.target)} (sessions deleted)")
        elif args.command == "release":
            out(f"released profile {release(db, args.target)}")
        elif args.command == "purge-profile":
            out(f"purged profile {purge_profile(db, cipher, args.target, args.yes)}")
        elif args.command == "rotate-key":
            if config.data_key_old is None:
                raise AdminError("rotate-key needs PORTAL_DATA_KEY_OLD (the previous key) next to PORTAL_DATA_KEY (the new one)")
            out("re-encrypted rows: " + ", ".join(f"{k}={v}" for k, v in rotate_key(db, cipher).items()))
    except (AdminError, ConfigError) as exc:
        print(f"Erreur : {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
