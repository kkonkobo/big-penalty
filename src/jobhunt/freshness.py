"""Freshness: one place that decides how old a job really is and how much that weighs in the ranking.

Aggregators (LinkedIn, Indeed...) reset `posted_at` on reposts, so the most reliable date is, in order:
the origin ATS posting date, the earliest date any source gave, then the day we first saw the job.
A date later than `first_seen` is impossible for a genuine posting (we saw it before), so it marks a repost.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Collection, Iterable
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from jobhunt.dedupe import MergedJob

MAX_JOB_AGE_DAYS = 14  # hard cutoff: searches ask for this and the shortlist hides anything older
# Owner (2026-11-26): the fresher the job when the application goes, the better its chances, so freshness weighs heavily:
# about -11 % after one day, -35 % after four, -49 % after a week.
HALF_LIFE_DAYS = 4.0
WEIGHT_FLOOR = 0.3  # weight of a very old job
WEIGHT_RANGE = 0.7  # floor + range = 1.0 for a job posted today
REPOST_TOLERANCE = timedelta(days=1)
# Some sources only give a date (no time) or round to whole days/windows, so a job is only stale once its age
# exceeds the limit by more than this tolerance (a "14 days" job dated at midnight is still 14.x days old).
AGE_TOLERANCE_DAYS = 1.0
UNKNOWN_AGE_DAYS = 7.0  # no usable date at all: neutral-ish weight
EXPIRE_AFTER_DAYS = 7  # a job not seen for this long is hidden (never deleted) if all its sources were checked since ...
EXPIRY_RUN_WINDOW_DAYS = 2  # ... but only judged by completed runs that finished this recently
PARIS = ZoneInfo("Europe/Paris")


def _utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def _parse(v: object) -> datetime | None:
    if isinstance(v, datetime):
        return _utc(v)
    if isinstance(v, str) and v:
        try:
            return _utc(datetime.fromisoformat(v.replace("Z", "+00:00")))
        except ValueError:
            return None
    return None


def _resolve(job: MergedJob, now: datetime | None = None) -> tuple[datetime | None, str, bool]:
    from jobhunt.origin import MIN_CONFIDENCE

    now = _utc(now) or datetime.now(UTC)
    first_seen = _utc(job.first_seen)
    origin = _parse((job.extra or {}).get("origin_posted_at"))
    if origin and (job.origin_confidence or 0) >= MIN_CONFIDENCE:
        chosen, label = origin, "origin"
    elif job.posted_at:
        chosen, label = _utc(job.posted_at), "source"
    elif first_seen:
        chosen, label = first_seen, "first_seen"
    else:
        return None, "none", False
    repost = False
    if first_seen and chosen > first_seen + REPOST_TOLERANCE:
        chosen, label, repost = first_seen, "first_seen", True
    return min(chosen, now), label, repost


def effective_posted_at(job: MergedJob, now: datetime | None = None) -> tuple[datetime | None, str]:
    """Most reliable posting date and where it came from: "origin", "source", "first_seen" ("none" if unknown)."""
    d, label, _ = _resolve(job, now)
    return d, label


def is_repost(job: MergedJob, now: datetime | None = None) -> bool:
    return _resolve(job, now)[2]


def age_days(job: MergedJob, now: datetime | None = None) -> float | None:
    d, _, _ = _resolve(job, now)
    if d is None:
        return None
    return max(0.0, ((_utc(now) or datetime.now(UTC)) - d).total_seconds() / 86400)


def is_stale(job: MergedJob, limit_days: float = MAX_JOB_AGE_DAYS, now: datetime | None = None) -> bool:
    """True when the effective age exceeds `limit_days` + AGE_TOLERANCE_DAYS. Unknown age is never stale."""
    age = age_days(job, now)
    return age is not None and age > limit_days + AGE_TOLERANCE_DAYS


def over_age_limit(job: MergedJob, limit_days: float = MAX_JOB_AGE_DAYS, now: datetime | None = None) -> bool | None:
    """STRICT age check (no AGE_TOLERANCE_DAYS): True when the effective age exceeds `limit_days`, None when it is unknown.
    Used before sending an application; the shortlist keeps its 1-day tolerance (`is_stale`)."""
    age = age_days(job, now)
    return None if age is None else age > limit_days


def freshness_weight(age: float | None) -> float:
    """1.0 today, 0.65 at 4 days, about 0.51 at 7 days and 0.36 at 14 days, tending to WEIGHT_FLOOR."""
    a = UNKNOWN_AGE_DAYS if age is None else max(0.0, age)
    return WEIGHT_FLOOR + WEIGHT_RANGE * 0.5 ** (a / HALF_LIFE_DAYS)


def rank_score(job: MergedJob, now: datetime | None = None) -> float:
    return (job.score or 0) * freshness_weight(age_days(job, now))


def start_of_today_paris(now: datetime | None = None) -> datetime:
    """Midnight (Europe/Paris) of the current Paris day, as an aware UTC datetime."""
    local = (_utc(now) or datetime.now(UTC)).astimezone(PARIS)
    return local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)


def new_cutoff(latest_completed_run_started_at: datetime | None, now: datetime | None = None) -> datetime:
    """A job first seen at or after this instant is "new": the earlier of the start of today (Paris) and the start
    of the profile's latest completed run (so a run that started just before midnight, or a catch-up after
    several days without a run, still reports everything it found)."""
    today = start_of_today_paris(now)
    run = _utc(latest_completed_run_started_at)
    return min(today, run) if run else today


def is_new(job: MergedJob, latest_completed_run_started_at: datetime | None = None, now: datetime | None = None) -> bool:
    """"New today": first seen since `new_cutoff` and not a repost of an older posting."""
    first_seen = _utc(job.first_seen)
    if first_seen is None or first_seen < new_cutoff(latest_completed_run_started_at, now):
        return False
    return not is_repost(job, now)


def is_expired(job: MergedJob, now: datetime | None = None) -> bool:
    """last_seen older than EXPIRE_AFTER_DAYS (necessary, not sufficient: see `expired_by_runs`)."""
    last_seen = _utc(job.last_seen)
    return last_seen is not None and (_utc(now) or datetime.now(UTC)) - last_seen > timedelta(days=EXPIRE_AFTER_DAYS)


def expired_by_runs(job: MergedJob, now: datetime | None, runs: Iterable[tuple[datetime, Collection[str]]]) -> bool:
    """A job is expired only if it was not seen for EXPIRE_AFTER_DAYS AND every source it was sighted on completed
    without error in some run that started after its last sighting. `runs` = (started_at, sources_ok) of the profile's
    completed runs from the last EXPIRY_RUN_WINDOW_DAYS. Sources that failed or were not queried never expire a job."""
    if not is_expired(job, now):
        return False
    last_seen = _utc(job.last_seen)
    sources = job.sources
    if not sources:
        return False
    runs = [(_utc(started), ok) for started, ok in runs]
    return all(any(started is not None and started > last_seen and s in ok for started, ok in runs) for s in sources)
