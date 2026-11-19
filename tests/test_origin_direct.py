"""Direct company apply URLs, tracker following, title/company normalisation, diagnostics and the `origins` command.
All data is fictional; HTTP goes through httpx mock transports."""

from __future__ import annotations

from unittest.mock import AsyncMock

import httpx
import pytest
from typer.testing import CliRunner

from jobhunt import db, origin
from jobhunt.dedupe import MergedJob
from jobhunt.models import JobPosting
from jobhunt.sources.base import SourceContext


def posting(**updates):
    base = dict(source="linkedin", source_id="9", url="https://www.linkedin.com/jobs/view/9", title="Stage ingénieur systèmes embarqués",
                company="Orbitex Aerospace", location="Toulouse")
    return JobPosting(**{**base, **updates})


def resolver_with(handler):
    """A Resolver whose HTTP client is an httpx mock transport (no pacing delays)."""
    ctx = SourceContext(profile_id=1)
    r = origin.Resolver(ctx)
    r.client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True, max_redirects=8)
    r.pacer.wait = AsyncMock()
    return r


# ---------------------------------------------------------------- host corroboration

@pytest.mark.parametrize("host,company,expected", [
    ("emploi.elsys-design.com", "ELSYS Design", True),
    ("www.emploi.cea.fr", "CEA", True),
    ("www.emploi.cea.fr", "Cea - Commissariat à l'énergie atomique", True),
    ("www.capgemini.com", "Capgemini", True),
    ("careers.thalesgroup.com", "THALES", True),
    ("careers.thalesgroup.com", "Thales SIX GTS France", True),
    ("www.naval-group.com", "Naval Group", True),
    ("careers.safran-group.com", "Safran Electronics & Defense", True),
    ("recrutement.alten.fr", "ALTEN", True),
    ("jobs.example.org", "Capgemini", False),
    ("www.capgeminiclone.com", "Capgemini", False),
    ("ceaa.com", "CEA", False),
    ("www.thalesfake.net", "Thales", False),
    ("mbda-fan-club.com", "MBDA", False),
])
def test_host_matches_company(host, company, expected):
    assert origin.host_matches_company(host, company) is expected


def test_host_corroborated_by_website_and_never_for_aggregators_or_homepage():
    site = "https://www.elsys-design.com"
    assert origin.host_corroborated("https://emploi.elsys-design.com/offre/123", "Another name", site) == "website"
    assert origin.host_corroborated("https://emploi.elsys-design.com/", "Elsys Design", site) is None  # bare homepage
    assert origin.host_corroborated("https://www.linkedin.com/jobs/view/1", "LinkedIn", "https://linkedin.com") is None
    assert origin.host_corroborated("https://other.example/job/1", "Elsys Design", site) is None
    assert origin.host_corroborated("https://jsv3.recruitics.com/redirect?rx_url=https%3A%2F%2Fa.fr%2Fj", "Recruitics", None) is None


@pytest.mark.parametrize("apply_url,company,website", [
    ("https://emploi.elsys-design.com/offre-emploi/stage-12", "ELSYS Design", "https://www.elsys-design.com"),
    ("https://www.emploi.cea.fr/offres/emploi-stage-12.aspx", "CEA", None),
    ("https://www.capgemini.com/fr-fr/jobs/12345-stage-ingenieur/", "Capgemini", None),
    ("https://careers.thalesgroup.com/fr/fr/apply?jobSeqNo=TGSEU12345", "Thales", None),
    ("https://careers.exemple-industrie.fr/job/12", "Industrie Exemple", "https://www.exemple-industrie.fr"),
])
async def test_direct_apply_url_on_employer_domain_is_accepted_without_fetching(apply_url, company, website):
    def refuse(request):  # careers sites that block plain HTTP must not matter
        raise AssertionError(f"no request expected, got {request.url}")

    r = resolver_with(refuse)
    try:
        got = await r.direct(posting(company=company, company_website=website, apply_url=apply_url, apply_method="external"))
        assert got is not None and got[1] == "direct_host" and got[0].startswith(apply_url.split("?")[0])
    finally:
        await r.aclose()


async def test_resolve_one_records_challenge_on_corroborated_host(tmp_path):
    r = resolver_with(lambda request: httpx.Response(403))
    r.cache = origin.DiskCache(tmp_path)
    r.enrich_from_page = AsyncMock(return_value=None)
    try:
        p = posting(company="CEA", apply_url="https://www.emploi.cea.fr/offres/emploi-stage-12.aspx", apply_method="external")
        res = await origin._resolve_traced(r, p)
        assert res.posting.origin_url is None and res.reason == "challenge"
        assert res.posting.origin_resolution["posting_url"] == p.apply_url
    finally:
        await r.aclose()


async def test_unrelated_host_is_still_refused_when_page_does_not_name_employer():
    def handler(request):
        return httpx.Response(200, text="<h1>Stage ingénieur systèmes embarqués</h1><p>Some other firm</p>")

    r = resolver_with(handler)
    try:
        assert await r.direct(posting(apply_url="https://random-careers.example/job/1", apply_method="external")) is None
    finally:
        await r.aclose()


# ---------------------------------------------------------------- trackers / redirects

@pytest.mark.parametrize("url", [
    "https://jsv3.recruitics.com/redirect?rx_cid=1&rx_url=https%3A%2F%2Fcareers.x.example%2Fj",
    "https://click.appcast.io/track/abc?cs=1",
    "https://ad.doubleclick.net/ddm/clk/1;adurl=https://x.example",
    "https://jobs.smartrecruiters.com/?redirect=https%3A%2F%2Fx.example",
    "https://www.jobijoba.com/fr/annonce/abc123",
    "https://www.bebee.com/fr/job/abc",
])
def test_trackers_are_never_origins(url):
    assert origin.is_aggregator(url) and origin.is_redirector(url)
    assert origin._apply(posting(), url, "careers_site", 1.0, None).origin_url is None


def test_real_smartrecruiters_job_is_not_a_tracker():
    assert not origin.is_aggregator("https://jobs.smartrecruiters.com/OrbitexAerospace/743999-stage-embarque")


def test_embedded_target_for_trackers_and_double_encoding():
    assert origin.embedded_target("https://jsv3.recruitics.com/redirect?rx_cid=1&rx_url=https%3A%2F%2Fcareers.x.example%2Fj%3Fid%3D4") == "https://careers.x.example/j?id=4"
    assert origin.embedded_target("https://www.googleadservices.com/pagead/aclk?adurl=https%3A%2F%2Fx.example%2Fj") == "https://x.example/j"
    assert origin.embedded_target("https://t.example/r?url=https%253A%252F%252Fx.example%252Fj") == "https://x.example/j"
    assert origin.embedded_target("https://t.example/r?url=https%3A%2F%2Fclick.appcast.io%2Ftrack%3Fu%3Dhttps%253A%252F%252Fx.example%252Fj") == "https://x.example/j"
    assert origin.embedded_target("https://t.example/r?url=https%3A%2F%2Fwww.linkedin.com%2Fjobs%2Fview%2F1") is None


async def test_follow_tracker_uses_head_and_returns_final_url():
    seen = []

    def handler(request):
        seen.append((request.method, str(request.url)))
        if request.url.host == "jsv3.recruitics.com":
            return httpx.Response(302, headers={"location": "https://click.appcast.io/track/z"})
        if request.url.host == "click.appcast.io":
            return httpx.Response(302, headers={"location": "https://careers.orbitex.example/jobs/42?utm_source=x"})
        return httpx.Response(405)  # HEAD refused on the final page: the URL still counts

    r = resolver_with(handler)
    try:
        assert await r.follow("https://jsv3.recruitics.com/redirect?id=1") == "https://careers.orbitex.example/jobs/42?utm_source=x"
        assert [m for m, _ in seen] == ["HEAD", "HEAD", "HEAD"]
    finally:
        await r.aclose()


async def test_follow_falls_back_to_get_and_reads_meta_refresh():
    def handler(request):
        if request.url.host == "www.jobijoba.com":
            if request.method == "HEAD":
                return httpx.Response(200)
            return httpx.Response(200, text='<meta http-equiv="refresh" content="0; url=https://careers.orbitex.example/jobs/7">')
        return httpx.Response(200)

    r = resolver_with(handler)
    try:
        assert await r.follow("https://www.jobijoba.com/fr/annonce/abc") == "https://careers.orbitex.example/jobs/7"
    finally:
        await r.aclose()


async def test_follow_ending_on_aggregator_is_not_an_origin_and_is_reported():
    def handler(request):
        if request.url.host == "click.appcast.io":
            return httpx.Response(302, headers={"location": "https://www.indeed.fr/viewjob?jk=1"})
        return httpx.Response(200, text="<html>indeed</html>")

    r = resolver_with(handler)
    trace = origin.Trace()
    token = origin._TRACE.set(trace)
    try:
        assert await r.follow("https://click.appcast.io/track/q") is None
        assert ("redirect_aggregator", "indeed.fr") in trace.notes
    finally:
        origin._TRACE.reset(token)
        await r.aclose()


async def test_followed_final_url_is_classified_by_host(tmp_path):
    def handler(request):
        if request.url.host == "jsv3.recruitics.com":
            return httpx.Response(302, headers={"location": "https://careers.thalesgroup.com/fr/fr/apply?jobSeqNo=TGS1"})
        return httpx.Response(403)

    r = resolver_with(handler)
    try:
        got = await r.direct(posting(company="Thales", apply_url="https://jsv3.recruitics.com/redirect?id=1", apply_method="external"))
        assert got == ("https://careers.thalesgroup.com/fr/fr/apply?jobSeqNo=TGS1", "direct_host")
    finally:
        await r.aclose()


# ---------------------------------------------------------------- titles & companies

@pytest.mark.parametrize("mine,theirs", [
    ("STAGE - Ingénieur Systèmes Embarqués (H/F)", "Ingénieur systèmes embarqués"),
    ("Stage : Ingénieur systèmes embarqués F/H", "Ingénieur Systèmes Embarqués (H/F)"),
    ("Stage de fin d'études - Ingénieur systèmes embarqués (H/F) - Réf. 2026-12345", "Ingénieur systèmes embarqués"),
    ("Ingénieur systèmes embarqués - stage 6 mois", "Stage - Ingénieur systèmes embarqués"),
    ("Stage Ingénieur systèmes embarqués (REQ-123456)", "Ingénieur systèmes embarqués"),
])
def test_internship_titles_match_across_wording(mine, theirs):
    assert origin.title_score(mine, theirs) >= 92
    assert origin.score_to_confidence(origin.title_score(mine, theirs), None) >= origin.MIN_CONFIDENCE


def test_title_contradictions_still_score_low():
    assert origin.title_score("Stage Data Analyst", "Alternance Data Analyst") < 90
    assert origin.title_score("Stage ingénieur systèmes", "Ingénieur systèmes senior") < 90
    assert origin.title_score("Stage ingénieur systèmes", "Stage comptable") < 70


def test_search_title_and_job_refs():
    assert origin.search_title("STAGE - Ingénieur Systèmes Embarqués (H/F) - Réf. 2026-12345") == "Ingénieur Systèmes Embarqués"
    assert origin.search_title("Alternance : Chargé de projet F/H") == "Chargé de projet"
    p = posting(title="Stage ingénieur (réf. TGS-20456)", description="Référence : 2026-77881 à rappeler")
    assert "tgs-20456" in origin.job_refs(p) and "2026-77881" in origin.job_refs(p)


@pytest.mark.parametrize("company,text,expected", [
    ("Thales SIX GTS France", "Rejoignez Thales, leader mondial", False),  # needs loose mode
    ("Thales SIX GTS France", "Thales SIX GTS recrute", True),
    ("Cea - Commissariat à l'énergie atomique", "Le CEA recrute", True),
    ("THALES", "careers at thales", True),
    ("Dassault Systèmes", "dassault aviation careers", False),  # strict (ATS employer checks)
    ("Alten", "Careers", False),
])
def test_company_in_text_strict(company, text, expected):
    assert origin.company_in_text(company, text) is expected


def test_company_in_text_loose_accepts_first_distinctive_word():
    assert origin.company_in_text("Dassault Systèmes", "dassault aviation careers", loose=True)
    assert origin.company_in_text("Thales SIX GTS France", "Rejoignez Thales, leader mondial", loose=True)
    assert origin.company_in_text("NXP Semiconductors", "NXP careers", loose=True) and not origin.company_in_text("NXP Semiconductors", "NXP careers")
    assert not origin.company_in_text("Mbda Foo Bar", "mbda careers", loose=True)  # 4-letter head is not specific enough


def test_slug_candidates_use_main_name():
    s = origin.slug_candidates("Thales SIX GTS France")
    assert "thales" in s
    assert origin.slug_candidates("Cea - Commissariat à l'énergie atomique")[0] == "cea"


def test_search_queries_are_specific_and_use_company_domain():
    r = origin.Resolver.__new__(origin.Resolver)
    p = posting(title="STAGE - Ingénieur Systèmes Embarqués (H/F)", company="Thales SIX GTS France", company_website="https://www.thalesgroup.com")
    qs = r.search_queries(p)
    assert qs[0] == '"Thales SIX GTS France" "Ingénieur Systèmes Embarqués" stage'
    assert any(q.startswith("site:thalesgroup.com ") for q in qs)
    assert any("site:myworkdayjobs.com" in q for q in qs) and len(qs) <= 5
    assert not any("(H/F)" in q for q in qs)


async def test_web_search_accepts_company_page_by_job_reference_and_host(tmp_path):
    page = '<html><title>Offre TGS-20456 | Orbitex</title><h1>Ingénieur systèmes embarqués junior</h1><p>Orbitex Aerospace, Toulouse. Réf TGS-20456</p></html>'
    r = resolver_with(lambda request: httpx.Response(200, text=page))
    r._search_hits = AsyncMock(return_value=[origin.SearchHit("https://www.orbitex-aerospace.example/fr/jobs/20456"),
                                              origin.SearchHit("https://www.indeed.fr/viewjob?jk=1")])
    try:
        p = posting(title="Stage - Ingénieur systèmes embarqués (Réf. TGS-20456)", company="Orbitex Aerospace")
        m = await r.web_search(p)
        assert m is not None and m.conf >= origin.MIN_CONFIDENCE and m.cand.url.startswith("https://www.orbitex-aerospace.example/")
    finally:
        await r.aclose()


async def test_best_match_boosts_candidates_on_the_employer_domain():
    r = origin.Resolver.__new__(origin.Resolver)
    p = posting(title="Stage - Ingénieur systèmes embarqués", company="Orbitex Aerospace")
    on_site = origin.Candidate("https://www.orbitex-aerospace.example/careers/12", "Ingénieur systèmes embarqués (stage)", "careers_site")
    elsewhere = origin.Candidate("https://other.example/careers/12", "Ingénieur systèmes embarqués (stage)", "careers_site")
    assert r.best_match(p, [on_site], "careers_anchor", 0.85).conf >= origin.MIN_CONFIDENCE
    assert r.best_match(p, [elsewhere], "careers_anchor", 0.85).conf < r.best_match(p, [on_site], "careers_anchor", 0.85).conf


# ---------------------------------------------------------------- diagnostics

async def test_miss_reasons(tmp_path):
    r = resolver_with(lambda request: httpx.Response(404))
    r.cache = origin.DiskCache(tmp_path)
    for step in ("probe_company", "discover_careers", "web_search"):
        setattr(r, step, AsyncMock(return_value=None))
    try:
        res = await origin._resolve_traced(r, posting())
        assert (res.reason, res.found) == ("no_apply_url", False)
        res = await origin._resolve_traced(r, posting(apply_url="https://random-careers.example/job/1", apply_method="external"), use_cache=False)
        assert res.reason == "employer_not_corroborated" and res.detail == "random-careers.example"
        r.discover_careers = AsyncMock(return_value=origin.Match(
            origin.Candidate("https://www.orbitex-aerospace.example/x/1", "t", "careers_site"), 90, 0.5, "careers_anchor"))
        res = await origin._resolve_traced(r, posting(apply_url=None), use_cache=False)
        assert res.reason == "below_threshold" and "orbitex-aerospace.example (0.50)" in res.detail
    finally:
        await r.aclose()


async def test_cache_can_be_ignored(tmp_path):
    r = resolver_with(lambda request: httpx.Response(404))
    r.cache = origin.DiskCache(tmp_path)
    for step in ("probe_company", "discover_careers", "web_search"):
        setattr(r, step, AsyncMock(return_value=None))
    try:
        p = posting()
        await origin._resolve_traced(r, p)  # caches the miss
        r.web_search.reset_mock()
        await origin._resolve_traced(r, p)
        assert r.web_search.await_count == 0
        await origin._resolve_traced(r, p, use_cache=False)
        assert r.web_search.await_count == 1
    finally:
        await r.aclose()


# ---------------------------------------------------------------- the `origins` command plumbing

def _store(profile_id, **jobkw):
    j = MergedJob.from_posting(posting(**jobkw.pop("posting", {})))
    for k, v in jobkw.items():
        setattr(j, k, v)
    return j


def test_reresolve_origins_updates_clears_and_dry_run(profile, monkeypatch):
    import asyncio

    from jobhunt import pipeline

    ok = _store(profile.id, score=80, posting=dict(source_id="1", url="https://www.linkedin.com/jobs/view/1", title="Stage A", company="Alpha",
                                                   apply_url="https://careers.alpha.example/a/1"))
    stuck = _store(profile.id, score=75, origin_url="https://fr.indeed.com/viewjob?jk=1", origin_ats="careers_site", origin_confidence=0.78,
                   posting=dict(source_id="2", url="https://www.linkedin.com/jobs/view/2", title="Stage B", company="Beta"))
    low = _store(profile.id, score=30, posting=dict(source_id="3", url="https://www.linkedin.com/jobs/view/3", title="Stage C", company="Gamma"))
    good = _store(profile.id, score=90, origin_url="https://jobs.lever.co/delta/1", origin_ats="lever", origin_confidence=1.0,
                  posting=dict(source_id="4", url="https://www.linkedin.com/jobs/view/4", title="Stage D", company="Delta"))
    db.save_jobs(profile.id, [ok, stuck, low, good])

    seen = {}

    async def fake(postings, ctx, *, use_cache=True, on_done=None):
        seen["use_cache"] = use_cache
        seen["cleared_before_resolve"] = [p.origin_url for p in postings]
        out = []
        for p in postings:
            if p.company == "Alpha":
                out.append(origin.Resolution(p.model_copy(update={"origin_url": "https://careers.alpha.example/a/1", "origin_ats": "careers_site",
                                                                  "origin_confidence": 0.8}), via="direct_host"))
            else:
                out.append(origin.Resolution(p, reason="no_apply_url"))
        return out

    monkeypatch.setattr(origin, "resolve_origins_detailed", fake)

    rep = asyncio.run(pipeline.reresolve_origins(profile.id, min_score=50, dry_run=True, refetch=False))
    assert (rep.total, rep.tried, rep.found, rep.cleared) == (3, 2, 1, 1)
    assert seen["use_cache"] is False
    stored = {j.company: j for j in db.load_jobs(profile.id)}
    assert stored["Alpha"].origin_url is None and stored["Beta"].origin_url == "https://fr.indeed.com/viewjob?jk=1"  # nothing written

    rep = asyncio.run(pipeline.reresolve_origins(profile.id, min_score=50, refetch=False))
    assert rep.by_via() == {"direct_host": 1} and rep.by_reason() == {"no_apply_url": 1}
    assert (rep.with_origin_before, rep.with_origin_after) == (1, 2)
    stored = {j.company: j for j in db.load_jobs(profile.id)}
    assert stored["Alpha"].origin_url == "https://careers.alpha.example/a/1" and stored["Alpha"].extra["origin_via"] == "direct_host"
    assert stored["Beta"].origin_url is None and stored["Beta"].origin_confidence is None  # job-board link cleared
    assert stored["Gamma"].origin_url is None and "origin_attempted_at" not in stored["Gamma"].extra  # below min score: untouched
    assert stored["Delta"].origin_url == "https://jobs.lever.co/delta/1"
    row = next(r for r in rep.rows if r.company == "Beta")
    assert row.before == "aggregator" and row.reason == "no_apply_url"


def test_origins_cli_prints_french_report(profile, monkeypatch):
    from jobhunt import cli, pipeline

    async def fake(profile_id, **kw):
        assert kw["min_score"] == 55 and kw["dry_run"] is True and kw["force"] is True and kw["limit"] == 3
        rep = pipeline.OriginReport(profile_id=profile_id, min_score=55, total=10, with_origin_before=4, with_origin_after=6, tried=6,
                                    found=2, cleared=1, dry_run=True)
        rep.rows = [pipeline.OriginRow(1, "Alpha", "Stage A", 80, "missing", True, via="direct_host", host="careers.alpha.example"),
                    pipeline.OriginRow(2, "Beta", "Stage B", 70, "aggregator", False, reason="below_threshold", detail="beta.example (0.55)")]
        return rep

    monkeypatch.setattr(pipeline, "reresolve_origins", fake)
    result = CliRunner().invoke(cli.app, ["origins", str(profile.id), "--min-score", "55", "--limit", "3", "--force", "--dry-run"])
    assert result.exit_code == 0, result.output
    out = result.output
    assert "simulation" in out and "direct_host" in out and "beta.example (0.55)" in out and "Beta" in out
    assert "meilleur candidat sous le seuil" in out


# ================================================================ pass 2: search results, JS-only ATS, aliases, trace

WD = "https://orbitex.wd3.myworkdayjobs.com/Careers/job/Toulouse/Stage-Ingenieur-Systemes-Embarques-H-F_R12345"


def hit(url, title="", snippet=""):
    return origin.SearchHit(url, title, snippet, "test")


@pytest.mark.parametrize("url,title", [
    (WD, "Stage - Ingénieur Systèmes Embarqués (H/F) | Orbitex Aerospace"),
    ("https://orbitex.wd3.myworkdayjobs.com/en-US/Careers/job/Toulouse/Stage-Ingenieur-Systemes-Embarques_R12345", ""),  # slug only
    ("https://orbitex.wd3.myworkdayjobs.com/Careers/job/Toulouse/Stage-Ingenieur-Systemes-Emb_R12345", "Careers"),  # truncated slug
    ("https://career5.successfactors.eu/career?company=orbitex&career_job_req_id=1234&career_ns=job_listing",
     "Stage ingénieur systèmes embarqués (H/F) - Orbitex Aerospace"),
    ("https://orbitex.talent-soft.com/offre-de-emploi/emploi-stage-ingenieur-systemes-embarques_4567.aspx", "Stage Ingénieur Systèmes Embarqués F/H"),
    ("https://careers.orbitex-aerospace.example/fr/job/REQ-4567/Stage-Ingenieur-Systemes-Embarques", "Ingénieur systèmes embarqués - stage | Orbitex"),
    ("https://jobs.lever.co/orbitex/0a1b2c3d-1111", "Orbitex - Ingénieur systèmes embarqués (stage)"),
])
def test_evaluate_hit_accepts_js_only_ats_and_employer_domain_from_result_alone(url, title):
    r = origin.Resolver.__new__(origin.Resolver)
    m = r.evaluate_hit(posting(company="Orbitex Aerospace"), hit(url, title))
    assert m is not None and m.conf >= origin.MIN_CONFIDENCE, (url, m)


@pytest.mark.parametrize("url,title,why", [
    ("https://othercorp.wd3.myworkdayjobs.com/Careers/job/Toulouse/Stage-Ingenieur-Systemes-Embarques_R1", "Stage Ingénieur Systèmes Embarqués", "foreign tenant"),
    ("https://orbitex.wd3.myworkdayjobs.com/Careers", "Stage Ingénieur Systèmes Embarqués", "board root"),
    ("https://orbitex.wd3.myworkdayjobs.com/Careers/job/Toulouse/Comptable_R2", "Comptable", "other title"),
    ("https://orbitex.wd3.myworkdayjobs.com/Careers/job/Lyon/Stage-Ingenieur-Systemes-Embarques_R3", "Stage Ingénieur Systèmes Embarqués", "other city"),
    ("https://www.orbitex-aerospace.example/", "Stage Ingénieur Systèmes Embarqués - Orbitex", "homepage"),
    ("https://www.indeed.fr/viewjob?jk=1", "Stage Ingénieur Systèmes Embarqués - Orbitex", "aggregator"),
    ("https://click.appcast.io/track/abc?x=1", "Stage Ingénieur Systèmes Embarqués", "tracker"),
    ("https://random-blog.example/jobs/stage-ingenieur-systemes-embarques", "Stage Ingénieur Systèmes Embarqués", "unrelated host"),
])
def test_evaluate_hit_refuses(url, title, why):
    r = origin.Resolver.__new__(origin.Resolver)
    assert r.evaluate_hit(posting(company="Orbitex Aerospace"), hit(url, title)) is None, why


async def test_web_search_accepts_workday_result_even_when_page_is_a_js_shell_and_confirms_with_api():
    calls = []

    def handler(request):
        calls.append((request.method, request.url.host))
        if request.method == "POST" and "/wday/cxs/orbitex/Careers/jobs" in request.url.path:
            return httpx.Response(200, json={"jobPostings": [{"title": "Stage - Ingénieur Systèmes Embarqués", "externalPath": "/job/Toulouse/Stage-Ingenieur_R12345",
                                                              "locationsText": "Toulouse", "postedOn": "Posted 3 Days Ago"}]})
        return httpx.Response(200, text="<html><div id='root'></div><script src='/app.js'></script></html>")  # JS shell

    r = resolver_with(handler)
    r._search_hits = AsyncMock(side_effect=lambda q: [hit(WD, "Stage - Ingénieur Systèmes Embarqués (H/F) | Orbitex Aerospace")])
    try:
        m = await r.web_search(posting(company="Orbitex Aerospace", company_website="https://www.orbitex-aerospace.example"))
        assert m and m.conf >= origin.MIN_CONFIDENCE
        assert m.cand.url == "https://orbitex.wd3.myworkdayjobs.com/Careers/job/Toulouse/Stage-Ingenieur_R12345" and m.cand.posted_at
        assert ("GET", "orbitex.wd3.myworkdayjobs.com") not in calls  # no page fetch needed
    finally:
        await r.aclose()


async def test_workday_seed_candidates_reach_the_threshold_with_employer_evidence():
    r = resolver_with(lambda request: httpx.Response(404))
    r.search_workday = AsyncMock(return_value=[origin.Candidate("https://valeo.wd3.myworkdayjobs.com/valeo_jobs/job/Paris/Stage-Firmware_R9",
                                                                 "Stage Firmware engineer", "workday", "Paris")])
    try:
        m = await r.probe_company(posting(company="Valeo", title="Stage - Firmware engineer (H/F)", location="Paris"), extra_slugs=["valeo"])
        assert m and m.conf >= origin.MIN_CONFIDENCE
    finally:
        await r.aclose()


@pytest.mark.parametrize("company,host,expected", [
    ("Liebherr Group", "www.liebherr.com", True),
    ("Safran Electronics & Defense", "www.safran-group.com", True),
    ("Safran Aircraft Engines", "careers.safran-group.com", True),
    ("KNDS France", "www.knds.com", True),
    ("GE HealthCare", "www.gehealthcare.com", True),
    ("IDEMIA Public Security", "www.idemia.com", True),
    ("Collins Aerospace", "www.collinsaerospace.com", True),
    ("NXP Semiconductors", "www.nxp.com", True),
    ("SERMA Ingénierie", "recrutement.serma.com", True),
    ("Cea - Commissariat à l'énergie atomique", "www.emploi.cea.fr", True),
    ("NXP Semiconductors", "www.nxpfake.com", False),
])
def test_group_names_yield_brand_tokens_for_hosts(company, host, expected):
    assert origin.host_matches_company(host, company) is expected


def test_brand_slugs_for_groups():
    assert origin.slug_candidates("Safran Electronics & Defense")[0] == "safran"
    assert origin.slug_candidates("NXP Semiconductors")[0] == "nxp"
    assert origin.slug_candidates("IDEMIA Public Security")[0] == "idemia"
    assert origin.slug_candidates("GE HealthCare")[0] == "gehealthcare"


def test_search_queries_with_discovered_domain_and_shortened_title():
    r = origin.Resolver.__new__(origin.Resolver)
    p = posting(title="Stage de fin d'études - Développement d'un système de contrôle-commande pour bancs d'essais moteurs (H/F)",
                company="Safran Aircraft Engines")
    qs = r.search_queries(p, "safran-group.com")
    assert qs[0].startswith('"Safran Aircraft Engines" "')
    assert any(q.startswith('site:safran-group.com "') for q in qs)
    assert any(q.startswith("site:safran-group.com ") and '"' not in q for q in qs)  # shortened, unquoted
    assert not any("(H/F)" in q or "Stage de fin" in q for q in qs) and len(qs) <= 5


async def test_careers_domain_is_discovered_then_site_query_runs():
    seen = []

    async def fake(query):
        seen.append(query)
        if "recrutement" in query:
            return [hit("https://www.linkedin.com/company/orbitex"), hit("https://careers.orbitex-aerospace.example/")]
        if query.startswith("site:orbitex-aerospace.example"):
            return [hit("https://careers.orbitex-aerospace.example/fr/job/REQ-9/Stage-Ingenieur-Systemes-Embarques",
                        "Stage ingénieur systèmes embarqués F/H | Orbitex")]
        return []

    r = resolver_with(lambda request: httpx.Response(404))
    r._search_hits = fake
    try:
        m = await r.web_search(posting(company="Orbitex Aerospace"))
        assert m and m.conf >= origin.MIN_CONFIDENCE and "/job/REQ-9/" in m.cand.url
        assert seen[0].startswith("Orbitex Aerospace carrières") and any(q.startswith("site:orbitex-aerospace.example ") for q in seen)
    finally:
        await r.aclose()


class _FakeFirecrawl:
    mode = "error"

    def __init__(self, api_key=None):
        pass

    def search(self, *a, **k):
        if _FakeFirecrawl.mode == "error":
            raise RuntimeError("quota")
        return type("R", (), {"web": []})()


async def test_firecrawl_error_or_empty_falls_through_to_duckduckgo_and_trace_names_provider(monkeypatch):
    import firecrawl

    monkeypatch.setattr(firecrawl, "Firecrawl", _FakeFirecrawl)
    ddg = ('<div class="result"><a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fcareers.orbitex-aerospace.example%2Fjob%2F1">'
           'Stage ingénieur systèmes</a><a class="result__snippet">Orbitex recrute</a></div>')
    r = resolver_with(lambda request: httpx.Response(200, text=ddg))
    monkeypatch.setattr(r.ctx.settings, "firecrawl_api_key", "fc-test")
    trace = origin.Trace()
    token = origin._TRACE.set(trace)
    try:
        for mode in ("error", "empty"):
            _FakeFirecrawl.mode = mode
            hits = await r._search_hits("orbitex stage")
            assert [h.provider for h in hits] == ["duckduckgo"] and hits[0].title == "Stage ingénieur systèmes" and hits[0].snippet == "Orbitex recrute"
        text = "\n".join(trace.lines)
        assert "[firecrawl] erreur RuntimeError" in text and "[firecrawl]" in text and "[duckduckgo]" in text
    finally:
        origin._TRACE.reset(token)
        await r.aclose()


async def test_search_failed_when_no_provider_answers(monkeypatch):
    r = resolver_with(lambda request: httpx.Response(403))
    r.ddg_blocked = True
    trace = origin.Trace()
    token = origin._TRACE.set(trace)
    try:
        assert await r._search_hits("x") == []
        assert trace.notes and trace.notes[0][0] == "search_failed"
    finally:
        origin._TRACE.reset(token)
        await r.aclose()


async def _miss(r, tmp_path, **kw):
    r.cache = origin.DiskCache(tmp_path)
    for step in ("probe_company", "discover_careers"):
        setattr(r, step, AsyncMock(return_value=None))
    return await origin._resolve_traced(r, posting(**kw), use_cache=False)


async def test_reason_for_posting_without_apply_url_is_the_search_outcome(tmp_path):
    # 1. search answers with nothing at all
    r = resolver_with(lambda request: httpx.Response(404))
    r._search_hits = AsyncMock(return_value=[])
    try:
        res = await _miss(r, tmp_path, company_website="https://www.orbitex-aerospace.example")
        assert res.reason == "search_no_results"
        assert any("étape 5" in line for line in res.trace)
    finally:
        await r.aclose()
    # 2. results exist but none validates
    r = resolver_with(lambda request: httpx.Response(200, text="<h1>Autre chose</h1>"))
    r._search_hits = AsyncMock(return_value=[hit("https://careers.orbitex-aerospace.example/fr/job/REQ-1/Comptable", "Comptable | Orbitex"),
                                              hit("https://elsewhere.example/p/1", "Ingénieur systèmes")])
    try:
        res = await _miss(r, tmp_path, company_website="https://www.orbitex-aerospace.example")
        assert res.reason == "search_none_validated" and "résultat" in res.detail
        text = "\n".join(res.trace)
        assert "candidats [résultats web]" in text and "titre non concordant" in text and "hôte non relié" in text
    finally:
        await r.aclose()
    # 3. search provider down
    r = resolver_with(lambda request: httpx.Response(403))
    r.ddg_blocked = True
    try:
        res = await _miss(r, tmp_path, company_website="https://www.orbitex-aerospace.example")
        assert res.reason == "search_failed" and "duckduckgo" in res.detail
    finally:
        await r.aclose()
    # 4. a below-threshold candidate is reported with host and confidence
    r = resolver_with(lambda request: httpx.Response(404))
    r._search_hits = AsyncMock(return_value=[])
    try:
        res = await _miss(r, tmp_path, company_website="https://www.orbitex-aerospace.example")
        r.web_search = AsyncMock(return_value=origin.Match(origin.Candidate("https://valeo.wd3.myworkdayjobs.com/x/job/p/t_R1", "t", "workday"), 88, 0.59, "web_search"))
        res = await origin._resolve_traced(r, posting(), use_cache=False)
        assert res.reason == "below_threshold" and res.detail == "valeo.wd3.myworkdayjobs.com (0.59)"
    finally:
        await r.aclose()


def test_origins_cli_trace_option(profile, monkeypatch):
    from jobhunt import cli, pipeline

    async def fake(profile_id, **kw):
        rep = pipeline.OriginReport(profile_id=profile_id, min_score=50, total=2, tried=1)
        rep.rows = [pipeline.OriginRow(1, "Alpha", "Stage A", 80, "missing", False, reason="search_no_results", has_apply_url=False,
                                       trace=["Alpha | Stage A | apply_url : aucun", "  recherche [firecrawl] 'q' -> 0 résultat(s)"])]
        return rep

    monkeypatch.setattr(pipeline, "reresolve_origins", fake)
    out = CliRunner().invoke(cli.app, ["origins", str(profile.id), "--trace", "1"]).output
    assert "Trace" in out and "recherche [firecrawl]" in out and "Alpha | Stage A | apply_url : aucun" in out
