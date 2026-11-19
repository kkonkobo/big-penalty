"""Daily run: search every profile headless, print a short French summary, log it, notify (macOS).

`jobhunt daily` is what the in-app scheduler (see schedule.py) launches. Everything with side effects
(search, notification, clock) is injectable so tests never touch the network, the LLM or the desktop.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterator, Mapping

from jobhunt import db
from jobhunt.config import get_settings
from jobhunt.filelock import try_lock, unlock
from jobhunt.freshness import age_days

LOCK_NAME = "daily.lock"
HIGH_SCORE = 70
TOP_N = 5

SearchFn = Callable[..., Awaitable[Any]]
ApplyFn = Callable[[int], Awaitable[Any]]  # profile_id -> ApplySummary (jobhunt.apply.service.daily_apply)


class DailyLocked(Exception):
    """Another daily run holds the lock."""


# ---------------------------------------------------------------- lock
@contextmanager
def daily_lock(data_dir: Path | None = None, now: datetime | None = None) -> Iterator[Path]:
    """Exclusive non-blocking lock (jobhunt.filelock: flock, or a byte lock on Windows) on the persistent file data/daily.lock (never unlinked: no takeover race).

    The kernel drops the lock when the process exits, however it dies, so there is no stale-lock logic. The file
    content (pid, start time) is for diagnostics only. Raises DailyLocked if another process holds it.
    """
    path = (data_dir or get_settings().data_dir) / LOCK_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    locked = False
    try:
        if not try_lock(fd):
            raise DailyLocked(f"un autre `jobhunt daily` est déjà en cours ({path})")
        locked = True
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()} {(now or datetime.now().astimezone()).isoformat()}\n".encode())
        yield path
    finally:
        if locked:
            unlock(fd)
        os.close(fd)  # the file stays


# ---------------------------------------------------------------- summary
@dataclass
class TopJob:
    title: str
    company: str
    fit: int
    age: float | None


@dataclass
class ProfileSummary:
    profile_id: int
    name: str
    new: int = 0
    high: int = 0  # new jobs with score >= HIGH_SCORE
    top: list[TopJob] = field(default_factory=list)
    raw_postings: int = 0
    run_id: int | None = None
    error: str | None = None  # real failure of the run (not a single source failing)
    apply: Any = None  # ApplySummary of the automatic applications step (None = not run / failed)


@dataclass
class DailyReport:
    profiles: list[ProfileSummary] = field(default_factory=list)
    locked: bool = False

    @property
    def new(self) -> int:
        return sum(p.new for p in self.profiles)

    @property
    def high(self) -> int:
        return sum(p.high for p in self.profiles)

    @property
    def failed(self) -> bool:
        return any(p.error for p in self.profiles)

    @property
    def sent(self) -> int:
        return sum(p.apply.sent for p in self.profiles if p.apply)

    @property
    def waiting(self) -> int:
        return sum(p.apply.waiting for p in self.profiles if p.apply)


def summarize_profile(profile_id: int, name: str, *, as_of: datetime | None = None) -> ProfileSummary:
    """New-today jobs of a profile: count, how many >= 70, top 5 by rank."""
    jobs = db.query_jobs(profile_id, new_only=True, as_of=as_of)  # default sort = rank (fit x freshness)
    return ProfileSummary(
        profile_id, name, new=len(jobs), high=sum(1 for j in jobs if (j.score or 0) >= HIGH_SCORE),
        top=[TopJob(j.title, j.company or "entreprise non précisée", int(j.score or 0), age_days(j, as_of))
             for j in jobs[:TOP_N]])


def _age_label(age: float | None) -> str:
    if age is None:
        return "âge inconnu"
    return "aujourd'hui" if age < 1 else f"{int(age)} j"


def _plural(n: int, one: str, many: str) -> str:
    return f"{n} {one if n <= 1 else many}"


def format_summary(s: ProfileSummary) -> list[str]:
    """Lines (plain text, no markup) for one profile."""
    if s.error:
        return [f"Profil {s.profile_id} ({s.name}) : ERREUR — {s.error}"] + ([apply_line(s.apply)] if s.apply is not None else [])
    lines = [f"Profil {s.profile_id} ({s.name}) : {_plural(s.new, 'nouvelle offre', 'nouvelles offres')} aujourd'hui, "
             f"{s.high} ≥ {HIGH_SCORE}"]
    for i, t in enumerate(s.top, 1):
        lines.append(f"  {i}. {t.title} — {t.company} · fit {t.fit} · {_age_label(t.age)}")
    if s.apply is not None:
        lines.append(apply_line(s.apply))
    return lines


def apply_line(a: Any) -> str:
    """"Candidatures : 2 envoyées, 3 en attente de validation, 1 en pause, 0 échec"."""
    return (f"Candidatures : {_plural(a.sent, 'envoyée', 'envoyées')}, {a.waiting} en attente de validation, "
            f"{a.paused} en pause, {_plural(a.failed, 'échec', 'échecs')}")


def log_line(when: datetime, s: ProfileSummary | None, message: str = "") -> str:
    ts = when.replace(microsecond=0).isoformat()
    if s is None:
        return f"{ts} {message}"
    status = f"ERREUR {s.error}" if s.error else "ok"
    line = (f"{ts} profil={s.profile_id} run={s.run_id} statut={status} brutes={s.raw_postings} "
            f"nouvelles={s.new} >=70={s.high}")
    if s.apply is not None:
        a = s.apply
        line += f" cv={a.prepared} envoyees={a.sent} attente={a.waiting} pause={a.paused} echecs={a.failed}"
    return line


def append_log(line: str, when: datetime, log_dir: Path | None = None) -> Path:
    d = log_dir or get_settings().data_dir / "logs"
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"daily-{when.date().isoformat()}.log"
    with path.open("a", encoding="utf-8") as f:
        f.write(line.replace("\n", " ") + "\n")
    return path


# ---------------------------------------------------------------- notification
def _applescript_quote(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def notification_body(new: int, high: int, failed: bool = False, sent: int = 0, waiting: int = 0) -> str:
    body = f"{_plural(new, 'nouvelle offre', 'nouvelles offres')}, {high} ≥{HIGH_SCORE}"
    if sent:
        body += f", {_plural(sent, 'candidature envoyée', 'candidatures envoyées')}"
    if waiting:
        body += f", {waiting} à valider"
    return body + " (erreur)" if failed else body


def notify(title: str, body: str, *, platform: str | None = None, env: Mapping[str, str] | None = None,
           run: Callable[..., Any] = subprocess.run) -> bool:
    """macOS notification via osascript. Best effort: never raises. Skipped off darwin or if JOBHUNT_NO_NOTIFY=1."""
    if (platform or sys.platform) != "darwin" or (env if env is not None else os.environ).get("JOBHUNT_NO_NOTIFY") == "1":
        return False
    script = f"display notification {_applescript_quote(body)} with title {_applescript_quote(title)}"
    try:
        run(["osascript", "-e", script], check=False, capture_output=True, timeout=15)
    except Exception:  # noqa: BLE001 - a missing notification must never fail the run
        return False
    return True


# ---------------------------------------------------------------- orchestration
def run_daily(
    profile_ids: list[int] | None = None, *, search: SearchFn | None = None,
    apply: ApplyFn | None = None, notify_fn: Callable[[str, str], Any] = notify, emit: Callable[[str], None] = print,
    now: Callable[[], datetime] | None = None, data_dir: Path | None = None,
) -> DailyReport:
    """Search every profile (or `profile_ids`) one after the other, headless, and report what is new today.

    A lock held by another live run -> nothing is done (report.locked). A profile whose run fails is recorded
    (report.failed) and the next profile still runs. Unknown ids raise ValueError before anything starts.
    """
    clock = now or (lambda: datetime.now().astimezone())
    profiles = db.list_profiles()
    if profile_ids:
        by_id = {p.id: p for p in profiles}
        missing = [i for i in profile_ids if i not in by_id]
        if missing:
            raise ValueError("profil(s) introuvable(s) : " + ", ".join(map(str, missing)))
        profiles = [by_id[i] for i in dict.fromkeys(profile_ids)]
    report = DailyReport()
    log_dir = (data_dir / "logs") if data_dir else None
    if not profiles:
        emit("Aucun profil : rien à faire.")
        return report
    try:
        with daily_lock(data_dir, clock()):
            if search is None:
                from jobhunt.pipeline import run_search as search  # noqa: PLC0415 - heavy imports only when needed
                # The real apply step only goes with the real search (injected fakes never open a browser), and only once
                # switched on (JOBHUNT_APPLY_DAILY=1): it spends LLM calls on CVs and may send applications.
                if apply is None and get_settings().apply_daily:
                    from jobhunt.apply.service import daily_apply  # noqa: PLC0415

                    async def apply(pid: int) -> Any:
                        return await daily_apply(pid, emit=emit)
            for p in profiles:
                summary = _search_profile(p.id, p.name, search)
                if apply is not None:
                    summary.apply = _apply_profile(p.id, apply, emit)
                report.profiles.append(summary)
                append_log(log_line(clock(), summary), clock(), log_dir)
                for line in format_summary(summary):
                    emit(line)
    except DailyLocked as e:
        report.locked = True
        append_log(log_line(clock(), None, f"ignoré : {e}"), clock(), log_dir)
        emit(f"Déjà en cours, rien à faire : {e}")
        return report
    notify_fn("Big Penalty", notification_body(report.new, report.high, report.failed, report.sent, report.waiting))
    return report


def _apply_profile(profile_id: int, apply: ApplyFn, emit: Callable[[str], None]) -> Any:
    """Prepare / send the applications of one profile. Never raises: the daily run must not fail because of it."""
    try:
        return asyncio.run(apply(profile_id))
    except Exception as e:  # noqa: BLE001
        emit(f"Candidatures : étape ignorée ({type(e).__name__})")
        return None


def _search_profile(profile_id: int, name: str, search: SearchFn) -> ProfileSummary:
    """One profile: run the pipeline, then summarize what is new. A failed run is an error, not an exception."""
    error = None
    run_id = raw = None
    try:
        result = asyncio.run(search(profile_id, headless=True))  # unattended: nobody to solve a challenge
        run_id, raw = result.run_id, result.raw_postings
        if result.stage == "error":
            error = "; ".join(result.errors[-1:]) or "échec de la recherche"
    except Exception as e:  # noqa: BLE001
        error = f"{type(e).__name__}: {e}"
    if error:
        return ProfileSummary(profile_id, name, run_id=run_id, raw_postings=raw or 0, error=error)
    summary = summarize_profile(profile_id, name)
    summary.run_id, summary.raw_postings = run_id, raw or 0
    return summary
