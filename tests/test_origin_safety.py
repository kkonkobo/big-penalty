import asyncio
import json
from dataclasses import asdict
from unittest.mock import AsyncMock

import httpx
import pytest
from typer.testing import CliRunner

from jobhunt import cli, db, origin, pipeline
from jobhunt.dedupe import MergedJob, Sighting
from test_origin_direct import posting, resolver_with


WORKDAY = "https://orbitex.wd3.myworkdayjobs.com/External/job/Toulouse/Stage_JR284"
BRANDED = "https://jobs.orbitex.example/fr/fr/job/JR284/stage"
SHORT = "https://ars2.equest.com/?response_id=fixture-secret"


def stored_job(profile, *, url=WORKDAY, ats="workday", apply_url=None, confidence=0.95):
    source = posting(origin_url=url, origin_ats=ats, origin_confidence=confidence, apply_url=apply_url)
    job = MergedJob.from_posting(source)
    job.score = 80
    if ats == "workday":
        job.extra["origin_resolution"] = asdict(origin.OriginEvidence(
            "resolved", confidence, "workday", posting_url=url, tenant="orbitex", requisition_id="JR284", site="External"))
    db.save_jobs(profile.id, [job])
    return job


def mock_resolver(monkeypatch, handler):
    async def handler_get_only(request):
        assert request.method == "GET"
        return handler(request)

    def client(*args, **kwargs):
        return httpx.AsyncClient(transport=httpx.MockTransport(handler_get_only), follow_redirects=True)

    monkeypatch.setattr(origin.SourceContext, "http", client)
    monkeypatch.setattr(origin.Pacer, "wait", AsyncMock())


@pytest.mark.parametrize("response,state,reason", [
    (429, "deferred", "rate_limited"), (410, "expired", "expired"), (403, "challenge", "challenge"),
    (None, "unresolved", "timeout"), (503, "unresolved", "unresolved"),
])
async def test_reresolve_preserves_verified_record(profile, monkeypatch, response, state, reason):
    job = stored_job(profile)

    def handler(request):
        if response is None:
            raise httpx.ReadTimeout("fixture timeout", request=request)
        return httpx.Response(response)

    mock_resolver(monkeypatch, handler)
    counts = await origin.reresolve_stored(profile.id)
    assert counts == {("workday", state): 1}
    refreshed = db.get_job(job.id)
    assert (refreshed.origin_url, refreshed.origin_ats, refreshed.origin_confidence) == (WORKDAY, "workday", 0.95)
    assert refreshed.extra["origin_resolution"] == job.extra["origin_resolution"]
    observation = refreshed.extra["origin_observation"]
    assert (observation["state"], observation["reason"]) == (state, reason)
    assert observation["observed_at"]


def test_verified_record_requires_positive_same_requisition(profile):
    job = stored_job(profile)

    def apply(url, ats, tenant, req):
        evidence = origin.OriginEvidence("resolved", 0.98, ats, posting_url=url, tenant=tenant, requisition_id=req)
        return job.apply_origin(posting(origin_url=url, origin_ats=ats, origin_confidence=0.98,
                                        origin_resolution=asdict(evidence)))

    assert not apply(WORKDAY, "careers_site", None, None)
    assert not apply(WORKDAY.replace("orbitex.wd3", "other.wd3"), "workday", "other", "JR284")
    assert not apply(WORKDAY.replace("JR284", "JR285"), "workday", "orbitex", "JR285")
    assert job.extra["origin_observation"]["reason"] == "different_or_unverified_requisition"
    assert job.origin_url == WORKDAY and job.origin_confidence == 0.95
    revised = WORKDAY.replace("Stage_", "Stage-avionique_")
    assert apply(revised, "workday", "orbitex", "JR284")
    assert (job.origin_url, job.origin_ats, job.origin_confidence) == (revised, "workday", 0.98)
    assert job.extra["origin_resolution"]["posting_url"] == revised
    assert job.extra["origin_resolution"]["requisition_id"] == "JR284"


async def test_origins_pipeline_retains_verified_identity_on_challenge(profile, monkeypatch):
    job = stored_job(profile, confidence=0.58, apply_url=WORKDAY)

    async def fake(postings, ctx, **kwargs):
        return [origin.Resolution(postings[0].model_copy(update={"origin_resolution": asdict(
            origin.OriginEvidence(state="challenge", posting_url=WORKDAY))}), reason="challenge")]

    monkeypatch.setattr(origin, "resolve_origins_detailed", fake)
    report = await pipeline.reresolve_origins(profile.id, force=True, refetch=False)
    assert report.tried == 1 and report.found == 0
    refreshed = db.get_job(job.id)
    assert (refreshed.origin_url, refreshed.origin_ats, refreshed.origin_confidence) == (WORKDAY, "workday", 0.58)
    assert refreshed.extra["origin_resolution"] == job.extra["origin_resolution"]
    assert refreshed.extra["origin_observation"]["state"] == "challenge"


@pytest.mark.parametrize("container,tenant,publication", [
    ('<main><h1>{title}</h1><a data-job-id="743999" href="{target}">Apply</a></main>', "Other", "743999"),
    ('<main><h1>{title}</h1></main><a href="{target}">Apply</a>', "Orbitex", "743999"),
    ('<main><h1>{title}</h1><a href="{target}">Apply</a></main>', "Orbitex", "other-publication"),
    ('<main><h1>{title}</h1><article data-posting-id="743998"><a href="{target}">Apply</a></article></main>', "Orbitex", "743999"),
])
async def test_apply_control_requires_scope_and_employer_tenant(container, tenant, publication):
    url = "https://jobs.orbitex.example/jobs/743999-stage"
    post = posting()
    target = f"https://jobs.smartrecruiters.com/oneclick-ui/company/{tenant}/publication/{publication}"
    html = container.format(title=post.title, target=target)
    assert origin._page_evidence(url, html, posting=post).ats is None


def test_hosted_apply_control_cannot_switch_shared_host_tenant():
    post = posting()
    url = "https://jobs.smartrecruiters.com/Orbitex/743999-stage"
    html = f'<main><h1>{post.title}</h1><a href="/oneclick-ui/company/Other/publication/743999">Apply</a></main>'
    observed = origin._page_evidence(url, html, posting=post)
    assert (observed.ats, observed.tenant, observed.apply_url) == ("smartrecruiters", "orbitex", None)


@pytest.mark.parametrize("initial,target,reason", [
    (WORKDAY, WORKDAY.replace("orbitex.wd3", "other.wd3"), "tenant_mismatch"),
    (WORKDAY, WORKDAY.replace("JR284", "JR285"), "requisition_mismatch"),
    (SHORT, "https://stranger.example/jobs/284", "unverified_redirect_tenant"),
    (SHORT, BRANDED.replace("https://", "https://user:secret@"), "unsafe_url"),
    (SHORT, BRANDED.replace(".example/", ".example:8080/"), "unsafe_url"),
    (SHORT, SHORT, "redirect_loop"),
])
async def test_redirect_is_rejected_before_fetch(initial, target, reason):
    seen = []

    def handler(request):
        seen.append(str(request.url))
        assert seen == [initial]
        return httpx.Response(302, headers={"location": target})

    resolver = resolver_with(handler)
    try:
        observed = await resolver.inspect_posting(posting(), initial)
        assert observed.evidence.reason == reason
    finally:
        await resolver.aclose()


@pytest.mark.parametrize("url", [WORKDAY.replace("https://", "https://user@"), WORKDAY.replace(".com/", ".com:8443/")])
async def test_unsafe_initial_url_is_neither_classified_nor_fetched(url):
    def refuse(request):
        pytest.fail("An unsafe URL reached the HTTP transport")

    resolver = resolver_with(refuse)
    try:
        assert origin.classify_ats(url) is None
        assert (await resolver.inspect_posting(posting(), url)).evidence.reason == "unsafe_url"
    finally:
        await resolver.aclose()


async def test_redirect_limit():
    visits = []

    def handler(request):
        visits.append(str(request.url))
        return httpx.Response(302, headers={"location": f"/jobs/{len(visits)}"})

    resolver = resolver_with(handler)
    try:
        result = await resolver.inspect_posting(posting(), "https://jobs.orbitex.example/jobs/start")
        assert result.evidence.reason == "redirect_limit"
        assert len(visits) == origin.REDIRECT_LIMIT + 1
    finally:
        await resolver.aclose()


async def test_redirect_cannot_switch_tenant_via_generic_same_host_hop():
    initial = "https://jobs.smartrecruiters.com/Orbitex/743999-stage"
    calls = []

    def handler(request):
        calls.append(request.url.path)
        assert len(calls) <= 2
        target = "/" if len(calls) == 1 else "/Other/743999-stage"
        return httpx.Response(302, headers={"location": target})

    resolver = resolver_with(handler)
    try:
        result = await resolver.inspect_posting(posting(), initial)
        assert result.evidence.reason == "tenant_mismatch"
        assert calls == ["/Orbitex/743999-stage", "/"]
    finally:
        await resolver.aclose()


def test_successfactors_shared_host_never_invents_tenant_key():
    shared = "https://career5.successfactors.eu/career?jobReqId=123"
    observed = origin._page_evidence(shared, "", posting=posting())
    assert (observed.ats, observed.tenant, observed.requisition_id) == ("successfactors", None, "123")


def test_explain_and_fallback_to_equest_sighting(profile, monkeypatch):
    job = stored_job(profile, url="https://jobs.orbitex.example/jobs/legacy?private=redacted", ats="careers_site")
    job.add(posting(source_id="other-sighting", url=SHORT))
    db.save_jobs(profile.id, [job])
    title = job.title
    seen = []

    def handler(request):
        seen.append(str(request.url))
        if str(request.url) == SHORT:
            return httpx.Response(302, headers={"location": BRANDED})
        if str(request.url) == BRANDED:
            return httpx.Response(200, text=f'''<script src="https://cdn.phenompeople.com/site.js"></script>
            <main><h1>{title}</h1></main><script>window.job = {{"jobReqId":"JR284","applyUrl":"{WORKDAY}"}};</script>''')
        return httpx.Response(200, text=f'<main><h1>{title}</h1><a href="/postuler/284">Postuler</a></main>')

    mock_resolver(monkeypatch, handler)
    result = CliRunner().invoke(cli.app, ["reresolve", str(profile.id), "--dry-run", "--explain", "1"])
    assert result.exit_code == 0, result.exception
    reason = "  jobs.orbitex.example: no_vendor_evidence; linkedin.com: intermediate_board"
    assert result.output.splitlines() == ["OBSERVED", "careers_site → workday: 1", reason,
                                         "STORED origin_ats", "careers_site → workday: 1", reason,
                                         "downgrades of a known vendor: 0"]
    assert SHORT in seen and BRANDED in seen and title not in result.output and "?" not in result.output
    assert db.get_job(job.id).origin_url == job.origin_url


async def test_host_requests_serialized_and_429_defers_queued_requests():
    started = asyncio.Event()
    respond = asyncio.Event()
    seen = []

    async def handler(request):
        seen.append(str(request.url))
        started.set()
        await respond.wait()
        return httpx.Response(429, headers={"retry-after": "3600"})

    resolver = resolver_with(handler)
    try:
        first = asyncio.create_task(resolver.inspect_posting(posting(), WORKDAY))
        await started.wait()
        second = asyncio.create_task(resolver.inspect_posting(posting(), WORKDAY.replace("JR284", "JR285")))
        await asyncio.sleep(0)
        assert seen == [WORKDAY] and not second.done()
        respond.set()
        one, two = await asyncio.gather(first, second)
        assert seen == [WORKDAY]
        assert (one.evidence.state, one.evidence.reason) == ("deferred", "rate_limited")
        assert (two.evidence.state, two.evidence.reason) == ("deferred", "host_cooldown")
        assert resolver._deferred_until[origin._host(WORKDAY)] - origin.time.monotonic() > 3500
    finally:
        await resolver.aclose()


async def test_redirect_hops_are_paced_individually():
    def handler(request):
        if str(request.url) == SHORT:
            return httpx.Response(302, headers={"location": BRANDED})
        return httpx.Response(200, text="<main></main>")

    resolver = resolver_with(handler)
    try:
        await resolver.inspect_posting(posting(), SHORT)
        assert [call.args[0] for call in resolver.pacer.wait.await_args_list] == [SHORT, BRANDED]
    finally:
        await resolver.aclose()


def test_dry_run_creates_no_files(profile, monkeypatch, tmp_path):
    stored_job(profile)
    mock_resolver(monkeypatch, lambda request: httpx.Response(410))
    monkeypatch.setattr(origin, "DiskCache", lambda *args: pytest.fail("dry run initialized DiskCache"))
    monkeypatch.setattr(db, "connect", lambda *args: pytest.fail("dry run opened writable DB"))
    before = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    result = CliRunner().invoke(cli.app, ["reresolve", str(profile.id), "--dry-run"])
    assert result.exit_code == 0, result.exception
    assert {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()} == before


async def test_blocked_linkedin_offsite_keeps_unverified_lead_and_tries_discovery():
    url = "https://offsite.orbitex.example/job/284?utm_source=x"
    resolver = resolver_with(lambda request: httpx.Response(403))
    resolver.probe_company = AsyncMock(return_value=None)
    resolver.discover_careers = AsyncMock(return_value=None)
    resolver.web_search = AsyncMock(return_value=None)
    post = posting(apply_url=url, raw={"apply_source": "linkedin_offsite"})
    try:
        result = (await origin._resolve_traced(resolver, post, use_cache=False)).posting
        assert not result.origin_url and not result.origin_ats
        assert result.origin_resolution["state"] == "challenge"
        assert result.origin_lead == {"url": origin.clean_url(url), "state": "unverified", "reason": "challenge"}
        resolver.probe_company.assert_awaited_once()
        resolver.discover_careers.assert_awaited_once()
        resolver.web_search.assert_awaited_once()
        job = MergedJob.from_posting(post)
        assert not job.apply_origin(result)
        assert job.extra["origin_lead"] == result.origin_lead
    finally:
        await resolver.aclose()


def test_ancestor_id_does_not_authorize_unrelated_smartrecruiters_publication():
    post = posting()
    url = "https://jobs.orbitex.example/jobs/284"
    target = "https://jobs.smartrecruiters.com/oneclick-ui/company/OrbitexFrance/publication/999"
    html = f'<main data-job-id="284"><h1>{post.title}</h1><a href="{target}">Apply</a></main>'
    evidence = origin._page_evidence(url, html, posting=post)
    assert evidence.ats != "smartrecruiters" and evidence.apply_url is None and evidence.confidence < 0.98


@pytest.mark.parametrize("command", ["origins", "reresolve"])
async def test_challenge_observation_does_not_revert_concurrent_origin(profile, monkeypatch, command):
    job = stored_job(profile, confidence=0.58)
    newer_url = WORKDAY.replace("Stage_", "Stage-aeronautique_")

    def concurrent_update():
        current = db.get_job(job.id)
        current.origin_url, current.origin_confidence = newer_url, 0.99
        current.extra["origin_resolution"] = {**current.extra["origin_resolution"], "posting_url": newer_url,
                                              "confidence": 0.99, "apply_url": newer_url + "/apply"}
        current.extra["concurrent"] = "keep"
        current.changed = True
        db.save_jobs(profile.id, [current])

    if command == "origins":
        async def resolve(postings, ctx, **kwargs):
            concurrent_update()
            evidence = asdict(origin.OriginEvidence(state="challenge", posting_url=WORKDAY))
            return [origin.Resolution(postings[0].model_copy(update={"origin_resolution": evidence}), reason="challenge")]

        monkeypatch.setattr(origin, "resolve_origins_detailed", resolve)
        await pipeline.reresolve_origins(profile.id, force=True, refetch=False)
    else:
        def handler(request):
            concurrent_update()
            return httpx.Response(403)

        mock_resolver(monkeypatch, handler)
        await origin.reresolve_stored(profile.id)
    current = db.get_job(job.id)
    assert (current.origin_url, current.origin_ats, current.origin_confidence) == (newer_url, "workday", 0.99)
    assert current.extra["origin_resolution"]["apply_url"] == newer_url + "/apply"
    assert current.extra["concurrent"] == "keep"
    assert current.extra["origin_observation"]["state"] == "challenge"
    assert current.extra["origin_observation"]["write_reason"] == "snapshot_conflict"


@pytest.mark.parametrize("command", ["origins", "reresolve"])
async def test_200_without_apply_preserves_verified_apply_and_frontend(profile, monkeypatch, command):
    job = stored_job(profile, confidence=0.58, apply_url=WORKDAY)
    job.extra["origin_resolution"].update(apply_url=WORKDAY + "/apply", frontend="phenom")
    job.changed = True
    db.save_jobs(profile.id, [job])
    mock_resolver(monkeypatch, lambda request: httpx.Response(200, text=f"<main><h1>{job.title}</h1></main>"))
    if command == "origins":
        await pipeline.reresolve_origins(profile.id, force=True, refetch=False)
    else:
        await origin.reresolve_stored(profile.id)
    current = db.get_job(job.id)
    assert current.origin_confidence == 0.98
    assert current.extra["origin_resolution"]["apply_url"] == WORKDAY + "/apply"
    assert current.extra["origin_resolution"]["frontend"] == "phenom"


def test_successfactors_company_key_requires_corroborated_routing_id():
    post = posting()
    routed = "https://career5.successfactors.eu/career?company=Orbitex&jobReqId=123"
    shared = routed.replace("company=Orbitex&", "")
    branded = "https://jobs.orbitex.example/job/stage/123/"
    assets = '<script src="https://rmkcdn.successfactors.com/j2w/site.js"></script>'
    html = f'<main><h1>{post.title}</h1>{assets}<a href="{routed}">Apply</a></main>'
    for url, content in [(routed, ""), (branded, html)]:
        evidence = origin._resolution_data(origin._page_evidence(url, content, posting=post))
        assert (evidence["tenant"], evidence["requisition_id"], evidence["canonical_ok"]) == ("orbitex", "123", True)
    for url, content in [(shared, ""), (branded, f"<main><h1>{post.title}</h1>{assets}</main>"),
                         (routed.replace("Orbitex", "Other"), "")]:
        evidence = origin._resolution_data(origin._page_evidence(url, content, posting=post))
        assert (evidence["ats"], evidence["tenant"], evidence["requisition_id"], evidence["canonical_ok"]) == (
            "successfactors", None, "123", False)


@pytest.mark.parametrize("suffix,expected", [("opaque/jobs/123", None), ("opaque/jobs/999", "requisition_mismatch"),
                                             ("other/jobs/123", "tenant_mismatch"), ("embed/job_app?token=123", "unverified_redirect_tenant")])
async def test_greenhouse_alias_redirect_requires_complete_matching_identity(suffix, expected):
    initial = "https://boards.greenhouse.io/opaque/jobs/123"
    target = f"https://job-boards.greenhouse.io/{suffix}"
    seen = []

    def handler(request):
        seen.append(str(request.url))
        if str(request.url) == initial:
            return httpx.Response(302, headers={"location": target})
        assert expected is None
        return httpx.Response(200)

    resolver = resolver_with(handler)
    try:
        evidence = (await resolver.inspect_posting(posting(), initial)).evidence
        assert evidence.reason == expected
        assert seen == ([initial, target] if expected is None else [initial])
        if expected is None:
            assert (evidence.ats, evidence.tenant, evidence.requisition_id) == ("greenhouse", "opaque", "123")
    finally:
        await resolver.aclose()


async def test_malformed_jsonld_and_candidate_exception_do_not_abort_batch(profile, monkeypatch):
    malformed = stored_job(profile, url=WORKDAY, ats=None)
    parse_error = stored_job(profile, url=WORKDAY.replace("JR284", "JR285"), ats=None)
    valid = stored_job(profile, url=WORKDAY.replace("JR284", "JR286"), ats=None)

    def handler(request):
        ld = {"@type": "JobPosting", "title": {"secret": "fixture"}, "description": [], "datePosted": 4,
              "jobLocation": {"address": {"addressLocality": {"invalid": True}}}}
        html = f'<script type="application/ld+json">{json.dumps(ld)}</script>' if str(request.url) == WORKDAY else ""
        return httpx.Response(200, text=f"<main><h1>{valid.title}</h1>{html}</main>")

    mock_resolver(monkeypatch, handler)
    original = origin.Resolver._inspect_posting

    async def inspect(self, posting, url):
        if url == parse_error.origin_url:
            raise ValueError("fixture-secret full URL")
        return await original(self, posting, url)

    monkeypatch.setattr(origin.Resolver, "_inspect_posting", inspect)
    await origin.reresolve_stored(profile.id)
    assert db.get_job(malformed.id).origin_ats == db.get_job(valid.id).origin_ats == "workday"
    observation = db.get_job(parse_error.id).extra["origin_observation"]
    assert observation["reason"] == "candidate_error" and "fixture-secret" not in str(observation)


async def test_linkedin_equest_403_keeps_original_lead_and_runs_discovery():
    resolver = resolver_with(lambda request: httpx.Response(403))
    resolver.probe_company = AsyncMock(return_value=None)
    resolver.discover_careers = AsyncMock(return_value=None)
    resolver.web_search = AsyncMock(return_value=None)
    try:
        post = posting(apply_url=SHORT, raw={"apply_source": "linkedin_offsite"})
        result = (await origin._resolve_traced(resolver, post, use_cache=False)).posting
        assert (result.origin_url, result.origin_ats) == (None, None)
        assert result.origin_lead == {"url": SHORT, "state": "unverified", "reason": "challenge"}
        resolver.discover_careers.assert_awaited_once()
    finally:
        await resolver.aclose()


async def test_retry_after_expires_under_lock():
    responses = iter([httpx.Response(429, headers={"Retry-After": "120"}), httpx.Response(200)])
    resolver = resolver_with(lambda request: next(responses))
    try:
        host = origin._host(WORKDAY)
        assert (await resolver._get(WORKDAY)).status_code == 429
        assert resolver._deferred_until[host] - origin.time.monotonic() > 119
        assert await resolver._get(WORKDAY) is None
        resolver._deferred_until[host] = origin.time.monotonic() - 1
        assert (await resolver._get(WORKDAY)).status_code == 200
        assert host not in resolver._deferred_until
    finally:
        await resolver.aclose()


async def test_streaming_body_deadline_releases_host_lock(monkeypatch):
    monkeypatch.setattr(origin, "REQUEST_DEADLINE", 0.05)
    closed = asyncio.Event()

    class SlowBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"start"
            await asyncio.Event().wait()

        async def aclose(self):
            closed.set()

    responses = iter([httpx.Response(200, stream=SlowBody()), httpx.Response(200)])
    resolver = resolver_with(lambda request: next(responses))
    try:
        assert await resolver._get(WORKDAY) is None
        assert origin._FETCH_ISSUE.get() == ("timeout", origin._host(WORKDAY))
        assert closed.is_set() and (await resolver._get(WORKDAY)).status_code == 200
    finally:
        await resolver.aclose()


async def test_reresolve_bounds_and_deduplicates_attempt_history(profile, monkeypatch):
    job = stored_job(profile, url="https://linkedin.com/jobs/view/88", ats=None)
    for index in range(14):
        job.sightings.append(Sighting("fixture", str(index), f"https://board{index // 2 if index < 4 else index}.linkedin.com/jobs/view/{index}"))
    job.changed = True
    db.save_jobs(profile.id, [job])
    mock_resolver(monkeypatch, lambda request: pytest.fail("boards must not be fetched"))
    await origin.reresolve_stored(profile.id)
    observation = db.get_job(job.id).extra["origin_observation"]
    pairs = {(entry["host"], entry["reason"]) for entry in observation["attempts"]}
    assert len(pairs) == len(observation["attempts"]) == 10
    assert observation["attempts_omitted"] == 6


def test_reresolve_reports_stored_vendor_not_challenge_in_dry_run(profile, monkeypatch):
    job = stored_job(profile)
    mock_resolver(monkeypatch, lambda request: httpx.Response(403))
    result = CliRunner().invoke(cli.app, ["reresolve", str(profile.id), "--dry-run", "--explain", "1"])
    assert result.exit_code == 0, result.exception
    observed, stored = result.output.split("STORED origin_ats\n")
    assert "workday → challenge: 1" in observed
    assert "workday → workday: 1" in stored
    assert "orbitex.wd3.myworkdayjobs.com: challenge" in observed and "orbitex.wd3.myworkdayjobs.com: challenge" in stored
    assert "downgrades of a known vendor: 0" in stored
    assert "origin_observation" not in db.get_job(job.id).extra


def test_reresolve_downgrade_guard_aborts_before_any_write(profile):
    from copy import deepcopy

    job = stored_job(profile)
    before = db.get_job(job.id)
    after = deepcopy(before)
    after.origin_ats = None
    after.extra["origin_observation"] = {"state": "unresolved"}
    with pytest.raises(ValueError, match="downgrades of a known vendor: 1"):
        db.save_origin_updates([(before, after)], max_downgrades=0)
    assert db.get_job(job.id).extra == before.extra
    assert db.get_job(job.id).origin_ats == "workday"
