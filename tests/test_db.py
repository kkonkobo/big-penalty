from datetime import UTC, datetime, timedelta
import json
import pytest

from jobhunt import db
from jobhunt.dedupe import MergedJob, merge_postings
from jobhunt.models import ContractType, JobPosting, Profile


def test_oversized_raw_stays_valid_and_corrupt_rows_load(profile):
    job = MergedJob.from_posting(post(raw={"huge": "é\\\"" * 30000}))
    db.save_jobs(profile.id, [job])
    with db.connect() as connection:
        encoded = connection.execute("SELECT raw FROM sightings WHERE job_id=?", (job.id,)).fetchone()[0]
        assert isinstance(json.loads(encoded), dict)
        connection.execute("UPDATE sightings SET raw=? WHERE job_id=?", ('{"broken":', job.id))
        connection.execute("UPDATE jobs SET extra='[1]', score_json='bad' WHERE id=?", (job.id,))
    [loaded] = db.load_jobs(profile.id)
    assert loaded.sightings[0].raw == {} and loaded.extra == {} and loaded.score_json == {}
    loaded.sightings[0].raw = {"small": "valid"}
    loaded.changed = True
    db.save_jobs(profile.id, [loaded])
    assert db.get_job(job.id).sightings[0].raw == {"small": "valid"}


@pytest.mark.parametrize("older_status,newer_status,expected", [
    ("new", "approved", "approved"), ("approved", "applied", "applied"),
    ("shortlisted", "rejected", "rejected"), ("rejected", "approved", "approved"),
    ("applied", "new", "applied"),
])
def test_consolidation_preserves_live_decisions(profile, older_status, newer_status, expected):
    from jobhunt.dedupe import consolidate

    jobs = [MergedJob.from_posting(post("1", title="First role")),
            MergedJob.from_posting(post("2", title="Second role"))]
    db.save_jobs(profile.id, jobs)
    db.set_status([jobs[0].id], older_status)
    loaded = db.load_jobs(profile.id)
    for job in loaded:
        job.origin_url = "https://acme.fr/jobs/shared"
    kept, removed = consolidate(loaded)
    db.set_status([jobs[1].id], newer_status)
    db.save_jobs(profile.id, kept, removed=removed)
    [survivor] = db.load_jobs(profile.id)
    assert survivor.id == jobs[0].id and survivor.status == expected
    assert len(survivor.sightings) == 2


def test_consolidation_rolls_back_if_duplicate_delete_fails(profile):
    import sqlite3
    from jobhunt.dedupe import consolidate

    jobs = [MergedJob.from_posting(post("1")), MergedJob.from_posting(post("2"))]
    jobs[1].status = "applied"
    db.save_jobs(profile.id, jobs)
    for job in jobs:
        job.origin_url = "https://acme.fr/jobs/shared"
    kept, removed = consolidate(jobs)
    with db.connect() as connection:
        connection.execute("CREATE TRIGGER fail_delete BEFORE DELETE ON jobs BEGIN SELECT RAISE(ABORT, 'blocked'); END")
    with pytest.raises(sqlite3.IntegrityError):
        db.save_jobs(profile.id, kept, removed=removed)
    assert [job.status for job in db.load_jobs(profile.id)] == ["new", "applied"]


def post(sid="1", source="indeed", **kw):
    d = dict(source=source, source_id=sid, url=f"https://{source}.com/{sid}", title="Dev firmware", company="Acme",
             location="Toulouse", posted_at=datetime.now(UTC) - timedelta(days=2))
    d.update(kw)
    return JobPosting(**d)


def test_profile_roundtrip(profile):
    got = db.get_profile(profile.id)
    assert got == profile and got.contract_types == [ContractType.STAGE, ContractType.ALTERNANCE]
    p2 = db.save_profile(got.model_copy(update={"name": "Léa B"}))
    assert p2.id == profile.id and db.get_profile(profile.id).name == "Léa B"
    assert len(db.list_profiles()) == 1


def test_migration_user_version():
    with db.connect() as c:
        assert c.execute("PRAGMA user_version").fetchone()[0] == len(db.MIGRATIONS)
        assert c.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_rerun_updates_instead_of_duplicating(profile):
    jobs, _ = merge_postings([post("1"), post("2", "wttj")], db.load_jobs(profile.id))
    db.save_jobs(profile.id, jobs)
    stored = db.load_jobs(profile.id)
    assert len(stored) == 1 and len(stored[0].sightings) == 2
    db.set_status([stored[0].id], "approved")
    # second run: same job from a third board + richer description
    jobs, stats = merge_postings([post("3", "linkedin", description="plus riche")], db.load_jobs(profile.id))
    db.save_jobs(profile.id, jobs)
    stored = db.load_jobs(profile.id)
    assert len(stored) == 1 and stats.new_jobs == 0
    assert len(stored[0].sightings) == 3 and stored[0].status == "approved" and stored[0].description == "plus riche"
    with db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM sightings").fetchone()[0] == 3


def test_unchanged_jobs_not_rewritten_and_jobs_scoped_to_profile(profile):
    other = db.save_profile(Profile(name="B", headline="x"))
    jobs, _ = merge_postings([post()], [])
    db.save_jobs(profile.id, jobs)
    assert db.load_jobs(other.id) == []
    assert db.load_jobs(profile.id)[0].changed is False


def test_query_filters_and_sort(profile):
    ps = [post("1", title="A", company="A", score_hint=None) if False else post("1", title="Alpha role", company="A"),
          post("2", title="Beta role", company="B", contract_type=ContractType.STAGE,
               posted_at=datetime.now(UTC) - timedelta(days=20)),
          post("3", "wttj", title="Gamma role", company="C")]
    jobs, _ = merge_postings(ps)
    for j, s in zip(jobs, (50, 90, 10)):
        j.score = s
    jobs[2].score_json = {"prefiltered": True}
    db.save_jobs(profile.id, jobs)
    q = db.query_jobs(profile.id)
    assert [j.title for j in q] == ["Beta role", "Alpha role"]  # prefiltered hidden
    assert len(db.query_jobs(profile.id, show_filtered=True)) == 3
    assert [j.title for j in db.query_jobs(profile.id, min_score=60)] == ["Beta role"]
    assert [j.title for j in db.query_jobs(profile.id, contract_type="stage")] == ["Beta role"]
    assert [j.title for j in db.query_jobs(profile.id, sort="freshness")] == ["Alpha role", "Beta role"]
    assert [j.title for j in db.query_jobs(profile.id, max_age_days=7)] == ["Alpha role"]
    assert db.query_jobs(profile.id, source="wttj") == []
    assert len(db.query_jobs(profile.id, source="wttj", show_filtered=True)) == 1


def test_status_validation_and_runs(profile):
    import pytest
    with pytest.raises(ValueError):
        db.set_status([1], "bogus")
    rid = db.create_run(profile.id)
    db.update_run(rid, plan=[{"keywords": "x"}], stats={"sources": {}}, status="done", finished=True)
    r = db.latest_run(profile.id)
    assert r["id"] == rid and r["plan"] == [{"keywords": "x"}] and r["finished_at"]
