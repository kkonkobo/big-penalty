"""LLM-guided apply entry: blockers closed, apply control chosen, landing bound by continuity; fictional pages only."""

import re

import pytest
from apply_fakes import FixtureServer, ScriptedPlanner, act, fast, server  # noqa: F401
from test_apply_dispatch_safety import approved_fixture, form, posting_form  # noqa: F401

from jobhunt.apply import service
from jobhunt.apply.apply_entry import Probe
from jobhunt.apply.destination import clean_url
from jobhunt.apply.entry_llm import EntryStep, embed_target
from jobhunt.apply.types import StepPlan

BLOCKER = {"consent", "chat", "overlay", "floating", "embedded"}


class Advisor(ScriptedPlanner):
    """Planner double that also answers the entry question like a naive model: closes every blocker it recognises, then
    takes the FIRST control that looks like an application link (document order, header before main)."""

    def __init__(self, steps, apply_re=r"candidature|postuler", force_dismiss=None):
        super().__init__(steps)
        self.views, self.apply_re, self.force_dismiss = [], apply_re, force_dismiss

    async def advise_entry(self, view, *, job_title, company):
        self.views.append(view)
        controls = view["controls"]
        dismiss = [c["id"] for c in controls if c["region"] in BLOCKER and re.search(r"accept|×|close|fermer", c["label"], re.I)]
        found = [c for c in controls if c["region"] not in BLOCKER and re.search(self.apply_re, c["label"], re.I)]
        if self.force_dismiss:
            dismiss = [c["id"] for c in controls if self.force_dismiss(c)]
        return EntryStep(page_state="job_posting", dismiss=dismiss[:3], apply=found[0]["id"] if found else None)


OVERLAYS = '''<div id="cookie-banner" role="dialog" aria-modal="true" style="position:fixed;inset:0;background:#fff;z-index:99">
  <p>Nous utilisons des cookies.</p><button onclick="document.getElementById('cookie-banner').remove()">Accepter</button></div>
<div id="chat-widget" style="position:fixed;right:0;bottom:0;width:260px;height:300px;z-index:100;background:#eee">
  Assistant IA <button aria-label="Close chat" onclick="document.getElementById('chat-widget').remove()">×</button></div>'''


def job_page(body, head=""):
    return f'<title>Offre</title>{head}<main><h1>Ingénieur systèmes embarqués</h1><p>Stage de six mois.</p>{body}</main>'


async def run(app, planner):
    return await service.run(app.id, mode="submit", trigger="user", headless=True, planner=planner)


@pytest.fixture(autouse=True)
def autonomous(monkeypatch):
    monkeypatch.setattr(service.get_settings(), "apply_autonomous", True)


async def test_chat_widget_and_accept_only_cookie_dialog_over_the_apply_link(server, profile, tmp_path, fast):
    _, app = approved_fixture(profile, tmp_path, server.url("jobs/100"))
    server.html["/jobs/100"] = OVERLAYS + job_page('<a href="/jobs/100/apply">Déposer ma candidature</a>')
    server.html["/jobs/100/apply"] = posting_form("/jobs/100/apply")
    planner = Advisor([StepPlan(page_kind="job_page"), form])
    result = await run(app, planner)
    assert result.status == "submitted", result.result.diagnostic_code
    assert [post["path"] for post in server.posts] == ["/jobs/100/apply"]
    first = planner.views[0]["controls"]
    assert {"consent", "chat"} <= {c["region"] for c in first}


async def test_a_reject_button_is_preferred_over_accept(server, profile, tmp_path, fast):
    _, app = approved_fixture(profile, tmp_path, server.url("jobs/100"))
    server.html["/jobs/100"] = '''<div id="cookie-banner" role="dialog" style="position:fixed;inset:0;background:#fff;z-index:99">
      <p>Cookies</p><button onclick="fetch('/pixel-accept');this.parentNode.remove()">Tout accepter</button>
      <button onclick="fetch('/pixel-reject');this.parentNode.remove()">Tout refuser</button></div>''' + job_page(
        '<a href="/jobs/100/apply">Déposer ma candidature</a>')
    server.html["/jobs/100/apply"] = posting_form("/jobs/100/apply")
    result = await run(app, Advisor([StepPlan(page_kind="job_page"), form]))
    assert result.status == "submitted"
    assert "/pixel-reject" in server.requests and "/pixel-accept" not in server.requests


async def test_two_different_apply_controls_use_the_one_in_the_main_content(server, profile, tmp_path, fast):
    _, app = approved_fixture(profile, tmp_path, server.url("jobs/100"))
    server.html["/jobs/100"] = ('<header><a href="/jobs/100/apply-bar">Candidature en ligne</a></header>'
                                + job_page('<a href="/jobs/100/apply">Candidature en ligne rapide</a>'))
    server.html["/jobs/100/apply"] = posting_form("/jobs/100/apply")
    server.html["/jobs/100/apply-bar"] = posting_form("/jobs/100/apply-bar")
    result = await run(app, Advisor([StepPlan(page_kind="job_page"), form]))
    assert result.status == "submitted"
    assert "/jobs/100/apply-bar" not in server.requests
    assert [post["path"] for post in server.posts] == ["/jobs/100/apply"]


@pytest.mark.parametrize("scenario", ["href_names_other_requisition", "landing_declares_other_requisition", "other_domain"])
async def test_a_landing_that_is_not_this_posting_is_never_followed_or_fed(scenario, server, profile, tmp_path, fast):
    _, app = approved_fixture(profile, tmp_path, server.url("jobs/100"))
    target = {"href_names_other_requisition": "/jobs/200/apply", "landing_declares_other_requisition": "/jobs/100/apply",
              "other_domain": server.url("jobs/100/apply").replace("127.0.0.1", "localhost")}[scenario]
    server.html["/jobs/100"] = job_page(f'<a href="{target}">Déposer ma candidature</a>')
    page = posting_form("/jobs/100/apply")
    server.html["/jobs/100/apply"] = ('<main data-requisition-id="200">' + page + '</main>'
                                      if scenario == "landing_declares_other_requisition" else page)
    server.html["/jobs/200/apply"] = posting_form("/jobs/200/apply")
    result = await run(app, Advisor([StepPlan(page_kind="job_page"), form]))
    assert result.status != "submitted"
    assert not server.posts and not server.sent
    assert "/jobs/200/apply" not in server.requests


async def test_the_apply_click_is_followed_for_a_few_hops_before_any_data(server, profile, tmp_path, fast):
    _, app = approved_fixture(profile, tmp_path, server.url("jobs/100"))
    server.html["/jobs/100"] = job_page('<a href="/jobs/100/start">Déposer ma candidature</a>')
    server.html["/jobs/100/start"] = job_page('<a href="/jobs/100/apply">Candidature : continuer</a>')
    server.html["/jobs/100/apply"] = posting_form("/jobs/100/apply")
    result = await run(app, Advisor([StepPlan(page_kind="job_page"), form]))
    assert result.status == "submitted", result.result.diagnostic_code
    assert [post["path"] for post in server.posts] == ["/jobs/100/apply"]


async def test_an_advisor_cannot_dismiss_an_apply_link_or_leave_the_page(server, profile, tmp_path, fast):
    _, app = approved_fixture(profile, tmp_path, server.url("jobs/100"))
    server.html["/jobs/100"] = ('<div id="promo" style="position:fixed;right:0;bottom:0;background:#eee;z-index:9">'
                                '<a href="/elsewhere">Newsletter</a></div>' + job_page('<p>Pas de lien.</p>'))
    result = await run(app, Advisor([StepPlan(page_kind="job_page"), form], force_dismiss=lambda c: c["label"] == "Newsletter"))
    assert result.status != "submitted"
    assert "/elsewhere" not in server.requests and not server.posts


def test_phenom_wizard_keys_are_tracking_only_for_navigation():
    url = "https://careers.example.test/global/en/apply?jobSeqNo=EXR123&source=WORKDAY&step=1&stepname=personalInformation"
    assert clean_url(url, navigation=True) == "https://careers.example.test/global/en/apply?jobseqno=EXR123".replace("jobseqno", "jobSeqNo")
    assert clean_url(url + "&next=https%3A%2F%2Fother.example", navigation=True) == ""
    assert clean_url(url + "&redirect=%2Fx", navigation=True) == ""


@pytest.mark.parametrize("src, expected", [
    ("https://careers-exemple.icims.com/jobs/1234/job?in_iframe=1&hashed=-1&mobile=false", "https://careers-exemple.icims.com/jobs/1234/job"),
    ("https://careers-exemple.icims.com/jobs/9999/job?in_iframe=1", ""),                       # another requisition
    ("https://boards.greenhouse.io/embed/job_app?for=exemple&token=1234", "https://job-boards.greenhouse.io/exemple/jobs/1234"),
    ("https://boards.greenhouse.io/embed/job_app?for=exemple&token=777", ""),
    ("https://forms.unknown.example/jobs/1234/job", ""),                                       # not a recognized ATS
    ("http://careers-exemple.icims.com/jobs/1234/job", ""),
])
def test_iframe_form_of_a_recognized_ats_must_name_this_posting(src, expected):
    probe = Probe("https://careers.exemple.test/offres/stage-1234", None, [])
    probe.lenient = True
    assert embed_target(probe, [src]) == expected


async def test_each_hop_may_open_its_own_new_tab(server, profile, tmp_path, fast):
    _, app = approved_fixture(profile, tmp_path, server.url("jobs/100"))
    server.html["/jobs/100"] = job_page('<a target="_blank" href="/jobs/100/start">Déposer ma candidature</a>')
    server.html["/jobs/100/start"] = job_page('<a target="_blank" href="/jobs/100/apply">Candidature : continuer</a>')
    server.html["/jobs/100/apply"] = posting_form("/jobs/100/apply")
    result = await run(app, Advisor([StepPlan(page_kind="job_page"), form]))
    assert result.status == "submitted", result.result.diagnostic_code
    assert [post["path"] for post in server.posts] == ["/jobs/100/apply"]


async def test_an_apply_control_of_a_similar_job_is_never_taken(server, profile, tmp_path, fast):
    _, app = approved_fixture(profile, tmp_path, server.url("jobs/100"))
    server.html["/jobs/100"] = job_page('<div class="similar-jobs"><a href="/offre/autre/apply">Postuler</a></div>')
    server.html["/offre/autre/apply"] = posting_form("/offre/autre/apply")
    result = await run(app, Advisor([StepPlan(page_kind="job_page"), form]))
    assert result.status != "submitted"
    assert "/offre/autre/apply" not in server.requests and not server.posts


def test_a_link_to_another_site_must_itself_say_apply():
    from types import SimpleNamespace

    from jobhunt.apply.entry_llm import _apply_allowed, _Item

    probe = Probe("https://careers.employer.example/jobs/embedded-intern", None, [])
    probe.lenient = True
    frame = SimpleNamespace(parent_frame=None)

    def link(label, href, region="main"):
        return _Item(1, frame, None, {"label": label, "href": href, "region": region, "hint": "", "tag": "a", "covered": False})

    other = "https://boards.greenhouse.io/partnerco/jobs/7654321"
    assert probe.navigation(other)  # the probe alone admits any recognized ATS from a career site
    assert not _apply_allowed(link("Voir l'offre de notre partenaire", other), probe)
    assert _apply_allowed(link("Postuler", other), probe)
    assert not _apply_allowed(link("Postuler", other, region="related"), probe)
    assert _apply_allowed(link("Continuer", "https://careers.employer.example/jobs/embedded-intern/start"), probe)
