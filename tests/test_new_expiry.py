"""'New today' and expiry: pure rules (freshness.py) and their use in db.query_jobs / shortlist_overview / web / CLI."""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from jobhunt import db
from jobhunt.dedupe import MergedJob
from jobhunt.freshness import (EXPIRE_AFTER_DAYS, expired_by_runs, is_expired, is_new, new_cutoff,
                               start_of_today_paris)
from jobhunt.models import JobPosting

NOW = datetime(2026, 11, 17, 12, tzinfo=UTC)  # 13:00 in Paris (CET, UTC+1)
PARIS_MIDNIGHT = datetime(2026, 11, 16, 23, tzinfo=UTC)


def d(days: float = 0, hours: float = 0) -> datetime:
    return NOW - timedelta(days=days, hours=hours)


def mj(first_seen=None, posted_at=None, last_seen=None) -> MergedJob:
    return MergedJob(title="T", first_seen=first_seen, last_seen=last_seen, posted_at=posted_at)


# ------------------------------------------------------------ pure rules
def test_start_of_today_paris_winter_and_summer():
    assert start_of_today_paris(NOW) == PARIS_MIDNIGHT
    assert start_of_today_paris(datetime(2026, 11, 16, 23, 30, tzinfo=UTC)) == PARIS_MIDNIGHT  # 00:30 on the 17th in Paris
    assert start_of_today_paris(datetime(2026, 11, 16, 22, 59, tzinfo=UTC)) == PARIS_MIDNIGHT - timedelta(days=1)  # 23:59 on the 16th
    summer = datetime(2026, 7, 1, 22, 30, tzinfo=UTC)  # 00:30 on 2 July in Paris (CEST, UTC+2)
    assert start_of_today_paris(summer) == datetime(2026, 7, 1, 22, tzinfo=UTC)


def test_new_cutoff_is_earlier_of_today_and_latest_run():
    assert new_cutoff(None, NOW) == PARIS_MIDNIGHT
    assert new_cutoff(d(hours=5), NOW) == PARIS_MIDNIGHT  # run started 07:00 UTC today: midnight is earlier
    assert new_cutoff(PARIS_MIDNIGHT - timedelta(minutes=10), NOW) == PARIS_MIDNIGHT - timedelta(minutes=10)  # run over midnight
    assert new_cutoff(d(3), NOW) == d(3)  # catch-up after 3 days without a run


@pytest.mark.parametrize("first_seen,run_started,expected", [
    (d(hours=1), None, True),
    (PARIS_MIDNIGHT, None, True),  # boundary is inclusive
    (PARIS_MIDNIGHT - timedelta(seconds=1), None, False),
    (d(2), d(3), True),  # seen since the latest completed run, even if before today
    (d(4), d(3), False),
    (None, None, False),
])
def test_is_new_boundaries(first_seen, run_started, expected):
    assert is_new(mj(first_seen=first_seen), run_started, NOW) is expected


def test_repost_is_never_new():
    repost = mj(first_seen=d(hours=1), posted_at=NOW + timedelta(days=2))  # source date later than first sighting
    assert not is_new(repost, None, NOW)


def test_expiry_rules():
    assert EXPIRE_AFTER_DAYS == 7
    assert is_expired(mj(last_seen=d(7, 1)), NOW) and not is_expired(mj(last_seen=d(6.9)), NOW)
    assert not is_expired(mj(), NOW)  # no last_seen: never expired


def sighted(source="indeed", **kw) -> MergedJob:
    j = mj(**kw)
    j.add(JobPosting(source=source, source_id="1", url="https://x.test/1", title="T"))
    return j


def test_expired_by_runs_requires_every_source_checked_after_last_seen():
    job = sighted(last_seen=d(8))
    assert expired_by_runs(job, NOW, [(d(1), {"indeed"})])
    assert not expired_by_runs(job, NOW, [])  # no run: nothing was checked
    assert not expired_by_runs(job, NOW, [(d(1), set())])  # old run without sources_ok / source failed
    assert not expired_by_runs(job, NOW, [(d(1), {"other"})])  # its source was not queried
    assert not expired_by_runs(job, NOW, [(d(9), {"indeed"})])  # run started before the last sighting
    assert not expired_by_runs(sighted(last_seen=d(6)), NOW, [(d(1), {"indeed"})])  # not old enough
    assert not expired_by_runs(mj(last_seen=d(8)), NOW, [(d(1), {"indeed"})])  # no sighting: no evidence
    two = sighted(last_seen=d(8))
    two.add(JobPosting(source="wttj", source_id="2", url="https://y.test/2", title="T"))
    assert not expired_by_runs(two, NOW, [(d(1), {"indeed"})])
    assert expired_by_runs(two, NOW, [(d(1), {"indeed"}), (d(0.5), {"wttj"})])  # across runs


# ------------------------------------------------------------ db
def add_job(profile, sid, *, first_seen, last_seen=None, posted_at=None, score=80, title=None, **kw) -> int:
    j = MergedJob.from_posting(JobPosting(
        source="indeed", source_id=sid, url=f"https://indeed.com/{sid}", title=title or f"Stage firmware {sid}",
        company="Acme", location="Toulouse", posted_at=posted_at or first_seen, **kw))
    j.score = score
    db.save_jobs(profile.id, [j])
    with db.connect() as c:
        c.execute("UPDATE jobs SET first_seen=?, last_seen=? WHERE id=?",
                  (first_seen.isoformat(), (last_seen or first_seen).isoformat(), j.id))
    return j.id


def add_run(profile, *, started, finished=None, status="done", raw=10, ok=("indeed",)) -> int:
    rid = db.create_run(profile.id)
    stats = {"raw_postings": raw}
    if ok is not None:
        stats["sources_ok"] = list(ok)
    db.update_run(rid, stats=stats, status=status, finished=True)
    with db.connect() as c:
        c.execute("UPDATE runs SET started_at=?, finished_at=? WHERE id=?",
                  (started.isoformat(), (finished or started + timedelta(minutes=20)).isoformat(), rid))
    return rid


def ids(jobs):
    return sorted(j.id for j in jobs)


def test_query_new_only_default_cutoff_and_still_14_day_limited(profile):
    today = add_job(profile, "today", first_seen=d(hours=2))
    yesterday = add_job(profile, "yday", first_seen=d(1), posted_at=d(1))
    old_post_new_to_us = add_job(profile, "oldpost", first_seen=d(hours=3), posted_at=d(20))  # source date 20 days old
    assert ids(db.query_jobs(profile.id, new_only=True, as_of=NOW)) == [today]  # old one: stale (>14 d) -> hidden
    assert old_post_new_to_us not in ids(db.query_jobs(profile.id, new_only=True, as_of=NOW))
    assert ids(db.query_jobs(profile.id, as_of=NOW)) == sorted([today, yesterday])


def test_query_new_only_excludes_reposts_and_uses_latest_completed_run(profile):
    repost = add_job(profile, "repost", first_seen=d(hours=3), posted_at=NOW + timedelta(days=3))
    since_run = add_job(profile, "sincerun", first_seen=d(2.5))
    before_run = add_job(profile, "beforerun", first_seen=d(4))
    add_run(profile, started=d(3))
    add_run(profile, started=d(hours=1), status="error")  # failed / not completed runs don't count
    got = ids(db.query_jobs(profile.id, new_only=True, as_of=NOW))
    assert got == [since_run] and repost not in got and before_run not in got


def test_new_only_keeps_other_filters_and_rank_sort(profile):
    low = add_job(profile, "low", first_seen=d(hours=2), score=40)
    high = add_job(profile, "high", first_seen=d(hours=2), score=90)
    assert [j.id for j in db.query_jobs(profile.id, new_only=True, as_of=NOW)] == [high, low]
    assert ids(db.query_jobs(profile.id, new_only=True, min_score=70, as_of=NOW)) == [high]


def test_expiry_hides_only_after_recent_run_that_checked_the_source(profile):
    stale = add_job(profile, "stale", first_seen=d(9), last_seen=d(8))
    fresh = add_job(profile, "fresh", first_seen=d(9), last_seen=d(1))
    db.set_status([stale], "approved")
    # no run at all, or only an old one: nothing expires
    assert ids(db.query_jobs(profile.id, include_stale=True, as_of=NOW)) == sorted([stale, fresh])
    add_run(profile, started=d(5))
    assert ids(db.query_jobs(profile.id, include_stale=True, as_of=NOW)) == sorted([stale, fresh])
    # successful run within 2 days that checked "indeed": stale one is hidden (kept, status untouched)
    add_run(profile, started=d(1), raw=12)
    assert ids(db.query_jobs(profile.id, include_stale=True, as_of=NOW)) == [fresh]
    assert ids(db.query_jobs(profile.id, include_stale=True, include_expired=True, as_of=NOW)) == sorted([stale, fresh])
    assert db.get_job(stale).status == "approved"


def test_failed_partial_or_legacy_runs_do_not_expire(profile):
    stale = add_job(profile, "stale", first_seen=d(9), last_seen=d(8))
    add_run(profile, started=d(1), status="error")
    add_run(profile, started=d(1), status="done", raw=0, ok=[])  # every source failed
    add_run(profile, started=d(1), raw=50, ok=["wttj"])  # lots of postings, but indeed was not checked
    add_run(profile, started=d(1), raw=50, ok=None)  # legacy run without sources_ok
    assert ids(db.query_jobs(profile.id, include_stale=True, as_of=NOW)) == [stale]
    assert db.shortlist_overview(profile.id, NOW)["expired"] == 0
    add_run(profile, started=d(1), raw=0)  # indeed checked fine and returned nothing: the job really is gone
    assert ids(db.query_jobs(profile.id, include_stale=True, as_of=NOW)) == []


def test_run_started_before_last_seen_or_finished_long_ago_does_not_count(profile):
    stale = add_job(profile, "stale", first_seen=d(9), last_seen=d(8))
    add_run(profile, started=d(9))  # before the last sighting
    add_run(profile, started=d(4), finished=d(3.9))  # after, but finished more than 2 days ago
    assert ids(db.query_jobs(profile.id, include_stale=True, as_of=NOW)) == [stale]


def test_shortlist_overview_counts(profile):
    add_job(profile, "n1", first_seen=d(hours=2))
    add_job(profile, "n2", first_seen=d(hours=1))
    add_job(profile, "old", first_seen=d(4), last_seen=d(3))
    add_job(profile, "gone", first_seen=d(9), last_seen=d(8), posted_at=d(8))
    assert db.shortlist_overview(profile.id, NOW) == {"new": 2, "expired": 0}  # no recent run -> no expiry
    add_run(profile, started=d(1))
    assert db.shortlist_overview(profile.id, NOW) == {"new": 2, "expired": 1}


# ------------------------------------------------------------ web + CLI
def test_web_new_tab_badge_and_expired_note(profile):
    from jobhunt.web.app import app

    add_job(profile, "n1", first_seen=datetime.now(UTC) - timedelta(minutes=5), title="Stage nouveauté unique")
    add_job(profile, "o1", first_seen=datetime.now(UTC) - timedelta(days=3), title="Stage ancienne annonce")
    add_job(profile, "x1", first_seen=datetime.now(UTC) - timedelta(days=9), last_seen=datetime.now(UTC) - timedelta(days=8),
            title="Stage expirée annonce")
    add_run(profile, started=datetime.now(UTC) - timedelta(hours=30))  # not today, no new cutoff surprises
    with TestClient(app) as c:
        full = c.get(f"/profiles/{profile.id}/shortlist").text
        assert "Nouveautés du jour" in full and "Stage nouveauté unique" in full and "Stage ancienne annonce" in full
        assert "Stage expirée annonce" not in full and "1 offre(s) expirée(s) masquée(s)" in full
        new = c.get(f"/profiles/{profile.id}/shortlist?new=1").text
        assert "Stage nouveauté unique" in new and "Stage ancienne annonce" not in new
        assert 'name="new" value="1"' in new
        assert "1</span></a>" in new  # badge count


def test_cli_new_command(profile):
    from jobhunt.cli import app

    add_job(profile, "n1", first_seen=datetime.now(UTC) - timedelta(minutes=5), title="Stage nouveauté unique", score=85)
    add_job(profile, "o1", first_seen=datetime.now(UTC) - timedelta(days=3), title="Stage ancienne annonce")
    out = CliRunner().invoke(app, ["new", str(profile.id)], env={"COLUMNS": "250"})
    assert out.exit_code == 0, out.output
    assert "nouveauté" in out.output and "ancienne" not in out.output
    assert "1 nouvelle(s) offre(s), dont 1 ≥ 70" in out.output
    assert CliRunner().invoke(app, ["new", "999"]).exit_code != 0
