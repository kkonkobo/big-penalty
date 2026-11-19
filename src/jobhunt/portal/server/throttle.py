"""Fixed-window counters in the database, with an atomic multi-bucket reserve.

Bucket names are built by the callers from keyed hashes (`Cipher.mac`), never from a raw address or handle. A window is
`[now - now % window, +window)`. Attempts are counted when they START; a success may refund (`refund`) or reset (`reset`)."""

from __future__ import annotations

from collections.abc import Iterable

from .db import Database

BucketSpec = tuple[str, int, int]  # (bucket, limit, window_seconds)
MAX_ROWS = 50_000


def _window(now: float, window: int) -> int:
    n = int(now)
    return n - n % window


def reserve(db: Database, specs: Iterable[BucketSpec], now: float) -> bool:
    """Increment all buckets atomically or none; expired rows may be removed even on refusal."""
    specs = list(specs)
    with db.transaction() as c:
        starts = []
        missing = set()
        for bucket, limit, window in specs:
            start = _window(now, window)
            row = c.execute("SELECT count FROM throttle WHERE bucket=? AND window_start=?", (bucket, start)).fetchone()
            if row is not None and row[0] >= limit:
                return False
            if row is None and not bucket.startswith("api:"):
                missing.add((bucket, start))
            starts.append(start)
        if missing:
            c.execute("DELETE FROM throttle WHERE expires_at <= ?", (int(now),))
            count = c.execute("SELECT COUNT(*) FROM throttle WHERE bucket NOT GLOB 'api:*'").fetchone()[0]
            if count + len(missing) > MAX_ROWS:
                return False
        for (bucket, _limit, window), start in zip(specs, starts, strict=True):
            c.execute("INSERT INTO throttle(bucket, window_start, count, expires_at) VALUES (?, ?, 1, ?) "
                      "ON CONFLICT(bucket, window_start) DO UPDATE SET count = count + 1", (bucket, start, start + window))
    return True


def refund(db: Database, specs: Iterable[tuple[str, int]], now: float) -> None:
    """Give one attempt back on each `(bucket, window_seconds)` (never below zero)."""
    with db.transaction() as c:
        for bucket, window in specs:
            c.execute("UPDATE throttle SET count = MAX(count - 1, 0) WHERE bucket=? AND window_start=?",
                      (bucket, _window(now, window)))


def reset(db: Database, buckets: Iterable[str]) -> None:
    with db.transaction() as c:
        for bucket in buckets:
            c.execute("DELETE FROM throttle WHERE bucket=?", (bucket,))


def count(db: Database, bucket: str, window: int, now: float) -> int:
    row = db.conn.execute("SELECT count FROM throttle WHERE bucket=? AND window_start=?", (bucket, _window(now, window))).fetchone()
    return row[0] if row else 0


def clear_login(conn, handle_key: str) -> None:
    """Delete every login bucket of one handle (`handle_key` = the keyed hash used in the bucket names). Caller's transaction."""
    conn.execute("DELETE FROM throttle WHERE bucket = ? OR bucket LIKE ? OR bucket LIKE ?",
                 (f"h:{handle_key}", f"ah:%:{handle_key}", f"d:{handle_key}:%"))
