"""The HTML pages: login, navigation, flash, answers, intents, documents, static files and the markup rules (no script, no inline
style, escaped text, every link under the base path)."""

from __future__ import annotations

import html as htmllib
import re
from datetime import UTC, datetime, timedelta

import pytest
from portal_server_fakes import (CUSTOM_ID, CUSTOM_MASKED, DOC_A, DOC_B, GRANT_EMAIL, GRANT_NOT_SENT, GRANT_SEND_1, GRANT_SEND_2,
                                 GRANT_SENT, ORIGIN, PDF_A, QID, SYNCED_AT, T0, Harness, doc_ref, fast_scrypt, make_pdf, make_schema,
                                 make_snapshot, ver)
from starlette.testclient import TestClient

from jobhunt.portal import protocol
from jobhunt.portal.protocol import (AnswerValue, Awaiting, CustomAnswer, EmailPause, EventResult, FieldSpec, InfoItem, Inferred, Prep,
                                     SentItem, Unconfirmed)
from jobhunt.portal.server import auth, ops, pages

HDR = {"Origin": ORIGIN}
XSS = '"><img src=x onerror=1>'
JOB_URL = "https://jobs.example/offre/1"
SECRET = "SECRET-TEXT-4242"
REF_1, REF_2, REF_3 = "e1" * 16, "e2" * 16, "e3" * 16


@pytest.fixture
def h(tmp_path, monkeypatch):
    fast_scrypt(monkeypatch)
    harness = Harness.create(tmp_path)
    yield harness
    harness.portal.db.close()


@pytest.fixture
def prof(h):
    return h.enable()


class Browser(TestClient):
    """A browser with its own cookie jar. `response.text` is the visible text (entities decoded); `raw(r)` is the markup."""

    def request(self, *args, **kwargs):
        r = super().request(*args, **kwargs)
        r._text = htmllib.unescape(r.content.decode("utf-8", "replace")) if r.headers.get("content-type", "").startswith("text/html") else r.text
        return r


def raw(r) -> str:
    return r.content.decode("utf-8")


@pytest.fixture
def b(h):
    return Browser(h.app, base_url=ORIGIN, raise_server_exceptions=False)


def nonce_of(html: str) -> str:
    return re.search(r'name="nonce" value="([^"]+)"', html).group(1)


def login(b, prof, code=None, handle=None, headers=HDR, nonce=None):
    if b.cookies.get("__Secure-bp_session"):  # already logged in: the form page would redirect, so get a nonce elsewhere
        anon = Browser(b.app, base_url=ORIGIN)
        page = anon.get("/app/connexion")
        b.cookies.set("__Secure-bp_login", anon.cookies.get("__Secure-bp_login"), path="/app/")
    else:
        page = b.get("/app/connexion")
    return b.post("/app/connexion", data={"identifiant": handle or prof.handle, "code": code or prof.passcode,
                                          "nonce": nonce if nonce is not None else nonce_of(page.text)},
                  headers=headers, follow_redirects=False)


@pytest.fixture
def user(b, prof):
    assert login(b, prof).status_code == 303
    return b


def csrf(b) -> str:
    return auth.csrf_token(b.app.state.portal, b.cookies.get("__Secure-bp_session"))


def post(b, path, data=None, token=True, headers=HDR):
    data = dict(data or {})
    if token:
        data["csrf"] = csrf(b)
    return b.post(f"/app{path}", data=data, headers=headers, follow_redirects=False)


def events(h, prof):
    return ops.load_events(h.portal.db.conn, h.portal.cipher, prof.row_id)


def push(h, prof, snap):
    assert h.push_snapshot(h.pair(), prof, snap).status_code == 200


def follow(b, r):
    assert r.status_code == 303, r.text
    return b.get(r.headers["location"])


def field_value(html: str, name: str) -> str:
    return re.search(rf'name="{name}"[^>]*?value="([^"]*)"', html).group(1)


def token_of(html: str, path: str) -> str:
    return field_value(html, "tok_" + path.replace(".", "__"))


def ans(path: str) -> str:
    return "ans_" + path.replace(".", "__")


# ---------------------------------------------------------------- rich snapshot
def rich_snapshot(xss: str = "") -> protocol.Snapshot:
    snap = make_snapshot()
    sent = [
        SentItem(ref=REF_1, company="Bureau Test" + xss, title="Ingénieur" + xss, host="ats.example",
                 submitted_at=datetime(2027, 1, 10, 9, 0, tzinfo=UTC), url=JOB_URL, doc_id=DOC_B, inferred=None,
                 prep=[Prep(message="Préparez votre disponibilité" + xss, hint="Indice" + xss)]),
        SentItem(ref=REF_2, company="Atelier Sans Réponse", title="Technicien", host="ats.example", inferred=[],
                 submitted_at=datetime(2027, 1, 12, 9, 0, tzinfo=UTC)),
        SentItem(ref=REF_3, company="Usine Déduite", title="Alternant", host="jobs.example",
                 submitted_at=datetime(2027, 1, 14, 9, 0, tzinfo=UTC),
                 inferred=[Inferred(label="Permis B" + xss, value="Oui" + xss, basis="Profil" + xss)]),
    ]
    snap = snap.model_copy(update={
        "sent": sent,
        "questions": [
            snap.questions[0].model_copy(update={"label": "Permis de conduire ?" + xss, "company": "Société Exemple" + xss}),
            protocol.PendingQuestion(qid="c2" * 8, label="Votre mobilité" + xss, company="Atelier Martin", title="Technicien"),
        ],
        "email_pauses": [EmailPause(grant=GRANT_EMAIL, company="Atelier Martin" + xss, title="Assistant" + xss, host="ats.example",
                                    note="Lien reçu" + xss)],
        "unconfirmed": [Unconfirmed(grant_sent=GRANT_SENT, grant_not_sent=GRANT_NOT_SENT, company="Maison Dupont" + xss,
                                    title="Stagiaire" + xss, host="jobs.example", when=datetime(2027, 1, 9, 12, 0, tzinfo=UTC))],
        "info": [InfoItem(kind="captcha", company="Cap" + xss, title="T", host="c.example"), InfoItem(kind="host", company="Hôte"),
                 InfoItem(kind="dry_run", company="Essai"), InfoItem(kind="sending", company="En cours")],
        "awaiting": [Awaiting(grant=GRANT_SEND_1, company="Société Exemple" + xss, title="Technicien" + xss, score=81.4, age_days=3,
                              host="ats.example", job_url=JOB_URL, doc_id=DOC_A),
                     Awaiting(grant=GRANT_SEND_2, company="Usine Fictive", title="Alternant", score=64.5, host="jobs.example")],
        "counts": protocol.Counts(sent_total=3),
    })
    return snap


def seed_results(h, prof):
    now = datetime(2027, 1, 15, 8, 30, tzinfo=UTC)
    statuses = ["applied", "conflict", "rejected", "awaiting_review", "queued", "running", "done", "refused", "failed", "cancelled",
                "interrupted", "lost"]
    results = [EventResult(event_id=f"{i:032x}", kind="answer", status=s, message=f"Message {s}", label=f"Libellé {s}", at=now)
               for i, s in enumerate(statuses, 1)]
    assert h.push_snapshot(h.pair(), prof, rich_snapshot(), results=results).status_code == 200
    return statuses


PAGES = ["/a-faire", "/reponses", "/accord", "/candidatures", f"/candidatures/{REF_1}", f"/candidatures/{REF_2}",
         f"/candidatures/{REF_3}"]


# ---------------------------------------------------------------- entry points
def test_root_is_neutral_and_redirects_when_logged_in(h, b, prof):
    r = b.get("/app/", follow_redirects=False)
    assert r.status_code == 200
    assert '<meta http-equiv="refresh" content="0; url=/app/connexion">' in r.text and "Continuer" in r.text
    assert b.get("/app/", follow_redirects=False).text == r.text  # identical for everyone
    login(b, prof)
    r = b.get("/app/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/app/a-faire"
    r = b.get("/app/connexion", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/app/a-faire"


def test_login_form(h, b):
    r = b.get("/app/connexion")
    assert r.status_code == 200
    for needle in ('name="identifiant"', 'autocomplete="username"', 'autocapitalize="none"', 'autocorrect="off"', 'spellcheck="false"',
                   'type="password"', 'name="code"', 'autocomplete="current-password"', "Se connecter", 'action="/app/connexion"'):
        assert needle in r.text
    cookie = [c for c in r.headers.get_list("set-cookie") if c.startswith("__Secure-bp_login=")][0]
    assert nonce_of(r.text) in cookie and "Path=/app/" in cookie and "HttpOnly" in cookie


def test_login_sets_three_cookies_and_a_new_session_each_time(h, b, prof):
    r = login(b, prof)
    assert r.status_code == 303 and r.headers["location"] == "/app/a-faire"
    jar = {c.split("=", 1)[0]: c for c in r.headers.get_list("set-cookie")}
    assert set(jar) == {"__Secure-bp_session", "__Secure-bp_dev", "__Secure-bp_login"}
    for name in ("__Secure-bp_session", "__Secure-bp_dev"):
        c = jar[name]
        assert "HttpOnly" in c and "Secure" in c and "SameSite=strict" in c.replace("Strict", "strict") and "Path=/app/" in c
        assert "Domain" not in c
    assert "Max-Age=0" in jar["__Secure-bp_login"]
    first = b.cookies.get("__Secure-bp_session")
    assert login(b, prof).status_code == 303
    second = b.cookies.get("__Secure-bp_session")
    assert first and second and first != second
    assert auth.resolve_session(h.portal, [first], h.clock()) is None  # the presented session was deleted


def norm(text: str) -> str:
    return text.replace(re.search(r'name="nonce" value="([^"]+)"', text).group(1), "NONCE")


def test_failed_logins_are_byte_identical(h, b, prof, monkeypatch):
    other = h.enable()
    h.put_profile(h.pair(), other, enabled=False, config_rev=2)
    pages_ = [login(b, prof, handle="inconnu123"), login(b, prof, code="mauvais-code-123"), login(b, other)]
    assert [r.status_code for r in pages_] == [401, 401, 401]
    bodies = [norm(r.text) for r in pages_]
    assert len(set(bodies)) == 1 and auth.GENERIC_LOGIN_ERROR.replace("'", "'") in bodies[0]
    throttled = None
    for _ in range(15):
        throttled = login(b, prof, code="encore-faux-12")
        if throttled.status_code == 429:
            break
    assert throttled.status_code == 429 and norm(throttled.text) == bodies[0]
    assert not [c for c in throttled.headers.get_list("set-cookie") if "bp_session" in c]
    # a fresh nonce each time
    assert nonce_of(pages_[0].text) != nonce_of(pages_[1].text)


def test_missing_nonce_and_cross_origin_are_refused(h, b, prof):
    ok_page = norm(login(b, prof, handle="inconnu123").text)
    r = login(b, prof, nonce="")
    assert r.status_code == 400 and norm(r.text) == ok_page
    r = login(b, prof, nonce="not.a.nonce")
    assert r.status_code == 400 and norm(r.text) == ok_page
    r = login(b, prof, headers={"Origin": "https://evil.example"})
    assert r.status_code == 400 and norm(r.text) == ok_page
    r = login(b, prof, headers={})
    assert r.status_code == 400 and norm(r.text) == ok_page
    assert b.cookies.get("__Secure-bp_session") is None


def test_logout(h, b, user, prof):
    sid = b.cookies.get("__Secure-bp_session")
    r = post(b, "/deconnexion", token=False)
    assert r.status_code == 403 and auth.resolve_session(h.portal, [sid], h.clock())
    r = post(b, "/deconnexion", headers={"Origin": "https://evil.example"})
    assert r.status_code == 403 and auth.resolve_session(h.portal, [sid], h.clock())
    r = post(b, "/deconnexion")
    assert r.status_code == 303 and r.headers["location"] == "/app/connexion"
    cleared = {c.split("=", 1)[0] for c in r.headers.get_list("set-cookie") if "Max-Age=0" in c}
    assert cleared == {"__Secure-bp_session", "__Secure-bp_dev", "__Secure-bp_flash"}
    assert auth.resolve_session(h.portal, [sid], h.clock()) is None
    assert b.get("/app/a-faire", follow_redirects=False).headers["location"] == "/app/connexion"


def test_every_page_needs_a_session(h, b, prof):
    for path in [*PAGES, f"/documents/{DOC_A}"]:
        r = b.get(f"/app{path}", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/app/connexion", path
    for path in ("/a-faire/questions", "/confirmer", "/intentions", "/annuler", "/reponses", "/reponses/retirer",
                 "/reponses/retirer/confirmer"):
        r = b.post(f"/app{path}", data={"g": GRANT_SEND_1}, headers=HDR, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/app/connexion", path
    assert events(h, prof) == []


POSTS = [("/a-faire/questions", {f"q_{QID}": "Oui"}), ("/confirmer", {"g": GRANT_SEND_1}), ("/intentions", {"g": GRANT_SEND_1}),
         ("/annuler", {"event": "aa" * 16}), ("/reponses", {ans("identity.city"): "Lyon"}), ("/reponses/retirer", {"retirer": CUSTOM_ID}),
         ("/reponses/retirer/confirmer", {"custom_id": CUSTOM_ID, "token": ver(7)}), ("/deconnexion", {})]


def test_posts_without_csrf_or_origin_are_403_and_store_nothing(h, user, prof):
    for path, data in POSTS:
        assert post(user, path, data, token=False).status_code == 403, path
        assert post(user, path, {**data, "csrf": "0" * 64}, token=False).status_code == 403, path
        assert post(user, path, data, headers={"Origin": "https://evil.example"}).status_code == 403, path
        assert post(user, path, data, headers={}).status_code == 403, path
    assert events(h, prof) == []
    assert user.get("/app/a-faire", follow_redirects=False).status_code == 200


# ---------------------------------------------------------------- rendering
def test_pages_render_from_a_rich_snapshot(h, user, prof):
    statuses = seed_results(h, prof)
    r = user.get("/app/a-faire")
    assert r.status_code == 200
    t = r.text
    for needle in ("Questions en attente", "Permis de conduire ?", "Votre mobilité", "Choisir…", "Vos réponses sont gardées pour tous les prochains formulaires.",
                   "Enregistrer mes réponses", "E-mails à confirmer", "Atelier Martin", "ats.example", "Lien reçu",
                   "Ouvrez votre boîte mail, cliquez sur le lien de confirmation envoyé par ce site, puis revenez ici.",
                   "C'est fait, reprendre la candidature", "Envois à vérifier", "Maison Dupont", "9 janvier 2027",
                   "Vérifiez sur le site de l'entreprise (ou dans vos e-mails) si la candidature est bien partie.",
                   "Elle a bien été envoyée", "Elle n'a pas été envoyée", "À régler sur le Mac", "CAPTCHA à résoudre",
                   "Site à approuver", "Essai à blanc en pause", "Envoi en cours", "Mes dernières actions",
                   "Big Penalty", "Léa", "Mac synchronisé", "Se déconnecter"):
        assert needle in t, needle
    for s in statuses:
        assert f"Libellé {s}" in t and f"Message {s}" in t
    assert "Non appliquée (modifiée sur le Mac)" in t and "Perdue : à refaire" in t and "En attente de validation sur le Mac" in t
    assert "Rien à faire pour l'instant." not in t
    assert t.count('<select name="q_') == 1 and 'name="q_' + "c2" * 8 + '" maxlength="500"' in t
    # navigation with counts: 2 questions + 1 e-mail + 1 unconfirmed, 2 waiting, 3 sent
    nav = re.search(r'<nav class="pnav".*?</nav>', t, re.S).group(0)
    for label in ("À faire", "Mes réponses", "Mon accord", "Envoyées"):
        assert label in nav
    assert re.findall(r'<span class="count[^"]*">(\d+)</span>', nav) == ["4", "2", "3"]

    t = user.get("/app/accord").text
    for needle in ("En attente de mon accord", "Adéquation 81 %", "publiée il y a 3 jours", "Voir l'annonce", "Voir le CV",
                   f"/app/documents/{DOC_A}", "CV non disponible sur le portail", "Envoyer celle-ci", "Envoyer la sélection",
                   "Tout envoyer (2)", "L'envoi est fait par le Mac à sa prochaine synchronisation", f'name="g" value="{GRANT_SEND_1}"'):
        assert needle in t, needle
    assert 'rel="noopener noreferrer"' in t and f'href="{JOB_URL}"' in t

    t = user.get("/app/candidatures").text
    assert t.index("Usine Déduite") < t.index("Atelier Sans Réponse") < t.index("Bureau Test")
    assert "14 janvier 2027" in t and f"/app/candidatures/{REF_1}" in t

    t = user.get(f"/app/candidatures/{REF_1}").text
    assert "Le détail des réponses n'est pas disponible pour cette candidature." in t and "Préparation entretien" in t
    assert "Préparez votre disponibilité" in t and "Indice" in t and f"/app/documents/{DOC_B}" in t and JOB_URL in t
    assert "Aucune réponse déduite." in user.get(f"/app/candidatures/{REF_2}").text
    t = user.get(f"/app/candidatures/{REF_3}").text
    assert "Réponses déduites (à connaître avant l'entretien)" in t and "Permis B" in t and "Profil" in t
    for bad in ("0" * 32, "zz", REF_1.upper(), "e1" * 15):
        assert user.get(f"/app/candidatures/{bad}").status_code == 404


def test_empty_pages(h, user, prof):
    empty = make_snapshot(questions=[], email_pauses=[], unconfirmed=[], awaiting=[], sent=[])
    push(h, prof, empty)
    assert "Rien à faire pour l'instant." in user.get("/app/a-faire").text
    assert "Aucune candidature n'attend votre accord." in user.get("/app/accord").text
    assert user.get("/app/candidatures").status_code == 200


def test_no_snapshot_state(h, b):
    p = h.enable(with_snapshot=False)
    login(b, p)
    for path in PAGES[:4]:
        r = b.get(f"/app{path}")
        assert r.status_code == 200 and "En attente de la première synchronisation du Mac." in r.text, path
        assert "Mes dernières actions" not in r.text and "Enregistrer mes réponses" not in r.text
    r = post(b, "/a-faire/questions", {f"q_{QID}": "Oui"})
    assert follow(b, r).status_code == 200 and events(h, p) == []


def test_staleness_notice_and_other_notices(h, user, prof):
    t = user.get("/app/a-faire").text
    assert "Mac synchronisé maintenant" in t and "n'a pas synchronisé" not in t
    h.clock.advance(3600)
    assert "n'a pas synchronisé" not in user.get("/app/a-faire").text  # not older than 60 minutes yet
    h.clock.advance(7200 + 1)
    t = user.get("/app/a-faire").text
    assert "Mac synchronisé il y a 3 heures" in t
    assert ("Le Mac n'a pas synchronisé depuis 3 heures : vos réponses et vos accords sont enregistrés et partiront "
            "dès qu'il se réveille.") in t
    push(h, prof, make_snapshot(paused=True, review_mode=True))
    t = user.get("/app/a-faire").text
    assert "Le portail est en pause côté Mac : rien n'est appliqué pour l'instant." in t
    assert "Vos réponses et accords sont validés sur le Mac avant d'être appliqués." in t


def test_inbox_full_notice(h, user, prof, monkeypatch):
    monkeypatch.setattr(protocol, "MAX_PENDING_EVENTS", 1)
    assert post(user, "/reponses", {ans("identity.city"): "Lyon", "tok_identity__city": token_of(user.get("/app/reponses").text, "identity.city")}).status_code == 303
    assert "Trop d'actions en attente du Mac : attendez sa prochaine synchronisation." in user.get("/app/a-faire").text


# ---------------------------------------------------------------- answers
def test_answers_form_is_schema_driven(h, user, prof):
    t = user.get("/app/reponses").text
    for needle in ("Téléphone", "Ville", "Disponible dès le", "Contrat", "Remarques", "Situation de handicap", 'type="date"', "<textarea",
                   'value="Toulouse"', 'value="06 12 34 56 78"', 'value="2027-09-01"', "Non renseigné", "Prétentions salariales",
                   'value="Selon grille"', "Avez-vous un handicap ?", "Retirer"):
        assert needle in t, needle
    assert re.search(r'<option value="Alternance" selected>', t) and t.count('name="ans_') == 6
    assert t.count("<form") == 2  # one answers form + the logout form
    assert "tok_identity__city" in t and "Autres réponses enregistrées" in t
    # a new field in the schema appears; a select whose value is not an option keeps it as extra selected option
    schema = make_schema()
    schema.fields.append(FieldSpec(path="extra.langue", label="Langue préférée", help="Aide langue", placeholder="ex. français",
                                   section="Situation", max_len=33))
    schema.fields.append(FieldSpec(path="extra.mobilite", label="Mobilité", widget="select", options=["Oui", "Non"], section="Divers"))
    snap = make_snapshot(answer_schema=schema)
    snap.answers["extra.langue"] = AnswerValue(value="Français", is_set=True, ver=ver(60))
    snap.answers["extra.mobilite"] = AnswerValue(value="Parfois", is_set=True, ver=ver(61))
    snap.answers["situation.start"] = AnswerValue(value="", is_set=True, legacy="janvier 2027", ver=ver(62))
    push(h, prof, snap)
    t = user.get("/app/reponses").text
    assert "Langue préférée" in t and "Aide langue" in t and 'placeholder="ex. français"' in t and 'maxlength="33"' in t and t.count('name="ans_') == 8
    assert '<option value="Parfois" selected>Parfois</option>' in t
    assert "Valeur enregistrée : « janvier 2027 ». Elle est conservée tant que vous ne choisissez pas de date." in t
    assert t.index("Contrat") < t.index("Langue préférée") < t.index("Remarques")  # sections in order, schema order within a section


def test_saving_answers_creates_the_right_events(h, user, prof):
    html = user.get("/app/reponses").text
    data = {ans(p): v for p, v in [("identity.phone", "06 12 34 56 78"), ("identity.city", "Lyon"), ("situation.start", "2027-09-01"),
                                    ("situation.contract", "Stage"), ("misc.notes", "ligne1\r\nligne2"), ("misc.health", "")]}
    data.update({"tok_" + p.replace(".", "__"): token_of(html, p) for p in ("identity.phone", "identity.city", "situation.start",
                                                                            "situation.contract", "misc.notes", "misc.health")})
    data[f"cus_{CUSTOM_ID}"], data[f"ctok_{CUSTOM_ID}"] = "Négociable", re.search(rf'name="ctok_{CUSTOM_ID}" value="([^"]+)"', html).group(1)
    data[f"cus_{CUSTOM_MASKED}"], data[f"ctok_{CUSTOM_MASKED}"] = "", ver(8)
    r = post(user, "/reponses", data)
    assert r.status_code == 303 and r.headers["location"] == "/app/reponses"
    assert "Lyon" not in " ".join(r.headers.get_list("set-cookie"))  # a flash never carries typed values
    page = user.get(r.headers["location"]).text
    assert "4 réponses enregistrées, en attente du Mac." in page
    assert "Modification en attente du Mac" in page
    assert "bp_flash" in " ".join(r.headers.get_list("set-cookie"))
    inbox = h.client.get(f"{h.api}/profiles/{prof.key}/inbox", headers=h.pair().headers).json()["events"]
    got = {(e["kind"], e.get("path") or e.get("custom_id")): e.get("value") or e.get("answer") for e in inbox}
    assert got == {("answer", "identity.city"): "Lyon", ("answer", "situation.contract"): "Stage", ("answer", "misc.notes"): "ligne1\nligne2",
                   ("custom_edit", CUSTOM_ID): "Négociable"}
    assert user.get("/app/reponses").text.count("Modification en attente du Mac") >= 4
    # nothing changed: nothing stored
    h2 = user.get("/app/reponses").text
    data = {ans("identity.city"): "Lyon", "tok_identity__city": token_of(h2, "identity.city")}
    assert post(user, "/reponses", data).status_code == 303 and len(events(h, prof)) == 4
    assert "Aucune modification à enregistrer." in user.get("/app/reponses").text


def test_stale_token_is_reported_and_stores_nothing(h, user, prof):
    old = token_of(user.get("/app/reponses").text, "identity.city")
    assert post(user, "/reponses", {ans("identity.city"): "Lyon", "tok_identity__city": old}).status_code == 303
    r = post(user, "/reponses", {ans("identity.city"): "Nice", "tok_identity__city": old})
    page = follow(user, r).text
    assert "Ville : Cette réponse a changé entre-temps : rechargez la page." in page
    assert len(events(h, prof)) == 1 and events(h, prof)[0].value == "Lyon"


def test_invalid_input_is_reported_without_echo(h, user, prof):
    html = user.get("/app/reponses").text
    r = post(user, "/reponses", {ans("situation.start"): "pas-une-date", "tok_situation__start": token_of(html, "situation.start")})
    page = follow(user, r).text
    assert "Disponible dès le : Date non valide" in page and "pas-une-date" not in page and events(h, prof) == []


def test_unknown_posted_fields_are_ignored(h, user, prof):
    html = user.get("/app/reponses").text
    data = {"ans_identity__city": "Lyon", "tok_identity__city": token_of(html, "identity.city"), "ans_secret__path": "x",
            "tok_secret__path": ver(1), "ans_identity.city": "Paris"}
    post(user, "/reponses", data)
    assert [e.path for e in events(h, prof)] == ["identity.city"]


def test_write_only_and_masked_values_are_never_shown(h, user, prof):
    snap = make_snapshot()
    snap.answers["identity.city"] = AnswerValue(value=SECRET, is_set=True, ver=ver(2))  # a NON write-only field: the probe finds it
    push(h, prof, snap)
    t = user.get("/app/reponses").text
    assert SECRET in t
    # write-only field and masked custom answer: the snapshot holds no value, the UI states "Renseigné"/"Effacer"
    assert t.count("Renseigné") >= 2 and "Remplacer par…" in t and "Effacer cette réponse" in t
    health = re.search(r"Situation de handicap.*?</label>\s*<label class=\"check\">.*?</label>", t, re.S).group(0)
    assert 'value=""' in health and 'name="clr_misc__health"' in health
    masked = t[t.index("Avez-vous un handicap ?"):]
    assert 'name="cus_' + CUSTOM_MASKED + '" maxlength="500" placeholder="Remplacer par…"' in masked
    # replacing a write-only value stores it as an event but no page shows it
    typed = "VALEUR-SAISIE-9999"
    html = user.get("/app/reponses").text
    r = post(user, "/reponses", {ans("misc.health"): typed, "tok_misc__health": token_of(html, "misc.health")})
    assert typed not in r.text and typed not in str(r.headers)
    for path in ("/a-faire", "/reponses", "/accord", "/candidatures"):
        assert typed not in user.get(f"/app{path}").text
    assert events(h, prof)[0].value == typed
    # clearing
    html = user.get("/app/reponses").text
    post(user, "/reponses", {ans("misc.health"): "", "clr_misc__health": "1", "tok_misc__health": token_of(html, "misc.health")})
    assert events(h, prof)[-1].value == "" and events(h, prof)[-1].path == "misc.health"
    # masked custom answer replaced
    html = user.get("/app/reponses").text
    ctok = re.search(rf'name="ctok_{CUSTOM_MASKED}" value="([^"]+)"', html).group(1)
    post(user, "/reponses", {f"cus_{CUSTOM_MASKED}": "Réponse-masquée-777", f"ctok_{CUSTOM_MASKED}": ctok})
    assert events(h, prof)[-1].kind == "custom_edit"
    assert "Réponse-masquée-777" not in user.get("/app/reponses").text


def test_custom_removal_goes_through_a_confirmation_page(h, user, prof):
    html = user.get("/app/reponses").text
    ctok = re.search(rf'name="ctok_{CUSTOM_ID}" value="([^"]+)"', html).group(1)
    assert f'formaction="/app/reponses/retirer"' in html
    r = post(user, "/reponses/retirer", {"retirer": CUSTOM_ID, f"ctok_{CUSTOM_ID}": ctok})
    assert r.status_code == 200 and "Prétentions salariales" in r.text and events(h, prof) == []
    assert "/app/reponses/retirer/confirmer" in r.text
    assert follow(user, post(user, "/reponses/retirer", {"retirer": "zz"})).status_code == 200 and events(h, prof) == []
    r = post(user, "/reponses/retirer/confirmer", {"custom_id": CUSTOM_ID, "token": ctok})
    assert "Réponse retirée, en attente du Mac." in follow(user, r).text
    assert [e.kind for e in events(h, prof)] == ["custom_delete"]
    assert "Prétentions salariales" not in user.get("/app/reponses").text
    r = post(user, "/reponses/retirer/confirmer", {"custom_id": CUSTOM_ID, "token": ctok})  # again, now stale
    assert len(events(h, prof)) == 1


# ---------------------------------------------------------------- questions and intents
def test_answering_questions(h, user, prof):
    push(h, prof, rich_snapshot())
    r = post(user, "/a-faire/questions", {f"q_{QID}": "Oui", "q_" + "c2" * 8: "  ", "q_" + "f0" * 8: "x"})
    page = follow(user, r).text
    assert "1 réponse enregistrée, en attente du Mac." in page
    assert [(e.kind, e.qid, e.answer) for e in events(h, prof)] == [("question_answer", QID, "Oui")]
    assert "Réponse envoyée, en attente du Mac" in page and f'name="q_{QID}"' not in page and 'name="q_' + "c2" * 8 in page
    r = post(user, "/a-faire/questions", {f"q_{QID}": "Non"})  # already answered: ignored
    assert len(events(h, prof)) == 1
    # a value outside the options is refused with a fixed sentence
    push(h, prof, rich_snapshot().model_copy(update={"questions": [protocol.PendingQuestion(qid="c3" * 8, label="Choix", options=["A", "B"])]}))
    page = follow(user, post(user, "/a-faire/questions", {"q_" + "c3" * 8: "Zorglub"})).text
    assert "Choisissez l'une des propositions." in page and "Zorglub" not in page


def test_two_step_intent_flow(h, user, prof):
    push(h, prof, rich_snapshot())
    r = post(user, "/confirmer", {"g": GRANT_SEND_1, "retour": "accord"})
    assert r.status_code == 200 and events(h, prof) == []
    for needle in ("Envoyer cette candidature ?", "Société Exemple", "Technicien", "ats.example", f"/app/documents/{DOC_A}", JOB_URL,
                   "Confirmer", 'action="/app/intentions"', 'href="/app/accord"', f'name="g" value="{GRANT_SEND_1}"'):
        assert needle in r.text, needle
    r = post(user, "/intentions", {"g": GRANT_SEND_1, "retour": "accord"})
    assert r.status_code == 303 and r.headers["location"] == "/app/accord"
    assert "Demande envoyée au Mac" in follow(user, r).text
    assert [(e.kind, e.grant) for e in events(h, prof)] == [("intent", GRANT_SEND_1)]
    # the page now shows the waiting state instead of the buttons
    t = user.get("/app/accord").text
    assert "Demande envoyée, en attente du Mac" in t and "Tout envoyer (1)" in t
    # a second request for the same grant is refused
    r = post(user, "/intentions", {"g": GRANT_SEND_1, "retour": "accord"})
    assert "Cette candidature a changé : rechargez la page." in follow(user, r).text and len(events(h, prof)) == 1
    r = post(user, "/confirmer", {"g": GRANT_SEND_1, "retour": "accord"})
    assert r.status_code == 303 and "Ces demandes sont déjà en attente du Mac." in follow(user, r).text


def test_unconfirmed_sibling_and_email_resume(h, user, prof):
    push(h, prof, rich_snapshot())
    r = post(user, "/confirmer", {"g": GRANT_SENT})
    assert "Elle sera marquée comme envoyée et ne sera pas renvoyée." in r.text and "Marquer comme envoyée ?" in r.text
    r2 = post(user, "/confirmer", {"g": GRANT_NOT_SENT})
    assert "La candidature retournera dans la file et pourra être envoyée à nouveau." in r2.text
    assert post(user, "/intentions", {"g": GRANT_SENT}).status_code == 303
    assert "Demande envoyée, en attente du Mac" in user.get("/app/a-faire").text
    r = post(user, "/intentions", {"g": GRANT_NOT_SENT})
    assert "Cette candidature a changé" in follow(user, r).text and len(events(h, prof)) == 1
    assert follow(user, post(user, "/confirmer", {"g": GRANT_NOT_SENT})).status_code == 200  # sibling refused: back to the list
    r = post(user, "/confirmer", {"g": GRANT_EMAIL})
    assert "Reprendre cette candidature ?" in r.text
    assert post(user, "/intentions", {"g": GRANT_EMAIL}).status_code == 303 and len(events(h, prof)) == 2


def test_select_and_send_all(h, user, prof):
    push(h, prof, rich_snapshot())
    r = post(user, "/confirmer", {"g": [GRANT_SEND_1, GRANT_SEND_2], "retour": "accord"})
    assert "Envoyer ces 2 candidatures ?" in r.text
    r = post(user, "/confirmer", {"tout": "1", "retour": "accord"})
    assert "Envoyer ces 2 candidatures ?" in r.text and GRANT_SEND_2 in r.text
    r = post(user, "/confirmer", {"un": GRANT_SEND_2, "g": GRANT_SEND_1, "retour": "accord"})
    assert "Envoyer cette candidature ?" in r.text and GRANT_SEND_1 not in r.text
    r = post(user, "/confirmer", {"retour": "accord"})
    assert "Sélectionnez au moins une candidature." in follow(user, r).text
    r = post(user, "/intentions", {"g": [GRANT_SEND_1, GRANT_SEND_2], "retour": "accord"})
    assert "2 demandes envoyées au Mac" in follow(user, r).text and len(events(h, prof)) == 2


@pytest.mark.parametrize("grant", ["f0" * 16, "zz", GRANT_SEND_1.upper(), "a1" * 15, ""])
def test_grants_not_in_the_snapshot_are_refused(h, user, prof, grant):
    push(h, prof, rich_snapshot())
    r = post(user, "/confirmer", {"g": grant, "retour": "accord"})
    assert r.status_code == 303 and r.headers["location"] in ("/app/accord", "/app/a-faire")
    assert "Cette candidature a changé : rechargez la page." in follow(user, r).text or not grant
    r = post(user, "/intentions", {"g": grant})
    assert events(h, prof) == []
    r = post(user, "/confirmer", {"g": [GRANT_SEND_1, grant]})
    assert r.status_code == 303  # one unknown grant refuses the whole page
    r = post(user, "/confirmer", {"g": GRANT_SEND_1, "retour": "http://evil.example"})
    assert 'href="/app/a-faire"' in r.text


def test_grant_gone_after_a_new_snapshot(h, user, prof):
    push(h, prof, rich_snapshot())
    push(h, prof, make_snapshot(awaiting=[], email_pauses=[], unconfirmed=[]))
    r = post(user, "/intentions", {"g": GRANT_SEND_1})
    assert "Cette candidature a changé" in follow(user, r).text and events(h, prof) == []


def test_cancel_a_pending_intent(h, user, prof):
    push(h, prof, rich_snapshot())
    post(user, "/intentions", {"g": GRANT_SEND_1, "retour": "accord"})
    t = user.get("/app/a-faire").text
    assert "Envoi : Société Exemple — Technicien" in t and "Annuler" in t
    event_id = re.search(r'name="event" value="([0-9a-f]{32})"', t).group(1)
    assert event_id == events(h, prof)[0].event_id
    r = post(user, "/annuler", {"event": event_id})
    assert "Annulation demandée" in follow(user, r).text
    assert [e.kind for e in events(h, prof)] == ["intent", "cancel"]
    t = user.get("/app/a-faire").text
    assert 'name="event"' not in t and "Annulation demandée" in t
    assert "Annulation demandée" in user.get("/app/accord").text
    r = post(user, "/annuler", {"event": event_id})  # twice: too late
    assert "Trop tard ou déjà annulée" in follow(user, r).text and len(events(h, prof)) == 2
    assert post(user, "/annuler", {"event": "zz"}).status_code == 303 and len(events(h, prof)) == 2
    assert post(user, "/intentions", {"g": GRANT_SEND_1}).status_code == 303 and len(events(h, prof)) == 2


@pytest.mark.parametrize("too_late", [False, True])
def test_cancel_live_intent_page_waits_for_mac_outcome(h, user, prof, too_late):
    snap = rich_snapshot()
    device = h.pair()
    assert h.push_snapshot(device, prof, snap).status_code == 200
    assert post(user, "/intentions", {"g": GRANT_SEND_1}).status_code == 303
    intent, = events(h, prof)
    live = EventResult(event_id=intent.event_id, kind="intent", key=f"g:{GRANT_SEND_1}", status="queued", label="Envoi en file", at=intent.at)
    assert h.push_snapshot(device, prof, snap, acked_seq=1, results=[live]).status_code == 200
    assert events(h, prof) == []
    for path in ("/app/a-faire", "/app/accord"):
        assert field_value(user.get(path).text, "event") == intent.event_id
    assert "Annulation demandée" in follow(user, post(user, "/annuler", {"event": intent.event_id})).text
    assert "Annulation demandée" in user.get("/app/a-faire").text
    assert 'name="event"' not in user.get("/app/accord").text
    cancel, = events(h, prof)
    received = EventResult(event_id=cancel.event_id, kind="cancel", key=f"g:{GRANT_SEND_1}", status="received", at=cancel.at)
    assert h.push_snapshot(device, prof, snap, acked_seq=2, results=[received, live]).status_code == 200
    assert events(h, prof) == [] and "Annulation demandée" in user.get("/app/accord").text
    assert 'name="event"' not in user.get("/app/a-faire").text
    intent_result = live.model_copy(update={"status": "running" if too_late else "cancelled"})
    message = "Trop tard : l'action avait déjà commencé." if too_late else "Annulée."
    cancel_result = EventResult(event_id=cancel.event_id, kind="cancel", key=f"g:{GRANT_SEND_1}",
                                status="refused" if too_late else "done", message=message, at=cancel.at)
    assert h.push_snapshot(device, prof, snap, acked_seq=2, results=[cancel_result, intent_result]).status_code == 200
    page = user.get("/app/a-faire").text
    assert "Annulation demandée" not in page and message in page and 'name="event"' not in page
    assert (f'name="g" value="{GRANT_SEND_1}"' in user.get("/app/accord").text) is not too_late


# ---------------------------------------------------------------- documents, flash, static files
def test_document_download(h, user, prof):
    r = user.get(f"/app/documents/{DOC_A}")
    assert r.status_code == 200 and r.content == PDF_A and r.headers["content-type"] == "application/pdf"
    assert r.headers["content-disposition"] == f'attachment; filename="cv-{DOC_A[:8]}.pdf"'
    assert r.headers["content-security-policy"] == "sandbox" and r.headers["cache-control"] == "no-store"
    assert r.headers["x-content-type-options"] == "nosniff"
    for bad in ("0" * 32, "zz", DOC_A.upper(), DOC_A[:31], make_pdf("zzz").hex()[:32]):
        assert user.get(f"/app/documents/{bad}").status_code == 404, bad
    push(h, prof, make_snapshot(documents=[doc_ref(PDF_A)], sent=[], awaiting=[]))
    assert user.get(f"/app/documents/{DOC_B}").status_code == 404  # no longer listed by the current snapshot
    assert user.get(f"/app/documents/{DOC_A}").status_code == 200


def test_flash_shows_once_and_is_cleared(h, user, prof):
    r = post(user, "/a-faire/questions", {f"q_{QID}": "Oui"})
    flash = [c for c in r.headers.get_list("set-cookie") if c.startswith("__Secure-bp_flash=")][0]
    assert "HttpOnly" in flash and "Secure" in flash and "Path=/app/" in flash and "Max-Age=60" in flash
    assert len(flash.split(";")[0].split("=", 1)[1]) <= 600
    page = user.get("/app/a-faire")
    assert "1 réponse enregistrée, en attente du Mac." in page.text
    assert any(c.startswith("__Secure-bp_flash=") and "Max-Age=0" in c for c in page.headers.get_list("set-cookie"))
    assert "1 réponse enregistrée" not in user.get("/app/a-faire").text and user.cookies.get("__Secure-bp_flash") is None


def test_flash_expires_and_cannot_be_forged_or_moved(h, user, prof):
    portal = h.portal
    sid = user.cookies.get("__Secure-bp_session")
    value = pages.make_flash(portal, sid, h.clock(), ["Bien"], ["Pas bien"])
    assert pages.read_flash(portal, sid, value, h.clock() + 59) == (["Bien"], ["Pas bien"])
    assert pages.read_flash(portal, sid, value, h.clock() + 61) == ([], [])
    assert pages.read_flash(portal, "autre-session", value, h.clock()) == ([], [])
    body, _, mac = value.rpartition(".")
    assert pages.read_flash(portal, sid, body + "A." + mac, h.clock()) == ([], [])
    assert pages.read_flash(portal, sid, "n.importe.quoi", h.clock()) == ([], [])
    long = pages.make_flash(portal, sid, h.clock(), ["x" * 500] * 6, ["y" * 500] * 6)
    assert long is None or len(long) <= 600
    user.cookies.set("__Secure-bp_flash", value, path="/app/")
    h.clock.advance(61)
    assert "Bien" not in user.get("/app/a-faire").text


def test_static_files(h, b):
    for name, marker in (("style.css", "--acc:"), ("portal.css", ".pnav")):
        r = b.get(f"/app/static/{name}")
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/css") and marker in r.text
        assert r.headers["cache-control"] == "public, max-age=3600"
        assert r.headers["content-security-policy"].startswith("default-src 'none'")
    for path in ("/app/static/", "/app/static", "/app/static/other.css", "/app/static/../server/pages.py", "/app/static/%2e%2e/app.py",
                 "/app/static/templates/base.html"):
        assert b.get(path).status_code in (404, 405, 308), path
    t = b.get("/app/connexion").text
    assert '/app/static/style.css?v=abc123"' in t and '/app/static/portal.css?v=abc123"' in t


def test_custom_base_path(tmp_path, monkeypatch):
    fast_scrypt(monkeypatch)
    hp = Harness.create(tmp_path, PORTAL_BASE_PATH="/portail")
    try:
        c = TestClient(hp.app, base_url=ORIGIN)
        t = c.get("/portail/connexion").text
        assert 'action="/portail/connexion"' in t and "/app" not in t.replace("/portail", "")
        assert c.get("/portail/static/portal.css").status_code == 200
        assert c.get("/portail/a-faire", follow_redirects=False).headers["location"] == "/portail/connexion"
    finally:
        hp.portal.db.close()


# ---------------------------------------------------------------- markup rules
def check_markup(html: str, base: str = "/app", allowed_urls: tuple[str, ...] = (JOB_URL,)) -> None:
    low = html.lower()
    assert "<script" not in low and "<style" not in low and "style=" not in low
    assert not re.search(r"\son[a-z]+\s*=", low)
    for url in re.findall(r"https?://[^\s\"'<>]+", html):
        assert url in allowed_urls, url
    for attr, value in re.findall(r'\b(href|action|src|formaction)="([^"]*)"', html):
        assert value.startswith(base + "/") or value in allowed_urls or value.startswith("#"), (attr, value)


def test_every_page_obeys_the_markup_rules_and_headers(h, user, prof):
    seed_results(h, prof)
    pages_ = {p: user.get(f"/app{p}", follow_redirects=False) for p in PAGES}
    pages_["/connexion-anon"] = Browser(h.app, base_url=ORIGIN).get("/app/connexion")
    pages_["/anon"] = Browser(h.app, base_url=ORIGIN).get("/app/")
    pages_["/confirmer"] = post(user, "/confirmer", {"g": [GRANT_SEND_1, GRANT_SEND_2]})
    pages_["/retirer"] = post(user, "/reponses/retirer", {"retirer": CUSTOM_ID})
    for name, r in pages_.items():
        assert r.status_code == 200, name
        check_markup(raw(r))
        assert r.headers["content-security-policy"].startswith("default-src 'none'; style-src 'self'; img-src 'self'; form-action 'self'"), name
        assert r.headers["cache-control"] == "no-store", name
        assert r.headers["content-type"].startswith("text/html"), name
        assert '<html lang="fr">' in r.text and 'name="viewport"' in r.text
    # job links are the only external URLs, and carry rel
    links = re.findall(r'<a href="(https?://[^"]+)"([^>]*)>', raw(user.get("/app/accord")))
    assert links and all('rel="noopener noreferrer"' in rel for _, rel in links)


def test_only_http_urls_are_linked():
    assert not pages.safe_url("javascript:alert(1)") and not pages.safe_url(None) and not pages.safe_url("ftp://x.example/a")
    assert pages.safe_url("https://a.example/x") and pages.safe_url("http://a.example/x")


def test_xss_probe_is_escaped_everywhere(h, user, prof):
    schema = make_schema()
    for f in schema.fields:
        object.__setattr__(f, "label", f.label + XSS)
        object.__setattr__(f, "help", "Aide" + XSS)
        object.__setattr__(f, "placeholder", "Ph" + XSS)
        object.__setattr__(f, "section", f.section)
    snap = rich_snapshot(XSS).model_copy(update={"display_name": "Léa" + XSS, "answer_schema": schema})
    snap.answers["identity.city"] = AnswerValue(value="Ville" + XSS, is_set=True, ver=ver(2))
    snap.answers["situation.contract"] = AnswerValue(value="Autre" + XSS, is_set=True, ver=ver(4))
    snap = snap.model_copy(update={"custom": [CustomAnswer(custom_id=CUSTOM_ID, question="Q" + XSS, answer="R" + XSS, ver=ver(7))],
                                   "questions": [protocol.PendingQuestion(qid=QID, label="Q" + XSS, options=["A" + XSS, "B"],
                                                                          company="C" + XSS, title="T" + XSS)]})
    results = [EventResult(event_id="1" * 32, kind="answer", status="failed", message="M" + XSS, label="L" + XSS,
                           at=datetime(2027, 1, 15, 8, 30, tzinfo=UTC))]
    assert h.push_snapshot(h.pair(), prof, snap, results=results).status_code == 200
    checked = 0
    for path in PAGES:
        r = user.get(f"/app{path}")
        assert r.status_code == 200, path
        assert XSS not in raw(r) and "<img" not in raw(r), path
        checked += raw(r).count("&#34;&gt;&lt;img src=x onerror=1&gt;")
    assert checked > 10
    r = post(user, "/confirmer", {"g": GRANT_SEND_1})
    assert XSS not in raw(r) and "&#34;&gt;&lt;img" in raw(r)
    r = post(user, "/reponses/retirer", {"retirer": CUSTOM_ID})
    assert XSS not in raw(r) and "<img" not in raw(r)


def test_old_session_cookie_dies_with_a_new_passcode_epoch(h, user, prof):
    h.put_profile(h.pair(), prof, config_rev=2, sessions_epoch=1)
    assert user.get("/app/a-faire", follow_redirects=False).status_code == 303
    assert post(user, "/reponses", {}).status_code == 303 and events(h, prof) == []
