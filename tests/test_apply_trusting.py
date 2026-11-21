"""Planner-trusting field handling (guard mode `core`): the engine runs the planner's mapping as given.

Pure unit tests build fields / snapshots / plans by hand and drive `trusting.*` with a minimal fake run; a few browser tests
cover the execution helpers (_toggle, blocker, dismiss_blocker). Every name, address and page here is fictional.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from apply_fakes import page, sample_facts  # noqa: F401

from jobhunt.apply import engine, trusting
from jobhunt.apply.types import Action, FactSheet, Fact, FieldOption, FormField, PageSnapshot, Question, StepPlan

URL = "https://careers.example.test/apply"


# ------------------------------------------------------------------ builders
class FakeRun:
    """Exactly what trusting.py reads from a run; the pure engine methods are the real ones."""

    mode = "submit"
    signup_scope = None
    login_box: set = set()
    combo_options: dict = {}
    _drop = engine._Run._drop
    _identity_item = engine._Run._identity_item
    _autonomous_decision = engine._Run._autonomous_decision
    _frame_url = engine._Run._frame_url
    _label_matches = staticmethod(engine._Run._label_matches)
    _has_first_name = staticmethod(engine._Run._has_first_name)
    _dial_option = staticmethod(engine._Run._dial_option)
    _cv_by_code = staticmethod(engine._Run._cv_by_code)

    def __init__(self, facts: FactSheet | None = None, *, app_touched: bool = False, secret: str | None = "pw-for-tests",
                 satisfied: bool = False):
        self.facts = facts or sample_facts()
        self.page = SimpleNamespace(url=URL)
        self.app_touched = app_touched
        self._secret_value = secret
        self._satisfied = satisfied

    def _secret(self, frame_url):
        return self._secret_value

    def _file_satisfied(self, field, snap):
        return self._satisfied


def run_of(facts: FactSheet | None = None, **kw) -> FakeRun:
    return FakeRun(facts, **kw)


def opts(*labels: str) -> list[FieldOption]:
    return [FieldOption(value=f"v{i}", label=label) for i, label in enumerate(labels)]


def field(ref="f1", kind="text", label="", **kw) -> FormField:
    return FormField(ref=ref, kind=kind, label=label, **kw)


def snap_of(*fields: FormField) -> PageSnapshot:
    return PageSnapshot(url=URL, fields=list(fields))


def plan_of(*actions: Action, kind="form", unknown=()) -> StepPlan:
    return StepPlan(page_kind=kind, actions=list(actions), unknown=list(unknown))


def do_check(run, action, f, snap=None):
    v = engine._Validated()
    out = trusting.check(run, action, f, v, snap or snap_of(f), None)
    return out, v


# ------------------------------------------------------------------ best_option
@pytest.mark.parametrize("labels, want, expected", [
    (["Oui", "Non"], "Non", 1),
    (["Paris", "Lyon"], "lyon", 1),
    (["Oui", "Non"], "no", 1),
    (["Oui", "Non"], "yes", 0),
    (["Monsieur", "Madame"], "Mme", 1),
    (["Oui, je suis disponible", "Non, aucun proche"], "Non", 1),
    (["Allemagne (+49)", "France (+33)", "Italie (+39)"], "France", 1),
    (["Bac", "Bac+2", "Bac+4", "Bac+5"], "bac 4", 2),
    (["Bac+4 en informatique", "Bac+4 info"], "Bac+4", 1),  # several that start with it: the shortest wins
    (["Ingénieur logiciel", "Ingénieur système, réseau"], "ingénieur", 0),
    (["Option A (France)", "Option B (France)"], "France", -1),  # ambiguous containment
    (["Paris", "Lyon"], "Marseille", -1),
    (["Paris", "Lyon"], "", -1),
    (["Paris", "Lyon"], None, -1),
])
def test_best_option(labels, want, expected):
    assert trusting.best_option(labels, want) == expected


# ------------------------------------------------------------------ check: text
def test_exact_fact_value_replaces_the_planner_text():
    f = field(label="Prénom", required=True)
    out, _ = do_check(run_of(), Action(ref="f1", op="fill", fact="first_name", value="CAMILLE!!"), f)
    assert out.act.op == "fill" and out.value == "Camille" and out.used == "first_name" and not out.inferred


def test_non_exact_fact_uses_the_planner_value():
    f = field(label="Ville souhaitée", required=True)
    out, _ = do_check(run_of(), Action(ref="f1", op="fill", fact="city", value="Toulouse (31)"), f)
    assert out.value == "Toulouse (31)" and out.used == "city"


def test_inferred_answer_on_required_field_is_kept_and_flagged():
    f = field(label="Quel est votre niveau d'anglais ?", required=True)
    action = Action(ref="f1", op="fill", value="Courant", inferred=True, basis="Profil bilingue")
    out, v = do_check(run_of(), action, f)
    assert out is not None and not v.questions
    assert out.value == "Courant" and out.inferred and out.basis == "Profil bilingue" and out.used == ""


def test_answer_without_fact_gets_a_default_basis():
    f = field(label="Compétences clés", required=True)
    out, _ = do_check(run_of(), Action(ref="f1", op="fill", value="Python"), f)
    assert out.inferred and out.basis == "déduit du profil"


@pytest.mark.parametrize("label", ["Date de naissance", "Adresse postale"])
def test_hard_fact_without_known_fact_is_dropped_and_asked(label):
    f = field(label=label, required=True)
    out, v = do_check(run_of(), Action(ref="f1", op="fill", value="01/01/1990", inferred=True), f)
    assert out is None
    assert [q.ref for q in v.questions] == ["f1"] and "jamais deviné" in v.notes[0]


def test_hard_fact_optional_is_dropped_without_question():
    f = field(label="Date de naissance", required=False)
    out, v = do_check(run_of(), Action(ref="f1", op="fill", value="01/01/1990"), f)
    assert out is None and not v.questions and v.notes


def test_hard_fact_with_known_fact_is_filled():
    facts = FactSheet(facts=[Fact(key="birth_date", label="Date de naissance", value="01/02/1995")])
    f = field(label="Date de naissance", required=True)
    out, v = do_check(run_of(facts), Action(ref="f1", op="fill", fact="birth_date"), f)
    assert out is not None and out.value == "01/02/1995" and out.used == "birth_date" and not v.questions


def test_empty_value_is_dropped():
    f = field(label="Commentaire", required=True)
    out, v = do_check(run_of(), Action(ref="f1", op="fill", value="  "), f)
    assert out is None and [q.ref for q in v.questions] == ["f1"]


def test_already_correct_text_is_a_noop():
    f = field(label="Prénom", required=True, value="camille")
    out, v = do_check(run_of(), Action(ref="f1", op="fill", fact="first_name"), f)
    assert out is None and not v.questions and not v.notes


def test_race_ethnicity_fact_is_never_typed():
    facts = sample_facts()
    facts.facts.append(Fact(key="race_ethnicity", label="Origine", value="Asian"))
    f = field(label="Ethnicity", required=True)
    out, v = do_check(run_of(facts), Action(ref="f1", op="fill", fact="race_ethnicity", value="Asian"), f)
    assert out is None and [q.ref for q in v.questions] == ["f1"]


def test_input_action_on_a_button_is_dropped_quietly():
    f = field(kind="button", label="Suivant", required=True)
    out, v = do_check(run_of(), Action(ref="f1", op="fill", value="x"), f)
    assert out is None and not v.questions and v.notes


def test_file_field_only_takes_cv_or_letter():
    f = field(kind="file", label="Pièce jointe", required=True)
    out, v = do_check(run_of(), Action(ref="f1", op="fill", value="x"), f)
    assert out is None and [q.ref for q in v.questions] == ["f1"]
    cv, _ = do_check(run_of(), Action(ref="f1", op="upload_cv"), f)
    assert cv.act.op == "upload_cv" and cv.used == "cv"


def test_upload_already_satisfied_is_dropped_and_optional_letter_ignored():
    f = field(kind="file", label="CV", required=True)
    out, v = do_check(run_of(satisfied=True), Action(ref="f1", op="upload_cv"), f)
    assert out is None and not v.questions
    letter = field(kind="file", label="Lettre", required=False)
    out, v = do_check(run_of(), Action(ref="f1", op="upload_letter"), letter)
    assert out is None and not v.questions
    letter = field(kind="file", label="Lettre", required=True)
    out, _ = do_check(run_of(), Action(ref="f1", op="upload_letter"), letter)
    assert out.used == "letter"


def test_upload_on_a_text_field_and_letter_text_on_a_select_are_dropped():
    out, v = do_check(run_of(), Action(ref="f1", op="upload_cv"), field(label="Nom", required=True))
    assert out is None and v.questions
    out, v = do_check(run_of(), Action(ref="f1", op="letter_text"), field(kind="select", label="Niveau d'études", required=True, options=opts("A")))
    assert out is None
    out, _ = do_check(run_of(), Action(ref="f1", op="letter_text"), field(kind="textarea", label="Message", required=True))
    assert out.used == "letter"


# ------------------------------------------------------------------ check: passwords
def test_password_field_gets_only_the_secret_fact():
    f = field(kind="password", label="Mot de passe", required=True)
    out, _ = do_check(run_of(), Action(ref="f1", op="fill", value="hunter2"), f)
    assert out.used == "account_password" and out.fact.secret and out.frame_url == URL and out.value is None


def test_secret_fact_never_goes_into_a_non_password_field():
    f = field(kind="text", label="Identifiant", required=True)
    out, v = do_check(run_of(), Action(ref="f1", op="fill", fact="account_password"), f)
    assert out is None and [q.ref for q in v.questions] == ["f1"]


def test_password_dropped_when_not_submitting_or_without_credential():
    f = field(kind="password", label="Mot de passe", required=True)
    dry = run_of()
    dry.mode = "dry_run"
    assert do_check(dry, Action(ref="f1", op="fill"), f)[0] is None
    assert do_check(run_of(secret=None), Action(ref="f1", op="fill"), f)[0] is None
    nofact = run_of(FactSheet(facts=[Fact(key="first_name", label="Prénom", value="Camille")]))
    assert do_check(nofact, Action(ref="f1", op="fill"), f)[0] is None


# ------------------------------------------------------------------ check: choices
def test_select_maps_the_answer_to_an_option_index():
    f = field(kind="select", label="Niveau d'études", required=True, options=opts("Allemagne", "France", "Italie"))
    out, _ = do_check(run_of(), Action(ref="f1", op="select", value="france"), f)
    assert out.act.op == "select" and out.option_index == 1 and out.value == "France"


def test_radio_yes_no_alias_maps_to_french_option():
    f = field(kind="radio", label="Travaillez-vous en équipe ?", required=True, options=opts("Oui", "Non"))
    out, _ = do_check(run_of(), Action(ref="f1", op="select", value="no", inferred=True), f)
    assert out.option_index == 1 and out.value == "Non"


def test_select_value_outside_the_options_is_dropped():
    f = field(kind="select", label="Niveau d'études", required=True, options=opts("Allemagne", "France"))
    out, v = do_check(run_of(), Action(ref="f1", op="select", value="Japon"), f)
    assert out is None and "hors des options" in v.notes[0] and [q.ref for q in v.questions] == ["f1"]


def test_select_already_on_the_answer_is_a_noop():
    f = field(kind="select", label="Niveau d'études", required=True, options=opts("Allemagne", "France"), value="France")
    assert do_check(run_of(), Action(ref="f1", op="select", value="France"), f)[0] is None


def test_select_falls_back_to_the_fact_value():
    f = field(kind="select", label="Situation", required=True, options=opts("Stage", "Alternance"))
    out, _ = do_check(run_of(), Action(ref="f1", op="select", fact="situation", value="???"), f)
    assert out.option_index == 1 and out.used == "situation"


def test_combobox_with_unmatched_answer_is_typed():
    f = field(kind="combobox", label="Secteur préféré", required=True, options=opts("Paris", "Lyon"))
    out, _ = do_check(run_of(), Action(ref="f1", op="select", value="Toulouse", inferred=True), f)
    assert out.act.op == "select" and out.value == "Toulouse" and out.option_index == -1


def test_check_op_on_text_field_is_dropped():
    out, v = do_check(run_of(), Action(ref="f1", op="check"), field(label="Nom", required=True))
    assert out is None and "incompatible" in v.notes[0]


# ------------------------------------------------------------------ check: checkboxes
@pytest.mark.parametrize("answer, op", [("Oui", "check"), ("oui", "check"), ("Non", "uncheck"), ("no", "uncheck")])
def test_checkbox_fill_with_yes_no_becomes_check_or_uncheck(answer, op):
    state = "false" if op == "check" else "true"
    f = field(kind="checkbox", label="Je suis majeur", required=True, value=state)
    out, _ = do_check(run_of(), Action(ref="f1", op="fill", value=answer, inferred=True), f)
    assert out.act.op == op


def test_checkbox_fill_with_unreadable_answer_is_dropped():
    f = field(kind="checkbox", label="Je suis majeur", required=True, value="false")
    out, v = do_check(run_of(), Action(ref="f1", op="fill", value="peut-être"), f)
    assert out is None and "illisible" in v.notes[0]


def test_optional_marketing_checkbox_is_never_ticked():
    f = field(kind="checkbox", label="Je souhaite recevoir la newsletter", required=False, value="false")
    out, v = do_check(run_of(), Action(ref="f1", op="check"), f)
    assert out is None and not v.questions and "facultative" in v.notes[0]


def test_checkbox_already_in_wanted_state_is_a_noop():
    ticked = field(kind="checkbox", label="Je certifie l'exactitude", required=True, value="true")
    assert do_check(run_of(), Action(ref="f1", op="check"), ticked)[0] is None
    empty = field(kind="checkbox", label="Je certifie l'exactitude", required=True, value="false")
    assert do_check(run_of(), Action(ref="f1", op="uncheck"), empty)[0] is None


# ------------------------------------------------------------------ check: phone
def phone_snapshot(code_kind="select"):
    code = field(ref="f1", kind=code_kind, label="Indicatif", options=opts("+33 France", "+49 Allemagne", "+39 Italie"),
                 group="tel", form="0:1", required=True)
    number = field(ref="f2", kind="tel", label="Téléphone", group="tel", form="0:1", required=True)
    return code, number, snap_of(code, number)


def test_phone_next_to_a_calling_code_picker_is_national():
    code, number, snap = phone_snapshot()
    assert trusting.beside_dial(number, snap)
    out, _ = do_check(run_of(), Action(ref="f2", op="fill", fact="phone"), number, snap)
    assert out.national is True and out.used == "phone"


def test_phone_alone_is_not_national():
    number = field(ref="f2", kind="tel", label="Téléphone", required=True)
    out, _ = do_check(run_of(), Action(ref="f2", op="fill", fact="phone"), number, snap_of(number))
    assert out.national is False and out.value == "+33 6 00 00 00 00"


def test_calling_code_picker_selects_the_candidates_country():
    code, _, snap = phone_snapshot()
    out, _ = do_check(run_of(), Action(ref="f1", op="select", value="+49 Allemagne"), code, snap)
    assert out.option_index == 0 and out.value == "+33 France"  # the candidate's own number wins over the planner


def test_national_number_already_typed_is_a_noop():
    _, number, snap = phone_snapshot()
    number.value = "06 00 00 00 00"
    assert do_check(run_of(), Action(ref="f2", op="fill", fact="phone"), number, snap)[0] is None


# ------------------------------------------------------------------ owner_item
def test_required_privacy_checkbox_is_ticked():
    f = field(kind="checkbox", label="J'accepte la politique de confidentialité", required=True, value="false")
    out = trusting.owner_item(run_of(), f)
    assert out.act.op == "check" and out.used == "application_privacy"


def test_required_terms_checkbox_uses_the_terms_code():
    f = field(kind="checkbox", label="J'accepte les conditions d'utilisation", required=True, value="false")
    out = trusting.owner_item(run_of(), f)
    assert out.act.op == "check" and out.used == "application_terms"


def test_required_consent_already_ticked_is_settled():
    f = field(kind="checkbox", label="J'accepte la politique de confidentialité", required=True, value="true")
    assert trusting.owner_item(run_of(), f) is True


def test_optional_newsletter_already_ticked_is_unticked():
    f = field(kind="checkbox", label="Recevoir la newsletter", required=False, value="true")
    out = trusting.owner_item(run_of(), f)
    assert out.act.op == "uncheck" and out.used == "optional_opt_out"


def test_optional_newsletter_left_alone_is_settled():
    f = field(kind="checkbox", label="Recevoir la newsletter", required=False, value="false")
    assert trusting.owner_item(run_of(), f) is True


def test_relatives_radio_is_answered_no():
    f = field(kind="radio", label="Avez-vous des proches travaillant chez ExempleCo ?", required=True, options=opts("Oui", "Non"))
    out = trusting.owner_item(run_of(), f)
    assert out.act.op == "select" and out.option_index == 1 and out.value == "Non" and out.used == "relatives_at_employer"


def test_relatives_radio_already_no_is_settled():
    f = field(kind="radio", label="Avez-vous des proches travaillant chez ExempleCo ?", required=True, options=opts("Oui", "Non"), value="Non")
    assert trusting.owner_item(run_of(), f) is True


def test_unruled_fields_have_no_owner_item():
    assert trusting.owner_item(run_of(), field(label="Prénom", required=True)) is None
    assert trusting.owner_item(run_of(), field(kind="checkbox", label="Je suis disponible", required=True, value="false")) is None
    assert trusting.owner_item(run_of(), field(kind="password", label="Mot de passe")) is None
    assert trusting.owner_item(run_of(), field(kind="button", label="Suivant")) is None


# ------------------------------------------------------------------ validate
def test_owner_rule_wins_over_the_planner_action():
    f = field(kind="checkbox", label="Recevoir la newsletter", required=False, value="true")
    plan = plan_of(Action(ref="f1", op="check"))
    v = trusting.validate(run_of(), plan, snap_of(f), None)
    assert [(i.act.op, i.used) for i in v.items] == [("uncheck", "optional_opt_out")]


def test_unknown_ref_is_noted_and_ignored():
    f = field(label="Prénom", required=True)
    v = trusting.validate(run_of(), plan_of(Action(ref="zz", op="fill", value="x")), snap_of(f), None)
    assert any("référence inconnue" in n for n in v.notes)
    assert all(i.field.ref != "zz" for i in v.items)


def test_only_the_last_click_is_kept_and_a_click_without_role_is_ignored():
    a = field(ref="b1", kind="button", label="Précédent")
    b = field(ref="b2", kind="button", label="Suivant")
    c = field(ref="b3", kind="button", label="Aide")
    plan = plan_of(Action(ref="b1", op="click", click_role="next"), Action(ref="b2", op="click", click_role="next"),
                   Action(ref="b3", op="click"))
    v = trusting.validate(run_of(), plan, snap_of(a, b, c), None)
    assert v.click is not None and v.click[0].ref == "b2" and v.click[1] == "next"
    assert any("clic ignoré" in n and "Aide" in n for n in v.notes)


def test_a_click_with_role_other_is_kept():
    # (closing a dialog, opening a section: the planner's own click, never taken for a send unless its label says so)
    close = field(ref="b1", kind="button", label="Annuler")
    v = trusting.validate(run_of(), plan_of(Action(ref="b1", op="click", click_role="other")), snap_of(close), None)
    assert v.click is not None and v.click == (close, "other")


def test_click_on_a_non_button_is_ignored():
    f = field(label="Prénom")
    v = trusting.validate(run_of(), plan_of(Action(ref="f1", op="click", click_role="next")), snap_of(f), None)
    assert v.click is None and v.notes


def test_uploads_are_sorted_first():
    name = field(ref="f1", label="Ville", required=True)
    cv = field(ref="f2", kind="file", label="CV", required=True)
    plan = plan_of(Action(ref="f1", op="fill", fact="city"), Action(ref="f2", op="upload_cv"))
    v = trusting.validate(run_of(), plan, snap_of(name, cv), None)
    assert [i.act.op for i in v.items] == ["upload_cv", "fill"]


def test_each_ref_is_handled_once():
    f = field(label="Compétences", required=True)
    plan = plan_of(Action(ref="f1", op="fill", value="Toulouse", inferred=True), Action(ref="f1", op="fill", value="Paris", inferred=True))
    v = trusting.validate(run_of(), plan, snap_of(f), None)
    assert [i.value for i in v.items] == ["Toulouse"]


def test_identity_fields_are_not_autofilled_on_a_job_page():
    f = field(label="Prénom", required=True)
    v = trusting.validate(run_of(), plan_of(kind="job_page"), snap_of(f), None)
    assert v.items == []


def test_job_page_applies_no_owner_rule_either():
    f = field(kind="checkbox", label="J'accepte la politique de confidentialité", required=True, value="false")
    v = trusting.validate(run_of(), plan_of(kind="job_page"), snap_of(f), None)
    assert v.items == []


def test_empty_first_name_the_planner_left_alone_is_filled_on_a_form():
    f = field(label="Prénom", required=True)
    v = trusting.validate(run_of(), plan_of(kind="form"), snap_of(f), None)
    assert [(i.used, i.value) for i in v.items] == [("first_name", "Camille")]


def test_phone_left_alone_next_to_a_dial_picker_is_national():
    _, number, snap = phone_snapshot()
    v = trusting.validate(run_of(), plan_of(kind="form"), snap, None)
    phones = [i for i in v.items if i.used == "phone"]
    assert len(phones) == 1 and phones[0].national is True


def test_required_empty_cv_field_is_uploaded_by_code():
    cv = field(ref="f1", kind="file", label="CV", required=True)
    v = trusting.validate(run_of(), plan_of(kind="form"), snap_of(cv), None)
    assert [(i.act.op, i.used) for i in v.items] == [("upload_cv", "cv")]
    v = trusting.validate(run_of(satisfied=True), plan_of(kind="form"), snap_of(cv), None)
    assert v.items == []


def test_signup_page_fills_the_empty_password_field_from_the_credential():
    pw = field(ref="f1", kind="password", label="Mot de passe", required=True)
    v = trusting.validate(run_of(), plan_of(kind="account_signup"), snap_of(pw), None)
    assert [i.used for i in v.items] == ["account_password"]


def test_login_box_outside_the_signup_scope_is_skipped():
    run = run_of()
    run.signup_scope = {"f2"}
    login = field(ref="f1", kind="password", label="Mot de passe", required=True)
    signup = field(ref="f2", kind="password", label="Créer un mot de passe", required=True)
    plan = plan_of(Action(ref="f1", op="fill"), kind="account_signup")
    v = trusting.validate(run, plan, snap_of(login, signup), None)
    assert [i.field.ref for i in v.items] == ["f2"]


# ------------------------------------------------------------------ account-creation form of a large multi-step ATS
def create_account_form(consent="J'ai lu et j'accepte les conditions générales", form="0:0"):
    return snap_of(
        field(ref="f1", label="Adresse e-mail", required=True, form=form),
        field(ref="f2", kind="password", label="Mot de passe", required=True, form=form),
        field(ref="f3", kind="password", label="Confirmer le nouveau mot de passe", required=True, form=form),
        field(ref="f4", kind="checkbox", label=consent, value="false", form=form),  # not flagged required by the markup
        field(ref="f5", label="Enter website. This input is for robots only, do not enter if you're human.", name="website", form=form),
        field(ref="f6", kind="button", label="Créer un compte", form=form),
        field(ref="f7", kind="button", label="Créer un compte", form=form),  # the click overlay over the real button
    )


@pytest.mark.parametrize("consent", [
    "J'ai lu et j'accepte les conditions générales",
    "Oui, j’ai lu et compris la notice d’information sur la confidentialité.",
    "Je reconnais que mes données peuvent être transférées en dehors de mon pays et je ne m'y oppose pas.",
    "Je consens au traitement de mes données personnelles dans le cadre du processus de recrutement",
])
def test_signup_consent_box_is_ticked_even_when_not_flagged_required(consent):
    v = trusting.validate(run_of(), plan_of(kind="account_signup"), create_account_form(consent), None)
    assert [(i.field.ref, i.act.op) for i in v.items if i.field.kind == "checkbox"] == [("f4", "check")]
    assert {i.used for i in v.items if i.field.ref == "f4"} <= {"application_privacy", "application_terms"}


@pytest.mark.parametrize("label", ["Je souhaite recevoir la newsletter et les offres de nos partenaires",
                                   "Ajoutez-moi au vivier de talents pour de futures opportunités",
                                   "Recevoir des alertes emploi"])
def test_signup_marketing_box_is_never_ticked(label):
    v = trusting.validate(run_of(), plan_of(kind="account_signup"), create_account_form(label), None)
    assert not [i for i in v.items if i.field.kind == "checkbox" and i.act.op == "check"]


def test_non_required_consent_box_outside_an_account_page_is_left_to_the_planner():
    box = field(ref="f4", kind="checkbox", label="J'accepte les conditions générales", value="false")
    assert trusting.validate(run_of(), plan_of(kind="form"), snap_of(box), None).items == []
    # ... but a box the site sent back with "Please check the box to continue" is required
    box.error = "Please check the box to continue"
    v = trusting.validate(run_of(), plan_of(kind="form"), snap_of(box), None)
    assert [(i.field.ref, i.act.op) for i in v.items] == [("f4", "check")]


def test_signup_consent_box_already_ticked_is_left_alone():
    snap = create_account_form()
    snap.fields[3].value = "true"
    plan = plan_of(Action(ref="f4", op="uncheck"), kind="account_signup")
    assert not [i for i in trusting.validate(run_of(), plan, snap, None).items if i.field.ref == "f4"]


def test_honeypot_input_is_never_filled():
    snap = create_account_form()
    plan = plan_of(Action(ref="f5", op="fill", value="https://example.test"), Action(ref="f1", op="fill", fact="account_email"),
                   kind="account_signup")
    v = trusting.validate(run_of(), plan, snap, None)
    assert "f5" not in {i.field.ref for i in v.items}
    assert {"f1", "f2", "f3", "f4"} <= {i.field.ref for i in v.items}
    assert trusting.honeypot(snap.fields[4])
    assert not trusting.honeypot(field(label="Site web personnel", name="website"))
    assert not trusting.honeypot(field(label="Adresse e-mail"))


def test_the_one_account_creation_button_is_found_by_code():
    snap = create_account_form()
    assert engine._Run._signup_submit(snap).ref == "f6"
    snap.fields.append(field(ref="f8", kind="button", label="Sign up with Google", form="0:0"))
    assert engine._Run._signup_submit(snap).ref == "f6"  # (a social button is never one)
    snap.fields.append(field(ref="f9", kind="button", label="Créer mon profil", form="0:0"))
    assert engine._Run._signup_submit(snap) is None  # two different creation buttons: ambiguous
    assert engine._Run._signup_submit(snap_of(field(ref="f1", kind="button", label="Créer un compte"))) is None  # no password form


def test_application_form_shape():
    required = [field(ref=f"f{i}", label=label, required=True) for i, label in enumerate(("Prénom", "Nom", "E-mail"))]
    assert engine._Run._application_form(snap_of(*required))
    assert not engine._Run._application_form(snap_of(*required[:2]))
    assert not engine._Run._application_form(snap_of(*required, field(ref="p", kind="password", label="Mot de passe")))


@pytest.mark.parametrize("text, pending", [
    ("Account created. Please verify your account using the link we sent to your e-mail.", True),
    ("Vérifiez votre adresse e-mail : un lien de vérification vous a été envoyé.", True),
    ("Bitte bestätigen Sie Ihr Konto über die Bestätigungs-E-Mail.", True),
    ("Resend Account Verification", True),
    ("Sign In. Email Address. Password. Forgot your password?", False),
    ("Connexion. Vous avez oublié votre mot de passe ?", False),
])
def test_pending_verification_wording(text, pending):
    from jobhunt.apply.answer_check import fold
    assert bool(engine.VERIFY_PENDING_RE.search(fold(text))) is pending


# ------------------------------------------------------------------ open_questions
def test_open_questions_on_a_job_page_is_empty():
    f = field(label="Ville", required=True)
    plan = plan_of(kind="job_page", unknown=[Question(ref="f1", label="Ville")])
    assert trusting.open_questions(run_of(), plan, snap_of(f), engine._Validated(), set(), None) == ([], [])


def test_planner_unknown_on_empty_required_field_blocks():
    f = field(label="Prétentions salariales", required=True)
    plan = plan_of(unknown=[Question(ref="f1", label="Prétentions salariales", required=True)])
    blocking, soft = trusting.open_questions(run_of(), plan, snap_of(f), engine._Validated(), set(), None)
    assert [q.ref for q in blocking] == ["f1"] and soft == []


def test_planner_unknown_on_a_filled_field_does_not_block():
    f = field(label="Ville", required=True, value="Toulouse")
    plan = plan_of(unknown=[Question(ref="f1", label="Ville", required=True)])
    assert trusting.open_questions(run_of(), plan, snap_of(f), engine._Validated(), set(), None) == ([], [])


def test_planner_unknown_on_a_button_or_a_settled_owner_field_does_not_block():
    button = field(ref="b1", kind="button", label="Suivant", required=True)
    box = field(ref="f1", kind="checkbox", label="Recevoir la newsletter", required=False, value="false")
    plan = plan_of(unknown=[Question(ref="b1", label="Suivant"), Question(ref="f1", label="Recevoir la newsletter")])
    blocking, _ = trusting.open_questions(run_of(), plan, snap_of(button, box), engine._Validated(), set(), None)
    assert blocking == []


def test_required_empty_field_the_planner_ignored_is_soft():
    f = field(label="Ville", required=True)
    blocking, soft = trusting.open_questions(run_of(), plan_of(), snap_of(f), engine._Validated(), set(), None)
    assert blocking == [] and [q.ref for q in soft] == ["f1"]


def test_optional_and_filled_fields_are_not_soft_questions():
    optional = field(ref="f1", label="Ville")
    filled = field(ref="f2", label="Niveau d'études", required=True, value="France")
    _, soft = trusting.open_questions(run_of(), plan_of(), snap_of(optional, filled), engine._Validated(), set(), None)
    assert soft == []


def test_handled_refs_are_excluded():
    f = field(label="Ville", required=True)
    v = engine._Validated(questions=[Question(ref="f1", label="Ville")])
    plan = plan_of(unknown=[Question(ref="f1", label="Ville", required=True)])
    assert trusting.open_questions(run_of(), plan, snap_of(f), v, {"f1"}, None) == ([], [])


def test_failed_answers_block_and_are_not_repeated_as_soft():
    f = field(label="Ville", required=True)
    v = engine._Validated(questions=[Question(ref="f1", label="Ville")])
    blocking, soft = trusting.open_questions(run_of(), plan_of(), snap_of(f), v, set(), None)
    assert [q.ref for q in blocking] == ["f1"] and soft == []


def test_soft_questions_are_limited_to_the_form_of_the_click():
    mine = field(ref="f1", label="Ville", required=True, form="0:1")
    other = field(ref="f2", label="Niveau d'études", required=True, form="0:2")
    click = field(ref="b1", kind="button", label="Envoyer", form="0:1")
    _, soft = trusting.open_questions(run_of(), plan_of(), snap_of(mine, other, click), engine._Validated(), set(), click)
    assert [q.ref for q in soft] == ["f1"]


# ------------------------------------------------------------------ effective_role
def test_next_becomes_submit_on_a_sending_label_once_the_application_is_touched():
    button = field(kind="button", label="Envoyer ma candidature")
    snap = snap_of(button)
    assert trusting.effective_role(run_of(app_touched=True), button, "next", snap) == "submit"
    assert trusting.effective_role(run_of(app_touched=True), button, "open_form", snap) == "submit"
    assert trusting.effective_role(run_of(app_touched=False), button, "next", snap) == "next"


def test_navigation_label_stays_next():
    button = field(kind="button", label="Suivant")
    assert trusting.effective_role(run_of(app_touched=True), button, "next", snap_of(button)) == "next"


def test_submit_label_with_navigation_wording_stays_next():
    button = field(kind="button", label="Envoyer et continuer")
    assert trusting.effective_role(run_of(app_touched=True), button, "next", snap_of(button)) == "next"


def test_signup_click_with_an_uploaded_file_is_a_submit():
    button = field(ref="b1", kind="button", label="Créer mon compte")
    upload = field(ref="f1", kind="file", label="CV", value="cv.pdf")
    assert trusting.effective_role(run_of(), button, "signup", snap_of(button, upload)) == "submit"
    assert trusting.effective_role(run_of(), button, "signup", snap_of(button)) == "signup"


@pytest.mark.parametrize("label", ["Continue", "Continuer →", "Suivant", "Next", "Weiter", "Nächste"])
def test_pure_navigation_is_not_a_final_click_even_if_the_planner_says_submit(label):
    button = field(kind="button", label=label)
    assert trusting.effective_role(run_of(app_touched=True), button, "submit", snap_of(button)) == "next"


def test_send_wording_is_not_downgraded():
    button = field(kind="button", label="Continuer et envoyer ma candidature")
    assert trusting.effective_role(run_of(app_touched=True), button, "submit", snap_of(button)) == "submit"


def test_required_cv_selector_uses_only_the_cv_uploaded_to_this_host():
    resume = field(kind="select", label="Mon CV*", required=True, options=opts("Choisir", "Ancien CV.pdf", "CV.pdf"))
    plan = plan_of(Action(ref=resume.ref, op="upload_cv"), unknown=[Question(ref=resume.ref, label="Mon CV", required=True, kind="select")])
    run = run_of()
    run.uploaded_cv_names = {"elsewhere.example.test": {"cv pdf"}}
    snap = snap_of(resume)
    assert not trusting.validate(run, plan, snap, None).items
    run.uploaded_cv_names["careers.example.test"] = {"cv pdf"}
    validated = trusting.validate(run, plan, snap, None)
    assert [(item.act.op, item.option_index) for item in validated.items] == [("select", 2)]
    assert trusting.open_questions(run, plan, snap, validated, {resume.ref}, None) == ([], [])


def test_inline_signup_fills_existing_confirmation_with_the_same_account_email():
    run = run_of()
    run.trusting = True
    snap = snap_of(field("login", "password", "Mot de passe", name="page$pw", form="0"),
                   field("login_button", "button", "Se connecter", form="0"),
                   field("email", "text", "Identifiant", name="page$CreationControl$email", form="0"),
                   field("confirm", "text", "Confirmer l’identifiant", name="page$CreationControl$confirmEmail", form="0", value="stale"),
                   field("pw", "password", "Mot de passe", name="page$CreationControl$pw", form="0"),
                   field("create", "button", "Je me crée un espace candidat", form="0"))
    _, actions = engine._Run._inline_signup(run, plan_of(kind="account_signup"), snap)
    assert [(action.ref, action.fact) for action in actions if action.op == "fill"] == [
        ("email", "account_email"), ("confirm", "account_email"), ("pw", "account_password")]


async def test_scoped_login_ignores_header_and_inline_signup(page):
    from jobhunt.apply.snapshot import take_snapshot

    await page.set_content('''
      <label>Email<input name="Login"></label><label>Password<input type="password" name="Password"></label>
      <button>Log in</button>
      <form><label>Email<input name="page$Email"></label><label>Password<input type="password" name="page$Password"></label>
      <button name="page$Login">Log in</button>
      <label>Email<input name="page$CreationControl$Email"></label>
      <label>Password<input type="password" name="page$CreationControl$Password"></label>
      <button name="page$CreationControl$Create">Create an account</button></form>''')
    snap, _ = await take_snapshot(page)
    names = {field.ref: field.name for field in snap.fields}
    assert [names[action.ref] for action in trusting.login_actions(snap)] == ["page$Email", "page$Password", "page$Login"]


def test_numbered_posting_redirect_to_home_is_not_a_generic_homepage_rule():
    posting = "https://careers.example.test/job/job-internship_123.aspx"
    home = "https://careers.example.test/accueil.aspx?LCID=1036"
    assert trusting.posting_to_home(posting, home)
    assert not trusting.posting_to_home(URL, home)
    assert not trusting.posting_to_home(posting, home.replace("careers.", "another."))
    assert not trusting.posting_to_home(posting, URL)


def test_account_notice_is_not_a_link_or_code_challenge():
    notice = PageSnapshot(url=URL, text="Votre espace candidat a été créé. Un e-mail vous a été envoyé.")
    assert not trusting.offers_verification(notice)
    assert trusting.offers_verification(notice.model_copy(update={"text": "Cliquez sur le lien d’activation."}))
    assert trusting.offers_verification(snap_of(field(label="Code", autocomplete="one-time-code")))


# ------------------------------------------------------------------ browser: _toggle
BOXES = """
<style>
  .pretty { position: relative; display: inline-block; padding-left: 30px; }
  .pretty input { position: absolute; opacity: 0; left: 0; top: 0; width: 20px; height: 20px; }
  .cover { position: absolute; left: 0; top: 0; width: 20px; height: 20px; background: #ccc; }
</style>
<label class="pretty"><input type="checkbox" id="cb"><span class="cover"></span>J'accepte</label>
<label class="pretty"><input type="checkbox" id="on" checked><span class="cover"></span>Déjà</label>
<label class="pretty"><input type="radio" name="g" id="r1" value="1"><span class="cover"></span>Oui</label>
<label class="pretty"><input type="radio" name="g" id="r2" value="2"><span class="cover"></span>Non</label>
"""


async def test_toggle_ticks_a_checkbox_hidden_behind_a_styled_label(page):
    await page.set_content(BOXES)
    await trusting._toggle(None, page.locator("#cb"), True)
    assert await page.locator("#cb").is_checked()
    await trusting._toggle(None, page.locator("#on"), False)
    assert not await page.locator("#on").is_checked()
    await trusting._toggle(None, page.locator("#on"), False)  # already in the wanted state: nothing to do
    assert not await page.locator("#on").is_checked()


async def test_toggle_selects_a_styled_radio(page):
    await page.set_content(BOXES)
    await trusting._toggle(None, page.locator("#r2"), True)
    assert await page.locator("#r2").is_checked() and not await page.locator("#r1").is_checked()


async def test_toggle_raises_skip_when_the_box_cannot_be_changed(page):
    await page.set_content('<input type="checkbox" id="cb" disabled><script>'
                           'document.getElementById("cb").addEventListener("click", e => e.preventDefault())</script>')
    with pytest.raises(engine._Skip):
        await trusting._toggle(None, page.locator("#cb"), True)


# ------------------------------------------------------------------ browser: blocker / dismiss_blocker
DIALOG = """
<main><p>Page de l'offre</p></main>
<div role="dialog" id="dlg" style="position:fixed; inset:0; background:#fff; z-index:10">
  <p>{body}</p>
  {extra}
</div>
"""


def dialog(body: str, extra: str = "") -> str:
    return DIALOG.format(body=body, extra=extra)


CONTINUE = '<button id="go" onclick="document.getElementById(\'dlg\').remove()">Continue to site</button>'


async def test_privacy_dialog_with_one_button_is_dismissed(page):
    await page.set_content(dialog("Privacy Terms", f'<a href="https://example.test/policy">Read the policy</a>{CONTINUE}'))
    found = await trusting.blocker(page)
    assert found and "Privacy Terms" in found["text"] and "Continue to site" in found["buttons"]
    clicked = await trusting.dismiss_blocker(SimpleNamespace(page=page))
    assert clicked == "Continue to site"
    assert await page.locator("#dlg").count() == 0
    assert await trusting.blocker(page) is None


async def test_dialog_with_an_input_is_not_a_blocker(page):
    await page.set_content(dialog("Privacy Terms", f'<input type="text" name="email">{CONTINUE}'))
    assert await trusting.blocker(page) is None
    assert await trusting.dismiss_blocker(SimpleNamespace(page=page)) == ""
    assert await page.locator("#dlg").count() == 1


async def test_dialog_without_privacy_wording_is_reported_but_not_dismissed(page):
    await page.set_content(dialog("Bienvenue sur notre site !", CONTINUE))
    found = await trusting.blocker(page)
    assert found and found["buttons"] == ["Continue to site"]
    assert await trusting.dismiss_blocker(SimpleNamespace(page=page)) == ""
    assert await page.locator("#dlg").count() == 1
    hint = await trusting.blocker_hint(SimpleNamespace(page=page))
    assert "Continue to site" in hint and "MODAL" in hint


async def test_cookie_dialog_prefers_the_refusal_button(page):
    buttons = ('<button onclick="document.getElementById(\'dlg\').remove()">Tout accepter</button>'
               '<button id="no" onclick="document.body.setAttribute(\'data-choice\', \'refused\'); document.getElementById(\'dlg\').remove()">'
               "Tout refuser</button>")
    await page.set_content(dialog("Nous utilisons des cookies", buttons))
    assert await trusting.dismiss_blocker(SimpleNamespace(page=page)) == "Tout refuser"
    assert await page.evaluate("document.body.getAttribute('data-choice')") == "refused"


def test_a_code_list_named_by_its_label_or_its_options_makes_the_phone_national():
    code = field(ref="c1", kind="select", label="Indicatif téléphonique du pays", options=[FieldOption(value="FRA_33", label="France (+33)"),
                                                                                         FieldOption(value="BEL_32", label="Belgique (+32)")])
    phone = field(ref="p1", kind="text", label="Numéro de téléphone", required=True)
    snap = snap_of(code, phone)
    assert trusting.is_dial(code, snap) and trusting.beside_dial(phone, snap)
    made = trusting.check(run_of(), Action(ref="p1", op="fill", fact="phone"), phone, engine._Validated(), snap, None)
    assert made is not None and made.national


def test_a_phone_the_site_refused_is_typed_in_another_spelling():
    phone = field(ref="p1", kind="text", label="Numéro de téléphone", required=True, value="+33 6 00 00 00 00",
                  error="Format de numéro de téléphone non valide.")
    made = trusting.check(run_of(), Action(ref="p1", op="fill", fact="phone"), phone, engine._Validated(), snap_of(phone), None)
    assert made is not None and not made.forced.startswith("+")


async def test_a_privacy_statement_required_to_apply_is_accepted_never_refused(page):  # noqa: F811
    # (refusing it is not "no cookies": one ATS then offers to delete the whole candidate account)
    from jobhunt.apply.consent import dismiss_consent

    statement = "Déclaration de confidentialité des données. " + "Nous traitons vos données pour le recrutement. " * 30 + "Ce site utilise des cookies."
    await page.set_content(f'''<main><form><input name="email"><button type="button">Postuler</button></form></main>
        <div role="dialog" style="position:fixed;inset:10%;background:#fff"><h2>Déclaration de confidentialité des données</h2>
        <p>{statement}</p><button onclick="window.choice='accept'; this.closest('[role=dialog]').remove()">Accepter</button>
        <button onclick="window.choice='refuse'">Refuser</button><button>Imprimer</button></div>''')
    assert await dismiss_consent(page) == "Accepter"
    assert await page.locator("[role=dialog]").count() == 0  # (the accept button removed it; "Refuser" would have left it)


async def test_a_cookie_dialog_is_still_refused_when_it_offers_that(page):  # noqa: F811
    from jobhunt.apply.consent import dismiss_consent

    await page.set_content('''<div role="dialog" style="position:fixed;inset:10%;background:#fff"><p>Nous utilisons des cookies et traceurs.</p>
        <button onclick="window.choice='accept'">Tout accepter</button><button onclick="window.choice='refuse'">Tout refuser</button></div>''')
    assert await dismiss_consent(page) == "Tout refuser"


def test_a_required_agreement_with_a_yes_i_agree_option_is_agreed_to():
    undertaking = field(ref="a1", kind="select", required=True,
                        label="I agree that I will not disclose or use any confidential information of others, including any former employer.",
                        options=opts("Yes, I agree.", "No, I do not agree and I understand that this withdraws me from consideration."))
    made = trusting.owner_item(run_of(), undertaking)
    assert made is not None and made is not True and made.option_index == 0 and made.used == "application_terms"


def test_a_plain_yes_no_question_is_not_taken_for_an_agreement():
    question = field(ref="q1", kind="select", required=True, label="Will you now or in the future require sponsorship for an employment visa?",
                     options=opts("No", "Yes"))
    assert trusting.owner_item(run_of(), question) is None


# ------------------------------------------------------------------ postings that are gone / not postings / stuck fields
@pytest.mark.parametrize("text", [
    "sorry, this job has expired",
    "desole, cette offre a expire",
    "cette annonce n'existe plus",
    "l'offre est cloturee",
    "the position is no longer available",
])
def test_missing_offer_wording(text):
    assert engine.MISSING_OFFER_RE.search(text)


@pytest.mark.parametrize("text", ["l'offre expire le 12 mars", "votre session a expire", "postuler a cette offre"])
def test_live_offer_wording_is_not_gone(text):
    assert not engine.MISSING_OFFER_RE.search(text)


@pytest.mark.parametrize("path, gone", [
    ("/oneclick-ui/company/Acme/publication/abc/expired", True),
    ("/en_US/jobs/Error", True),
    ("/jobs/error-page", True),
    ("/careers/jobs/", False),
    ("/job/internship-errors-in-code", False),
])
def test_gone_paths(path, gone):
    assert bool(engine.GONE_PATH_RE.search(path.lower())) is gone


@pytest.mark.parametrize("kind, url, fields, expected", [
    ("job_page", "https://ats.example.test/co/pub/1/expired", [], True),
    ("error", "https://careers.example.test/jobs/Error", [field(kind="link", label="Accueil")], True),
    ("job_page", "https://ats.example.test/co/pub/1/expired", [field(label="Prénom")], False),
    ("form", "https://ats.example.test/co/pub/1/expired", [], False),
    ("job_page", "https://ats.example.test/co/pub/1", [], False),
])
def test_error_page_gone(kind, url, fields, expected):
    run = SimpleNamespace(autonomous=object(), app_touched=False)
    snap = PageSnapshot(url=url, fields=list(fields))
    assert engine._Run._error_page_gone(run, kind, snap) is expected


def test_list_of_offers_is_not_a_posting():
    assert engine.AGGREGATOR_RE.search("liste d'offres, pas une annonce")
    assert engine.AGGREGATOR_RE.search("a page of search results")


def test_print_view_of_an_ad_maps_to_the_normal_view():
    url = "https://careers.example.test/offre-de-emploi/imprimer-fiche-emploi-stage-ia-h-f_2326.aspx"
    assert engine.PRINT_VIEW_RE.sub("/emploi-", url, count=1) == "https://careers.example.test/offre-de-emploi/emploi-stage-ia-h-f_2326.aspx"


def test_stuck_hint_names_the_fields_and_asks_for_the_click():
    hint = trusting.stuck_hint(["Ville", "Code pays"])
    assert "Ville | Code pays" in hint and "click that moves on" in hint


def test_generated_password_fits_sites_that_cap_it_at_eighteen():
    from jobhunt.apply import accounts

    pw = accounts.generate_password()
    assert 8 <= len(pw) <= 18 and any(c.isdigit() for c in pw) and any(c.isupper() for c in pw) and any(c.islower() for c in pw)


def test_a_one_time_passcode_mailed_from_the_ats_vendor_domain_is_read():
    from jobhunt.apply import mail

    text = ("One-Time Password for Candidate Account Verification. Dear Camille Durand, your passcode for account verification is 482913 "
            "Enter this passcode on the Create Account page. This passcode is valid for 30 minutes.")
    found = mail.extract(text, "https://careers7.successfactors.eu/careers", sender_domain="successfactors.eu")
    assert found is not None and found.kind == "code" and found.value == "482913"
    assert mail.extract(text, "https://careers7.successfactors.eu/careers", sender_domain="mailer.example.test") is None


def test_former_employer_question_is_a_declaration_answered_from_its_fact():
    asked = field(ref="q1", kind="select", required=True, label="Avez-vous déjà travaillé pour notre entreprise ?", options=opts("Oui", "Non"))
    assert trusting.declaration_key(asked) == "former_employer"


def test_a_recorded_nationality_is_matched_to_a_list_that_words_it_differently():
    adjectives = field(ref="n1", kind="select", label="Nationalité", required=True, options=opts("Belge", "Burkinabé(e)", "Française"))
    countries = field(ref="n2", kind="select", label="Nazionalità", options=opts("Belgio", "Burkina Faso", "Burundi", "Francia"))
    run = run_of(facts=FactSheet(facts=[Fact(key="nationality", label="Nationalité", value="Burkinabè")]))
    v = trusting.validate(run, plan_of(), snap_of(adjectives, countries), None)
    chosen = {item.field.ref: item.field.options[item.option_index].label for item in v.items}
    assert chosen == {"n1": "Burkinabé(e)", "n2": "Burkina Faso"}


def test_an_ambiguous_stem_picks_nothing():
    assert trusting._closest(["Niger", "Nigeria", "France"], "Nigérienne") == -1


class _FixedPlanner:
    def __init__(self, plan):
        self.plan_, self.calls = plan, 0

    async def plan(self, snapshot, facts, **context):
        self.calls += 1
        return self.plan_


@pytest.mark.parametrize("scripted, uses_llm", [
    (StepPlan(page_kind="captcha"), False),
    (StepPlan(page_kind="email_verification"), False),
    (StepPlan(page_kind="job_page", actions=[Action(ref="b1", op="click", click_role="open_form")]), False),
    (StepPlan(page_kind="account_login", actions=[Action(ref="b1", op="click", click_role="login")]), False),
    (StepPlan(page_kind="job_page"), True),                                                    # "nothing to do": ask the LLM
    (StepPlan(page_kind="account_signup", actions=[Action(ref="f1", op="fill", fact="account_password")]), True),  # no click
    (StepPlan(page_kind="form", actions=[Action(ref="b1", op="click", click_role="next")]), True),  # form steps are the LLM's
])
async def test_scripted_planner_keeps_its_entry_and_account_clicks_and_the_llm_plans_the_rest(scripted, uses_llm):
    answer = StepPlan(page_kind="form", actions=[Action(ref="b9", op="click", click_role="next")])
    first, second = _FixedPlanner(scripted), _FixedPlanner(answer)
    plan = await trusting.ScriptedThenLLM(first, second).plan(PageSnapshot(url="https://ats.example.test/apply"), FactSheet())
    assert first.calls == 1 and second.calls == (1 if uses_llm else 0)
    assert plan is (answer if uses_llm else scripted)


def test_a_calling_code_search_box_is_recognized_and_makes_the_number_national():
    code = field(ref="c1", kind="text", label="Indicatif de pays/province", required=True)
    phone = field(ref="p1", kind="text", label="Nº de téléphone", required=True)
    snap = snap_of(code, phone)
    assert trusting.is_dial(code, snap) and not trusting.is_dial(phone, snap) and trusting.beside_dial(phone, snap)
    made = trusting.check(run_of(), Action(ref="c1", op="fill", fact="phone"), code, engine._Validated(), snap, None)
    assert made is not None and made.dial_code == "+33" and not made.national
    number = trusting.check(run_of(), Action(ref="p1", op="fill", fact="phone"), phone, engine._Validated(), snap, None)
    assert number is not None and number.national and not number.dial_code


async def test_an_opened_list_is_remembered_and_a_category_sub_list_is_followed(page):  # noqa: F811
    await page.set_content('''<label id="l">Comment nous avez-vous connus ?</label>
      <button id="b" aria-haspopup="listbox" aria-labelledby="l" onclick="show(['Site carrière', 'Réseaux sociaux', 'Salon'])">Select One</button>
      <div id="pop"></div>
      <script>
        const SUB = {'Réseaux sociaux': ['LinkedIn', 'Autre réseau']};
        function show(items) { pop.innerHTML = '<ul role="listbox">' + items.map(t => `<li role="option">${t}</li>`).join('') + '</ul>';
          for (const li of pop.querySelectorAll('li')) li.onclick = () => { if (SUB[li.textContent]) show(SUB[li.textContent]);
            else { document.getElementById('b').textContent = li.textContent; pop.innerHTML = ''; } }; }
      </script>''')
    from jobhunt.apply.snapshot import take_snapshot

    snap, resolve = await take_snapshot(page)
    combo = next(f for f in snap.fields if f.kind == "combobox")
    run = SimpleNamespace(page=page, combo_options={}, _combo_value=engine._Run._combo_value, _abandon_combobox=lambda loc: engine._Run._abandon_combobox(None, loc))
    item = engine._Item(Action(ref=combo.ref, op="select"), combo, Fact(key="job_source", label="Source", value="LinkedIn"), value="Réseaux sociaux")
    shown = await trusting._combobox(run, await resolve.element(combo.ref), combo, item)
    assert shown == "LinkedIn"
    assert run.combo_options[trusting.norm(combo.label)] == ["Site carrière", "Réseaux sociaux", "Salon"]


MULTISELECT = '''<form><label for="code">Indicatif de pays</label>
  <div class="box"><input id="code" type="text">
    <ul role="listbox" data-automation-id="selectedItemList">ITEMS</ul></div>
  <label>Nº de téléphone <input type="text" name="phoneNumber"></label></form>'''
CHOSEN = ('<li role="option"><span>{0}</span><span data-automation-id="DELETE_charm" '
          'onclick="this.closest(\'li\').remove()">x</span></li>')


async def test_a_multiselect_search_box_shows_its_chosen_item_as_its_value(page):  # noqa: F811
    from jobhunt.apply.snapshot import take_snapshot

    await page.set_content(MULTISELECT.replace("ITEMS", CHOSEN.format("France (+33)")))
    snap, _ = await take_snapshot(page)
    box = next(f for f in snap.fields if f.label == "Indicatif de pays")
    assert "France (+33)" in box.value
    assert trusting.check(run_of(), Action(ref=box.ref, op="fill", fact="phone"), box, engine._Validated(), snap, None) is None
    assert await trusting._popup_options(SimpleNamespace(page=page)) == []  # (a chosen item is not a choice of an open list)


async def test_another_countrys_calling_code_is_taken_out_of_the_search_box(page):  # noqa: F811
    from jobhunt.apply.snapshot import take_snapshot

    await page.set_content(MULTISELECT.replace("ITEMS", CHOSEN.format("Belgique (+32)")))
    snap, resolve = await take_snapshot(page)
    box = next(f for f in snap.fields if f.label == "Indicatif de pays")
    made = trusting.check(run_of(), Action(ref=box.ref, op="fill", fact="phone"), box, engine._Validated(), snap, None)
    assert made is not None and made.dial_code == "+33"
    assert await (await resolve.element(box.ref)).evaluate(trusting._REMOVE_CHOSEN_JS, "(+33)") == 1
    assert await page.locator("li[role=option]").count() == 0
