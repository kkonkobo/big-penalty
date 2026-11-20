"""Autonomous candidate-account creation on employer ATS pages (Talentsoft-like and talentview-like fixtures, all synthetic)."""

from urllib.parse import parse_qs, urlsplit

import pytest
from apply_fakes import ScriptedPlanner, act, fast, server  # noqa: F401
from test_apply_dispatch_safety import approved_fixture, posting_form

from jobhunt.apply import service
from jobhunt.apply.facts import save_answers
from jobhunt.apply.types import StepPlan

CREATE = "Je me crée un espace candidat"

TALENTSOFT = f'''<h1>Mon espace candidat</h1>
<form method="post" action="/my-account/log-in.aspx" id="aspnetForm">
  <a href="/my-account/log-in.aspx?titletype=dashboard">{CREATE}</a>
  <label>Identifiant (email)<input type="text" name="Login"></label>
  <label>Mot de passe<input type="password" name="Password"></label>
  <button type="submit" name="go1">Se connecter</button>
  <h2>Création compte</h2>
  <label>Identifiant (email, de type exemple@exemple.fr)<input type="text" name="CreationControl$txtNouveauIdentifiant"></label>
  <label>Mot de passe<input type="password" name="CreationControl$txtNouveauMotDePasse"></label>
  <input type="submit" name="CreationControl$btnCreateAccount" value="{CREATE}">
</form>
<a href="/my-account/log-in.aspx?titletype=dashboard">{CREATE}</a>'''


def setup(server, profile, tmp_path, monkeypatch, *, known=False):
    monkeypatch.setattr(service.get_settings(), "apply_autonomous", True)
    monkeypatch.setattr(service.accounts, "create_password", lambda host, email: "fixture-password")
    monkeypatch.setattr(service.accounts, "account_known", lambda host, email: known)
    _, app = approved_fixture(profile, tmp_path, server.url("jobs/100"))
    server.html["/jobs/100"] = '<main><h1>Offre</h1><a href="/entry">Postuler</a></main>'
    server.redirects["/entry"] = "/my-account/log-in.aspx?ReturnUrl=%2Fapplication"
    server.html["/jobs/100/apply"] = posting_form("/jobs/100/apply")
    return app


def planner(login_kind="account_login"):
    def plan(snap):
        path = urlsplit(snap.url).path
        if path == "/jobs/100":
            return StepPlan(page_kind="job_page")
        if any(f.kind == "password" for f in snap.fields):
            # a careless planner: fills the LOGIN box and clicks "Se connecter" (the engine must not follow it)
            return StepPlan(page_kind=login_kind, actions=[act(snap, "Identifiant|E-?mail", "fill", "account_email"),
                                                          act(snap, "Mot de passe", "fill", "account_password"),
                                                          act(snap, "Se connecter|Connexion|Inscription$|Créer mon compte", "click", role="login")])
        return StepPlan(page_kind="form", actions=[act(snap, "Prénom", "fill", "first_name"), act(snap, "CV", "upload_cv", kind="file"),
                                                   act(snap, "Envoyer", "click", role="submit")])
    return plan


async def test_talentsoft_inline_creation_form_is_used(server, profile, tmp_path, fast, monkeypatch):
    app = setup(server, profile, tmp_path, monkeypatch)
    server.html["/my-account/log-in.aspx"] = TALENTSOFT
    server.post_redirects["/my-account/log-in.aspx"] = "/jobs/100/apply"
    result = await service.run(app.id, mode="submit", trigger="user", headless=True, planner=ScriptedPlanner(planner()))
    account = server.posts_to("/my-account/log-in.aspx")
    assert len(account) == 1, result.result.diagnostic_code
    body = parse_qs(account[0]["body"].decode(), keep_blank_values=True)
    assert body["CreationControl$txtNouveauIdentifiant"] == ["camille@example.test"]
    assert body["CreationControl$txtNouveauMotDePasse"] == ["fixture-password"]
    assert body["Login"] == [""] and body["Password"] == [""]  # the login box stays untouched
    assert "go1" not in body
    assert result.status == "submitted", (result.status, result.result.diagnostic_code, result.message, [(x.page_kind, x.note, x.clicked) for x in result.result.steps], server.requests)


@pytest.mark.parametrize("planner_kind", ["account_login", "other"])
async def test_two_identical_signup_links_reach_the_signup_page(planner_kind, server, profile, tmp_path, fast, monkeypatch):
    app = setup(server, profile, tmp_path, monkeypatch)
    server.html["/my-account/log-in.aspx"] = f'''<a href="/Account/Register">{CREATE}</a><h1>Connexion</h1>
        <form method="post" action="/Account/Login"><label>E-mail<input type="email" name="email"></label>
        <label>Mot de passe<input type="password" name="password"></label><button>Se connecter</button></form>
        <p><a href="/Account/Register">{CREATE}</a></p>'''
    save_answers(profile.id, {"civility": "M."})
    server.html["/Account/Register"] = '''<h1>Inscription</h1><form method="post" action="/Account/Register">
        <label>Civilité<select name="civ" required><option value="">--</option><option value="1">Monsieur</option>
        <option value="2">Madame</option></select></label>
        <label>Prénom<input name="first_name" required></label><label>Nom<input name="last_name" required></label>
        <label>E-mail<input type="email" name="email" required></label>
        <label>Mot de passe<input type="password" name="password" required></label>
        <label><input type="checkbox" name="cgu" required> J'accepte les conditions générales d'utilisation *</label>
        <label><input type="checkbox" name="news"> Je souhaite recevoir des offres</label>
        <button>Créer mon compte</button></form>'''
    server.post_redirects["/Account/Register"] = "/jobs/100/apply"

    def plan(snap):
        if urlsplit(snap.url).path.endswith("/Register"):
            return StepPlan(page_kind="account_signup", actions=[
                act(snap, "Civilité", "select", "civility", "Monsieur"), act(snap, "Prénom", "fill", "first_name"),
                act(snap, "^Nom", "fill", "last_name"), act(snap, "E-mail", "fill", "account_email"),
                act(snap, "Mot de passe", "fill", "account_password"), act(snap, "Créer mon compte", "click", role="signup")])
        return planner(planner_kind)(snap)

    result = await service.run(app.id, mode="submit", trigger="user", headless=True, planner=ScriptedPlanner(plan))
    register = server.posts_to("/Account/Register")
    assert len(register) == 1, (result.status, result.result.diagnostic_code)
    body = parse_qs(register[0]["body"].decode(), keep_blank_values=True)
    assert body["first_name"] == ["Camille"] and body["email"] == ["camille@example.test"] and body["password"] == ["fixture-password"]
    assert body["cgu"] and "news" not in body  # the required terms box is ticked, the marketing one is not
    assert not server.posts_to("/Account/Login")
    assert result.status == "submitted", (result.status, result.result.diagnostic_code, result.message, [(x.page_kind, x.note, x.clicked) for x in result.result.steps], server.requests)


async def test_social_signup_buttons_are_never_clicked(server, profile, tmp_path, fast, monkeypatch):
    app = setup(server, profile, tmp_path, monkeypatch)
    server.html["/my-account/log-in.aspx"] = '''<h1>Inscription</h1><form method="post" action="/auth/google"><button>Inscription avec Google</button></form>
        <form method="post" action="/Account/Register">
        <label>Prénom<input name="first_name" required></label><label>Nom<input name="last_name" required></label>
        <label>Email<input type="email" name="email" required></label>
        <label>Créez votre mot de passe<input type="password" name="password" required></label>
        <label><input type="checkbox" name="legals" required> En postulant, j'accepte également les conditions générales d'utilisation. *</label>
        <button>Inscription</button></form>'''
    server.post_redirects["/Account/Register"] = "/jobs/100/apply"

    def plan(snap):
        if urlsplit(snap.url).path.startswith("/my-account"):
            return StepPlan(page_kind="account_signup", actions=[act(snap, "avec Google", "click", role="signup")])
        return planner()(snap)

    result = await service.run(app.id, mode="submit", trigger="user", headless=True, planner=ScriptedPlanner(plan))
    assert not server.posts_to("/auth/google")
    body = parse_qs(server.posts_to("/Account/Register")[0]["body"].decode(), keep_blank_values=True)
    assert body["legals"] and body["password"] == ["fixture-password"]
    assert result.status == "submitted", (result.status, result.result.diagnostic_code, result.message, [(x.page_kind, x.note, x.clicked) for x in result.result.steps], server.requests)


async def test_no_signup_link_stops_with_a_diagnosable_reason(server, profile, tmp_path, fast, monkeypatch):
    app = setup(server, profile, tmp_path, monkeypatch)
    server.html["/my-account/log-in.aspx"] = '''<h1>Connexion</h1><form method="post" action="/Account/Login">
        <label>E-mail<input type="email" name="email"></label><label>Mot de passe<input type="password" name="password"></label>
        <button>Se connecter</button></form>'''
    result = await service.run(app.id, mode="submit", trigger="user", headless=True, planner=ScriptedPlanner(planner()))
    assert result.status == "needs_human"
    assert result.result.diagnostic_code == "account_signup_missing_link"
    assert "credential_page" not in result.result.message
    assert not server.posts


async def test_signup_with_an_unfillable_required_field_names_the_reason(server, profile, tmp_path, fast, monkeypatch):
    app = setup(server, profile, tmp_path, monkeypatch)
    server.html["/my-account/log-in.aspx"] = '''<h1>Inscription</h1><form method="post" action="/Account/Register">
        <label>Email<input type="email" name="email" required></label>
        <label>Mot de passe<input type="password" name="password" required></label>
        <label>Date de naissance<input name="dob" required></label><button>Créer mon compte</button></form>'''
    result = await service.run(app.id, mode="submit", trigger="user", headless=True, planner=ScriptedPlanner(planner("account_signup")))
    assert result.status == "needs_input"
    assert result.result.diagnostic_code == "account_signup_field_unfilled"
    assert not server.posts


TALENTSOFT_HEADER = ('<header><form method="post" action="/my-account/log-in.aspx"><label>Identifiant (email)<input type="text" name="Login"></label>'
                     '<label>Mot de passe<input type="password" name="Password"></label><label>Rester connecté<input type="checkbox" name="StayConnected"></label>'
                     '<button type="submit">Se connecter</button></form></header>')
TALENTSOFT_REAL = (TALENTSOFT_HEADER + TALENTSOFT.replace('name="Login"', 'name="ctl00$corps$tbxIdentifiant"')
                   .replace('name="Password"', 'name="ctl00$corps$tbxPassword"')
                   .replace("CreationControl$", "ctl00$corps$CreationControl$"))


def two_step_site(server, state):
    """The site rejects a login (the page comes back) and accepts the creation box."""
    server.html["/my-account/log-in.aspx"] = TALENTSOFT_REAL
    server.post_redirects["/my-account/log-in.aspx"] = "/my-account/log-in.aspx"
    real = server.post_redirects

    class Redirects(dict):
        def __contains__(self, path):
            return path.startswith("/my-account/log-in.aspx")

        def __getitem__(self, path):
            body = server.posts[-1]["body"].decode()
            return "/jobs/100/apply" if "CreationControl%24txtNouveauIdentifiant=camille" in body.replace("%40", "@") else "/my-account/log-in.aspx"

    server.post_redirects = Redirects(real)


@pytest.mark.parametrize("known", [False, True])
async def test_talentsoft_duplicated_login_boxes(known, server, profile, tmp_path, fast, monkeypatch):
    """Header + body login boxes and the creation box; `known` is the stale credential of a signup that never completed."""
    app = setup(server, profile, tmp_path, monkeypatch, known=known)
    unconfirmed = []
    live = {"known": known}
    monkeypatch.setattr(service.accounts, "account_known", lambda host, email: live["known"])
    real_callbacks = service.accounts.guarded_account_callbacks

    def callbacks(*args, **kw):
        password, known_cb = real_callbacks(*args, **kw)
        known_cb.unconfirm = lambda url: (unconfirmed.append(url), live.update(known=False))[0]
        return password, known_cb

    monkeypatch.setattr(service.accounts, "guarded_account_callbacks", callbacks)
    two_step_site(server, known)
    result = await service.run(app.id, mode="submit", trigger="user", headless=True, planner=ScriptedPlanner(planner()))
    posts = [parse_qs(p["body"].decode(), keep_blank_values=True) for p in server.posts_to("/my-account/log-in.aspx")]
    created = [b for b in posts if b.get("ctl00$corps$CreationControl$txtNouveauIdentifiant") == ["camille@example.test"]]
    assert len(created) == 1, (result.status, result.result.diagnostic_code, result.message, posts)
    assert created[0]["ctl00$corps$CreationControl$txtNouveauMotDePasse"] == ["fixture-password"]
    assert len(unconfirmed) == (1 if known else 0)
    assert "fixture-password" not in repr(result.result.model_dump())
    assert result.status == "submitted", (result.status, result.result.diagnostic_code, result.message)
