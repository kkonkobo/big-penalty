import httpx
import pytest
from typer.testing import CliRunner

from jobhunt import cli, db, origin
from jobhunt.dedupe import MergedJob
from test_origin_direct import posting, resolver_with


TITLE = "Stage ingénieur systèmes embarqués"


async def resolve(handler, tmp_path, **kwargs):
    def offline(request):
        assert request.method in {"GET", "HEAD"}
        return handler(request)

    resolver = resolver_with(offline)
    resolver.cache = origin.DiskCache(tmp_path)
    try:
        post = posting(description="Annonce de test", **kwargs)
        result = await origin._resolve_traced(resolver, post)
        cached = await origin._resolve_traced(resolver, post)
        assert cached.cached and cached.posting.origin_resolution == result.posting.origin_resolution
        return result.posting
    finally:
        await resolver.aclose()


async def test_equest_phenom_workday(tmp_path):
    front = "https://jobs.orbitex.example/fr/fr/job/JR284/Stage"
    apply_url = "https://orbitex.wd3.myworkdayjobs.com/fr-FR/External/job/Toulouse/Stage-systmes_JR284/apply"

    def handler(request):
        if request.url.host == "ars2.equest.com":
            return httpx.Response(302, headers={"location": front + "?utm_source=test"})
        assert request.url.host == "jobs.orbitex.example"
        return httpx.Response(200, text=f'''<script src="https://cdn.phenompeople.com/site.js"></script>
          <main><h1>{TITLE}</h1></main>
          <script>window.jobs = {{"current":{{"jobId":"JR284","applyUrl":"{apply_url}"}},
            "related":[{{"jobId":"JR285","applyUrl":"{apply_url.replace('JR284', 'JR285')}"}}]}};</script>''')

    result = await resolve(handler, tmp_path, apply_url="https://ars2.equest.com/?response_id=fixture")
    assert (result.origin_url, result.origin_ats) == (front, "workday")
    assert result.origin_resolution == {
        "state": "resolved", "confidence": 0.98, "ats": "workday", "frontend": "phenom",
        "posting_url": front, "apply_url": apply_url, "tenant": "orbitex", "requisition_id": "JR284", "site": "External",
        "canonical_ok": True,
    }


@pytest.mark.parametrize("url,html,ats,req", [
    ("https://emploi.orbitex.example/offre-de-emploi/emploi-stage_42123.aspx",
     '<a href="/Account/Login">Connexion</a><script src="/runclientscript.axd"></script>', "talentsoft", "42123"),
    ("https://orbitex-recrutement.profils.org/job/job-stage_11089.aspx?LCID=1040",
     '<a href="/Account/Login">Connexion</a><script src="/runclientscript/bundle.js"></script>', "talentsoft", "11089"),
    ("https://jobsearch.orbitex.example/job/Stage-Toulouse/14461/",
     '<script src="https://rmkcdn.successfactors.com/j2w/min.js"></script><a href="/talentcommunity/apply/14461">Apply now</a>',
     "successfactors", "14461"),
    ("https://careers.orbitex.example/jobs/85851-avionics-intern",
     '<link rel="stylesheet" href="https://assets.teamtailor-cdn.com/careersite.css">'
     '<form action="/jobs/85851/applications/new"></form>', "teamtailor", "85851"),
    ("https://recrutement.orbitex.example/fr/annonce/46518-stage",
     '<script>window.api = "https://api.digitalrecruiters.com/careers/v1";</script>', "digitalrecruiters", "46518"),
    ("https://emploi.orbitex.example/20235/1/stage",
     '<script src="/assets/gestmax-template.js"></script>', "gestmax", "20235"),
    ("https://jobs.orbitex.example/jobs/743999-stage",
     '<a data-job-id="743999" href="https://jobs.smartrecruiters.com/oneclick-ui/company/Orbitex/publication/pub-84">Postuler</a>',
     "smartrecruiters", "pub-84"),
])
async def test_branded_vacancy(url, html, ats, req, tmp_path):
    def handler(request):
        assert str(request.url) == url
        return httpx.Response(200, text=f"<main><h1>{TITLE}</h1><p>Orbitex Aerospace</p>{html}</main>")

    result = await resolve(handler, tmp_path, apply_url=url)
    assert (result.origin_url, result.origin_ats) == (url, ats)
    assert result.origin_resolution["requisition_id"] == req
    assert result.origin_resolution["canonical_ok"] is (ats != "successfactors")


@pytest.mark.parametrize("branded", [False, True])
async def test_greenhouse_shortlink(tmp_path, branded):
    short = "https://grnh.se/fixture"
    board = "https://job-boards.greenhouse.io/orbitex/jobs/4567"
    final = "https://orbitex.example/careers/search/4567?gh_jid=4567" if branded else board

    def handler(request):
        if str(request.url) == short:
            return httpx.Response(302, headers={"location": final})
        if request.url.host == "boards-api.greenhouse.io":
            assert branded and request.url.path == "/v1/boards/orbitex/jobs/4567"
            return httpx.Response(200, json={"id": 4567, "title": TITLE})
        assert str(request.url) == final
        return httpx.Response(200, text="<div id='app'></div>")

    result = await resolve(handler, tmp_path, apply_url=short)
    assert (result.origin_url, result.origin_ats) == (final, "greenhouse")
    assert (result.origin_resolution["tenant"], result.origin_resolution["requisition_id"]) == ("orbitex", "4567")


@pytest.mark.parametrize("status,html,state", [
    (200, '<div id="app"></div>', "unresolved"),
    (410, '<h1>Ce poste n’est plus disponible</h1>', "expired"),
    (200, '<form id="challenge-form"></form>', "challenge"),
    (200, '<h1>Toutes les offres</h1>', "intermediate_board"),
])
async def test_direct_uses_final_redirect_for_negative_outcomes(tmp_path, status, html, state):
    source = "https://jobs.orbitex.example/jobs/88"
    final = "https://jobs.orbitex.example/" if state == "intermediate_board" else "https://jobs.orbitex.example/jobs/99"

    def handler(request):
        if str(request.url) == source:
            return httpx.Response(302, headers={"location": final})
        assert str(request.url) == final
        return httpx.Response(status, text=html)

    result = await resolve(handler, tmp_path, apply_url=source)
    assert result.origin_url is None and result.origin_ats is None
    assert result.origin_resolution["state"] == state and result.origin_resolution["posting_url"] == final


@pytest.mark.parametrize("url,html", [
    ("https://notdigitalrecruiters.com/fr/annonce/1-test", ""),
    ("https://jobs.orbitex.example/job/Stage/22/?source=https://x.myworkdayjobs.com", '<script>var SAPBTP = true;</script>'),
    ("https://jobs.orbitex.example/fr/annonce/22-stage", '<script>let api="https://app.digitalrecruiters.com";</script>'),
    ("https://orbitex.cegid.com/jobs/22.aspx", '<a href="/Account/Login">Connexion</a>'),
    ("https://orbitex.profils.org/job/job-stage_11089.aspx", '<a href="/Account/Login">Connexion</a>'),
    ("https://careers.orbitex.example/jobs/33-stage", '<script src="https://assets.teamtailor-cdn.com/x.js"></script>'
     '<aside><form action="/jobs/33/applications/new"></form></aside>'),
    ("https://careers.orbitex.example/jobs/55", '<footer><a href="https://jobs.smartrecruiters.com/oneclick-ui/company/Other/publication/4">Apply</a></footer>'),
    ("https://careers.orbitex.example/job/JR1/Stage/", '<script src="https://cdn.phenompeople.com/a.js"></script>'
     '<script>window.job = {"jobId":"JR1","applyUrl":"https://orbitex.wd3.myworkdayjobs.com/External/job/Paris/Stage_JR2"};</script>'),
])
def test_unscoped_or_incomplete_signals_do_not_classify(url, html):
    assert origin.classify_ats(url, html) is None


def test_reresolve_dry_run_and_write(profile, monkeypatch):
    url = "https://careers.orbitex.example/jobs/85851-avionics-intern"
    job = MergedJob.from_posting(posting(apply_url=url, origin_url=url, origin_ats="careers_site", origin_confidence=0.8))
    job.extra["untouched"] = {"example": True}
    db.save_jobs(profile.id, [job])

    async def fetch(self, requested, **kwargs):
        assert requested == url
        return httpx.Response(200, request=httpx.Request("GET", url), text=f'''<main><h1>{TITLE}</h1>
          <script src="https://assets.teamtailor-cdn.com/site.js"></script>
          <a href="/jobs/85851/applications/new">Apply</a></main>''')

    monkeypatch.setattr(origin.Resolver, "_get", fetch)
    runner = CliRunner()
    dry = runner.invoke(cli.app, ["reresolve", str(profile.id), "--dry-run"])
    assert dry.exit_code == 0, dry.exception
    assert dry.output == ("OBSERVED\ncareers_site → teamtailor: 1\nSTORED origin_ats\ncareers_site → teamtailor: 1\n"
                          "downgrades of a known vendor: 0\n")
    before = db.load_jobs(profile.id)[0]
    assert before.origin_ats == "careers_site" and "origin_resolution" not in before.extra
    write = runner.invoke(cli.app, ["reresolve"])
    assert write.exit_code == 0, write.exception
    after = db.load_jobs(profile.id)[0]
    assert (after.id, after.origin_ats, after.extra["untouched"]) == (before.id, "teamtailor", {"example": True})
    assert after.extra["origin_resolution"]["requisition_id"] == "85851"
