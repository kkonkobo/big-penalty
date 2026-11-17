import sys
import types

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
    patch_registry(monkeypatch, ["boarda"])
    mod = types.ModuleType("jobhunt.origin")
    seen = []

    async def resolve_origins(postings, ctx):
        seen.extend(p.source_id for p in postings)
        return [p.model_copy(update={"origin_url": "https://careers.airbus.test/jobs/1", "origin_ats": "careers_site",
                                     "origin_confidence": 0.9}) for p in postings]

    mod.resolve_origins = resolve_origins
    monkeypatch.setitem(sys.modules, "jobhunt.origin", mod)
    await run_search(profile.id, llm=FakeLLM())
    jobs = db.load_jobs(profile.id)
    assert seen and all(j.origin_url == "https://careers.airbus.test/jobs/1" for j in jobs)
    assert len(jobs) == 1  # both postings now share an origin -> consolidated


async def test_origin_failure_does_not_kill_run(monkeypatch, profile):
    patch_registry(monkeypatch, ["boarda"])
    mod = types.ModuleType("jobhunt.origin")

    async def resolve_origins(postings, ctx):
        raise RuntimeError("nope")

    mod.resolve_origins = resolve_origins
    monkeypatch.setitem(sys.modules, "jobhunt.origin", mod)
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
