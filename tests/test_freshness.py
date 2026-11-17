from datetime import UTC, datetime, timedelta

import pytest

from jobhunt.dedupe import MergedJob
from jobhunt.freshness import (MAX_JOB_AGE_DAYS, age_days, effective_posted_at, freshness_weight, is_repost,
                               rank_score)
from jobhunt.models import JobPosting, SearchQuery

NOW = datetime(2026, 11, 17, 12, tzinfo=UTC)


def d(days: float) -> datetime:
    return NOW - timedelta(days=days)


def job(**kw) -> MergedJob:
    extra = kw.pop("extra", {})
    return MergedJob(title="T", extra=extra, **kw)


def test_default_query_window_uses_constant():
    assert MAX_JOB_AGE_DAYS == 14
    assert SearchQuery(keywords="x").posted_within_days == MAX_JOB_AGE_DAYS


def test_origin_date_preferred_when_confident():
    j = job(posted_at=d(1), first_seen=d(10), origin_confidence=0.8, extra={"origin_posted_at": d(9).isoformat()})
    assert effective_posted_at(j, NOW) == (d(9), "origin")
    assert not is_repost(j, NOW)


def test_low_confidence_origin_ignored():
    j = job(posted_at=d(3), first_seen=d(2), origin_confidence=0.3, extra={"origin_posted_at": d(9).isoformat()})
    assert effective_posted_at(j, NOW) == (d(3), "source")


def test_source_then_first_seen_fallback():
    assert effective_posted_at(job(posted_at=d(6), first_seen=d(4)), NOW) == (d(6), "source")
    assert effective_posted_at(job(first_seen=d(2)), NOW) == (d(2), "first_seen")
    assert effective_posted_at(job(), NOW) == (None, "none")


def test_repost_falls_back_to_first_seen():
    j = job(posted_at=d(1), first_seen=d(12))  # source says yesterday but we saw it 12 days ago
    assert effective_posted_at(j, NOW) == (d(12), "first_seen")
    assert is_repost(j, NOW)
    assert not is_repost(job(posted_at=d(1), first_seen=d(1) - timedelta(hours=5)), NOW)  # within 1 day tolerance


def test_future_date_clamped_and_naive_treated_as_utc():
    j = job(posted_at=NOW + timedelta(days=3))
    assert effective_posted_at(j, NOW)[0] == NOW
    naive = job(posted_at=d(2).replace(tzinfo=None))
    assert age_days(naive, NOW) == pytest.approx(2)


def test_weight_curve_and_rank():
    assert freshness_weight(0) == pytest.approx(1.0)
    assert freshness_weight(7) == pytest.approx(0.7)
    assert freshness_weight(14) == pytest.approx(0.55)
    assert rank_score(job(score=80, posted_at=d(7)), NOW) == pytest.approx(56)
    assert rank_score(job(posted_at=d(7)), NOW) == 0


def test_origin_posted_at_carried_into_extra():
    j = job()
    j.apply_origin(JobPosting(source="s", source_id="1", url="u", title="T", origin_url="https://x.com/job/1",
                              origin_ats="lever", origin_confidence=0.9, origin_posted_at=d(5)))
    assert j.extra["origin_posted_at"] == d(5).isoformat()


def test_is_stale_has_date_granularity_tolerance():
    from jobhunt.freshness import is_stale
    assert not is_stale(job(posted_at=d(14.9)), now=NOW)
    assert is_stale(job(posted_at=d(15.5)), now=NOW)
    assert not is_stale(job(), now=NOW)


def test_relative_30_plus_dates_are_lower_bounds():
    from jobhunt.sources._freelance_util import parse_relative_fr
    from jobhunt.sources._fr_public import parse_fr_date
    from jobhunt.sources.cadremploi import parse_relative_date
    from jobhunt.sources.jobijoba import parse_pub_date
    for f in (parse_pub_date, parse_fr_date, parse_relative_date, parse_relative_fr):
        r = f("Il y a 30+ jours")
        assert r is not None and (datetime.now(UTC) - r).days >= 29, f


def test_jobspy_dates_keep_offsets():
    from jobhunt.sources._common import _to_dt
    assert _to_dt("2026-11-01T11:00:00+02:00") == datetime(2026, 11, 1, 9, tzinfo=UTC)
    assert _to_dt("2026-11-01").tzinfo is not None
