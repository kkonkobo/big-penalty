"""Autonomous recipient policy: abort what is not authorized and keep going, refuse only on evidence of another posting.

Every page, host, employer and candidate here is fictional. ATS hosts use the reserved `.test` TLD (registered as a
synthetic ATS for the test) and are mapped to the loopback fixture server at the browser's transport boundary: nothing
resolves or contacts a real site.
"""

from __future__ import annotations

import json
import re
from contextlib import closing
from urllib.parse import urlsplit

import pytest
from apply_cv_helpers import make_app, make_job
from apply_fakes import FixtureServer, ScriptedPlanner, act, fast, server  # noqa: F401
from cv_fakes import save_confirmed

from jobhunt import db, origin as origin_module
from jobhunt.apply import service, store
from jobhunt.apply.apply_entry import Probe
from jobhunt.apply.autonomous import Audience
from jobhunt.apply.types import StepPlan
from jobhunt.cv import service as cvs

CAREERS = "https://careers.example.test"
ATS = "https://exampleco.ats-fixture.test"
OTHER_ATS = "https://otherco.ats-fixture.test"
HOSTS = {"careers.example.test": "/site", "exampleco.ats-fixture.test": "/ats", "otherco.ats-fixture.test": "/other"}


@pytest.fixture
def synthetic_ats(monkeypatch):
    """Recognize `*.ats-fixture.test` as an ATS whose tenant is the host and requisition the /jobs/<n> number."""
    monkeypatch.setattr(origin_module, "_ATS_RULES",
                        [("teamtailor", re.compile(r"\.ats-fixture\.test$", re.I), None), *origin_module._ATS_RULES])


@pytest.fixture
def mapped(server, monkeypatch, synthetic_ats):  # noqa: F811
    """Serve the synthetic HTTPS hosts from the loopback fixture server (path prefix per host)."""
    from patchright.async_api import Route

    original = Route.fetch

    async def loopback_fetch(route, **kwargs):
        parts = urlsplit(route.request.url)
        prefix = HOSTS.get(parts.hostname or "")
        if prefix is None:
            assert parts.hostname in {"127.0.0.1", "localhost"}, parts.hostname  # never a real host
            return await original(route, **kwargs)
        query = f"?{parts.query}" if parts.query else ""
        return await original(route, url=server.base + prefix + parts.path + query, **kwargs)

    monkeypatch.setattr(Route, "fetch", loopback_fetch)
    monkeypatch.setattr(service.get_settings(), "apply_autonomous", True)
    return server


def reached(receiver):
    """Paths the third-party fixture actually served to the page (a local port probe of `/` is not page traffic)."""
    return [path for path in receiver.requests if path != "/"] + [post["path"] for post in receiver.posts]


def form_page(action, extra=""):
    return (f'<title>Ingénieur systèmes embarqués</title><main><h1>Ingénieur systèmes embarqués</h1>'
            f'<form action="{action}" method="post">'
            '<label for="name">Prénom</label><input id="name" name="first_name">'
            '<label for="cv">CV</label><input id="cv" type="file" name="cv"><button>Envoyer</button></form></main>' + extra)


def form(snapshot):
    if not any(field.kind == "file" for field in snapshot.fields):
        return StepPlan(page_kind="job_page")
    return StepPlan(page_kind="form", actions=[
        act(snapshot, "Prénom", "fill", "first_name"),
        act(snapshot, "CV", "upload_cv", kind="file"),
        act(snapshot, "Envoyer", "click", role="submit"),
    ])


def approved(profile, tmp_path, url, **kwargs):
    master = save_confirmed(profile.id)
    job = make_job(profile, url=url, title="Ingénieur systèmes embarqués", company="Société Exemple", **kwargs)
    app = make_app(profile, job, tmp_path, master_hash=master.content_hash)
    cvs.approve(app.id, app.current_rev)
    return job, app


async def send(app):
    return await service.run(app.id, mode="submit", trigger="user", headless=True, planner=ScriptedPlanner(form))


def binding(run_id):
    with db.connect() as conn:
        return json.loads(conn.execute("SELECT binding_json FROM apply_reservations WHERE run_id=?", (run_id,)).fetchone()[0])


# ---------------------------------------------------------------- rule 1: abort, never close, for subresources


async def test_third_party_traffic_at_load_is_aborted_and_the_run_continues(server, profile, tmp_path, fast, monkeypatch):  # noqa: F811
    """Formerly recipient_refused_other_navigation / _other_write / _redirect at step 0: a tracker iframe, beacon and a
    redirected pixel write on the posting page closed the window before any field was filled."""
    monkeypatch.setattr(service.get_settings(), "apply_autonomous", True)
    with closing(FixtureServer()) as receiver:
        server.redirects["/track"] = receiver.url("pixel")
        server.html["/jobs/100"] = form_page("/jobs/100", f"""
            <iframe src="{receiver.url('consent')}"></iframe>
            <script>
              navigator.sendBeacon({json.dumps(receiver.url('beacon'))}, 'synthetic');
              fetch({json.dumps(receiver.url('collect'))}, {{method: 'POST', body: 'synthetic', mode: 'no-cors'}}).catch(() => {{}});
              fetch('/track', {{method: 'POST', body: 'visit'}}).catch(() => {{}});
            </script>""")
        _, app = approved(profile, tmp_path, server.url("jobs/100"))
        result = await send(app)
        assert result.status == "submitted", (result.message, result.result.diagnostic_code)
        assert not reached(receiver)
        assert server.posts_to("/jobs/100") and b"first_name=" in server.posts_to("/jobs/100")[0]["body"]
        trace = result.result.dispatch_diagnostics
        assert trace.aborted_hosts == ["127.0.0.1"] and trace.aborted_requests >= 3


async def test_third_party_beacon_after_fill_is_aborted_and_the_send_succeeds(server, profile, tmp_path, fast, monkeypatch):  # noqa: F811
    monkeypatch.setattr(service.get_settings(), "apply_autonomous", True)
    with closing(FixtureServer()) as receiver:
        server.html["/jobs/100"] = form_page("/jobs/100", f"""<script>
            document.querySelector('#name').addEventListener('input', event => {{
              navigator.sendBeacon({json.dumps(receiver.url('beacon'))}, event.target.value);
              fetch({json.dumps(receiver.url('collect'))}, {{method: 'POST', body: event.target.value, mode: 'no-cors'}}).catch(() => {{}});
            }});
            document.querySelector('#cv').addEventListener('change', event =>
              fetch({json.dumps(receiver.url('cv'))}, {{method: 'POST', body: event.target.files[0], mode: 'no-cors'}}).catch(() => {{}}));
        </script>""")
        _, app = approved(profile, tmp_path, server.url("jobs/100"))
        result = await send(app)
        assert result.status == "submitted", (result.message, result.result.diagnostic_code)
        assert not receiver.sent and not reached(receiver)  # the candidate's data never reached the third party
        assert [post["path"] for post in server.posts] == ["/jobs/100"]
        assert result.result.dispatch_diagnostics.aborted_hosts == ["127.0.0.1"]
        assert store.outstanding_attempt(app.id) is None


# ---------------------------------------------------------------- rule 2: before any input, navigation and same-site writes


@pytest.mark.parametrize("hop", ["direct", "server_redirect"])
async def test_same_site_write_and_script_navigation_before_input_are_allowed(hop, server, profile, tmp_path, fast, monkeypatch):  # noqa: F811
    """Formerly recipient_refused_same_site_write / _same_site_navigation: a session POST and a script redirect to the
    application page of the same posting, both before any field was filled (optionally through a server redirect)."""
    monkeypatch.setattr(service.get_settings(), "apply_autonomous", True)
    server.post_json["/api/session"] = {"ok": True}
    server.redirects["/go"] = "/jobs/100/apply"
    server.html["/jobs/100"] = ('<title>Ingénieur systèmes embarqués</title><h1>Offre</h1><script>'
                                "fetch('/api/session', {method: 'POST', body: '{}'})"
                                f".finally(() => location.replace('{'/go' if hop == 'server_redirect' else '/jobs/100/apply'}'));</script>")
    server.html["/jobs/100/apply"] = form_page("/jobs/100/apply")
    _, app = approved(profile, tmp_path, server.url("jobs/100"))
    result = await send(app)
    assert result.status == "submitted", (result.message, result.result.diagnostic_code)
    assert [post["path"] for post in server.posts] == ["/api/session", "/jobs/100/apply"]


async def test_redirect_to_another_posting_on_the_same_site_is_still_refused(server, profile, tmp_path, fast, monkeypatch):  # noqa: F811
    monkeypatch.setattr(service.get_settings(), "apply_autonomous", True)
    server.html["/jobs/100"] = ('<title>Offre</title><h1>Offre</h1>'
                                "<script>location.replace('/jobs/200/apply');</script>")
    server.html["/jobs/200/apply"] = form_page("/jobs/200/apply")
    _, app = approved(profile, tmp_path, server.url("jobs/100"))
    result = await send(app)
    assert result.status == "needs_human" and result.result.diagnostic_code == "recipient_refused_same_site_navigation"
    assert not server.posts and "/jobs/200/apply" not in server.requests


# ---------------------------------------------------------------- rule 3: career site -> recognized ATS, bound before input


@pytest.mark.parametrize("how", ["initial_redirect", "script_then_redirect"])
async def test_career_site_redirect_to_recognized_ats_is_bound_by_continuity(how, mapped, profile, tmp_path, fast):
    """Formerly recipient_refused_redirect: the career page redirects to the employer's ATS posting, either directly or
    from a script after the page loaded (through a server redirect, followed hop by hop by the guard)."""
    if how == "initial_redirect":
        mapped.redirects["/site/jobs/100"] = f"{ATS}/jobs/4567-ingenieur"
    else:
        mapped.html["/site/jobs/100"] = "<title>Offre</title><h1>Offre</h1><script>location.replace('/go');</script>"
        mapped.redirects["/site/go"] = f"{ATS}/jobs/4567-ingenieur"
    mapped.html["/ats/jobs/4567-ingenieur"] = form_page("/jobs/4567-ingenieur")
    _, app = approved(profile, tmp_path, f"{CAREERS}/jobs/100")
    result = await send(app)
    assert result.status == "submitted", (result.message, result.result.diagnostic_code)
    assert [post["path"] for post in mapped.posts] == ["/ats/jobs/4567-ingenieur"]
    record = binding(result.id)
    assert (record["landing_host"], record["landing_binding"]) == ("exampleco.ats-fixture.test", "continuity")
    assert record["requisition_id"] == "4567"


async def test_apply_link_from_career_site_to_ats_with_its_own_numbering_is_followed(mapped, profile, tmp_path, fast):
    """Formerly apply_target_unverified: the career site's URL number (100) is not the ATS requisition (4567)."""
    mapped.html["/site/jobs/100"] = (f'<title>Ingénieur</title><main><h1>Ingénieur systèmes embarqués</h1>'
                                     f'<a href="{ATS}/jobs/4567-ingenieur/apply">Postuler</a></main>')
    mapped.html["/ats/jobs/4567-ingenieur/apply"] = form_page("/jobs/4567-ingenieur/apply")
    _, app = approved(profile, tmp_path, f"{CAREERS}/jobs/100")
    result = await send(app)
    assert result.status == "submitted", (result.message, result.result.diagnostic_code)
    assert [post["path"] for post in mapped.posts] == ["/ats/jobs/4567-ingenieur/apply"]
    assert binding(result.id)["landing_host"] == "exampleco.ats-fixture.test"


@pytest.mark.parametrize("how", ["redirect", "script"])
async def test_navigation_to_another_employers_posting_is_still_refused(how, mapped, profile, tmp_path, fast):
    target = f"{OTHER_ATS}/jobs/4567-ingenieur"
    if how == "redirect":
        mapped.redirects["/ats/jobs/4567-ingenieur"] = target
    else:
        mapped.html["/ats/jobs/4567-ingenieur"] = f"<title>Offre</title><script>location.replace({json.dumps(target)});</script>"
    mapped.html["/other/jobs/4567-ingenieur"] = form_page("/jobs/4567-ingenieur")
    _, app = approved(profile, tmp_path, f"{ATS}/jobs/4567-ingenieur")
    result = await send(app)
    assert result.status == "needs_human", result.message
    assert result.result.diagnostic_code.startswith("recipient_refused_")
    assert not mapped.posts and "/other/jobs/4567-ingenieur" not in mapped.requests


async def test_another_requisition_on_the_same_ats_is_still_refused(mapped, profile, tmp_path, fast):
    mapped.redirects["/ats/jobs/4567-ingenieur"] = f"{ATS}/jobs/9999-autre-poste"
    mapped.html["/ats/jobs/9999-autre-poste"] = form_page("/jobs/9999-autre-poste")
    _, app = approved(profile, tmp_path, f"{ATS}/jobs/4567-ingenieur")
    result = await send(app)
    assert result.status == "needs_human" and result.result.diagnostic_code == "recipient_refused_redirect"
    assert not mapped.posts and "/ats/jobs/9999-autre-poste" not in mapped.requests


async def test_unrecognized_foreign_site_is_never_bound(mapped, profile, tmp_path, fast):
    """No evidence of the employer: candidate data never goes to an unverified third-party site."""
    HOSTS["jobs.unknown-board.test"] = "/unknown"
    try:
        mapped.redirects["/site/jobs/100"] = "https://jobs.unknown-board.test/jobs/100"
        mapped.html["/unknown/jobs/100"] = form_page("/jobs/100")
        _, app = approved(profile, tmp_path, f"{CAREERS}/jobs/100")
        result = await send(app)
    finally:
        HOSTS.pop("jobs.unknown-board.test")
    assert result.status != "submitted" and not mapped.posts
    assert "/unknown/jobs/100" not in mapped.requests


# ---------------------------------------------------------------- rules 2-4: the policy decisions themselves


def test_navigation_verdicts_before_and_after_input(synthetic_ats):
    audience = Audience(f"{CAREERS}/jobs/100", (), lambda: None)
    # Before any input: same site and recognized ATS are allowed, an unknown site is aborted (never bound).
    assert audience.navigation_verdict(f"{CAREERS}/jobs/100/apply") == "allow"
    assert audience.navigation_verdict(f"{ATS}/jobs/4567-ingenieur") == "allow"
    assert audience.navigation_verdict("https://tracker.example.test/landing") == "abort"
    assert audience.navigation_verdict(f"{ATS}/jobs/4567", write=True) == "abort"
    assert audience.pre_data_write(f"{CAREERS}/api/session") and not audience.pre_data_write("https://tracker.example.test/c")
    # Evidence of another posting on the same site is refused.
    assert audience.navigation_verdict(f"{CAREERS}/jobs/200/apply") == "refuse"
    # Bound to the ATS landing: another tenant or requisition on that ATS is refused.
    audience.bind_recipient(f"{ATS}/jobs/4567-ingenieur")
    assert audience.navigation_verdict(f"{OTHER_ATS}/jobs/4567-ingenieur") == "refuse"
    assert audience.navigation_verdict(f"{ATS}/jobs/9999-autre") == "refuse"
    assert audience.landing_allowed(f"{ATS}/jobs/4567-ingenieur/apply")
    # After input: nothing new is allowed, every unadmitted request is aborted, evidence still refuses.
    audience.filled = True
    assert audience.navigation_verdict(f"{ATS}/jobs/4567-ingenieur/apply") == "abort"
    assert not audience.pre_data_write(f"{ATS}/api/autosave")
    assert not audience.landing_allowed(f"{ATS}/jobs/4567-ingenieur/apply")
    assert audience.navigation_verdict(f"{OTHER_ATS}/jobs/4567-ingenieur") == "refuse"


def test_verified_origin_identity_contradicted_by_an_ats_landing_is_refused(synthetic_ats):
    audience = Audience(f"{CAREERS}/jobs/100", (), lambda: None)
    audience.expected_identity = "teamtailor", ("exampleco.ats-fixture.test", "4567")
    assert audience.navigation_verdict(f"{ATS}/jobs/4567-ingenieur") == "allow"
    assert audience.navigation_verdict(f"{ATS}/jobs/4568-ingenieur") == "refuse"
    assert audience.navigation_verdict(f"{OTHER_ATS}/jobs/4567-ingenieur") == "refuse"


def test_writes_after_input_reach_only_the_bound_recipient_and_its_ats(synthetic_ats, monkeypatch):
    audience = Audience(f"{ATS}/jobs/4567-ingenieur", (), lambda: None)
    assert audience.ats_infrastructure("https://uploads.ats-fixture.test/cv")  # same ATS vendor, other host
    assert not audience.ats_infrastructure("https://uploads.example.test/cv")
    plain = Audience(f"{CAREERS}/jobs/100", (), lambda: None)
    assert not plain.ats_infrastructure(f"{ATS}/upload")  # a career site has no ATS infrastructure


def test_probe_accepts_an_ats_reached_from_the_posting_unless_a_requisition_contradicts_it(synthetic_ats):
    probe = Probe(f"{CAREERS}/jobs/100", None, [])
    assert not probe.navigation(f"{ATS}/jobs/4567-ingenieur/apply")  # dry inspection keeps the strict rule
    probe.lenient = True
    assert probe.navigation(f"{ATS}/jobs/4567-ingenieur/apply")
    verified = Probe(f"{CAREERS}/jobs/100", "4567", [])
    verified.lenient = True
    assert verified.navigation(f"{ATS}/jobs/4567-ingenieur/apply")
    assert not verified.navigation(f"{ATS}/jobs/9999-autre/apply")
