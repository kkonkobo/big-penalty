"""Freshness: one place that decides how old a job really is and how much that weighs in the ranking.

Aggregators (LinkedIn, Indeed...) reset `posted_at` on reposts, so the most reliable date is, in order:
the origin ATS posting date, the earliest date any source gave, then the day we first saw the job.
A date later than `first_seen` is impossible for a genuine posting (we saw it before), so it marks a repost.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from jobhunt.dedupe import MergedJob

MAX_JOB_AGE_DAYS = 14  # hard cutoff: searches ask for this and the shortlist hides anything older
HALF_LIFE_DAYS = 7.0
WEIGHT_FLOOR = 0.4  # weight of a very old job
WEIGHT_RANGE = 0.6  # floor + range = 1.0 for a job posted today
REPOST_TOLERANCE = timedelta(days=1)
# Some sources only give a date (no time) or round to whole days/windows, so a job is only stale once its age
# exceeds the limit by more than this tolerance (a "14 days" job dated at midnight is still 14.x days old).
AGE_TOLERANCE_DAYS = 1.0
UNKNOWN_AGE_DAYS = 7.0  # no usable date at all: neutral-ish weight


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


def freshness_weight(age: float | None) -> float:
    """1.0 today, 0.7 at 7 days, 0.55 at 14 days, tending to WEIGHT_FLOOR."""
    a = UNKNOWN_AGE_DAYS if age is None else max(0.0, age)
    return WEIGHT_FLOOR + WEIGHT_RANGE * 0.5 ** (a / HALF_LIFE_DAYS)


def rank_score(job: MergedJob, now: datetime | None = None) -> float:
    return (job.score or 0) * freshness_weight(age_days(job, now))
