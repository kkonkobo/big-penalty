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


TRACKER_PAGE = """<script>
        const input = document.querySelector('#name');
        input.addEventListener('input', () => {
          fetch('https://tracker.thirdparty.test/collect', {method: 'POST', body: input.value}).catch(() => {});
        });</script>"""


async def test_third_party_writes_are_not_filtered_by_default_and_the_run_submits_once(core, profile, tmp_path, fast):
    # (a bot check, a consent service or an upload store a submit depends on is such a write)
    core.html["/site/jobs/100"] = form_page("/jobs/100", TRACKER_PAGE)
    _, app = approved(profile, tmp_path, f"{CAREERS}/jobs/100")
    result = await send(app)
    assert result.status == "submitted", (result.message, result.result.diagnostic_code)
    assert paths(core, "/track")
    assert paths(core, "/site") == ["/site/jobs/100"]
    assert not result.result.dispatch_diagnostics.aborted_hosts


async def test_write_to_a_tracker_after_data_entry_is_aborted_and_the_run_still_submits(core, profile, tmp_path, fast, monkeypatch):
    monkeypatch.setattr(service.get_settings(), "apply_network_filter", True)
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


def basic_fields_page(action, extra=""):
    return ('<title>Ingénieur</title><main><h1>Ingénieur systèmes embarqués</h1>'
            f'<form action="{action}" method="post">'
            '<label for="n">Votre nom *</label><input id="n" name="field_surname" required>'
            '<label for="p">Votre prénom *</label><input id="p" name="field_name" required>'
            '<label for="e">Votre email *</label><input id="e" type="email" name="field_email" required>'
            '<label for="cv">CV</label><input id="cv" type="file" name="cv">'
            '<label><input type="checkbox" name="field_accept" required> Validation</label>'
            '<button>Envoyer</button></form></main>' + extra)


async def test_basic_identity_fields_are_filled_by_code_even_when_the_plan_leaves_them_alone(core, profile, tmp_path, fast):
    core.html["/site/jobs/100"] = basic_fields_page("/jobs/100")
    _, app = approved(profile, tmp_path, f"{CAREERS}/jobs/100")

    def plan(snapshot):  # a low-effort plan: only the CV and the click
        if not any(field.kind == "file" for field in snapshot.fields):
            return StepPlan(page_kind="job_page")
        return StepPlan(page_kind="form", actions=[act(snapshot, "CV", "upload_cv", kind="file"),
                                                   act(snapshot, "Envoyer", "click", role="submit")])

    result = await service.run(app.id, mode="submit", trigger="user", headless=True, planner=ScriptedPlanner(plan))
    assert result.status == "submitted", (result.message, result.result.diagnostic_code, result.result.questions)
    body = core.posts_to("/site/jobs/100")[0]["body"]
    for name in (b"field_surname=", b"field_name=", b"field_email=", b"field_accept="):
        assert name in body
    assert b"field_surname=&" not in body and b"field_name=&" not in body and b"field_email=&" not in body


async def test_a_job_alert_box_of_the_ad_is_not_a_question_of_the_application(core, profile, tmp_path, fast):
    core.html["/site/jobs/100"] = ('<title>Ingénieur</title><main><h1>Ingénieur systèmes embarqués</h1>'
                                   '<aside><h2>Get notified for similar jobs</h2>'
                                   '<input type="email" required aria-label="Enter Email address"></aside></main>')
    _, app = approved(profile, tmp_path, f"{CAREERS}/jobs/100")
    result = await service.run(app.id, mode="submit", trigger="user", headless=True,
                               planner=ScriptedPlanner(lambda snapshot: StepPlan(page_kind="job_page")))
    assert not any("mail" in question.label for question in result.result.questions)
    assert not core.posts


@pytest.mark.parametrize(("answer", "sent"), [(None, True), ("form.html", False)])
async def test_a_send_click_the_page_interrupts_is_judged_by_the_page_that_follows(core, profile, tmp_path, fast, monkeypatch, answer, sent):
    """The receipt page loading under the send click makes the click itself fail: the page that follows says whether the
    application was received. Nothing is clicked again either way."""
    from jobhunt.apply import engine

    original = engine._Run._click

    async def interrupted(self, target, *, recheck=None):
        final = self.final_awaiting
        await original(self, target, recheck=recheck)
        if final:
            raise engine._Dispatched("TimeoutError")

    monkeypatch.setattr(engine._Run, "_click", interrupted)
    core.html["/site/jobs/100"] = form_page("/jobs/100")
    if answer:
        core.post_pages["/site/jobs/100"] = answer  # the answer is the form again: no receipt
    _, app = approved(profile, tmp_path, f"{CAREERS}/jobs/100")
    result = await send(app)
    assert (result.status == "submitted") is sent, (result.message, result.result.diagnostic_code)
    assert result.result.submit_clicked or sent
    assert paths(core, "/site") == ["/site/jobs/100"]  # one send, whatever the outcome


def test_a_posting_link_keeps_the_parameters_the_cleaner_does_not_know_in_core_mode_only(monkeypatch):
    from jobhunt.apply.destination import clean_url, posting_url

    url = "https://careers.example.test/offre/detail.aspx?idOffre=100&utm_source=annonce&LCID=1036"
    settings = service.get_settings()
    monkeypatch.setattr(settings, "apply_guard_mode", "strict")
    assert posting_url(url) == ""
    monkeypatch.setattr(settings, "apply_guard_mode", "core")
    assert posting_url(url) == "https://careers.example.test/offre/detail.aspx?idOffre=100"  # (tracking is still dropped)
    assert posting_url(url + "&token=" + "a" * 301) == ""
    assert clean_url(url, navigation=True) == ""  # any other link (a landing, a redirect) is judged as before


async def test_two_postings_told_apart_by_a_parameter_the_cleaner_does_not_know_are_each_sent_once(core, profile, tmp_path, fast):
    """`detail.aspx?idOffre=100` and `?idOffre=200` are two postings of one site: neither is left to the supervised mode,
    each is opened with its own parameter, and the second is not taken for the first one sent again."""
    from apply_cv_helpers import make_app, make_job
    from cv_fakes import save_confirmed

    from jobhunt.cv import service as cvs

    core.html["/site/offre/detail.aspx"] = form_page("/offre/postuler")
    master, apps = save_confirmed(profile.id), []
    for ref, title in (("100", "Ingénieur systèmes embarqués"), ("200", "Technicienne d'essais mécaniques")):
        job = make_job(profile, url=f"{CAREERS}/offre/detail.aspx?idOffre={ref}&utm_source=annonce", title=title,
                       company="Société Exemple")
        assert service._generic_enabled(job)
        (tmp_path / ref).mkdir()
        app = make_app(profile, job, tmp_path / ref, master_hash=master.content_hash)
        cvs.approve(app.id, app.current_rev)
        apps.append(app)
    for app in apps:
        result = await send(app)
        assert result.status == "submitted", (result.message, result.result.diagnostic_code)
    opened = [path for path in core.requests if path.startswith("/site/offre/detail.aspx")]
    assert opened == ["/site/offre/detail.aspx?idOffre=100", "/site/offre/detail.aspx?idOffre=200"]
    assert paths(core, "/site") == ["/site/offre/postuler"] * 2
    again = await send(apps[0])
    assert again.status != "submitted" and len(core.posts) == 2


async def test_a_receipt_shown_on_the_same_page_inside_its_page_wide_form_is_read(core, profile, tmp_path, fast):
    """A one-click application (the account holds the CV): nothing is typed, one form wraps the whole page (its search box
    included) and the answer keeps the address. The "form of the click" is still there, yet the page says the application
    was sent; a screen reader's "page is loaded" alert is not an error banner."""
    core.html["/site/jobs/100"] = (
        '<title>Ingénieur systèmes embarqués</title><form action="/jobs/100" method="post">'
        '<label for="keywords">Mots-clés</label><input id="keywords" name="keywords">'
        '<label for="place">Lieu</label><select id="place" name="place"><option value="">Tous</option><option>Lyon</option></select>'
        '<main><h1>Ingénieur systèmes embarqués</h1><p>Votre CV enregistré sera joint à la candidature.</p>'
        '<button name="apply" value="1">Envoyer ma candidature</button></main></form>')
    core.post_pages["/site/jobs/100"] = "receipt_one_form.html"

    def one_click(snapshot):
        if not any(field.label == "Envoyer ma candidature" for field in snapshot.fields):
            return StepPlan(page_kind="other")
        return StepPlan(page_kind="form", actions=[act(snapshot, "Envoyer ma candidature", "click", role="submit")])

    _, app = approved(profile, tmp_path, f"{CAREERS}/jobs/100")
    result = await service.run(app.id, mode="submit", trigger="user", headless=True, planner=ScriptedPlanner(one_click))
    assert result.status == "submitted", (result.message, result.result.diagnostic_code)
    assert paths(core, "/site") == ["/site/jobs/100"] and store.outstanding_attempt(app.id) is None


async def test_what_an_error_banner_says_reaches_the_planner_even_far_down_the_page(core, profile, tmp_path, fast):
    """The site refuses the form and names the missing list in a banner that stands below the part of the page the planner
    reads; the list itself is not marked required and shows its "nothing chosen" option. The planner is told what the
    banner says, picks the option, and the form is sent."""
    core.html["/site/jobs/100"] = (
        '<title>Ingénieur systèmes embarqués</title><main><h1>Ingénieur systèmes embarqués</h1>'
        '<form action="/jobs/100" method="post" enctype="multipart/form-data"><label for="name">Prénom</label>'
        '<input id="name" name="first_name"><label for="level">Niveau d\'études</label><select id="level" name="level">'
        '<option value="0">Selezionare un valore</option><option value="5">Bac+5</option></select>'
        '<label for="cv">CV</label><input id="cv" type="file" name="cv"><button>Envoyer</button></form></main>')
    core.post_pages["/site/jobs/100"] = "refused_banner_far_below.html"
    seen = []

    def plan(snapshot):
        told = "THE PAGE SHOWS THIS MESSAGE" in snapshot.text and "Niveau d'études" in snapshot.text[:600]
        seen.append(told)
        level = [act(snapshot, "Niveau", "select", value="Bac+5")] if told else []
        return StepPlan(page_kind="form", actions=[act(snapshot, "Prénom", "fill", "first_name"),
                                                   act(snapshot, "CV", "upload_cv", kind="file"), *level,
                                                   act(snapshot, "Envoyer", "click", role="submit")])

    _, app = approved(profile, tmp_path, f"{CAREERS}/jobs/100")
    result = await service.run(app.id, mode="submit", trigger="user", headless=True, planner=ScriptedPlanner(plan))
    assert result.status == "submitted", (result.message, result.result.diagnostic_code, seen)
    assert seen[0] is False and True in seen
    assert paths(core, "/site") == ["/site/jobs/100", "/site/jobs/100/retry"]
    assert b'name="level"\r\n\r\n5' in core.posts_to("/site/jobs/100/retry")[0]["body"]


async def test_what_a_floating_notice_says_about_a_refused_form_reaches_the_planner(core, profile, tmp_path, fast):
    """The site sends the form back under an "error" title and names the missing list in a notice floating over the page (a
    bare block with a close button, nothing marking it as an error). The planner is told what it says."""
    core.html["/site/jobs/100"] = (
        '<title>Ingénieur systèmes embarqués</title><main><h1>Ingénieur systèmes embarqués</h1>'
        '<form action="/jobs/100" method="post" enctype="multipart/form-data"><label for="name">Prénom</label>'
        '<input id="name" name="first_name"><label for="level">Niveau d\'études</label><select id="level" name="level">'
        '<option value="0">Selezionare un valore</option><option value="5">Bac+5</option></select>'
        '<label for="cv">CV</label><input id="cv" type="file" name="cv"><button>Envoyer</button></form></main>')
    core.post_pages["/site/jobs/100"] = "refused_floating_notice.html"
    seen = []

    def plan(snapshot):
        told = "THE PAGE SHOWS THIS MESSAGE" in snapshot.text and "Niveau d'études" in snapshot.text[:600]
        seen.append(told)
        level = [act(snapshot, "Niveau", "select", value="Bac+5")] if told else []
        return StepPlan(page_kind="form", actions=[act(snapshot, "Prénom", "fill", "first_name"),
                                                   act(snapshot, "CV", "upload_cv", kind="file"), *level,
                                                   act(snapshot, "Envoyer", "click", role="submit")])

    _, app = approved(profile, tmp_path, f"{CAREERS}/jobs/100")
    result = await service.run(app.id, mode="submit", trigger="user", headless=True, planner=ScriptedPlanner(plan))
    assert result.status == "submitted", (result.message, result.result.diagnostic_code, seen)
    assert seen[0] is False and True in seen
    assert paths(core, "/site") == ["/site/jobs/100", "/site/jobs/100/retry"]
    assert b'name="level"\r\n\r\n5' in core.posts_to("/site/jobs/100/retry")[0]["body"]


STEPPED_FORM = """<title>Ingénieur systèmes embarqués</title><main><h1>Ingénieur systèmes embarqués</h1>
<form action="/jobs/100" method="post" enctype="multipart/form-data">
  <section id="s1"><label for="name">Prénom</label><input id="name" name="first_name">
    <label for="cv">CV</label><input id="cv" type="file" name="cv"></section>
  <section id="s2" hidden><fieldset><legend>Dans quelles régions êtes-vous mobile ?</legend>
    <label><input type="checkbox" name="r" value="0" aria-required="true"> Aucune</label>
    <label><input type="checkbox" name="r" value="1" aria-required="true"> Bretagne</label>
    <label><input type="checkbox" name="r" value="2" aria-required="true"> Occitanie</label>
    <label><input type="checkbox" name="r" value="3" aria-required="true"> Grand Est</label></fieldset>
    <label for="more">Remarque *</label><input id="more" name="more"></section>
  <button type="button" id="next">Suivant</button><button id="send" hidden>Envoyer</button></form></main>
<script>document.getElementById('next').onclick = () => {
  const first = document.getElementById('s1'), second = document.getElementById('s2');
  if (!first.hidden) { first.hidden = true; second.hidden = false; return; }
  second.hidden = true; document.getElementById('next').hidden = true; document.getElementById('send').hidden = false; };</script>"""


async def test_the_next_button_of_a_stepped_form_that_keeps_its_address_is_not_a_loop(core, profile, tmp_path, fast):
    """A multi-step form keeps one address and one "Suivant" button. Its second step holds a "tick all that apply" list
    whose every box is marked required (one is "none") and a field marked required that the site does not insist on:
    clicking "Suivant" there is the next step, not the first click made again on a page that refuses it."""
    core.html["/site/jobs/100"] = STEPPED_FORM

    def plan(snapshot):
        labels = {field.label for field in snapshot.fields}
        if "Prénom" in labels:
            return StepPlan(page_kind="form", actions=[act(snapshot, "Prénom", "fill", "first_name"),
                                                       act(snapshot, "CV", "upload_cv", kind="file"),
                                                       act(snapshot, "Suivant", "click", role="next")])
        if "Bretagne" in labels:
            ticks = [act(snapshot, region, "check") for region in ("Bretagne", "Occitanie", "Grand Est")
                     if next(field for field in snapshot.fields if field.label == region).value != "true"]
            return StepPlan(page_kind="form", actions=[*ticks, act(snapshot, "Suivant", "click", role="next")])
        return StepPlan(page_kind="form", actions=[act(snapshot, "Envoyer", "click", role="submit")])

    _, app = approved(profile, tmp_path, f"{CAREERS}/jobs/100")
    result = await service.run(app.id, mode="submit", trigger="user", headless=True, planner=ScriptedPlanner(plan))
    assert result.status == "submitted", (result.message, result.result.diagnostic_code)
    body = core.posts_to("/site/jobs/100")[0]["body"]
    assert body.count(b'name="r"') == 3 and b'name="r"\r\n\r\n0' not in body


async def test_a_page_that_moves_between_the_read_and_the_typing_is_read_again(core, profile, tmp_path, fast):
    """The posting shows its form, then takes the browser to the form's own address a moment after it was read: the fields
    that were read are gone. Nothing is "to be filled by hand": the page the browser is on is read and filled."""
    core.html["/site/jobs/100"] = form_page("/jobs/100", """<script>
      new MutationObserver(() => location.replace('/jobs/100/apply'))
        .observe(document.getElementById('name'), {attributes: true, attributeFilter: ['data-bp-ref']});</script>""")
    core.html["/site/jobs/100/apply"] = form_page("/jobs/100/apply")
    _, app = approved(profile, tmp_path, f"{CAREERS}/jobs/100")
    result = await send(app)
    assert result.status == "submitted", (result.message, result.result.diagnostic_code)
    assert paths(core, "/site") == ["/site/jobs/100/apply"]
