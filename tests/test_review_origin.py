from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from jobhunt import origin
from jobhunt.models import JobPosting
from jobhunt.sources.base import Source, SourceContext


def posting(**updates):
    return JobPosting(source="linkedin", source_id="1", url="https://linkedin.com/jobs/view/1",
                      title="Firmware engineer", company="Acme Robotics", location="Paris").model_copy(update=updates)


@pytest.mark.parametrize("ats", ["greenhouse", "lever", "ashby", "smartrecruiters", "workable", "recruitee", "teamtailor", "workday"])
def test_guessed_board_requires_employer_evidence(ats):
    resolver = origin.Resolver.__new__(origin.Resolver)
    candidate = origin.Candidate("https://jobs.lever.co/acme/1", "Firmware engineer", ats, "Paris",
                                 employer="Unrelated corporation")
    unverified = resolver.best_match(posting(), [candidate], "ats_api")
    assert unverified.conf < origin.MIN_CONFIDENCE
    candidate.employer = "Acme Robotics SAS"
    assert resolver.best_match(posting(), [candidate], "ats_api").conf >= 0.9
    candidate.employer = "Acme Robotic"
    assert resolver.best_match(posting(), [candidate], "ats_api").conf >= 0.9
    candidate.employer = None
    candidate.description = "Join Acme Robotics to develop firmware."
    assert resolver.best_match(posting(), [candidate], "ats_api").conf >= 0.9
    candidate.description = "Build balancing systems."
    assert resolver.best_match(posting(company="Alan"), [candidate], "ats_api").conf < origin.MIN_CONFIDENCE


async def test_probe_company_does_not_accept_colliding_slug(tmp_path):
    resolver = origin.Resolver(SourceContext(profile_id=1))
    candidate = origin.Candidate("https://jobs.lever.co/acme/1", "Firmware engineer", "lever", "Paris",
                                 employer="Different business")
    resolver._search_ref = AsyncMock(return_value=(True, [candidate]))
    try:
        match = await resolver.probe_company(posting())
        assert match.conf < origin.MIN_CONFIDENCE
        assert resolver._search_ref.await_count > len(origin._API_ATS)
    finally:
        await resolver.aclose()


async def test_greenhouse_board_metadata_verifies_employer(monkeypatch):
    calls = []

    def respond(request):
        calls.append(str(request.url))
        if request.url.path.endswith("/jobs"):
            return httpx.Response(200, json={"jobs": [{"id": 1, "title": "Firmware engineer", "location": {"name": "Paris"},
                                                       "absolute_url": "https://job-boards.greenhouse.io/acme/jobs/1"}]})
        return httpx.Response(200, json={"name": "Acme Robotics"})

    monkeypatch.setattr(SourceContext, "http", lambda self, **kwargs: httpx.AsyncClient(transport=httpx.MockTransport(respond)))
    resolver = origin.Resolver(SourceContext(profile_id=1))
    resolver.pacer.wait = AsyncMock()
    try:
        candidates = await resolver.board_jobs(origin.BoardRef("greenhouse", "acme"))
        assert resolver.best_match(posting(), candidates, "ats_api").conf >= 0.9
        assert len(calls) == 2
    finally:
        await resolver.aclose()


@pytest.mark.parametrize("location,remote,accepted", [("Lyon", "unknown", False), ("Lyon, remote", "unknown", True),
                                                      ("Lyon, Paris", "unknown", True), ("Lyon", "full", True)])
def test_origin_matching_rejects_city_contradictions(location, remote, accepted):
    resolver = origin.Resolver.__new__(origin.Resolver)
    candidate = origin.Candidate("https://jobs.lever.co/acme/1", "Firmware engineer", "lever", location,
                                 employer="Acme Robotics")
    match = resolver.best_match(posting(remote=remote), [candidate], "ats_api")
    assert bool(match and match.conf >= origin.MIN_CONFIDENCE) == accepted


async def test_direct_origin_rejects_city_contradiction():
    resolver = origin.Resolver(SourceContext(profile_id=1))
    resolver.direct = AsyncMock(return_value=("https://jobs.lever.co/acme/1", "direct"))
    resolver.enrich_from_page = AsyncMock(return_value=origin.Candidate("https://jobs.lever.co/acme/1", "Firmware engineer", "lever", "Lyon"))
    resolver.probe_company = AsyncMock(return_value=None)
    resolver.discover_careers = AsyncMock(return_value=None)
    resolver.web_search = AsyncMock(return_value=None)
    try:
        assert (await origin._resolve_one(resolver, posting())).origin_url is None
    finally:
        await resolver.aclose()


async def test_late_origin_enrichment_cannot_override_location_rejection():
    resolver = origin.Resolver(SourceContext(profile_id=1))
    candidate = origin.Candidate("https://jobs.lever.co/acme/1", "Firmware engineer", "lever", employer="Acme Robotics")
    resolver.direct = AsyncMock(return_value=None)
    resolver.probe_company = AsyncMock(return_value=origin.Match(candidate, 100, 0.92, "ats_api"))
    resolver.enrich_from_page = AsyncMock(return_value=origin.Candidate(candidate.url, candidate.title, "lever", "Lyon"))
    try:
        assert (await origin._resolve_one(resolver, posting())).origin_url is None
    finally:
        await resolver.aclose()


@pytest.mark.parametrize("host", ["collective.work", "linkedin.com", "indeed.fr", "glassdoor.fr", "jobijoba.com",
                                 "francetravail.fr", "hellowork.com", "welcometothejungle.com", "apec.fr", "cadremploi.fr",
                                 "jobteaser.com", "meteojob.com", "free-work.com", "codeur.com", "freelance-informatique.fr",
                                 "malt.fr", "choisirleservicepublic.gouv.fr", "labonnealternance.apprentissage.beta.gouv.fr",
                                 "mon-vie-via.businessfrance.fr", "civiweb.com"])
def test_every_supported_board_is_discovery_only(host):
    assert origin.is_aggregator(f"https://{host}/jobs/1")
    assert origin._apply(posting(), f"https://{host}/jobs/1", "careers_site", 1.0, None).origin_url is None


def test_aggregator_domain_metadata_is_used(monkeypatch):
    class NewBoard(Source):
        name, label, kind = "newboard", "New board", "http"
        is_aggregator = True
        domains = ("newboard.test",)

        async def search(self, query, ctx):
            return []

    monkeypatch.setattr(origin, "registry", lambda: {"newboard": NewBoard})
    assert origin.is_aggregator("https://fr.newboard.test/jobs/1")
    assert not origin.is_aggregator("https://notnewboard.test/jobs/1")


@pytest.mark.parametrize("company_text,expected", [("Unrelated employer", False), ("Acme Robotics", True)])
async def test_unknown_host_requires_company_corroboration(company_text, expected):
    resolver = origin.Resolver(SourceContext(profile_id=1))
    url = "https://unknown.test/jobs/1"
    resolver._get = AsyncMock(return_value=httpx.Response(200, request=httpx.Request("GET", url),
        text=f"<h1>Firmware engineer</h1><p>{company_text}</p>"))
    try:
        assert bool(await resolver.direct(posting(url=url))) == expected
    finally:
        await resolver.aclose()


async def test_company_linked_tenant_is_not_a_guess():
    resolver = origin.Resolver(SourceContext(profile_id=1))
    url = "https://acme.test/careers"
    resolver._careers_pages = AsyncMock(return_value=[url])
    resolver._get = AsyncMock(return_value=httpx.Response(200, request=httpx.Request("GET", url),
        text='<h1>Acme Robotics careers</h1><a href="https://jobs.lever.co/odd-tenant">Jobs</a>'))
    resolver._search_ref = AsyncMock(return_value=(True, [origin.Candidate(
        "https://jobs.lever.co/odd-tenant/1", "Firmware engineer", "lever", "Paris")]))
    try:
        match = await resolver.discover_careers(posting(company_website="https://acme.test"))
        assert match and match.conf >= origin.MIN_CONFIDENCE
    finally:
        await resolver.aclose()


async def test_careers_discovery_requires_verified_company_site():
    resolver = origin.Resolver(SourceContext(profile_id=1))
    url = "https://unrelated.test"
    resolver._get = AsyncMock(return_value=httpx.Response(200, request=httpx.Request("GET", url),
        text='<h1>Other employer</h1><a href="https://jobs.lever.co/acme">Careers</a>'))
    try:
        assert await resolver._careers_pages(url, "Acme Robotics") == []
        assert resolver._get.await_count == 1
    finally:
        await resolver.aclose()


async def test_verified_company_site_can_link_opaque_tenant():
    resolver = origin.Resolver(SourceContext(profile_id=1))
    homepage = "https://acme.test"
    board = "https://jobs.lever.co/opaque-tenant"

    async def get(url, **kwargs):
        if url == homepage:
            text = f'<h1>Acme Robotics</h1><a href="{board}">Careers</a>'
        else:
            text = "<h1>Open positions</h1>"
        return httpx.Response(200, request=httpx.Request("GET", url), text=text)

    resolver._get = get
    resolver._search_ref = AsyncMock(return_value=(True, [origin.Candidate(
        board + "/1", "Firmware engineer", "lever", "Paris")]))
    try:
        match = await resolver.discover_careers(posting(company_website=homepage))
        assert match and match.conf >= origin.MIN_CONFIDENCE
    finally:
        await resolver.aclose()


def test_origin_publication_normalizes_offsets_and_multilocation():
    old = datetime(2026, 11, 1, 10, tzinfo=UTC)
    candidate = origin.Candidate("https://acme.test/jobs/1", "Firmware engineer", "careers_site",
                                 posted_at=datetime(2026, 11, 1, 11, tzinfo=timezone(timedelta(hours=2))))
    resolved = origin._apply(posting(posted_at=old), candidate.url, candidate.ats, 0.9, candidate)
    assert resolved.posted_at == old  # source date untouched
    assert resolved.origin_posted_at == datetime(2026, 11, 1, 9, tzinfo=UTC)
    assert origin.location_match("Paris", origin._ld_location({"jobLocation": [
        {"address": {"addressLocality": "Lyon"}}, {"address": {"addressLocality": "Paris"}}]})) is True


@pytest.mark.parametrize("url", ["file:///etc/passwd", "javascript:alert(1)", "data:text/html,hello", "ftp://host/job", "httpx://host/job"])
async def test_origin_refuses_non_http_urls(url):
    client = SimpleNamespace(get=AsyncMock(), aclose=AsyncMock())
    resolver = origin.Resolver.__new__(origin.Resolver)
    resolver.client = client
    resolver.ctx = SimpleNamespace(browser=SimpleNamespace(page=AsyncMock()))
    assert await resolver._get(url) is None
    assert await resolver.follow(url) is None
    assert await resolver._ddg_via_browser(url) is None
    client.get.assert_not_awaited()
    resolver.ctx.browser.page.assert_not_called()
