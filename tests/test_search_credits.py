"""Paid web search of the origin resolver: Serper first, Firecrawl fallback, on-disk answer cache, per-posting share,
per-run budget, and the daily run deferring search until the score is known. All data is fictional; HTTP goes through
httpx mock transports."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import httpx

from jobhunt import origin, pipeline
from jobhunt.dedupe import MergedJob
from jobhunt.models import JobPosting
from jobhunt.sources.base import SourceContext

CAREERS = "https://careers.orbitex-aerospace.example/job/1"


def posting(**updates):
    base = dict(source="linkedin", source_id="9", url="https://www.linkedin.com/jobs/view/9", title="Stage ingénieur systèmes embarqués",
                company="Orbitex Aerospace", location="Toulouse")
    return JobPosting(**{**base, **updates})


class Serper:
    """Mock transport: counts Serper calls, answers `organic`; any other host (DuckDuckGo) gets a 403."""

    def __init__(self, organic=None, status=200):
        self.calls = []
        self.organic = [{"link": CAREERS, "title": "Stage ingénieur | Orbitex", "snippet": "Orbitex recrute"}] if organic is None else organic
        self.status = status

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "google.serper.dev":
            self.calls.append(json.loads(request.content))
            return httpx.Response(self.status, json={"organic": self.organic})
        return httpx.Response(403)


def resolver(handler, *, serper=True, firecrawl=False, budget=None, **kw):
    ctx = SourceContext(profile_id=1)
    ctx.settings.serper_api_key = "serper-test" if serper else None
    ctx.settings.firecrawl_api_key = "fc-test" if firecrawl else None
    r = origin.Resolver(ctx, budget=budget, **kw)
    r.client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True)
    r.pacer.wait = AsyncMock()
    r.ddg_blocked = True  # free fallback out of the way: only paid providers answer
    return r


async def test_serper_answer_is_cached_on_disk_and_reused_by_a_cacheless_resolver():
    serper = Serper()
    r = resolver(serper)
    try:
        hits = await r._search_hits('"Orbitex" "Stage ingénieur"')
        assert [(h.url, h.provider) for h in hits] == [(CAREERS, "serper")]
        assert serper.calls[0]["gl"] == "fr" and "-site:linkedin.com" in serper.calls[0]["q"]
        assert r.budget.summary()["serper"] == 1 and r.budget.credits == 1
    finally:
        await r.aclose()
    again = resolver(serper, cache=False)  # `use_cache=False` re-runs: the search answer still comes from disk
    try:
        hits = await again._search_hits('"Orbitex" "Stage ingénieur"')
        assert [h.url for h in hits] == [CAREERS] and len(serper.calls) == 1
        assert again.budget.cached == 1 and again.budget.paid == 0
    finally:
        await again.aclose()


async def test_usage_log_records_paid_calls_without_the_query(tmp_path):
    serper = Serper()
    budget = origin.SearchBudget(10, tmp_path / "search-usage.jsonl")
    r = resolver(serper, budget=budget)
    try:
        await r._search_hits("Orbitex secret query")
    finally:
        await r.aclose()
    line = json.loads((tmp_path / "search-usage.jsonl").read_text())
    assert line["provider"] == "serper" and line["credits"] == 1 and "Orbitex" not in json.dumps(line)


async def test_serper_error_falls_back_to_firecrawl_and_empty_answer_pays_once(monkeypatch):
    import firecrawl

    fc_calls = []

    class FakeFirecrawl:
        def __init__(self, api_key=None):
            pass

        def search(self, *a, **k):
            fc_calls.append(a)
            return type("R", (), {"web": [{"url": CAREERS, "title": "Stage", "description": ""}]})()

    monkeypatch.setattr(firecrawl, "Firecrawl", FakeFirecrawl)
    r = resolver(Serper(status=500), firecrawl=True)
    try:
        hits = await r._search_hits("q1")
        assert [h.provider for h in hits] == ["firecrawl"] and len(fc_calls) == 1
        assert r.budget.summary()["firecrawl"] == 1 and r.budget.summary()["serper"] == 0  # a failed call is not counted
    finally:
        await r.aclose()
    empty = Serper(organic=[])
    r = resolver(empty, firecrawl=True)
    try:
        assert await r._search_hits("q2") == []
        assert len(empty.calls) == 1 and len(fc_calls) == 1  # Serper said "nothing": Firecrawl is not paid as well
        assert await r._search_hits("q2") == [] and len(empty.calls) == 1  # empty answer cached too
    finally:
        await r.aclose()


async def test_per_posting_share_and_run_budget_stop_paid_queries():
    serper = Serper()
    r = resolver(serper)
    token = origin._PAID_LEFT.set([2])
    try:
        for q in ("a", "b", "c"):
            await r._search_hits(q)
        assert len(serper.calls) == 2 and r.budget.over_job_cap == 1
    finally:
        origin._PAID_LEFT.reset(token)
        await r.aclose()

    r = resolver(serper, budget=origin.SearchBudget(1))
    trace = origin.Trace()
    trace_token = origin._TRACE.set(trace)
    try:
        await r._search_hits("d")
        assert await r._search_hits("e") == []
        assert r.budget.paid == 1 and r.budget.over_budget == 1
        assert trace.notes and trace.notes[-1][0] == "search_failed" and "budget" in trace.notes[-1][1]
    finally:
        origin._TRACE.reset(trace_token)
        await r.aclose()


async def test_ladder_spends_at_most_the_posting_share(tmp_path):
    serper = Serper(organic=[])
    r = resolver(serper)
    r.cache = origin.DiskCache(tmp_path)
    for step in ("probe_company", "discover_careers"):
        setattr(r, step, AsyncMock(return_value=None))
    try:
        await origin._resolve_traced(r, posting(company_website="https://www.orbitex-aerospace.example"), use_cache=False)
        assert len(serper.calls) == r.ctx.settings.search_paid_per_job == 2
        assert r.budget.over_job_cap >= 1
    finally:
        await r.aclose()


async def test_search_disabled_defers_step_5_and_does_not_cache_the_miss(tmp_path):
    r = resolver(Serper(), search=False)
    r.cache = origin.DiskCache(tmp_path)
    for step in ("probe_company", "discover_careers", "web_search"):
        setattr(r, step, AsyncMock(return_value=None))
    try:
        res = await origin._resolve_traced(r, posting())
        assert not res.found and any("reportée" in line for line in res.trace)
        r.web_search.assert_not_called()
        assert not list(tmp_path.glob("*.json"))  # the post-score pass must not find a cached miss
        r.search_enabled = True
        await origin._resolve_traced(r, posting())
        r.web_search.assert_called_once()
        assert list(tmp_path.glob("*.json"))
    finally:
        await r.aclose()


def job(score, *, origin_url=None, title="Stage ingénieur", status="new"):
    j = MergedJob.from_posting(posting(title=title, source_id=title))
    j.score, j.origin_url, j.status = score, origin_url, status
    return j


async def test_search_pass_only_takes_jobs_worth_applying_to(monkeypatch):
    seen = {}

    async def resolve(postings, ctx, *, search=True, budget=None):
        seen.setdefault("titles", []).extend(p.title for p in postings)
        seen["search"], seen["budget"] = search, budget
        return [p.model_copy(update={"origin_url": CAREERS, "origin_ats": "careers_site", "origin_confidence": 0.8})
                if p.title == "Bon" else p for p in postings]

    monkeypatch.setattr(origin, "resolve_origins", resolve)
    jobs = [job(80, title="Bon"), job(65, title="Moyen"), job(40, title="Faible"), job(90, title="Résolu", origin_url=CAREERS),
            job(95, title="Postulé", status="applied")]
    result = pipeline.RunResult(run_id=1, profile_id=1)
    budget = origin.SearchBudget(10)
    await pipeline._search_origins(jobs, SourceContext(profile_id=1), result, budget)
    assert seen["titles"] == ["Bon", "Moyen"] and seen["search"] is True and seen["budget"] is budget
    assert (result.origin_searched, result.origin_search_found) == (2, 1)
    assert jobs[0].origin_url == CAREERS and jobs[1].extra["origin_searched_at"]

    seen.clear()  # searched with the same inputs: not again before SEARCH_RETRY_AFTER
    await pipeline._search_origins(jobs, SourceContext(profile_id=1), result, budget)
    assert "titles" not in seen


async def test_search_pass_stops_when_the_run_budget_is_spent(monkeypatch):
    async def resolve(postings, ctx, *, search=True, budget=None):
        raise AssertionError("budget already spent")

    monkeypatch.setattr(origin, "resolve_origins", resolve)
    budget = origin.SearchBudget(0)
    result = pipeline.RunResult(run_id=1, profile_id=1)
    await pipeline._search_origins([job(80)], SourceContext(profile_id=1), result, budget)
    assert result.origin_searched == 0 and any("budget" in e for e in result.errors)


async def test_old_style_resolver_never_gets_a_search_pass(monkeypatch):
    calls = []

    async def resolve(postings, ctx):
        calls.append(postings)
        return postings

    monkeypatch.setattr(origin, "resolve_origins", resolve)
    await pipeline._search_origins([job(80)], SourceContext(profile_id=1), pipeline.RunResult(run_id=1, profile_id=1), origin.SearchBudget(5))
    assert calls == []
