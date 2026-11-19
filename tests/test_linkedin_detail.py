"""LinkedIn detail provider (external command), its use by the `origins` re-resolution and the search pipeline, and the
"trust LinkedIn's own apply link" rule of the origin ladder. No process, no network: the runner is injected."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from jobhunt import db, origin, pipeline
from jobhunt.dedupe import MergedJob
from jobhunt.models import JobPosting
from jobhunt.sources import linkedin_detail as ld
from jobhunt.sources.base import SourceContext


def payload(**el):
    base = {"id": "4111111111", "title": "Stage ingénieur", "jobState": "LISTED", "postedDate": "2026-11-10T08:00:00.000Z", "closedAt": None,
            "easyApplyUrl": None, "applyMethod": {"companyApplyUrl": "https://orbitex.talentview.io/jobs/abc?source=linkedin&utm_medium=x&id=7",
                                                  "type": "OffsiteApply"},
            "company": {"name": "Orbitex", "website": "http://www.orbitex.example"}, "jobApplicationLimitReached": False}
    base.update(el)
    return json.dumps({"element": base, "status": 200, "cost": 0.0005})


def runner_returning(text, seen=None):
    async def run(argv, timeout):
        if seen is not None:
            seen.append(argv)
        return text
    return run


@pytest.fixture(autouse=True)
def _usage():
    ld.reset_usage()


async def test_offsite_job():
    d = await ld.fetch_job_detail("4111111111", runner=runner_returning("log line\n" + payload() + "\ntrailing"))
    assert d.apply_url == "https://orbitex.talentview.io/jobs/abc?id=7" and not d.easy_apply
    assert d.company_website == "http://www.orbitex.example" and d.state == "listed" and d.posted_at.year == 2026
    assert (ld.USAGE.calls, round(ld.USAGE.cost, 4)) == (1, 0.0005)


async def test_easy_apply_job():
    d = await ld.fetch_job_detail("4111111111", runner=runner_returning(payload(applyMethod={"type": "EasyApply"}, easyApplyUrl="https://www.linkedin.com/jobs/view/4111111111/apply")))
    assert d.easy_apply and d.apply_url is None


@pytest.mark.parametrize("change", [{"closedAt": "2026-11-15T00:00:00Z"}, {"jobState": "CLOSED"}, {"jobApplicationLimitReached": True}])
async def test_closed_job(change):
    assert (await ld.fetch_job_detail("4111111111", runner=runner_returning(payload(**change)))).state == "closed"


async def test_linkedin_target_is_not_an_employer_link():
    d = await ld.fetch_job_detail("4111111111", runner=runner_returning(payload(applyMethod={"companyApplyUrl": "https://www.linkedin.com/jobs/view/1"})))
    assert d.apply_url is None


@pytest.mark.parametrize("out", ['{"element": null, "status": 200, "cost": 0.0005}', '{"status": 404, "cost": 0.0005}',
                                 "totally not json", "", None, "{broken"])
async def test_missing_garbage_or_empty(out):
    assert await ld.fetch_job_detail("4111111111", runner=runner_returning(out)) is None


async def test_cost_is_counted_even_for_an_empty_answer():
    await ld.fetch_job_detail("4111111111", runner=runner_returning('{"element": null, "status": 200, "cost": 0.0005}'))
    assert ld.USAGE.cost == 0.0005


async def test_timeout_and_crash_never_raise():
    async def slow(argv, timeout):
        raise asyncio.TimeoutError

    async def boom(argv, timeout):
        raise OSError("no such file")

    assert await ld.fetch_job_detail("4111111111", runner=slow) is None
    assert await ld.fetch_job_detail("4111111111", runner=boom) is None
    assert ld.USAGE.failures == 2


async def test_real_subprocess_timeout_is_enforced(monkeypatch):
    monkeypatch.setattr(ld, "TIMEOUT_S", 0.3)
    assert await ld.fetch_job_detail("4111111111", setting="sleep 5 {id}") is None  # real process, killed after 0.3 s


@pytest.mark.parametrize("bad", ["", "abc", "12345", "1234567890123456", "4111111111; rm -rf /", "$(id)", "41 11", "-1234567"])
async def test_bad_id_never_reaches_the_command(bad):
    seen = []
    assert await ld.fetch_job_detail(bad, runner=runner_returning(payload(), seen)) is None and seen == []


def test_command_setting(monkeypatch):
    monkeypatch.setattr(ld.shutil, "which", lambda name: "/opt/bin/treg" if name == "treg" else None)
    assert ld.command_template("auto") == ["/opt/bin/treg", "call", "harvestapi.linkedin.job.get", "--query", "jobId={id}"]
    monkeypatch.setattr(ld.shutil, "which", lambda name: None)
    assert ld.command_template("auto") is None and ld.command_template("off") is None and ld.command_template("") is None
    assert ld.command_template("fetcher --job {id} --json") == ["fetcher", "--job", "{id}", "--json"]
    assert ld.command_template("fetcher --json") is None  # no {id}: refused


async def test_template_is_split_without_a_shell(monkeypatch):
    seen = []
    await ld.fetch_job_detail("4111111111", runner=runner_returning(payload(), seen), setting="fetch --q 'a b' id={id} ; echo hi")
    assert seen == [["fetch", "--q", "a b", "id=4111111111", ";", "echo", "hi"]]  # ';' is just an argument


async def test_disabled_means_no_call():
    assert await ld.fetch_job_detail("4111111111", setting="off") is None and ld.USAGE.calls == 0


async def test_at_most_three_concurrent_calls():
    running = peak = 0

    async def run(argv, timeout):
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.02)
        running -= 1
        return payload()

    await asyncio.gather(*(ld.fetch_job_detail(f"41111111{i:02d}", runner=run) for i in range(10)))
    assert peak == 3


# ---------------------------------------------------------------- origin ladder: LinkedIn's own apply link

def li_posting(url, **kw):
    return JobPosting(source="linkedin", source_id="4111111111", url="https://www.linkedin.com/jobs/view/4111111111", title="Stage ingénieur",
                      company="Orbitex", location="Toulouse", apply_url=url, raw={"apply_source": "linkedin_offsite"}, **kw)


def resolver(handler=lambda request: httpx.Response(403)):
    r = origin.Resolver(SourceContext(profile_id=1))
    r.client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True, max_redirects=8)
    r.pacer.wait = None
    from unittest.mock import AsyncMock
    r.pacer.wait = AsyncMock()
    return r


async def test_offsite_link_on_ats_host_without_company_name_is_trusted():
    r = resolver()
    try:
        assert await r.direct(li_posting("https://tenant42.talentview.io/jobs/abc?id=7")) == ("https://tenant42.talentview.io/jobs/abc?id=7", "linkedin_offsite")
        assert await r.direct(li_posting("https://jobs.randomats.example/offre/77")) == ("https://jobs.randomats.example/offre/77", "linkedin_offsite")
    finally:
        await r.aclose()


async def test_same_url_without_provenance_is_not_trusted():
    r = resolver()
    try:
        p = li_posting("https://jobs.randomats.example/offre/77").model_copy(update={"raw": {}})
        assert await r.direct(p) is None
    finally:
        await r.aclose()


@pytest.mark.parametrize("url", ["https://www.indeed.fr/viewjob?jk=1", "https://www.linkedin.com/jobs/view/1", "https://click.appcast.io/track/x",
                                 "https://jobs.randomats.example/", "https://www.orbitex.example"])
async def test_offsite_link_refused_when_aggregator_tracker_linkedin_or_homepage(url):
    r = resolver(lambda request: httpx.Response(200, text="<html>nothing</html>"))
    try:
        assert await r.direct(li_posting(url)) is None
    finally:
        await r.aclose()


async def test_offsite_tracker_is_followed_and_final_url_trusted():
    def handler(request):
        if request.url.host == "click.appcast.io":
            return httpx.Response(302, headers={"location": "https://jobs.randomats.example/offre/77?utm_source=x"})
        return httpx.Response(200)

    r = resolver(handler)
    try:
        assert (await r.direct(li_posting("https://click.appcast.io/track/x")))[1] == "linkedin_offsite"
    finally:
        await r.aclose()


async def test_resolve_gives_confidence_09_and_ats_or_site(tmp_path):
    def handler(request):
        html = '<main><h1>Stage ingénieur systèmes</h1><a href="/postuler/77">Postuler</a></main>'
        return httpx.Response(200, text=html if request.url.host == "jobs.randomats.example" else "")

    r = resolver(handler)
    r.cache = origin.DiskCache(tmp_path)
    from unittest.mock import AsyncMock
    r.enrich_from_page = AsyncMock(return_value=None)
    try:
        res = await origin._resolve_traced(r, li_posting("https://tenant42.talentview.io/jobs/abc"))
        assert (res.posting.origin_url, res.posting.origin_ats, res.posting.origin_confidence, res.via) == (
            "https://tenant42.talentview.io/jobs/abc", "talentview", 0.9, "linkedin_offsite")
        res = await origin._resolve_traced(r, li_posting("https://jobs.randomats.example/offre/77"), use_cache=False)
        assert res.posting.origin_ats == "careers_site" and res.posting.origin_confidence == 0.9
    finally:
        await r.aclose()


# ---------------------------------------------------------------- stored jobs and the search pipeline

def stored(profile, n, score=80, **kw):
    j = MergedJob.from_posting(JobPosting(source="linkedin", source_id=f"41111111{n:02d}", url=f"https://www.linkedin.com/jobs/view/41111111{n:02d}",
                                          title=f"Stage ingénieur {n}", company=f"Orbitex{n}", location="Toulouse"))
    j.score = score
    for k, v in kw.items():
        setattr(j, k, v)
    return j


def by_id_runner(table):
    async def run(argv, timeout):
        return table.get(argv[-1].split("=")[-1], '{"element": null, "status": 200, "cost": 0.0005}')
    return run


def test_reresolve_uses_linkedin_provider_and_reports(profile, monkeypatch):
    jobs = [stored(profile, i) for i in range(1, 6)]
    db.save_jobs(profile.id, jobs)
    table = {
        "4111111101": payload(),
        "4111111102": payload(applyMethod={"type": "EasyApply"}, easyApplyUrl="https://www.linkedin.com/jobs/view/4111111102/apply"),
        "4111111103": payload(jobState="CLOSED", closedAt="2026-11-15T00:00:00Z", applyMethod={"companyApplyUrl": "https://jobs.randomats.example/offre/3"}),
        "4111111104": payload(applyMethod={"companyApplyUrl": "https://jobs.randomats.example/offre/9"}),
    }
    async def none(self, *a, **k):
        return None

    for step in ("enrich_from_page", "probe_company", "discover_careers", "web_search"):
        monkeypatch.setattr(origin.Resolver, step, none)
    async def fetch(self, url, **kwargs):
        html = '<main><h1>Stage ingénieur 4</h1><a href="/postuler/9">Postuler</a></main>' if url.endswith("/9") else ""
        return httpx.Response(200, request=httpx.Request("GET", url), text=html)

    monkeypatch.setattr(origin.Resolver, "_get", fetch)
    refetched = []
    monkeypatch.setattr(pipeline, "_refetch_apply_urls", lambda jobs, ctx, report, skip_sources=(): refetched.append(skip_sources) or asyncio.sleep(0))

    rep = asyncio.run(pipeline.reresolve_origins(profile.id, min_score=50, detail_runner=by_id_runner(table)))
    assert refetched == [("linkedin",)]  # guest-page refetch skipped for LinkedIn when the provider is used
    assert (rep.li_calls, rep.li_links, rep.li_easy, rep.li_closed) == (5, 3, 1, 1) and round(rep.li_cost, 4) == 0.0025
    stored_jobs = {j.source_id if False else j.title: j for j in db.load_jobs(profile.id)}
    one, easy, closed, four = (stored_jobs[f"Stage ingénieur {i}"] for i in (1, 2, 3, 4))
    assert one.origin_url == "https://orbitex.talentview.io/jobs/abc?id=7" and one.origin_confidence == 0.9 and one.extra["origin_via"] == "linkedin_offsite"
    assert one.sightings[0].apply_url == one.origin_url and one.extra["apply_method"] == "external"
    assert one.extra["company_website"] == "http://www.orbitex.example" and one.extra["linkedin_state"] == "listed"
    assert easy.extra["apply_method"] == "easy_apply" and easy.origin_url is None
    assert closed.extra["closed_at_source"] is True and closed.extra["linkedin_state"] == "closed"  # kept, flagged
    assert four.origin_url == "https://jobs.randomats.example/offre/9"
    assert {r.title for r in rep.rows if not r.found} >= {"Stage ingénieur 2", "Stage ingénieur 5"}


def test_reresolve_does_not_pay_twice_for_a_checked_job(profile, monkeypatch):
    j = stored(profile, 7, extra={"linkedin_state": "listed"})
    db.save_jobs(profile.id, [j])
    monkeypatch.setattr(pipeline, "_refetch_apply_urls", lambda *a, **k: asyncio.sleep(0))
    rep = asyncio.run(pipeline.reresolve_origins(profile.id, min_score=50, dry_run=True, detail_runner=by_id_runner({})))
    assert rep.li_calls == 0
    rep = asyncio.run(pipeline.reresolve_origins(profile.id, min_score=50, dry_run=True, force=True, detail_runner=by_id_runner({})))
    assert rep.li_calls == 1


def test_second_pass_after_scoring_respects_threshold_cap_and_only_linkedin(profile, monkeypatch):
    from types import SimpleNamespace

    calls = []

    async def fake_enrich(jobs, **kw):
        calls.append((len(jobs), kw.get("max_calls")))
        for j in jobs:
            j.apply_url = "https://jobs.randomats.example/offre/" + j.title[-1]
            j.sightings[0].apply_url = j.apply_url
            j.extra.update(apply_source="linkedin_offsite", linkedin_apply_url=j.apply_url)
        return pipeline.LinkedInStats(calls=len(jobs), links=len(jobs))

    seen = {}

    async def fake_resolve(postings, ctx):
        seen["raw"] = [p.raw.get("apply_source") for p in postings]
        return [p.model_copy(update={"origin_url": p.apply_url, "origin_ats": "careers_site", "origin_confidence": 0.9}) for p in postings]

    monkeypatch.setattr(pipeline, "enrich_linkedin_jobs", fake_enrich)
    monkeypatch.setattr(origin, "resolve_origins", fake_resolve)
    s = pipeline.get_settings()
    monkeypatch.setattr(s, "linkedin_detail_cmd", "fetch {id}")
    monkeypatch.setattr(s, "linkedin_detail_max_per_run", 2)
    monkeypatch.setattr(s, "apply_prepare_min_score", 60)
    high = [stored(profile, i, score=90 - i) for i in range(1, 5)]
    low = stored(profile, 8, score=40)
    other = stored(profile, 9, score=95)
    other.sightings[0].source = "jobijoba"
    done = stored(profile, 10, score=95, origin_url="https://jobs.lever.co/x/1")
    result = pipeline.RunResult(run_id=0, profile_id=profile.id)
    changed = asyncio.run(pipeline._linkedin_second_pass([*high, low, other, done], SimpleNamespace(), result))
    assert changed and calls == [(4, 2)]  # only the 4 linkedin-only jobs >= 60 without origin are candidates; the cap is passed down
    assert seen["raw"] == ["linkedin_offsite"] * 4 and all(j.origin_url for j in high) and low.origin_url is None and other.origin_url is None
    assert result.linkedin_detail_calls == 4 and result.linkedin_links == 4


def test_second_pass_disabled_by_setting(profile, monkeypatch):
    from types import SimpleNamespace

    called = []
    monkeypatch.setattr(pipeline, "enrich_linkedin_jobs", lambda *a, **k: called.append(1))
    assert asyncio.run(pipeline._linkedin_second_pass([stored(profile, 1)], SimpleNamespace(), pipeline.RunResult(run_id=0, profile_id=1))) is False
    assert called == []


async def test_enrich_cap_and_skip_rules(profile):
    jobs = [stored(profile, i) for i in range(1, 6)]
    with_link = stored(profile, 6)
    with_link.sightings[0].apply_url = "https://jobs.randomats.example/x"
    stats = await pipeline.enrich_linkedin_jobs([*jobs, with_link], runner=by_id_runner({}), max_calls=3)
    assert stats.calls == 3
