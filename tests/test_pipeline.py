
import pytest

from jobhunt import db
from jobhunt.llm import LLMClient
from jobhunt.models import ContractType
from jobhunt.pipeline import run_search
from jobhunt.scoring import JobScore

from pipeline_fakes import BoardA, patch_registry


class FakeLLM(LLMClient):
    """Pretends to be enabled; scores by title."""

    def __init__(self):
        super().__init__()
        self.score_calls = []

    @property
    def enabled(self):
        return True

    async def plan(self, profile, max_queries=25):
        from jobhunt.llm import PlannedQuery, build_queries
        return build_queries(profile, [PlannedQuery(keywords="systèmes embarqués"), PlannedQuery(keywords="robotique")], max_queries)

    async def score(self, profile, jobs):
        self.score_calls.append([j.title for j in jobs])
        return [JobScore(fit=90 if "embarqu" in j.title.lower() else 40, matches_function=True, rationale="ok", origin="llm") for j in jobs]


async def test_full_run_with_failures_and_skips(monkeypatch, profile):
    patch_registry(monkeypatch)
    llm = FakeLLM()
    events = []
    res = await run_search(profile.id, llm=llm, progress=events.append)
    assert res.stage == "done"
    s = res.sources
    assert s["boarda"].status == "done" and s["boardb"].status == "done"
    assert s["broken"].status == "error" and len(s["broken"].errors) == 2
    assert s["needslogin"].needs_login and s["needslogin"].queries_done == 0
    assert s["cdionly"].status == "skipped" and "contrat" in s["cdionly"].skipped_reason
    assert s["needskey"].status == "skipped" and "identifiants" in s["needskey"].skipped_reason
    assert len(BoardA.calls) == len(res.plan) == 2
    assert any(e.get("event") == "query_done" for e in events)

    jobs = db.query_jobs(profile.id, show_filtered=True)
    titles = {j.title for j in jobs}
    # Airbus stage deduped across both boards; senior CDI prefiltered out (contract mismatch) w/o LLM
    assert len(jobs) == 3 and res.prefiltered == 1 and res.unique_jobs == 3
    emb = next(j for j in jobs if "mbarqu" in j.title)
    assert emb.sources == ["boarda", "boardb"] and emb.score == 90
    assert all("Senior" not in t for batch in llm.score_calls for t in batch)
    run = db.latest_run(profile.id)
    assert run["status"] == "done" and run["stats"]["sources"]["broken"]["status"] == "error"
    assert len(run["plan"]) == 2


async def test_rerun_does_not_duplicate_or_rescore(monkeypatch, profile):
    patch_registry(monkeypatch, ["boarda", "boardb"])
    llm = FakeLLM()
    await run_search(profile.id, llm=llm)
    j = db.load_jobs(profile.id)
    db.set_status([x.id for x in j if "mbarqu" in x.title], "approved")
    n_calls = len(llm.score_calls)
    res = await run_search(profile.id, llm=llm)
    j2 = db.load_jobs(profile.id)
    assert len(j2) == len(j) and res.new_jobs == 0 and len(llm.score_calls) == n_calls
    assert [x.status for x in j2 if "mbarqu" in x.title] == ["approved"]
    assert all(len(x.sightings) <= 2 for x in j2)


async def test_source_filter_and_max_queries(monkeypatch, profile):
    patch_registry(monkeypatch)
    res = await run_search(profile.id, sources=["boardb"], max_queries=1, llm=FakeLLM())
    assert list(res.sources) == ["boardb"] and len(res.plan) == 1 and not BoardA.calls


async def test_runs_without_api_key_end_to_end(monkeypatch, profile):
    patch_registry(monkeypatch, ["boarda", "boardb"])
    res = await run_search(profile.id)  # real LLMClient, no key -> rules
    assert res.stage == "done" and not res.llm_enabled and res.plan
    jobs = db.query_jobs(profile.id)
    assert jobs and all(j.score_json["origin"] == "rules" for j in jobs)
    assert jobs[0].score >= jobs[-1].score


async def test_origin_module_is_called_when_present(monkeypatch, profile):
    from jobhunt import origin

    patch_registry(monkeypatch, ["boarda"])
    seen = []

    async def resolve_origins(postings, ctx):
        seen.extend(p.source_id for p in postings)
        return [p.model_copy(update={"origin_url": "https://careers.airbus.test/jobs/1", "origin_ats": "careers_site",
                                     "origin_confidence": 0.9}) for p in postings]

    monkeypatch.setattr(origin, "resolve_origins", resolve_origins)
    await run_search(profile.id, llm=FakeLLM())
    jobs = db.load_jobs(profile.id)
    assert seen == ["a1"]  # the senior CDI is prefiltered: no ladder run spent on it
    resolved = [j for j in jobs if j.origin_url]
    assert [j.origin_url for j in resolved] == ["https://careers.airbus.test/jobs/1"] and len(jobs) == 2


async def test_origin_failure_does_not_kill_run(monkeypatch, profile):
    from jobhunt import origin

    patch_registry(monkeypatch, ["boarda"])

    async def resolve_origins(postings, ctx):
        raise RuntimeError("nope")

    monkeypatch.setattr(origin, "resolve_origins", resolve_origins)
    res = await run_search(profile.id, llm=FakeLLM())
    assert res.stage == "done" and any("origine" in e.lower() for e in res.errors)


async def test_scores_recomputed_after_profile_and_posting_changes(monkeypatch, profile):
    from jobhunt.dedupe import MergedJob
    from jobhunt.pipeline import RunResult, _score_jobs
    from pipeline_fakes import posting

    job = MergedJob.from_posting(posting("boarda", "1", ct=ContractType.CDI))
    job.status = "approved"
    llm = FakeLLM()
    result = RunResult(run_id=1, profile_id=profile.id)
    await _score_jobs(profile, [job], llm, result, None)
    assert job.score_json["prefiltered"] and not llm.score_calls
    changed = profile.model_copy(update={"contract_types": [ContractType.CDI], "experience_years": 5})
    await _score_jobs(changed, [job], llm, result, None)
    assert job.score == 90 and not job.score_json["prefiltered"] and len(llm.score_calls) == 1
    await _score_jobs(changed, [job], llm, result, None)
    assert len(llm.score_calls) == 1
    for attribute, value in [("description", "richer description"), ("origin_url", "https://acme.fr/jobs/1"),
                             ("title", "firmware role"), ("company", "Other"), ("location", "Paris")]:
        setattr(job, attribute, value)
        await _score_jobs(changed, [job], llm, result, None)
    assert len(llm.score_calls) == 6 and job.status == "approved"


async def test_detail_enrichment_bounded_paced_deduplicated_and_nonfatal(monkeypatch, profile):
    import asyncio
    from collections import defaultdict
    from jobhunt.pipeline import RunResult, _enrich_details
    from jobhunt.sources.base import Source, SourceContext
    from pipeline_fakes import posting

    active = defaultdict(int)
    maximum = defaultdict(int)
    calls, delays = [], []
    real_sleep = asyncio.sleep

    async def recorded_sleep(delay):
        delays.append(delay)
        await real_sleep(0)

    monkeypatch.setattr("jobhunt.pipeline.asyncio.sleep", recorded_sleep)
    monkeypatch.setattr("jobhunt.pipeline.DETAIL_LIMIT", 4)

    class Enriching(Source):
        name, label, kind = "boarda", "Board", "http"
        detail_delay_s = 0.01

        async def search(self, query, ctx):
            return []

        async def fetch_details(self, job, ctx):
            calls.append((job.source, job.source_id))
            active[job.source] += 1
            active["total"] += 1
            maximum[job.source] = max(maximum[job.source], active[job.source])
            maximum["total"] = max(maximum["total"], active["total"])
            await real_sleep(0)
            active[job.source] -= 1
            active["total"] -= 1
            if job.source_id == "fail":
                raise RuntimeError("offline failure")
            return job.model_copy(update={"description": "enriched", "apply_url": "https://acme.fr/jobs/" + job.source_id})

    postings = [posting("boarda", "1", description=None), posting("boarda", "1", description=None),
                posting("boardb", "2", description=None), posting("boarda", "fail", description=None),
                posting("boarda", "3", description=None), posting("boarda", "4", description=None),
                posting("boarda", "full", apply_url="https://acme.fr/jobs/full")]
    result = RunResult(run_id=1, profile_id=profile.id)
    enriched = await _enrich_details(postings, {"boarda": Enriching(), "boardb": Enriching()},
                                     SourceContext(profile_id=profile.id), result)
    assert len(calls) == 4 and calls.count(("boarda", "1")) == 1
    assert maximum["boarda"] == maximum["boardb"] == 1 and maximum["total"] >= 2
    assert delays == [0.01, 0.01]
    assert enriched[0].description == enriched[1].description == "enriched"
    assert enriched[4].description == "enriched" and enriched[5].description is None
    assert len(result.errors) == 1 and "fail" in result.errors[0]


async def test_run_enriches_before_origin_and_scoring(monkeypatch, profile):
    from jobhunt.sources.base import Source
    from pipeline_fakes import posting
    seen = []

    class Details(Source):
        name, label, kind = "details", "Details", "http"

        async def search(self, query, ctx):
            return [posting(self.name, "1", description=None)]

        async def fetch_details(self, job, ctx):
            return job.model_copy(update={"description": "Detailed STM32 role", "apply_url": "https://acme.fr/jobs/1"})

    async def origins(postings, ctx):
        seen.extend(postings)
        return postings

    monkeypatch.setattr("jobhunt.pipeline.registry", lambda: {"details": Details})
    monkeypatch.setattr("jobhunt.origin.resolve_origins", origins)
    result = await run_search(profile.id, max_queries=1, llm=FakeLLM())
    assert result.stage == "done" and seen[0].description == "Detailed STM32 role"
    assert db.load_jobs(profile.id)[0].apply_url == "https://acme.fr/jobs/1"


async def test_origin_retry_after_three_days_or_input_change(monkeypatch, profile):
    from datetime import UTC, datetime, timedelta
    from jobhunt.dedupe import MergedJob
    from jobhunt.pipeline import RunResult, _resolve_origins
    from jobhunt.sources.base import SourceContext
    from pipeline_fakes import posting

    current = datetime(2026, 11, 17, tzinfo=UTC)
    monkeypatch.setattr(db, "now", lambda: current)
    calls = []

    async def resolve(postings, ctx):
        calls.append(postings)
        return postings

    monkeypatch.setattr("jobhunt.origin.resolve_origins", resolve)
    job = MergedJob.from_posting(posting("boarda", "1"))
    job.extra["origin_tried"] = True
    result, context = RunResult(run_id=1, profile_id=profile.id), SourceContext(profile_id=profile.id)
    await _resolve_origins([job], context, result)
    await _resolve_origins([job], context, result)
    assert len(calls) == 1
    current += timedelta(days=3)
    await _resolve_origins([job], context, result)
    assert len(calls) == 2
    job.company = "New employer"
    await _resolve_origins([job], context, result)
    job.sightings[0].apply_url = "https://new.fr/jobs/1"
    await _resolve_origins([job], context, result)
    job.extra["company_website"] = "https://new.fr"
    await _resolve_origins([job], context, result)
    assert len(calls) == 5 and "origin_tried" not in job.extra


def test_origin_due_backfills_date_once_and_reset_repairs_posted_at():
    from datetime import UTC, datetime
    from jobhunt import pipeline
    from jobhunt.dedupe import MergedJob, Sighting
    j = MergedJob(title="T", origin_url="https://acme.test/j/1", extra={})
    assert pipeline._origin_due(j)
    j.extra["origin_date_checked"] = datetime.now(UTC).isoformat()
    assert not pipeline._origin_due(j)
    j.sightings.append(Sighting("s", "1", "u", None, datetime(2026, 11, 3, tzinfo=UTC), {}))
    j.posted_at = datetime(2026, 10, 1, tzinfo=UTC)
    j.recompute_posted_at()
    assert j.posted_at == datetime(2026, 11, 3, tzinfo=UTC)


async def test_sources_ok_records_only_fully_checked_sources(monkeypatch, profile):
    patch_registry(monkeypatch)
    res = await run_search(profile.id, llm=FakeLLM())
    # failed (broken), login wall (needslogin) and skipped sources are never "ok"
    assert res.sources_ok == ["boarda", "boardb"]
    assert db.latest_run(profile.id)["stats"]["sources_ok"] == ["boarda", "boardb"]


async def test_source_with_no_results_but_no_error_is_ok(monkeypatch, profile):
    reg = patch_registry(monkeypatch, ["boarda"])

    class Empty(reg["boarda"]):
        name = "empty"

        async def search(self, query, ctx):
            return []

    reg["empty"] = Empty
    res = await run_search(profile.id, llm=FakeLLM())
    assert res.sources_ok == ["boarda", "empty"]


async def test_search_resighting_bumps_last_seen_but_rescore_does_not(monkeypatch, profile):
    from datetime import UTC, datetime, timedelta

    from jobhunt.pipeline import rescore

    patch_registry(monkeypatch, ["boarda", "boardb"])
    llm = FakeLLM()
    await run_search(profile.id, llm=llm)
    old = (datetime.now(UTC) - timedelta(days=10)).isoformat()
    with db.connect() as c:
        c.execute("UPDATE jobs SET last_seen=?", (old,))
        c.execute("UPDATE sightings SET seen_at=?", (old,))
    # rescore (even after a profile change invalidating scores) must not look like a sighting
    p = profile.model_copy(update={"headline": "Autre titre", "skills": ["Rust"]})
    db.save_profile(p)
    await rescore(profile.id, llm=llm)
    with db.connect() as c:
        assert {r[0] for r in c.execute("SELECT last_seen FROM jobs")} == {old}
        assert {r[0] for r in c.execute("SELECT seen_at FROM sightings")} == {old}
    # a real search that sees the jobs again bumps both
    await run_search(profile.id, llm=llm)
    with db.connect() as c:
        assert all(r[0] > old for r in c.execute("SELECT last_seen FROM jobs"))
        assert all(r[0] > old for r in c.execute("SELECT seen_at FROM sightings"))


async def test_run_search_headless_is_explicit_and_leaves_settings_alone(monkeypatch, profile):
    from jobhunt.config import get_settings

    monkeypatch.delenv("JOBHUNT_FORCE_HEADLESS", raising=False)
    patch_registry(monkeypatch, ["boarda"])
    seen = []

    class Spy(BoardA):
        async def search(self, query, ctx):
            seen.append(ctx.browser.headless)
            return []

    monkeypatch.setattr("jobhunt.pipeline.registry", lambda: {"boarda": Spy})
    monkeypatch.setattr(get_settings(), "headless", False)
    await run_search(profile.id, llm=FakeLLM(), headless=True)
    await run_search(profile.id, llm=FakeLLM())
    assert seen[0] is True and seen[-1] is False and get_settings().headless is False


async def test_a_query_whose_cancellation_hangs_is_abandoned(monkeypatch, profile):
    """A source stuck in its cleanup (a browser tab that never closes) must not block the run: it is abandoned."""
    import asyncio
    from typing import ClassVar

    from jobhunt import pipeline
    from jobhunt.sources.base import Source

    release = asyncio.Event()

    class Stuck(Source):
        name: ClassVar[str] = "stuck"
        label: ClassVar[str] = "Stuck"
        kind: ClassVar[str] = "browser"

        async def search(self, query, ctx):
            try:
                await asyncio.sleep(3600)
            finally:  # the cleanup ignores cancellation, as a hung page.close() does
                while not release.is_set():
                    try:
                        await release.wait()
                    except asyncio.CancelledError:
                        continue

    reg = patch_registry(monkeypatch, ["boarda"])
    reg["stuck"] = Stuck
    monkeypatch.setattr(pipeline, "QUERY_TIMEOUT_S", 0.05)
    monkeypatch.setattr(pipeline, "CANCEL_GRACE_S", 0.05)
    try:
        res = await asyncio.wait_for(run_search(profile.id, llm=FakeLLM()), 20)
    finally:
        release.set()  # let the abandoned call end so the test's event loop can close
    assert res.stage == "done"
    assert res.sources["stuck"].status == "error" and any("délai" in e or "Abandon" in e for e in res.sources["stuck"].errors)
    assert res.sources["boarda"].status == "done"


# ---------------------------------------------------------------- incremental / resumable discovery
def _two_query_sources(monkeypatch, sources):
    monkeypatch.setattr("jobhunt.pipeline.registry", lambda: {s.name: s for s in sources})

    async def offline(postings, ctx):
        return postings

    monkeypatch.setattr("jobhunt.origin.resolve_origins", offline)


async def test_interruption_keeps_jobs_from_finished_queries(monkeypatch, profile):
    import asyncio
    from jobhunt.sources.base import Source
    from pipeline_fakes import posting

    class Slow(Source):
        name, label, kind = "slow", "Slow", "http"

        async def search(self, query, ctx):
            if query.keywords == "robotique":
                await asyncio.sleep(3600)  # the process is killed here
            return [posting("slow", "s1", title="Stage systèmes embarqués")]

    _two_query_sources(monkeypatch, [Slow])
    task = asyncio.ensure_future(run_search(profile.id, llm=FakeLLM()))
    for _ in range(100):
        await asyncio.sleep(0.05)
        if db.load_jobs(profile.id):
            break
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert [j.title for j in db.load_jobs(profile.id)] == ["Stage systèmes embarqués"]
    assert db.latest_run(profile.id)["status"] == "cancelled"


async def test_rerun_skips_scored_jobs_and_resolved_origins(monkeypatch, profile):
    from jobhunt import origin
    monkeypatch.setattr("jobhunt.pipeline.registry", lambda: {"boarda": BoardA})
    resolved = []

    async def resolve_origins(postings, ctx):
        resolved.extend(p.source_id for p in postings)
        return [p.model_copy(update={"origin_url": "https://careers.airbus.test/jobs/1", "origin_ats": "careers_site",
                                     "origin_confidence": 0.9}) for p in postings]

    monkeypatch.setattr(origin, "resolve_origins", resolve_origins)
    llm = FakeLLM()
    await run_search(profile.id, llm=llm)
    n_scores, n_resolved = len(llm.score_calls), len(resolved)
    assert n_scores and n_resolved
    await run_search(profile.id, llm=llm)
    assert len(llm.score_calls) == n_scores
    assert len(resolved) == n_resolved  # origin already resolved and date-checked: no second ladder run


async def test_scores_are_stored_batch_by_batch(monkeypatch, profile):
    import asyncio
    from jobhunt import pipeline
    from pipeline_fakes import posting
    from jobhunt.sources.base import Source

    class Many(Source):
        name, label, kind = "many", "Many", "http"

        async def search(self, query, ctx):
            return [posting("many", f"m{i}", title=f"Stage systèmes embarqués {i}", company=f"Firme {i}") for i in range(6)]

    _two_query_sources(monkeypatch, [Many])
    monkeypatch.setattr(pipeline, "SCORE_BATCH", 2)
    monkeypatch.setattr(pipeline, "SCORE_CONCURRENCY", 1)

    class DiesOnThirdBatch(FakeLLM):
        async def score(self, profile, jobs):
            if len(self.score_calls) == 2:
                raise asyncio.CancelledError  # not an Exception: the run dies mid-scoring
            return await super().score(profile, jobs)

    with pytest.raises(asyncio.CancelledError):
        await run_search(profile.id, llm=DiesOnThirdBatch())
    jobs = db.load_jobs(profile.id)
    assert len(jobs) == 6 and sum(j.score is not None for j in jobs) == 4


async def test_source_abandoned_after_repeated_blocks(monkeypatch, profile):
    from jobhunt.sources.base import Source, SourceError
    calls = []

    class Limited(Source):
        name, label, kind = "limited", "Limited", "http"

        async def search(self, query, ctx):
            calls.append(query.keywords)
            raise SourceError("HTTP 429: Just a moment...")

    _two_query_sources(monkeypatch, [Limited])
    monkeypatch.setattr("jobhunt.pipeline.MAX_CONSECUTIVE_ERRORS", 99)
    res = await run_search(profile.id, max_queries=25, llm=FakeLLM())
    stat = res.sources["limited"]
    assert len(calls) == 2 and stat.status == "error" and "bloquant" in stat.errors[-1]


async def test_source_stops_at_time_budget_and_keeps_partial(monkeypatch, profile):
    from jobhunt import pipeline
    from jobhunt.sources.base import Source
    from pipeline_fakes import posting

    class Quick(Source):
        name, label, kind = "quick", "Quick", "http"

        async def search(self, query, ctx):
            return [posting("quick", "q1")]

    _two_query_sources(monkeypatch, [Quick])
    monkeypatch.setattr(pipeline, "SEARCH_BUDGET_S", -1.0)
    res = await run_search(profile.id, llm=FakeLLM())
    assert res.stage == "done" and res.sources["quick"].queries_done == 0
    assert "quick" not in res.sources_ok and "Budget" in res.sources["quick"].errors[0]


async def test_stale_running_rows_are_reaped_not_blocking(monkeypatch, profile):
    import os
    from jobhunt import pipeline
    dead = db.create_run(profile.id)
    db.update_run(dead, stats={"pid": 2 ** 22 + 12345})
    mine = db.create_run(profile.id)
    db.update_run(mine, stats={"pid": os.getpid()})
    old_unknown = db.create_run(profile.id)
    with db.connect() as c:
        c.execute("UPDATE runs SET started_at='2020-01-01T00:00:00+00:00' WHERE id=?", (old_unknown,))
    patch_registry(monkeypatch, ["boarda"])
    res = await run_search(profile.id, llm=FakeLLM())
    assert res.stage == "done"
    assert db.get_run(dead)["status"] == "error" and db.get_run(old_unknown)["status"] == "error"
    assert db.get_run(mine)["status"] == "running" and db.get_run(dead)["finished_at"]
    assert pipeline.reap_stale_runs() == 0
