"""SQLite persistence (stdlib sqlite3, WAL). One short-lived connection per call: safe from threads and the event loop."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator

from jobhunt.config import get_settings
from jobhunt.dedupe import MergedJob, Sighting, canonical_url, reconcile_status
from jobhunt.models import ContractType, Profile

STATUSES = ("new", "shortlisted", "approved", "rejected", "applied")
DB_PATH_OVERRIDE: Path | None = None  # tests
_migrated: set[str] = set()

MIGRATIONS: list[str] = [
    """
    CREATE TABLE profiles (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        data TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE TABLE runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        profile_id INTEGER NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
        started_at TEXT NOT NULL,
        finished_at TEXT,
        status TEXT NOT NULL DEFAULT 'running',
        plan TEXT,
        stats TEXT,
        error TEXT
    );
    CREATE TABLE jobs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        profile_id INTEGER NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
        dedupe_key TEXT NOT NULL,
        title TEXT NOT NULL,
        company TEXT,
        location TEXT,
        contract_type TEXT NOT NULL DEFAULT 'unknown',
        remote TEXT NOT NULL DEFAULT 'unknown',
        salary_text TEXT,
        salary_min_eur INTEGER,
        salary_max_eur INTEGER,
        description TEXT,
        posted_at TEXT,
        apply_url TEXT,
        origin_url TEXT,
        origin_ats TEXT,
        origin_confidence REAL,
        extra TEXT NOT NULL DEFAULT '{}',
        score REAL,
        score_json TEXT NOT NULL DEFAULT '{}',
        status TEXT NOT NULL DEFAULT 'new',
        first_seen TEXT NOT NULL,
        last_seen TEXT NOT NULL
    );
    CREATE INDEX idx_jobs_profile ON jobs(profile_id, status, score);
    CREATE INDEX idx_jobs_key ON jobs(profile_id, dedupe_key);
    CREATE TABLE sightings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
        source TEXT NOT NULL,
        source_id TEXT NOT NULL,
        url TEXT NOT NULL,
        apply_url TEXT,
        posted_at TEXT,
        raw TEXT,
        seen_at TEXT NOT NULL,
        UNIQUE(job_id, source, source_id)
    );
    CREATE INDEX idx_sightings_job ON sightings(job_id);
    """,
]


def db_path() -> Path:
    return DB_PATH_OVERRIDE or get_settings().db_path


def now() -> datetime:
    return datetime.now(UTC)


def _iso(dt: datetime | None) -> str | None:
    return dt.astimezone(UTC).isoformat() if dt else None


def _dt(s: str | None) -> datetime | None:
    if not s:
        return None
    d = datetime.fromisoformat(s)
    return d if d.tzinfo else d.replace(tzinfo=UTC)


def _json_object(value: str | None) -> dict[str, Any]:
    try:
        parsed = json.loads(value or "{}")
    except (ValueError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _raw_json(raw: dict[str, Any]) -> str | None:
    encoded = json.dumps(raw, ensure_ascii=False, default=str)
    return encoded if len(encoded) <= 20000 else "{}"


def _migrate(conn: sqlite3.Connection) -> None:
    v = conn.execute("PRAGMA user_version").fetchone()[0]
    for i in range(v, len(MIGRATIONS)):
        conn.executescript("BEGIN;" + MIGRATIONS[i] + f"PRAGMA user_version = {i + 1};COMMIT;")


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        if str(path) not in _migrated:
            _migrate(conn)
            _migrated.add(str(path))
        with conn:
            yield conn
    finally:
        conn.close()


# ---------------------------------------------------------------- profiles
def save_profile(p: Profile) -> Profile:
    ts = _iso(now())
    with connect() as c:
        previous = c.execute("SELECT data FROM profiles WHERE id=?", (p.id,)).fetchone() if p.id else None
        if p.id is None:
            cur = c.execute(
                "INSERT INTO profiles(name, data, created_at, updated_at) VALUES (?,?,?,?)",
                (p.name, "{}", ts, ts),
            )
            p = p.model_copy(update={"id": cur.lastrowid})
        c.execute(
            "UPDATE profiles SET name=?, data=?, updated_at=? WHERE id=?",
            (p.name, p.model_dump_json(), ts, p.id),
        )
        if previous:
            from jobhunt.scoring import profile_scoring_inputs
            if profile_scoring_inputs(Profile.model_validate_json(previous["data"])) != profile_scoring_inputs(p):
                c.execute("UPDATE jobs SET score=NULL, score_json='{}' WHERE profile_id=?", (p.id,))
    return p


def _profile_from_row(r: sqlite3.Row) -> Profile:
    p = Profile.model_validate_json(r["data"])
    return p.model_copy(update={"id": r["id"]})


def get_profile(profile_id: int) -> Profile | None:
    with connect() as c:
        r = c.execute("SELECT * FROM profiles WHERE id=?", (profile_id,)).fetchone()
    return _profile_from_row(r) if r else None


def list_profiles() -> list[Profile]:
    with connect() as c:
        rows = c.execute("SELECT * FROM profiles ORDER BY id").fetchall()
    return [_profile_from_row(r) for r in rows]


def delete_profile(profile_id: int) -> None:
    with connect() as c:
        c.execute("DELETE FROM profiles WHERE id=?", (profile_id,))


# ---------------------------------------------------------------- runs
def create_run(profile_id: int) -> int:
    with connect() as c:
        cur = c.execute("INSERT INTO runs(profile_id, started_at) VALUES (?,?)", (profile_id, _iso(now())))
        return int(cur.lastrowid)


def update_run(run_id: int, *, plan: Any = None, stats: Any = None, status: str | None = None,
               error: str | None = None, finished: bool = False) -> None:
    sets, vals = [], []
    if plan is not None:
        sets.append("plan=?"); vals.append(json.dumps(plan, ensure_ascii=False, default=str))
    if stats is not None:
        sets.append("stats=?"); vals.append(json.dumps(stats, ensure_ascii=False, default=str))
    if status:
        sets.append("status=?"); vals.append(status)
    if error is not None:
        sets.append("error=?"); vals.append(error)
    if finished:
        sets.append("finished_at=?"); vals.append(_iso(now()))
    if not sets:
        return
    with connect() as c:
        c.execute(f"UPDATE runs SET {', '.join(sets)} WHERE id=?", (*vals, run_id))


def _run_row(r: sqlite3.Row) -> dict[str, Any]:
    d = dict(r)
    d["plan"] = json.loads(d["plan"]) if d["plan"] else []
    d["stats"] = json.loads(d["stats"]) if d["stats"] else {}
    return d


def get_run(run_id: int) -> dict[str, Any] | None:
    with connect() as c:
        r = c.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    return _run_row(r) if r else None


def latest_run(profile_id: int | None = None) -> dict[str, Any] | None:
    with connect() as c:
        if profile_id is None:
            r = c.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
        else:
            r = c.execute("SELECT * FROM runs WHERE profile_id=? ORDER BY id DESC LIMIT 1", (profile_id,)).fetchone()
    return _run_row(r) if r else None


# ---------------------------------------------------------------- jobs
def _job_from_row(r: sqlite3.Row, sightings: list[Sighting]) -> MergedJob:
    return MergedJob(
        id=r["id"], profile_id=r["profile_id"], dedupe_key=r["dedupe_key"], title=r["title"], company=r["company"],
        location=r["location"], contract_type=ContractType(r["contract_type"]), remote=r["remote"],
        salary_text=r["salary_text"], salary_min_eur=r["salary_min_eur"], salary_max_eur=r["salary_max_eur"],
        description=r["description"], posted_at=_dt(r["posted_at"]), apply_url=r["apply_url"],
        origin_url=r["origin_url"], origin_ats=r["origin_ats"], origin_confidence=r["origin_confidence"],
        extra=_json_object(r["extra"]), score=r["score"], score_json=_json_object(r["score_json"]),
        status=r["status"], first_seen=_dt(r["first_seen"]), last_seen=_dt(r["last_seen"]),
        sightings=sightings, changed=False,
    )


def _sightings_by_job(c: sqlite3.Connection, job_ids: list[int]) -> dict[int, list[Sighting]]:
    out: dict[int, list[Sighting]] = {i: [] for i in job_ids}
    for chunk_start in range(0, len(job_ids), 500):
        chunk = job_ids[chunk_start:chunk_start + 500]
        q = ",".join("?" * len(chunk))
        for s in c.execute(f"SELECT * FROM sightings WHERE job_id IN ({q}) ORDER BY id", chunk):
            out[s["job_id"]].append(
                Sighting(s["source"], s["source_id"], s["url"], s["apply_url"], _dt(s["posted_at"]),
                         _json_object(s["raw"]))
            )
    return out


def load_jobs(profile_id: int, *, with_sightings: bool = True) -> list[MergedJob]:
    with connect() as c:
        rows = c.execute("SELECT * FROM jobs WHERE profile_id=? ORDER BY id", (profile_id,)).fetchall()
        sg = _sightings_by_job(c, [r["id"] for r in rows]) if with_sightings else {}
    return [_job_from_row(r, sg.get(r["id"], [])) for r in rows]


def get_job(job_id: int) -> MergedJob | None:
    with connect() as c:
        r = c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not r:
            return None
        sg = _sightings_by_job(c, [job_id])
    return _job_from_row(r, sg[job_id])


def save_jobs(profile_id: int, jobs: list[MergedJob], *, removed: list[MergedJob] = ()) -> None:
    """Save jobs; reconcile live decisions atomically before deleting origin duplicates.

    Ordinary refreshes never overwrite status. Consolidation uses the highest-precedence
    decision from all rows, including decisions made since the jobs were loaded.
    """
    ts = _iso(now())
    with connect() as c:
        c.execute("BEGIN IMMEDIATE")
        for j in jobs:
            duplicates = [other for other in removed if canonical_url(other.origin_url)
                          and canonical_url(other.origin_url) == canonical_url(j.origin_url)]
            if duplicates:
                statuses = [j.status, *(other.status for other in duplicates)]
                for related in [j, *duplicates]:
                    row = c.execute("SELECT status FROM jobs WHERE id=? AND profile_id=?", (related.id, profile_id)).fetchone()
                    if row:
                        statuses.append(row["status"])
                j.status = reconcile_status(*statuses)
                j.changed = True
            if j.id is not None and not j.changed:
                continue
            vals = (
                j.dedupe_key, j.title, j.company, j.location, j.contract_type.value, j.remote, j.salary_text,
                j.salary_min_eur, j.salary_max_eur, j.description, _iso(j.posted_at), j.apply_url, j.origin_url,
                j.origin_ats, j.origin_confidence, json.dumps(j.extra, ensure_ascii=False, default=str),
                j.score, json.dumps(j.score_json, ensure_ascii=False, default=str),
            )
            if j.id is None:
                cur = c.execute(
                    "INSERT INTO jobs(profile_id, dedupe_key, title, company, location, contract_type, remote,"
                    " salary_text, salary_min_eur, salary_max_eur, description, posted_at, apply_url, origin_url,"
                    " origin_ats, origin_confidence, extra, score, score_json, status, first_seen, last_seen)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (profile_id, *vals, j.status, ts, ts),
                )
                j.id, j.profile_id, j.first_seen, j.last_seen = int(cur.lastrowid), profile_id, now(), now()
            else:
                c.execute(
                    "UPDATE jobs SET dedupe_key=?, title=?, company=?, location=?, contract_type=?, remote=?,"
                    " salary_text=?, salary_min_eur=?, salary_max_eur=?, description=?, posted_at=?, apply_url=?,"
                    " origin_url=?, origin_ats=?, origin_confidence=?, extra=?, score=?, score_json=?, last_seen=?,"
                    " status=CASE WHEN ? THEN ? ELSE status END WHERE id=? AND profile_id=?",
                    (*vals, ts, bool(duplicates), j.status, j.id, profile_id),
                )
            for s in j.sightings:
                c.execute(
                    "INSERT INTO sightings(job_id, source, source_id, url, apply_url, posted_at, raw, seen_at)"
                    " VALUES (?,?,?,?,?,?,?,?)"
                    " ON CONFLICT(job_id, source, source_id) DO UPDATE SET url=excluded.url,"
                    " apply_url=excluded.apply_url, posted_at=excluded.posted_at, raw=excluded.raw, seen_at=excluded.seen_at",
                    (j.id, s.source, s.source_id, s.url, s.apply_url, _iso(s.posted_at),
                     _raw_json(s.raw) if s.raw else None, ts),
                )
            j.changed = False
        for j in removed:
            if j.id is not None:
                c.execute("DELETE FROM jobs WHERE id=? AND profile_id=?", (j.id, profile_id))


def set_status(job_ids: list[int], status: str, profile_id: int | None = None) -> int:
    if status not in STATUSES:
        raise ValueError(f"invalid status {status!r}")
    if not job_ids:
        return 0
    q = ",".join("?" * len(job_ids))
    sql = f"UPDATE jobs SET status=? WHERE id IN ({q})"
    args: list[Any] = [status, *job_ids]
    if profile_id is not None:
        sql += " AND profile_id=?"
        args.append(profile_id)
    with connect() as c:
        return c.execute(sql, args).rowcount


def query_jobs(
    profile_id: int, *, min_score: float | None = None, contract_type: str | None = None, source: str | None = None,
    max_age_days: int | None = None, status: str | None = None, sort: str = "score", show_filtered: bool = False,
) -> list[MergedJob]:
    """Filtered, sorted list for the shortlist UI."""
    jobs = load_jobs(profile_id)
    cutoff = now().timestamp() - max_age_days * 86400 if max_age_days else None
    out = []
    for j in jobs:
        if not show_filtered and j.score_json.get("prefiltered"):
            continue
        if min_score is not None and (j.score or 0) < min_score:
            continue
        if contract_type and j.contract_type.value != contract_type:
            continue
        if source and source not in j.sources:
            continue
        if status and j.status != status:
            continue
        if cutoff is not None:
            ref = j.posted_at or j.first_seen
            if ref and ref.timestamp() < cutoff:
                continue
        out.append(j)
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    if sort == "freshness":
        out.sort(key=lambda j: (j.posted_at or j.first_seen or epoch, j.score or 0), reverse=True)
    else:
        out.sort(key=lambda j: (j.score or 0, j.posted_at or j.first_seen or epoch), reverse=True)
    return out


def job_counts(profile_id: int) -> dict[str, int]:
    with connect() as c:
        rows = c.execute("SELECT status, COUNT(*) n FROM jobs WHERE profile_id=? GROUP BY status", (profile_id,)).fetchall()
    return {r["status"]: r["n"] for r in rows}
