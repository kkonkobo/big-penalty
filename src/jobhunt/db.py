"""SQLite persistence (stdlib sqlite3, WAL). One short-lived connection per call: safe from threads and the event loop."""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Iterator

from jobhunt.config import get_settings
from jobhunt.freshness import (EXPIRY_RUN_WINDOW_DAYS, MAX_JOB_AGE_DAYS, effective_posted_at, expired_by_runs, is_new, is_stale,
                               rank_score)
from jobhunt.dedupe import MergedJob, Sighting, canonical_url, consolidate, reconcile_status
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
    # 2: tailored CV applications. job_id has NO foreign key on purpose: consolidating duplicate jobs deletes job rows
    # and must never fail or wipe an application (job title/company are snapshotted).
    """
    CREATE TABLE applications (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        profile_id INTEGER NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
        job_id INTEGER NOT NULL,
        job_title TEXT,
        company TEXT,
        status TEXT NOT NULL DEFAULT 'draft',
        tailored_json TEXT NOT NULL DEFAULT '{}',
        flags_json TEXT NOT NULL DEFAULT '[]',
        cv_path TEXT,
        cover_letter_path TEXT,
        ats TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        notes TEXT
    );
    CREATE INDEX idx_applications_job ON applications(profile_id, job_id);
    """,
    # 3: immutable CV revisions + approval bound to a revision. applications.tailored_json/flags_json/cv_path stay as a
    # mirror of the current revision. Existing applications get revision 1 (old, mutable PDF path, no hash, no verifier:
    # never eligible, they must be regenerated). At most one non-withdrawn application per (profile, job).
    """
    ALTER TABLE applications ADD COLUMN current_rev INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE applications ADD COLUMN approved_rev INTEGER;
    ALTER TABLE applications ADD COLUMN approved_sha256 TEXT;
    ALTER TABLE applications ADD COLUMN approved_at TEXT;
    ALTER TABLE applications ADD COLUMN approval_override INTEGER NOT NULL DEFAULT 0;
    CREATE TABLE application_revisions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        application_id INTEGER NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
        rev INTEGER NOT NULL,
        tailored_json TEXT NOT NULL,
        flags_json TEXT NOT NULL DEFAULT '[]',
        verifier_json TEXT,
        pdf_path TEXT,
        pdf_sha256 TEXT,
        fit_notes_json TEXT NOT NULL DEFAULT '{}',
        master_hash TEXT,
        created_at TEXT NOT NULL,
        UNIQUE(application_id, rev)
    );
    INSERT INTO application_revisions(application_id, rev, tailored_json, flags_json, pdf_path, created_at)
        SELECT id, 1, tailored_json, flags_json, cv_path, created_at FROM applications;
    UPDATE applications SET current_rev = 1;
    UPDATE applications SET status = 'withdrawn'
        WHERE status != 'withdrawn' AND id NOT IN (
            SELECT MAX(id) FROM applications WHERE status != 'withdrawn' GROUP BY profile_id, job_id);
    CREATE UNIQUE INDEX idx_applications_active ON applications(profile_id, job_id) WHERE status != 'withdrawn';
    """,
    # 4: milestone 2b (apply on the origin ATS): who approved, when it was sent, and one row per dry-run / submit run.
    """
    ALTER TABLE applications ADD COLUMN approved_by TEXT;
    ALTER TABLE applications ADD COLUMN submitted_at TEXT;
    CREATE TABLE application_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        application_id INTEGER NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
        rev INTEGER NOT NULL,
        mode TEXT NOT NULL,
        trigger TEXT NOT NULL,
        status TEXT NOT NULL,
        message TEXT,
        result_json TEXT,
        out_dir TEXT,
        started_at TEXT NOT NULL,
        finished_at TEXT
    );
    CREATE INDEX idx_runs_app ON application_runs(application_id, id);
    """,
    # 5: crash-safe "possibly sent" bookkeeping: click time of a run, the URL an application was sent to, and one
    # reservation per attempted send (consumes the daily cap and blocks duplicates until a human releases it).
    """
    ALTER TABLE application_runs ADD COLUMN submit_clicked_at TEXT;
    ALTER TABLE applications ADD COLUMN sent_origin_url TEXT;
    CREATE TABLE apply_reservations (
        id INTEGER PRIMARY KEY,
        profile_id INTEGER NOT NULL,
        application_id INTEGER NOT NULL,
        run_id INTEGER NOT NULL,
        trigger TEXT NOT NULL,
        role_key TEXT NOT NULL,
        url_key TEXT NOT NULL,
        created_at TEXT NOT NULL,
        released_at TEXT
    );
    CREATE INDEX idx_reservations_profile ON apply_reservations(profile_id, released_at);
    CREATE INDEX idx_reservations_app ON apply_reservations(application_id);
    """,
    # 6: durable record of navigation clicks (a pending reservation exists from the first click that follows entered data),
    # and the pid of the process driving a run (a live owner is never taken over).
    """
    ALTER TABLE application_runs ADD COLUMN nav_clicked_at TEXT;
    ALTER TABLE application_runs ADD COLUMN pid INTEGER;
    ALTER TABLE apply_reservations ADD COLUMN pending INTEGER NOT NULL DEFAULT 0;
    """,
    # 7: which run was the sending attempt of an application (set when it is recorded as submitted, also by a manual
    # confirmation): the answers deduced in THAT run are the ones to prepare for the interview.
    """
    ALTER TABLE applications ADD COLUMN sent_run_id INTEGER;
    """,
    # 8: who approved the application when a run started ("auto" / "user").
    """
    ALTER TABLE application_runs ADD COLUMN approved_by TEXT;
    """,
    # 9: how many times the automatic pass regenerated the application's CV because its revision had blocking errors.
    """
    ALTER TABLE applications ADD COLUMN auto_regens INTEGER NOT NULL DEFAULT 0;
    """,
    """
    ALTER TABLE application_runs ADD COLUMN operator_not_sent_at TEXT;
    """,
    """
    ALTER TABLE application_runs ADD COLUMN origin_sha TEXT;
    ALTER TABLE application_runs ADD COLUMN application_version TEXT;
    """,
    """
    ALTER TABLE apply_reservations ADD COLUMN binding_json TEXT;
    ALTER TABLE apply_reservations ADD COLUMN dedupe_key TEXT;
    ALTER TABLE apply_reservations ADD COLUMN receipt_kind TEXT;
    ALTER TABLE apply_reservations ADD COLUMN receipt_outcome TEXT;
    ALTER TABLE apply_reservations ADD COLUMN receipt_at TEXT;
    CREATE UNIQUE INDEX idx_active_requisition ON apply_reservations(dedupe_key) WHERE released_at IS NULL;
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


def _statements(script: str) -> Iterator[str]:
    """Split a migration script into statements (no triggers in our migrations, so `;` at end of a complete statement)."""
    buf = ""
    for line in script.splitlines(keepends=True):
        buf += line
        if sqlite3.complete_statement(buf):
            if buf.strip():
                yield buf
            buf = ""
    if buf.strip():
        yield buf


def _migrate(conn: sqlite3.Connection) -> None:
    """Take the write lock BEFORE reading user_version: concurrent startups serialize and the second one sees the
    finished schema instead of replaying the migrations. Each run is one transaction (DDL and user_version roll back together)."""
    previous = conn.isolation_level
    conn.isolation_level = None  # explicit transaction control
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            v = conn.execute("PRAGMA user_version").fetchone()[0]
            for i in range(v, len(MIGRATIONS)):
                for stmt in _statements(MIGRATIONS[i]):
                    conn.execute(stmt)
                conn.execute(f"PRAGMA user_version = {i + 1}")
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.isolation_level = previous


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA busy_timeout=30000")
        _enable_wal(conn)
        conn.execute("PRAGMA foreign_keys=ON")
        if str(path) not in _migrated:
            _migrate(conn)
            _migrated.add(str(path))
        with conn:
            yield conn
    finally:
        conn.close()


def _enable_wal(conn: sqlite3.Connection) -> None:
    for delay in (0.01, 0.05, 0.15, 0.5, 1.0, None):
        try:
            mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
            if mode.lower() != "wal":
                raise sqlite3.OperationalError("SQLite could not enable WAL")
            return
        except sqlite3.OperationalError as exc:
            if delay is None or (getattr(exc, "sqlite_errorcode", 0) & 255) not in (
                sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED,
            ):
                raise
            time.sleep(delay)


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


def latest_completed_run(profile_id: int) -> dict[str, Any] | None:
    """Most recent run of the profile that finished with status "done" (not running, cancelled or failed)."""
    with connect() as c:
        r = c.execute("SELECT * FROM runs WHERE profile_id=? AND status='done' ORDER BY id DESC LIMIT 1",
                      (profile_id,)).fetchone()
    return _run_row(r) if r else None


def recent_ok_runs(profile_id: int, as_of: datetime | None = None) -> list[tuple[datetime, set[str]]]:
    """(started_at, sources_ok) of the profile's completed runs finished within EXPIRY_RUN_WINDOW_DAYS.
    Runs recorded without `sources_ok` (older versions) yield an empty set: no source counts as checked."""
    cutoff = (as_of or now()) - timedelta(days=EXPIRY_RUN_WINDOW_DAYS)
    with connect() as c:
        rows = c.execute("SELECT * FROM runs WHERE profile_id=? AND status='done' AND finished_at IS NOT NULL",
                         (profile_id,)).fetchall()
    out = []
    for r in rows:
        started, finished = _dt(r["started_at"]), _dt(r["finished_at"])
        if started is None or finished is None or finished < cutoff or finished > (as_of or now()):
            continue
        ok = (_run_row(r)["stats"] or {}).get("sources_ok")
        out.append((started, set(ok) if isinstance(ok, list) else set()))
    return out


# ---------------------------------------------------------------- jobs
def _job_from_row(r: sqlite3.Row, sightings: list[Sighting]) -> MergedJob:
    job = MergedJob(
        id=r["id"], profile_id=r["profile_id"], dedupe_key=r["dedupe_key"], title=r["title"], company=r["company"],
        location=r["location"], contract_type=ContractType(r["contract_type"]), remote=r["remote"],
        salary_text=r["salary_text"], salary_min_eur=r["salary_min_eur"], salary_max_eur=r["salary_max_eur"],
        description=r["description"], posted_at=_dt(r["posted_at"]), apply_url=r["apply_url"],
        origin_url=r["origin_url"], origin_ats=r["origin_ats"], origin_confidence=r["origin_confidence"],
        extra=_json_object(r["extra"]), score=r["score"], score_json=_json_object(r["score_json"]),
        status=r["status"], first_seen=_dt(r["first_seen"]), last_seen=_dt(r["last_seen"]),
        sightings=sightings, changed=False,
    )
    _remember_origin(job)
    return job


def _sightings_by_job(c: sqlite3.Connection, job_ids: list[int]) -> dict[int, list[Sighting]]:
    out: dict[int, list[Sighting]] = {i: [] for i in job_ids}
    for chunk_start in range(0, len(job_ids), 500):
        chunk = job_ids[chunk_start:chunk_start + 500]
        q = ",".join("?" * len(chunk))
        for s in c.execute(f"SELECT * FROM sightings WHERE job_id IN ({q}) ORDER BY id", chunk):
            out[s["job_id"]].append(
                Sighting(s["source"], s["source_id"], s["url"], s["apply_url"], _dt(s["posted_at"]),
                         _json_object(s["raw"]), seen_at=_dt(s["seen_at"]))
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


def get_jobs(job_ids: Iterable[int]) -> dict[int, MergedJob]:
    """Load jobs and their sightings by ID, omitting IDs that no longer exist."""
    ids = list(dict.fromkeys(job_ids))
    if not ids:
        return {}
    jobs = {}
    with connect() as conn:
        for offset in range(0, len(ids), 500):
            chunk = ids[offset:offset + 500]
            placeholders = ",".join("?" * len(chunk))
            rows = conn.execute(f"SELECT * FROM jobs WHERE id IN ({placeholders})", chunk).fetchall()
            sightings = _sightings_by_job(conn, [row["id"] for row in rows])
            jobs.update((row["id"], _job_from_row(row, sightings[row["id"]])) for row in rows)
    return jobs


def save_jobs(profile_id: int, jobs: list[MergedJob], *, removed: list[MergedJob] = (),
              consolidate_origins: bool = False) -> None:
    """Save jobs; reconcile live decisions atomically before deleting origin duplicates.

    Compare origins with their load-time snapshots before writing or merging. Consolidation
    also uses the highest-precedence decision, including decisions made since load time.
    """
    ts = _iso(now())
    with connect() as c:
        c.execute("BEGIN IMMEDIATE")
        current_rows = {}
        conflicts = False
        def checked(batch: Iterable[MergedJob]) -> list[MergedJob]:
            nonlocal conflicts
            live = []
            for job in batch:
                if job.id is None:
                    live.append(job)
                    continue
                row = c.execute("SELECT * FROM jobs WHERE id=? AND profile_id=?", (job.id, profile_id)).fetchone()
                if row is None:
                    conflicts = True
                    continue
                current = _origin_job_from_row(row)
                conflicts |= _guard_search_origin(job, current)
                current_rows[job.id] = current
                live.append(job)
            return live

        live_jobs = checked(jobs)
        checked(removed)
        if removed and conflicts:
            raise ValueError("Origins changed before consolidation; reload jobs before merging")
        if consolidate_origins:
            jobs[:], removed = consolidate(live_jobs)
        else:
            jobs[:] = live_jobs
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
            if j.id is not None:
                current = current_rows[j.id]
                j.score, j.score_json = current.score, current.score_json
                if "scoring_fingerprint" in current.extra:
                    j.extra["scoring_fingerprint"] = current.extra["scoring_fingerprint"]
                else:
                    j.extra.pop("scoring_fingerprint", None)
            vals = (
                j.dedupe_key, j.title, j.company, j.location, j.contract_type.value, j.remote, j.salary_text,
                j.salary_min_eur, j.salary_max_eur, j.description, _iso(j.posted_at), j.apply_url, j.origin_url,
                j.origin_ats, j.origin_confidence, json.dumps(j.extra, ensure_ascii=False, default=str),
            )
            if j.id is None:
                cur = c.execute(
                    "INSERT INTO jobs(profile_id, dedupe_key, title, company, location, contract_type, remote,"
                    " salary_text, salary_min_eur, salary_max_eur, description, posted_at, apply_url, origin_url,"
                    " origin_ats, origin_confidence, extra, score, score_json, status, first_seen, last_seen)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (profile_id, *vals, j.score, json.dumps(j.score_json, ensure_ascii=False, default=str), j.status, ts, ts),
                )
                j.id, j.profile_id, j.first_seen, j.last_seen = int(cur.lastrowid), profile_id, now(), now()
            else:
                c.execute(
                    "UPDATE jobs SET dedupe_key=?, title=?, company=?, location=?, contract_type=?, remote=?,"
                    " salary_text=?, salary_min_eur=?, salary_max_eur=?, description=?, posted_at=?, apply_url=?,"
                    " origin_url=?, origin_ats=?, origin_confidence=?, extra=?,"
                    " last_seen=CASE WHEN ? THEN ? ELSE COALESCE(MAX(last_seen, ?), last_seen) END,"  # only a sighting bumps it
                    " status=CASE WHEN ? THEN ? ELSE status END WHERE id=? AND profile_id=?",
                    (*vals, j.sighted, ts, _iso(j.last_seen), bool(duplicates), j.status, j.id, profile_id),
                )
                if j.sighted:
                    j.last_seen = now()
            for s in j.sightings:
                c.execute(
                    "INSERT INTO sightings(job_id, source, source_id, url, apply_url, posted_at, raw, seen_at)"
                    " VALUES (?,?,?,?,?,?,?,?)"
                    " ON CONFLICT(job_id, source, source_id) DO UPDATE SET url=excluded.url,"
                    " apply_url=excluded.apply_url, posted_at=excluded.posted_at, raw=excluded.raw,"
                    " seen_at=CASE WHEN ? THEN excluded.seen_at ELSE MAX(sightings.seen_at, excluded.seen_at) END",
                    (j.id, s.source, s.source_id, s.url, s.apply_url, _iso(s.posted_at),
                     _raw_json(s.raw) if s.raw else None, ts if s.seen_now else (_iso(s.seen_at) or ts), s.seen_now),
                )
                if s.seen_now:
                    s.seen_at, s.seen_now = now(), False
            j.changed = False
            j.sighted = False
            _remember_origin(j)
        for j in removed:
            if j.id is not None:
                c.execute("DELETE FROM jobs WHERE id=? AND profile_id=?", (j.id, profile_id))


def save_scores(profile_id: int, jobs: list[MergedJob]) -> None:
    """Write only scoring-owned fields, never an origin or sighting from a stale job."""
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        for job in jobs:
            if job.id is None or not job.changed:
                continue
            row = conn.execute("SELECT extra FROM jobs WHERE id=? AND profile_id=?", (job.id, profile_id)).fetchone()
            if row is None:
                continue
            extra = _json_object(row["extra"])
            if "scoring_fingerprint" in job.extra:
                extra["scoring_fingerprint"] = job.extra["scoring_fingerprint"]
            else:
                extra.pop("scoring_fingerprint", None)
            conn.execute("UPDATE jobs SET score=?, score_json=?, extra=? WHERE id=? AND profile_id=?",
                         (job.score, json.dumps(job.score_json, ensure_ascii=False, default=str),
                          json.dumps(extra, ensure_ascii=False, default=str), job.id, profile_id))
            job.changed = False


def _origin_snapshot(job: MergedJob) -> tuple:
    return job.origin_url, job.origin_ats, job.origin_confidence, job.extra.get("origin_resolution"), job.apply_url


def _origin_metadata(job: MergedJob) -> dict:
    return {key: value for key, value in job.extra.items() if key.startswith("origin_")}


def _remember_origin(job: MergedJob) -> None:
    job.persisted_origin = deepcopy((_origin_snapshot(job), _origin_metadata(job)))


def _guard_search_origin(job: MergedJob, current: MergedJob) -> bool:
    previous, baseline = job.persisted_origin or (None, {})
    conflict = previous != _origin_snapshot(current)
    proposed, live = _origin_metadata(job), _origin_metadata(current)
    if conflict:
        job.origin_url, job.origin_ats, job.origin_confidence, job.apply_url = (
            current.origin_url, current.origin_ats, current.origin_confidence, current.apply_url)
        observation = proposed.get("origin_observation")
        if observation and observation != baseline.get("origin_observation"):
            live["origin_observation"] = {**observation, "write_reason": "snapshot_conflict"}
    else:
        _merge_updates(live, baseline, proposed)
    for key in proposed:
        job.extra.pop(key)
    job.extra.update(live)
    _remember_origin(current)
    job.persisted_origin = current.persisted_origin
    return conflict


def _merge_updates(extra: dict, before: dict, after: dict) -> None:
    for key in before.keys() | after.keys():
        if (key in before, before.get(key)) == (key in after, after.get(key)):
            continue
        if not key.startswith("origin_") and extra.get(key) != before.get(key):
            continue
        if key in after:
            extra[key] = after[key]
        else:
            extra.pop(key, None)


def _origin_job_from_row(row: sqlite3.Row) -> MergedJob:
    return MergedJob(id=row["id"], profile_id=row["profile_id"], title=row["title"], description=row["description"],
                     origin_url=row["origin_url"], origin_ats=row["origin_ats"], origin_confidence=row["origin_confidence"],
                     apply_url=row["apply_url"], extra=_json_object(row["extra"]), score=row["score"],
                     score_json=_json_object(row["score_json"]))


def origin_downgrade(before: str | None, after: str | None) -> bool:
    return before not in (None, "careers_site", "phenom") and before != after


@contextmanager
def origin_connection(path: Path, *, write: bool = False) -> Iterator[sqlite3.Connection]:
    """Open an existing database without initializing or altering its journal or schema."""
    path = path.absolute()
    wal, shm = (path.with_name(path.name + suffix) for suffix in ("-wal", "-shm"))
    if write:
        mode = "?mode=rw"
    elif wal.exists() and shm.exists():
        mode = "?mode=ro"
    elif wal.exists() and wal.stat().st_size:
        raise ValueError("Uncheckpointed job database needs its existing SQLite shared-memory file")
    else:
        mode = "?mode=ro&immutable=1"
    conn = sqlite3.connect(path.as_uri() + mode, uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        with conn:
            conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            if write:
                require_current_schema(conn)
            yield conn
    finally:
        conn.close()


def require_current_schema(conn: sqlite3.Connection) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version != len(MIGRATIONS):
        raise ValueError(f"Job database schema v{version}; expected v{len(MIGRATIONS)}. Run the standard migration separately before reresolve writes.")


def _plan_origin_updates(conn: sqlite3.Connection, updates: list[tuple[MergedJob, MergedJob]]) -> tuple[list, list]:
    transitions, pending = [], []
    for before, after in updates:
        row = conn.execute("SELECT * FROM jobs WHERE id=? AND profile_id=?", (before.id, before.profile_id)).fetchone()
        if row is None:
            transitions.append((None, None, "row_missing"))
            continue
        current = _origin_job_from_row(row)
        conflict = _origin_snapshot(current) != _origin_snapshot(before)
        next_origin = current if conflict else after
        transitions.append((current.origin_ats, next_origin.origin_ats, "snapshot_conflict" if conflict else None))
        pending.append((before, after, current, row["posted_at"], conflict))
    return transitions, pending


def save_origin_updates(updates: list[tuple[MergedJob, MergedJob]], *, max_downgrades: int | None = None,
                        path: Path | None = None, dry_run: bool = False) -> list[tuple]:
    """Plan from current rows; commit only if the snapshot still matches and the downgrade guard allows it."""
    if not updates:
        return []
    with origin_connection(path or db_path(), write=not dry_run) as conn:
        transitions, pending = _plan_origin_updates(conn, updates)
        if dry_run:
            return transitions
        downgrades = sum(origin_downgrade(before, after) for before, after, _ in transitions)
        if max_downgrades is not None and downgrades > max_downgrades:
            raise ValueError(f"downgrades of a known vendor: {downgrades} (maximum {max_downgrades}); no jobs written")
        for before, after, current, current_posted_at, conflict in pending:
            extra = current.extra
            if conflict:
                if observation := after.extra.get("origin_observation"):
                    extra["origin_observation"] = {**observation, "write_reason": "snapshot_conflict"}
                conn.execute("UPDATE jobs SET extra=? WHERE id=?", (json.dumps(extra, ensure_ascii=False, default=str), current.id))
                continue
            _merge_updates(extra, before.extra, after.extra)
            description = after.description if current.description == before.description else current.description
            posted_at = current_posted_at
            if before.posted_at != after.posted_at and _iso(before.posted_at) == current_posted_at:
                posted_at = _iso(after.posted_at)
            conn.execute("UPDATE jobs SET origin_url=?, origin_ats=?, origin_confidence=?, extra=?, apply_url=?,"
                         " description=?, posted_at=? WHERE id=?",
                         (after.origin_url, after.origin_ats, after.origin_confidence,
                          json.dumps(extra, ensure_ascii=False, default=str), after.apply_url,
                          description, posted_at, current.id))
            old_links = {(item.source, item.source_id): item.apply_url for item in before.sightings}
            for item in after.sightings:
                key = (item.source, item.source_id)
                if key in old_links and item.apply_url != old_links[key]:
                    conn.execute("UPDATE sightings SET apply_url=? WHERE job_id=? AND source=? AND source_id=? AND apply_url IS ?",
                                 (item.apply_url, current.id, *key, old_links[key]))
    return transitions


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
    max_age_days: int | None = None, status: str | None = None, sort: str = "rank", show_filtered: bool = False,
    include_stale: bool = False, new_only: bool = False, include_expired: bool = False,
    as_of: datetime | None = None,
) -> list[MergedJob]:
    """Filtered, sorted list for the shortlist UI.

    Jobs whose effective age exceeds MAX_JOB_AGE_DAYS (+ date tolerance) are hidden, never deleted; `max_age_days`
    can only narrow that window; `include_stale=True` (internal use only) disables it. Sorts: "rank" (fit x freshness), "score", "freshness".
    `new_only` keeps the "new today" jobs (see freshness.is_new). Jobs not seen for EXPIRE_AFTER_DAYS are hidden
    (status untouched) when every source they were seen on was checked OK by a run since, unless `include_expired`. `as_of` = clock (tests).
    """
    jobs = load_jobs(profile_id)
    limit = None if include_stale else min(max_age_days or MAX_JOB_AGE_DAYS, MAX_JOB_AGE_DAYS)  # hard cutoff: never wider
    ref_now = as_of or now()
    runs = [] if include_expired else recent_ok_runs(profile_id, ref_now)
    run_started = None
    if new_only:
        run = latest_completed_run(profile_id)
        run_started = _dt(run["started_at"]) if run else None
    out = []
    for j in jobs:
        if not show_filtered and j.score_json.get("prefiltered"):
            continue
        if not include_expired and expired_by_runs(j, ref_now, runs):
            continue
        if new_only and not is_new(j, run_started, ref_now):
            continue
        if min_score is not None and (j.score or 0) < min_score:
            continue
        if contract_type and j.contract_type.value != contract_type:
            continue
        if source and source not in j.sources:
            continue
        if status and j.status != status:
            continue
        if limit is not None:
            if is_stale(j, limit, ref_now):
                continue
        out.append(j)
    epoch = datetime(1970, 1, 1, tzinfo=UTC)

    def when(j: MergedJob) -> datetime:
        return effective_posted_at(j, ref_now)[0] or epoch

    if sort == "freshness":
        out.sort(key=lambda j: (when(j), j.score or 0), reverse=True)
    elif sort == "score":
        out.sort(key=lambda j: (j.score or 0, when(j)), reverse=True)
    else:
        out.sort(key=lambda j: (rank_score(j, ref_now), when(j)), reverse=True)
    return out


def shortlist_overview(profile_id: int, as_of: datetime | None = None) -> dict[str, int]:
    """Counts for the shortlist header, over the default view (no prefiltered, no >14-day, no expired jobs):
    `new` = new today, `expired` = jobs hidden only because they were not seen for EXPIRE_AFTER_DAYS."""
    ref_now = as_of or now()
    run = latest_completed_run(profile_id)
    run_started = _dt(run["started_at"]) if run else None
    runs = recent_ok_runs(profile_id, ref_now)
    new = expired = 0
    for j in load_jobs(profile_id):
        if j.score_json.get("prefiltered") or is_stale(j, MAX_JOB_AGE_DAYS, ref_now):
            continue
        if expired_by_runs(j, ref_now, runs):
            expired += 1
        elif is_new(j, run_started, ref_now):
            new += 1
    return {"new": new, "expired": expired}


def job_counts(profile_id: int) -> dict[str, int]:
    with connect() as c:
        rows = c.execute("SELECT status, COUNT(*) n FROM jobs WHERE profile_id=? GROUP BY status", (profile_id,)).fetchall()
    return {r["status"]: r["n"] for r in rows}
