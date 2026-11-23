"""Autonomous candidate-account creation on employer ATS pages (Talentsoft-like and talentview-like fixtures, all synthetic)."""

from urllib.parse import parse_qs, urlsplit

import pytest
from apply_fakes import ScriptedPlanner, act, fast, server  # noqa: F401
from test_apply_dispatch_safety import approved_fixture, posting_form

from jobhunt.apply import engine, service
from jobhunt.apply.facts import save_answers
from jobhunt.apply.types import Action, Question, RunResult, StepPlan

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


@pytest.mark.parametrize("guard_mode", ["strict", "core"])
async def test_talentsoft_inline_creation_form_is_used(guard_mode, server, profile, tmp_path, fast, monkeypatch):
    monkeypatch.setattr(service.get_settings(), "apply_guard_mode", guard_mode)  # core: the account page needs no entry binding
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


POSTBACK = TALENTSOFT_REAL.replace('action="/my-account/log-in.aspx"', 'action=""')  # ASP.NET posts back to the current URL, query included


class AccountSite:
    """A stateful synthetic Talentsoft site on top of the fixture server: creation makes the account, a login only works once it exists."""

    def __init__(self, server, *, exists=False):
        self.server, self.exists, self.next, self.created, self.logins = server, exists, "/jobs/100/apply", 0, 0
        server.html["/my-account/log-in.aspx"] = POSTBACK
        site = self

        class Redirects(dict):
            def __contains__(self, path):
                return path.startswith("/my-account/log-in.aspx")

            def __getitem__(self, path):
                body = parse_qs(server.posts[-1]["body"].decode(), keep_blank_values=True)
                server.html["/my-account/log-in.aspx"] = POSTBACK
                if body.get("ctl00$corps$CreationControl$txtNouveauIdentifiant", [""])[0]:
                    site.created += 1
                    if site.exists:
                        server.html["/my-account/log-in.aspx"] = POSTBACK + "<p>Cette adresse e-mail est déjà utilisée.</p>"
                        return path
                    site.exists = True
                    return site.next
                site.logins += 1
                return site.next if site.exists else path

        server.post_redirects = Redirects()


def store_accounts(monkeypatch, *, state):
    """The credential store for the host: `state` None (nothing), "pending" or "active"; set_state records what the engine confirms."""
    store = {"state": state, "calls": []}
    monkeypatch.setattr(service.accounts, "account_known", lambda host, email: store["state"] == "active")
    monkeypatch.setattr(service.accounts, "set_state", lambda host, email, new, site=None: (store["calls"].append(new), store.update(state=new))[0])
    return store


async def run_posting(app, plan_fn):
    return await service.run(app.id, mode="submit", trigger="user", headless=True, planner=ScriptedPlanner(plan_fn))


async def test_account_is_active_after_the_first_signup_so_the_next_posting_logs_in(server, profile, tmp_path, fast, monkeypatch):
    app = setup(server, profile, tmp_path, monkeypatch)
    store = store_accounts(monkeypatch, state=None)
    _, app2 = approved_fixture(profile, tmp_path, server.url("jobs/200"), title="Autre poste")
    server.html["/jobs/200"] = '<main><h1>Offre</h1><a href="/entry2">Postuler</a></main>'
    server.redirects["/entry"] = "/my-account/log-in.aspx?ReturnUrl=%2Fmon-compte%2Fmon-cv-pour-ma-candidature-pour-stage-x_100.aspx"
    server.redirects["/entry2"] = "/my-account/log-in.aspx?ReturnUrl=%2Fmon-compte%2Fmon-cv-pour-ma-candidature-pour-stage-y_200.aspx"
    server.html["/jobs/200/apply"] = posting_form("/jobs/200/apply").replace("Ingénieur systèmes embarqués", "Autre poste")
    server.html["/mon-compte/mon-cv-pour-ma-candidature-pour-stage-x_100.aspx"] = posting_form("/jobs/100/apply")
    server.html["/mon-compte/mon-cv-pour-ma-candidature-pour-stage-y_200.aspx"] = posting_form("/jobs/200/apply").replace("Ingénieur systèmes embarqués", "Autre poste")
    site = AccountSite(server)
    site.next = "/mon-compte/mon-cv-pour-ma-candidature-pour-stage-x_100.aspx"
    base = planner()

    def two_postings(snap):
        if urlsplit(snap.url).path in ("/jobs/100", "/jobs/200"):
            return StepPlan(page_kind="job_page")
        return base(snap)

    first = await run_posting(app, two_postings)
    assert first.status == "submitted", (first.status, first.result.diagnostic_code, first.message)
    assert (site.created, site.logins, store["calls"]) == (1, 0, ["active"])
    site.next = "/mon-compte/mon-cv-pour-ma-candidature-pour-stage-y_200.aspx"
    second = await run_posting(app2, two_postings)
    assert second.status == "submitted", (second.status, second.result.diagnostic_code, second.message, [(x.page_kind, x.note, x.clicked) for x in second.result.steps], server.requests)
    assert (site.created, site.logins) == (1, 1)  # no second signup
    assert len(server.posts_to("/jobs/100/apply")) == 1 and len(server.posts_to("/jobs/200/apply")) == 1


async def test_signup_answered_account_exists_logs_in_with_the_stored_credential(server, profile, tmp_path, fast, monkeypatch):
    app = setup(server, profile, tmp_path, monkeypatch)
    store_accounts(monkeypatch, state="pending")
    site = AccountSite(server, exists=True)
    result = await run_posting(app, planner())
    assert result.status == "submitted", (result.status, result.result.diagnostic_code, result.message)
    assert (site.created, site.logins) == (1, 1)  # one signup attempt, answered "already used", then one login
    assert len(server.posts_to("/jobs/100/apply")) == 1
    assert "fixture-password" not in repr(result.result.model_dump())


SEARCH_BOX = ('<fieldset><legend>Moteur de recherche</legend>'
              '<label>Localisation du poste<select name="ctl00$search$Location"><option value="0">Veuillez sélectionner une valeur</option>'
              '<optgroup label="Zone"><option value="79">France (3)</option><option value="80">Autre (1)</option></optgroup></select></label>'
              '<label>Contrat<select name="ctl00$search$Contract"><option value="0">Veuillez sélectionner une valeur</option>'
              '<option value="1">Alternance (3)</option><option value="2">Stage (7)</option></select></label>'
              '<input type="submit" name="ctl00$search$go" value="Lancer ma recherche"></fieldset>')


@pytest.mark.parametrize("known", [False, True])
async def test_talentsoft_search_selects_in_the_account_form_are_not_questions(known, server, profile, tmp_path, fast, monkeypatch):
    """Real Talentsoft account pages are ONE ASP.NET form that also holds the site's job-search selects, whose blank option is
    "Veuillez sélectionner une valeur" = 0. That value must not read as a choice the candidate has to confirm. The planner's plan
    is the one observed live: the creation fields + the creation button (signup), or the login box (known account)."""
    app = setup(server, profile, tmp_path, monkeypatch, known=known)
    page = TALENTSOFT_HEADER + TALENTSOFT.replace('name="Login"', 'name="ctl00$corps$tbxIdentifiant"').replace(
        'name="Password"', 'name="ctl00$corps$tbxPassword"').replace("CreationControl$", "ctl00$corps$CreationControl$").replace(
        'id="aspnetForm">', 'id="aspnetForm">' + SEARCH_BOX)
    assert "Veuillez sélectionner" in page
    server.html["/my-account/log-in.aspx"] = page
    server.post_redirects["/my-account/log-in.aspx"] = "/jobs/100/apply"

    def plan(snap):
        if urlsplit(snap.url).path == "/jobs/100":
            return StepPlan(page_kind="job_page")
        if any(f.kind == "password" for f in snap.fields):
            if known:
                return StepPlan(page_kind="account_login", actions=[
                    act(snap, "Identifiant", "fill", "account_email", kind="text"), act(snap, "Mot de passe", "fill", "account_password"),
                    act(snap, "Se connecter", "click", role="login")])
            by = {f.name.rsplit("$", 1)[-1]: f.ref for f in snap.fields if "CreationControl" in f.name}
            return StepPlan(page_kind="account_signup", actions=[
                Action(ref=by["txtNouveauIdentifiant"], op="fill", fact="account_email"),
                Action(ref=by["txtNouveauMotDePasse"], op="fill", fact="account_password"),
                Action(ref=by["btnCreateAccount"], op="click", fact="account_exists", click_role="signup")])
        return planner()(snap)

    result = await service.run(app.id, mode="submit", trigger="user", headless=True, planner=ScriptedPlanner(plan))
    posts = [parse_qs(p["body"].decode(), keep_blank_values=True) for p in server.posts_to("/my-account/log-in.aspx")]
    assert len(posts) == 1, (result.status, result.result.diagnostic_code, result.message, posts)
    if known:
        assert posts[0]["Login"] == ["camille@example.test"]  # the (header) login box the scripted plan picked
    else:
        assert posts[0]["ctl00$corps$CreationControl$txtNouveauIdentifiant"] == ["camille@example.test"]
        assert posts[0]["ctl00$corps$CreationControl$txtNouveauMotDePasse"] == ["fixture-password"]
        assert posts[0]["ctl00$corps$tbxIdentifiant"] == [""]
        assert posts[0]["ctl00$search$Location"] == ["0"] and posts[0]["ctl00$search$Contract"] == ["0"]  # the search boxes stay untouched
    assert result.status == "submitted", (result.status, result.result.diagnostic_code, result.message)


def test_stop_reason_names_the_unanswered_field_labels_but_no_passwords():
    run = object.__new__(engine._Run)
    run.steps, run.stop_note, run._r = [], "", (lambda text: text)
    res = RunResult(status="needs_input", message="x", questions=[
        Question(label="Localisation du poste", kind="select", reason="unmapped"),
        Question(label="Mot de passe", kind="password", reason="unmapped")])
    reason = run._stop_reason(res)
    assert "2 unanswered question(s): unmapped" in reason and "Localisation du poste" in reason and "Mot de passe" not in reason


CLOSED_HOME = '''<div id="didomi-host"><div role="dialog"><p>Nous utilisons des cookies.</p><button>En savoir plus</button>
<button>Refuser</button><button>Accepter &amp; Fermer</button></div></div>
<header><form method="post" action="/Account/Login"><label>Identifiant (email)<input type="text" name="Login"></label>
<label>Mot de passe<input type="password" name="Password"></label><button type="submit">Se connecter</button></form></header>
<div role="alert">L'offre que vous essayez d'afficher n'existe pas ou plus sur ce site.</div>
<main><h1>Bienvenue</h1><form action="/search"><label>Contrat<select name="c"><option value="0">Veuillez sélectionner une valeur</option>
<option value="1">Stage</option></select></label><button>Lancer ma recherche</button></form></main>'''


@pytest.mark.parametrize("known", [False, True])
async def test_login_that_lands_on_the_home_with_a_missing_offer_banner_is_a_closed_posting(known, server, profile, tmp_path, fast, monkeypatch):
    """The offer was withdrawn: the site bounces the account step to its home page (login box still in the header)."""
    app = setup(server, profile, tmp_path, monkeypatch, known=known)
    server.html["/my-account/log-in.aspx"] = TALENTSOFT_REAL.replace('action="/my-account/log-in.aspx"', 'action="/Account/Login"', 1)
    server.post_redirects["/Account/Login"] = "/accueil.aspx"
    server.post_redirects["/my-account/log-in.aspx"] = "/accueil.aspx"
    server.html["/accueil.aspx"] = CLOSED_HOME
    result = await service.run(app.id, mode="submit", trigger="user", headless=True, planner=ScriptedPlanner(planner()))
    assert (result.status, result.result.diagnostic_code) == ("blocked", "job_closed"), (
        result.status, result.result.diagnostic_code, result.result.dispatch_diagnostics, server.requests)


SF_COMBINED = '''<title>Ingénieur systèmes embarqués</title><form action="/careers/apply" method="post"><h2>Please sign in</h2>
<label>Email Address: *<input name="email" required></label><label>Choose Password: *<input type="password" name="pw" required></label>
<label>Prénom<input name="first_name"></label><label>Ville<input name="city"></label><label>Adresse<input name="street"></label>
<label>Code postal<input name="zip"></label><label>Délai de préavis<input name="notice"></label>
<label>Prétentions salariales<input name="salary"></label><label>CV<input type="file" name="cv"></label>
<button type="submit">Envoyer</button></form>'''


async def test_one_page_account_plus_application_form_runs_unattended(server, profile, tmp_path, fast, monkeypatch):
    """SuccessFactors-like: the sign-in / profile fields and the questionnaire share one page: it is the application, not an account stop."""
    app = setup(server, profile, tmp_path, monkeypatch)
    server.redirects["/entry"] = "/careers"
    server.html["/careers"] = SF_COMBINED
    server.post_redirects["/careers/apply"] = "/careers/done"
    server.html["/careers/done"] = "<h1>Merci</h1><p>Votre candidature a bien été envoyée.</p>"

    def plan(snap):
        if urlsplit(snap.url).path == "/jobs/100":
            return StepPlan(page_kind="job_page")
        return StepPlan(page_kind="form", actions=[
            act(snap, "Email", "fill", "account_email"), act(snap, "Password", "fill", "account_password"),
            act(snap, "Prénom", "fill", "first_name"), act(snap, "CV", "upload_cv", kind="file"), act(snap, "Envoyer", "click", role="submit")])

    result = await service.run(app.id, mode="submit", trigger="user", headless=True, planner=ScriptedPlanner(plan))
    assert result.result.diagnostic_code != "supervised_only" and result.message != service.SUPERVISED_ONLY, (
        result.status, result.message, result.result.diagnostic_code)
    assert server.posts_to("/careers/apply")


ITI = '''<div class="iti"><div class="iti__selected-flag" role="combobox" aria-haspopup="listbox" aria-controls="lst" aria-expanded="false"
 aria-label="Telephone country code" tabindex="0" title="France"><div class="iti__selected-dial-code">+33</div></div>
<ul id="lst" role="listbox" aria-label="List of countries" style="display:none">
<li role="option" data-dial-code="33">France<span>+33</span></li><li role="option" data-dial-code="1">United States<span>+1</span></li>
<li role="option" data-dial-code="33">France<span>+33</span></li></ul></div>'''


async def test_preselected_flag_picker_with_the_candidates_dial_code_is_not_a_question(server, profile, tmp_path, fast, monkeypatch):
    """talentview-like signup: an intl-tel-input flag picker already on +33 (its list repeats France) matches the French phone."""
    app = setup(server, profile, tmp_path, monkeypatch)
    server.html["/my-account/log-in.aspx"] = f'''<h1>Inscription</h1><form method="post" action="/Account/Register">
        <label>Email<input type="email" name="email" required></label>
        <label>Créez votre mot de passe<input type="password" name="password" required></label>
        {ITI}<label>Numéro de téléphone<input type="tel" name="phone"></label>
        <label><input type="checkbox" name="legals" required> En postulant, j'accepte également les conditions générales d'utilisation. *</label>
        <button>Inscription</button></form>'''
    server.post_redirects["/Account/Register"] = "/jobs/100/apply"

    def plan(snap):
        if urlsplit(snap.url).path.startswith("/my-account"):
            return StepPlan(page_kind="account_signup", actions=[act(snap, "Email", "fill", "account_email"),
                                                                 act(snap, "mot de passe", "fill", "account_password"),
                                                                 act(snap, "Inscription", "click", role="signup")])
        return planner()(snap)

    result = await service.run(app.id, mode="submit", trigger="user", headless=True, planner=ScriptedPlanner(plan))
    assert server.posts_to("/Account/Register"), (result.status, result.result.diagnostic_code, result.message, result.result.questions)


async def test_site_error_page_after_the_posting_is_a_closed_offer(server, profile, tmp_path, fast, monkeypatch):
    app = setup(server, profile, tmp_path, monkeypatch)
    server.redirects["/entry"] = "/errorpage/"
    server.html["/errorpage/"] = '<title>Jobs</title><h1>Oops</h1><a href="/jobs">Retour</a>'

    def plan(snap):
        if urlsplit(snap.url).path == "/jobs/100":
            return StepPlan(page_kind="job_page")
        return StepPlan(page_kind="error")

    result = await service.run(app.id, mode="submit", trigger="user", headless=True, planner=ScriptedPlanner(plan))
    assert (result.status, result.result.diagnostic_code) == ("blocked", "job_closed"), (result.status, result.message, result.result.diagnostic_code)


async def test_unanswered_option_list_re_rendered_after_typing_does_not_cancel_the_action(server, profile, tmp_path, fast, monkeypatch):
    """Dynamic forms re-render (or lazy-load) the options of a question the candidate has not answered: not a changed question."""
    app = setup(server, profile, tmp_path, monkeypatch)
    server.redirects["/entry"] = "/jobs/100/apply"
    server.html["/jobs/100/apply"] = ('<title>Ingénieur systèmes embarqués</title><form action="/jobs/100/apply" method="post">'
        '<label for="name">Prénom</label><input id="name" name="first_name">'
        '<label for="src">Comment avez-vous connu l\'offre ?</label><select id="src" name="src"><option value="">Choisir</option></select>'
        '<label for="cv">CV</label><input id="cv" type="file" name="cv"><button>Envoyer</button></form>'
        '<script>document.getElementById("name").addEventListener("input", () => {'
        'document.getElementById("src").innerHTML = "<option value=\\"\\">Choisir</option><option>Site</option><option>Ami</option>"; });</script>')
    server.post_redirects["/jobs/100/apply"] = "/jobs/100/done"
    server.html["/jobs/100/done"] = "<h1>Merci</h1><p>Votre candidature a bien été envoyée.</p>"
    result = await service.run(app.id, mode="submit", trigger="user", headless=True, planner=ScriptedPlanner(planner()))
    assert "action annulée" not in result.message, (result.status, result.message)
    assert server.posts_to("/jobs/100/apply")


DIDOMI = ('<div id="didomi-host"><div id="didomi-popup" class="didomi-popup-backdrop" style="position:fixed;inset:0;z-index:9999;background:#8888">'
          '<div style="background:#fff;margin:20% auto;width:60%"><p>Nous utilisons des cookies sur le site.</p>'
          '<button>En savoir plus</button><button onclick="document.getElementById(\'didomi-popup\').remove()">Refuser</button>'
          '<button onclick="document.getElementById(\'didomi-popup\').remove()">Accepter &amp; Fermer</button></div></div></div>')


async def test_account_page_that_keeps_coming_back_stops_with_a_reason_not_a_failed_send(server, profile, tmp_path, fast, monkeypatch):
    """Talentsoft-like: the ASP.NET postback re-renders the same page with an error (captcha / validation) and a consent popup is on top.
    The account click must not end as an unconfirmed send (`failed`/credential_page): it is retried a bounded number of times, then
    reported with a non-secret reason."""
    app = setup(server, profile, tmp_path, monkeypatch)
    scripted = (POSTBACK.replace('<button type="submit" name="go1">', '<button type="button" onclick="this.form.submit()" name="go1">')
                .replace(f'<input type="submit" name="ctl00$corps$CreationControl$btnCreateAccount" value="{CREATE}">',
                         f'<input type="button" onclick="this.form.submit()" name="ctl00$corps$CreationControl$btnCreateAccount" value="{CREATE}">'))
    server.html["/my-account/log-in.aspx"] = scripted + DIDOMI + '<div role="alert">Une erreur est survenue, veuillez réessayer.</div>'

    class SamePage(dict):
        def __contains__(self, path):
            return path.startswith("/my-account/log-in.aspx")

        def __getitem__(self, path):
            return "/my-account/log-in.aspx"

    server.post_redirects = SamePage()
    result = await service.run(app.id, mode="submit", trigger="user", headless=True, planner=ScriptedPlanner(planner()))
    assert (result.status, result.result.diagnostic_code) == ("needs_human", "account_not_accepted"), (
        result.status, result.result.diagnostic_code, result.message)
    assert 1 <= len(server.posts) <= 3
    assert "fixture-password" not in repr(result.result.model_dump())


# A large multi-step ATS's "Create Account" step: its consent box is not flagged required in the markup, the page itself refuses
# to send the form while it is unticked ("Please check the box to continue"), and a hidden-by-wording honeypot input must stay empty.
MULTISTEP_CREATE = '''<h1>Créer un compte</h1><form id="create" method="post" action="/Account/Register">
  <label for="em">Adresse e-mail*</label><input id="em" type="text" name="email" aria-required="true"{email}>
  <label for="pw">Mot de passe*</label><input id="pw" type="password" name="password" aria-required="true">
  <label for="pw2">Confirmer le nouveau mot de passe*</label><input id="pw2" type="password" name="password2" aria-required="true">
  <p>En créant un compte, vous acceptez que vos données soient traitées pour votre candidature.</p>
  <input type="checkbox" id="ok" name="consent"><label for="ok">J'ai lu et j'accepte les conditions générales</label>
  <p id="err" hidden>Error: Please check the box to continue</p>
  <label for="hp">Enter website. This input is for robots only, do not enter if you're human.</label><input id="hp" name="website">
  <button type="submit">Créer un compte</button>
</form>
<p>Vous avez déjà un compte ? <a href="/my-account/log-in.aspx">Connexion</a></p>
<script>document.getElementById('create').addEventListener('submit', e => {
  if (!document.getElementById('ok').checked) { e.preventDefault(); document.getElementById('err').hidden = false; }
});</script>'''

MULTISTEP_LOGIN = '''<h1>Connexion</h1>{notice}<form method="post" action="/Account/Login">
  <label>Adresse e-mail<input type="email" name="email"></label><label>Mot de passe<input type="password" name="password"></label>
  <button>Connexion</button></form><p><a href="/Account/Register">Créer un compte</a></p>'''


def trusting_setup(server, profile, tmp_path, monkeypatch):
    monkeypatch.setattr(service.get_settings(), "apply_guard_mode", "core")
    monkeypatch.setattr(service.get_settings(), "apply_fill_mode", "planner")
    return setup(server, profile, tmp_path, monkeypatch)


def multistep_planner(*, signup_click=True, fill_honeypot=True):
    """A planner that never ticks the account form's consent box (it is not flagged required) and types into its honeypot."""
    def plan(snap):
        path = urlsplit(snap.url).path
        if path == "/jobs/100":
            return StepPlan(page_kind="job_page", actions=[act(snap, "Postuler", "click", role="open_form")])
        if path == "/Account/Register":
            actions = [act(snap, "Mot de passe", "fill", "account_password"), act(snap, "Confirmer", "fill", "account_password")]
            if fill_honeypot:
                actions.append(act(snap, "robots", "fill", value="https://example.test"))
            if signup_click:
                actions.append(act(snap, "Créer un compte", "click", role="signup", kind="button"))
            return StepPlan(page_kind="account_signup", actions=actions)
        if any(f.kind == "password" for f in snap.fields):
            return StepPlan(page_kind="account_login", actions=[
                act(snap, "Adresse e-mail", "fill", "account_email"), act(snap, "Mot de passe", "fill", "account_password"),
                act(snap, "^Connexion$", "click", role="login", kind="button")])
        return planner()(snap)
    return plan


async def test_multistep_ats_create_account_ticks_its_consent_box_and_leaves_the_honeypot_empty(server, profile, tmp_path, fast, monkeypatch):
    app = trusting_setup(server, profile, tmp_path, monkeypatch)
    server.redirects["/entry"] = "/Account/Register"
    server.html["/Account/Register"] = MULTISTEP_CREATE.replace("{email}", "")
    server.post_redirects["/Account/Register"] = "/jobs/100/apply"
    result = await service.run(app.id, mode="submit", trigger="user", headless=True, planner=ScriptedPlanner(multistep_planner()))
    register = server.posts_to("/Account/Register")
    assert len(register) == 1, (result.status, result.result.diagnostic_code, [(x.page_kind, x.note, x.clicked) for x in result.result.steps])
    body = parse_qs(register[0]["body"].decode(), keep_blank_values=True)
    assert body["consent"] and body["website"] == [""]
    assert body["email"] == ["camille@example.test"] and body["password"] == body["password2"] == ["fixture-password"]
    assert result.status == "submitted", (result.status, result.result.diagnostic_code, result.message)


async def test_multistep_ats_create_account_without_a_planned_click_is_clicked_by_code(server, profile, tmp_path, fast, monkeypatch):
    # The form came back with its e-mail already typed and the planner gave no click (it read "do not create the account
    # again"): the one creation button is clicked by code.
    app = trusting_setup(server, profile, tmp_path, monkeypatch)
    server.redirects["/entry"] = "/Account/Register"
    server.html["/Account/Register"] = MULTISTEP_CREATE.replace("{email}", ' value="camille@example.test"')
    server.post_redirects["/Account/Register"] = "/jobs/100/apply"
    result = await service.run(app.id, mode="submit", trigger="user", headless=True,
                               planner=ScriptedPlanner(multistep_planner(signup_click=False, fill_honeypot=False)))
    register = server.posts_to("/Account/Register")
    assert len(register) == 1, (result.status, result.result.diagnostic_code, [(x.page_kind, x.note, x.clicked) for x in result.result.steps])
    assert parse_qs(register[0]["body"].decode(), keep_blank_values=True)["consent"]
    assert result.status == "submitted", (result.status, result.result.diagnostic_code, result.message)


async def test_multistep_ats_signin_page_asking_for_the_email_verification_is_a_verification_step(server, profile, tmp_path, fast, monkeypatch):
    # After "Create Account" the site shows its sign-in page saying the account must first be verified by e-mail: the run
    # takes it as the verification step (no mailbox in tests: it goes back to the posting) and then signs in, never creating
    # the account a second time.
    app = trusting_setup(server, profile, tmp_path, monkeypatch)
    server.redirects["/entry"] = "/my-account/log-in.aspx"
    server.html["/my-account/log-in.aspx"] = MULTISTEP_LOGIN.format(notice="")
    server.html["/Account/Register"] = MULTISTEP_CREATE.replace("{email}", "")
    server.post_redirects["/Account/Register"] = "/signin-pending"
    server.html["/signin-pending"] = MULTISTEP_LOGIN.format(
        notice="<p>Compte créé. Vérifiez votre adresse e-mail : un lien de vérification vous a été envoyé.</p>")
    server.post_redirects["/Account/Login"] = "/jobs/100/apply"
    result = await service.run(app.id, mode="submit", trigger="user", headless=True, planner=ScriptedPlanner(multistep_planner()))
    steps = [(x.page_kind, x.note, x.clicked) for x in result.result.steps]
    assert len(server.posts_to("/Account/Register")) == 1, steps
    assert len(server.posts_to("/Account/Login")) == 1, steps
    assert result.status == "submitted", (result.status, result.result.diagnostic_code, result.message, steps)


async def test_a_login_asked_for_in_answer_to_the_send_click_is_followed_by_one_send(server, profile, tmp_path, fast, monkeypatch):
    """A form that creates the account and applies at once answers "this e-mail is already registered: sign in". After the
    login the same form is shown again, without its account fields and with nothing new: the login sent no application, so
    the form is sent from there - instead of the run ending "envoi non confirmé" on a login click."""
    monkeypatch.setattr(service.get_settings(), "apply_guard_mode", "core")
    app = setup(server, profile, tmp_path, monkeypatch)
    create = ('<label for="mail">Adresse e-mail</label><input id="mail" name="email">'
              '<label for="new">Choisir un mot de passe</label><input id="new" type="password" name="new_password">')
    form = ('<title>Ingénieur systèmes embarqués</title><main><h1>Ingénieur systèmes embarqués</h1>'
            '<form action="{action}" method="post" enctype="multipart/form-data">{create}<label for="name">Prénom</label>'
            '<input id="name" name="first_name"><label for="cv">CV</label><input id="cv" type="file" name="cv">'
            '<button>Envoyer</button></form></main>')
    server.html["/jobs/100"] = form.format(action="/jobs/100/apply", create=create)
    server.post_pages["/jobs/100/apply"] = "account_exists_form.html"
    server.post_redirects["/login"] = "/jobs/100/signed-in"
    server.html["/jobs/100/signed-in"] = form.format(action="/jobs/100/send", create="")

    def plan(snap):
        if any(f.kind == "button" and f.label == "Se connecter" for f in snap.fields):
            return StepPlan(page_kind="account_login", actions=[
                act(snap, "^Identifiant", "fill", "account_email"), act(snap, "Mot de passe du compte", "fill", "account_password"),
                act(snap, "Se connecter", "click", "account_exists", role="login")])
        account = [act(snap, "Adresse e-mail", "fill", "account_email"), act(snap, "Choisir un mot de passe", "fill", "account_password")
                   ] if any(f.kind == "password" for f in snap.fields) else []
        return StepPlan(page_kind="form", actions=account + [
            act(snap, "Prénom", "fill", "first_name"), act(snap, "CV", "upload_cv", kind="file"),
            act(snap, "Envoyer", "click", role="submit")])

    result = await service.run(app.id, mode="submit", trigger="user", headless=True, planner=ScriptedPlanner(plan))
    assert result.status == "submitted", (result.status, result.result.diagnostic_code, result.message,
                                          [(x.page_kind, x.note, x.clicked) for x in result.result.steps])
    assert [len(server.posts_to(path)) for path in ("/jobs/100/apply", "/login", "/jobs/100/send")] == [1, 1, 1]


async def test_a_posting_that_says_the_signed_in_candidate_already_applied_is_on_record_as_sent(server, profile, tmp_path, monkeypatch, fast):  # noqa: F811
    """Signed in to the candidate's account, the posting's page says - before anything is entered - that the application is
    already there (a send whose receipt was never read). The run records it as sent instead of failing on an "unexpected
    confirmation" and meeting the same page again at every pass. A page that only greets the candidate changes nothing."""
    monkeypatch.setattr(service.get_settings(), "apply_guard_mode", "core")
    app = setup(server, profile, tmp_path, monkeypatch, known=True)
    server.html["/jobs/100"] = ('<title>Ingénieur systèmes embarqués</title><main><h1>Ingénieur systèmes embarqués</h1>'
                                '<form action="/login" method="post"><label for="id">Identifiant</label><input id="id" name="login">'
                                '<label for="pw">Mot de passe du compte</label><input id="pw" type="password" name="password">'
                                '<button>Se connecter</button></form></main>')
    server.post_redirects["/login"] = "/jobs/100/signed-in"
    page = '<title>Ingénieur systèmes embarqués</title><main><h1>{said}</h1><a href="/jobs">Toutes nos offres</a></main>'
    server.html["/jobs/100/signed-in"] = page.format(said="Vous avez déjà postulé à cette offre")

    def plan(snap):
        if any(f.kind == "password" for f in snap.fields):
            return StepPlan(page_kind="account_login", actions=[
                act(snap, "^Identifiant", "fill", "account_email"), act(snap, "Mot de passe du compte", "fill", "account_password"),
                act(snap, "Se connecter", "click", "account_exists", role="login")])
        return StepPlan(page_kind="confirmation")

    result = await service.run(app.id, mode="submit", trigger="user", headless=True, planner=ScriptedPlanner(plan))
    assert result.status == "submitted", (result.status, result.message, [(x.page_kind, x.note, x.clicked) for x in result.result.steps])
    assert len(server.posts_to("/login")) == 1
    assert engine.ALREADY_APPLIED_RE.search(engine.fold("You have already applied for this job"))
    assert not engine.ALREADY_APPLIED_RE.search(engine.fold("Bienvenue dans votre espace candidat. Postulez à nos offres."))
