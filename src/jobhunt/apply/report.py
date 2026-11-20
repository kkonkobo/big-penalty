"""`big-penalty apply report`: what stopped the runs of a batch, grouped so the next fix is obvious.

Read-only: the database is opened with mode=ro, and the run folders' `failure-run.json` / `failure-page.json` are read for the
few example runs of each cluster only. Prints job titles / companies (local data) but never names, e-mails or phones: the
reasons were already redacted when written, and are masked again here.
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter, defaultdict
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

from jobhunt import db
from jobhunt.apply.page_outline import OUTLINE_FILE, RUN_FILE, SCREENSHOT_FILE, safe_reason

UNSENT = ("needs_input", "needs_email", "needs_human", "blocked", "failed")
BATCH_GAP = timedelta(minutes=30)


@dataclass
class Example:
    run_id: int
    title: str
    reason: str
    refused: str
    overlays: list[str]
    screenshot: str
    url: str


@dataclass
class Cluster:
    code: str
    family: str
    runs: int = 0
    examples: list[Example] = field(default_factory=list)


@dataclass
class Report:
    since: str
    total: int
    by_status: Counter
    by_code: Counter
    clusters: list[Cluster]


def _utc(text: str) -> datetime:
    when = datetime.fromisoformat(text)
    if when.tzinfo is None:
        when = when.astimezone()  # naive input: local time
    return when.astimezone(timezone.utc)


def last_batch_start(conn: sqlite3.Connection, mode: str) -> str:
    """Start of the latest burst of runs (consecutive starts less than 30 minutes apart)."""
    rows = conn.execute("SELECT started_at FROM application_runs WHERE mode = ? ORDER BY julianday(started_at) DESC LIMIT 5000", (mode,)).fetchall()
    if not rows:
        return ""
    start = previous = _utc(rows[0][0])
    for (text,) in rows[1:]:
        current = _utc(text)
        if previous - current > BATCH_GAP:
            break
        start = previous = current
    return start.isoformat()


def family_of(url: str) -> str:
    from jobhunt.apply.identity import ats_target
    from jobhunt.origin import ats_of_host

    try:
        host = (urlsplit(url or "").hostname or "").lower()
    except ValueError:
        return "?"
    if not host:
        return "?"
    try:
        ats = ats_target(url)[0]
    except Exception:
        ats = None
    return ats or ats_of_host(host) or host


def _load(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _example(run_id: int, title: str, message: str, out_dir: str, url: str) -> Example:
    folder = Path(out_dir) if out_dir else None
    record = _load(folder / RUN_FILE) if folder else {}
    outline = _load(folder / OUTLINE_FILE) if folder else {}
    overlays = []
    for item in outline.get("overlays") or []:
        if isinstance(item, dict):
            label = item.get("id") or item.get("class") or item.get("role") or item.get("tag") or "?"
            overlays.append(f"{item.get('tag', '?')}#{label[:40]}{' [covers centre]' if item.get('covers_center') else ''}"
                            f" \"{safe_reason(str(item.get('text') or ''), limit=60)}\"")
    shot = folder / SCREENSHOT_FILE if folder else None
    return Example(run_id=run_id, title=title, url=outline.get("url") or record.get("final_url") or url,
                   reason=safe_reason(str(record.get("reason") or message or ""), limit=300),
                   refused=safe_reason(str(record.get("refused_request") or ""), limit=160), overlays=overlays[:4],
                   screenshot=str(shot) if shot and shot.is_file() else "")


def build_report(*, since: str = "", last_batch: bool = False, mode: str = "submit", path: Path | None = None,
                 per_cluster: int = 3) -> Report:
    uri = (path if path is not None else db.db_path()).absolute().as_uri() + "?mode=ro"
    marks = ",".join("?" for _ in UNSENT)
    with closing(sqlite3.connect(uri, uri=True)) as conn:
        if since:
            start = _utc(since).isoformat()
        elif last_batch:
            start = last_batch_start(conn, mode)
        else:
            start = ""
        where, args = f"r.status IN ({marks})", list(UNSENT)
        if mode != "all":
            where += " AND r.mode = ?"
            args.append(mode)
        if start:
            where += " AND julianday(r.started_at) >= julianday(?)"
            args.append(start)
        rows = conn.execute(
            f"""SELECT r.id, r.status, COALESCE(json_extract(r.result_json, '$.diagnostic_code'), ''), r.message, r.out_dir,
                       COALESCE(NULLIF(json_extract(r.result_json, '$.final_url'), ''), json_extract(r.result_json, '$.steps[0].url'), ''),
                       COALESCE(a.job_title, ''), COALESCE(a.company, ''), COALESCE(a.ats, '')
                FROM application_runs r LEFT JOIN applications a ON a.id = r.application_id
                WHERE {where} ORDER BY r.id DESC""", args).fetchall()
    by_status: Counter = Counter()
    by_code: Counter = Counter()
    groups: dict[tuple[str, str], Cluster] = {}
    for run_id, status, code, message, out_dir, url, title, company, label in rows:
        code = code or "(none)"
        by_status[status] += 1
        by_code[f"{status}/{code}"] += 1
        family = family_of(url) if url else label or "?"
        if family == "?" and label:
            family = label
        cluster = groups.setdefault((code, family), Cluster(code, family))
        cluster.runs += 1
        if len(cluster.examples) < per_cluster:
            cluster.examples.append(_example(run_id, f"{company} - {title}".strip(" -"), message or "", out_dir or "", url))
    ordered = sorted(groups.values(), key=lambda c: (-c.runs, c.code, c.family))
    return Report(since=start, total=len(rows), by_status=by_status, by_code=by_code, clusters=ordered)


def render(report: Report, *, max_clusters: int = 30) -> str:
    lines = [f"Unsent runs since {report.since or 'the beginning'}: {report.total}", ""]
    lines.append("By status: " + ", ".join(f"{k}={v}" for k, v in report.by_status.most_common()))
    lines.append("By status/code:")
    lines += [f"  {v:4d}  {k}" for k, v in report.by_code.most_common()]
    lines += ["", "Clusters (code, ATS / host family):"]
    for cluster in report.clusters[:max_clusters]:
        lines.append(f"\n[{cluster.runs}] {cluster.code}  @ {cluster.family}")
        for ex in cluster.examples:
            lines.append(f"  run {ex.run_id}  {ex.title[:70]}")
            lines.append(f"    reason : {ex.reason or '-'}")
            if ex.refused:
                lines.append(f"    refused: {ex.refused}")
            if ex.overlays:
                lines.append("    overlays: " + " ; ".join(ex.overlays))
            lines.append(f"    screenshot: {ex.screenshot or '-'}")
    if len(report.clusters) > max_clusters:
        lines.append(f"\n... {len(report.clusters) - max_clusters} more clusters")
    return "\n".join(lines)
