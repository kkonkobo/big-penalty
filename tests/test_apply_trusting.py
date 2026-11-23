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


SEARCH_PROMPT = '''<form><label for="school">Établissement ou université</label>
  <div data-automation-id="multiSelectContainer"><input id="school" type="text" data-uxi-widget-type="selectinput" REQUIRED>
    <ul role="listbox" data-automation-id="selectedItemList"></ul></div>
  <div id="pop"></div></form>
  <script>
    const ALL = ['École Centrale de Testville', 'Institut Polytechnique de Testville', 'Université de Testville', 'Université de Fauxbourg', 'Autre'];
    const fold = (t) => t.normalize('NFD').replace(/[\\u0300-\\u036f]/g, '').toLowerCase();
    school.addEventListener('keydown', (event) => {
      if (event.key !== 'Enter') return;
      event.preventDefault();
      const hits = ALL.filter((name) => fold(name).includes(fold(school.value)));
      pop.innerHTML = '<div role="listbox">' + hits.map((name) => `<div role="option">${name}</div>`).join('') + '</div>';
      for (const row of pop.querySelectorAll('[role=option]')) row.onclick = () => {
        document.querySelector('[data-automation-id=selectedItemList]').innerHTML = `<li role="option">${row.textContent}</li>`;
        school.value = ''; pop.innerHTML = ''; };
    });
  </script>'''


@pytest.mark.parametrize(("labels", "want", "query", "index"), [
    (["Université de Testville", "École Centrale de Testville"], "École Centrale de Testville", "x", 1),
    (["Institut Polytechnique de Testville (IPT)"], "Polytechnique Testville", "Polytechnique", 0),  # the one result of a query
    (["Université de Testville", "Institut Polytechnique de Testville", "Université de Fauxbourg"], "Polytechnique Testville", "Testville", 1),
    (["Université de Testville", "Université de Fauxbourg"], "École des Mines de Nullepart", "Université", -1),
    ([], "École Centrale", "École Centrale", -1),
])
def test_the_search_result_that_means_the_answer(labels, want, query, index):
    assert trusting.search_choice(labels, want, query) == index


@pytest.mark.parametrize(("required", "want", "chosen"), [
    ("required", "Ecole centrale de Testville", "École Centrale de Testville"),
    ("required", "Institut Polytechnique Testville (IPT)", "Institut Polytechnique de Testville"),  # found by its telling words
    ("required", "École des Mines de Nullepart", "Autre"),  # unknown to a required prompt: its "other" entry
    ("", "École des Mines de Nullepart", None),  # optional: left empty
])
async def test_a_search_prompt_takes_the_result_that_means_the_answer(page, required, want, chosen):  # noqa: F811
    from jobhunt.apply.snapshot import take_snapshot

    await page.set_content(SEARCH_PROMPT.replace("REQUIRED", required))
    snap, resolve = await take_snapshot(page)
    box = next(f for f in snap.fields if f.kind == "text")
    target = await resolve.element(box.ref)
    assert await trusting.is_search_prompt(target)
    run = SimpleNamespace(page=page)
    if chosen is None:
        with pytest.raises(engine._Skip):
            await trusting._search_pick(run, target, box, want)
        assert await page.locator("li[role=option]").count() == 0 and await page.input_value("#school") == ""
        return
    assert await trusting._search_pick(run, target, box, want) == chosen
    snap, _ = await take_snapshot(page)
    assert next(f for f in snap.fields if f.kind == "text").value == chosen


async def test_a_plain_text_box_is_not_a_search_prompt(page):  # noqa: F811
    await page.set_content('<label>Ville <input id="city" type="text"></label>')
    assert not await trusting.is_search_prompt(await page.query_selector("#city"))


def test_a_page_showing_another_countrys_calling_code_is_not_sent():
    code = field(ref="c1", kind="text", label="Indicatif de pays", value="Belgique (+32)")
    assert trusting.foreign_dial(run_of(), snap_of(code)) == "+32"
    assert trusting.foreign_dial(run_of(), snap_of(code.model_copy(update={"value": "France (+33)"}))) == ""
    assert trusting.foreign_dial(run_of(), snap_of(field(ref="t1", label="Ville", value="Testville"))) == ""
    review = PageSnapshot(url="https://ats.example.test/review", text="Téléphone Mobile Belgique (+32) 06 00 00 00 00", fields=[])
    assert trusting.foreign_dial(run_of(), review) == "+32"
    both = PageSnapshot(url="https://ats.example.test/review", text="France (+33) 06 00 00 00 00 — siège : Belgique (+32)", fields=[])
    assert trusting.foreign_dial(run_of(), both) == ""


def test_a_calling_code_box_the_plan_left_with_another_code_is_corrected():
    code = field(ref="c1", kind="text", label="Indicatif de pays", required=True, value="Belgique (+32)")
    phone = field(ref="p1", kind="text", label="Nº de téléphone", required=True, value="06 00 00 00 00")
    made = trusting.validate(run_of(), StepPlan(page_kind="form", actions=[]), snap_of(code, phone), None)
    assert [(item.field.ref, item.dial_code) for item in made.items if item.field.ref == "c1"] == [("c1", "+33")]
    right = snap_of(code.model_copy(update={"value": "France (+33)"}), phone)
    assert not [item for item in trusting.validate(run_of(), StepPlan(page_kind="form", actions=[]), right, None).items if item.field.ref == "c1"]


LIST_BUTTONS = '''<form>
  <fieldset><legend><div><p>Dans quel cadre cherchez-vous un stage ?</p></div></legend>
    <div><button type="button" id="q1" aria-haspopup="listbox" aria-label="Select One Required">Select One</button></div></fieldset>
  <fieldset><legend>Rythme du stage</legend>
    <div><button type="button" id="q2" aria-haspopup="listbox" aria-label="Une seule période Required">Une seule période</button></div></fieldset>
  <div><p>Niveau visé ?</p><div><button type="button" id="q3" aria-haspopup="listbox" aria-label="Select One">Select One</button></div></div>
  <label for="q4">Pays</label><button type="button" id="q4" aria-haspopup="listbox" aria-label="Select One Required">Select One</button>
</form>'''


async def test_a_list_button_named_after_its_value_takes_its_question_as_label(page):  # noqa: F811
    from jobhunt.apply.snapshot import take_snapshot

    await page.set_content(LIST_BUTTONS)
    snap, _ = await take_snapshot(page)
    lists = [f for f in snap.fields if f.kind == "combobox"]
    assert [f.label for f in lists] == ["Dans quel cadre cherchez-vous un stage ?", "Rythme du stage", "Niveau visé ?", "Pays"]
    assert [f.value for f in lists] == ["", "Une seule période", "", ""]


async def test_options_of_a_list_named_after_its_placeholder_are_not_remembered(page):  # noqa: F811
    await page.set_content('''<button id="b" aria-haspopup="listbox" aria-label="Select One" onclick="pop.innerHTML = '<ul role=listbox><li role=option>Un</li><li role=option>Deux</li></ul>'">Select One</button>
      <div id="pop"></div>''')
    from jobhunt.apply.snapshot import take_snapshot

    snap, resolve = await take_snapshot(page)
    combo = next(f for f in snap.fields if f.kind == "combobox")
    assert combo.label == "Select One"
    run = SimpleNamespace(page=page, combo_options={}, _combo_value=engine._Run._combo_value, _abandon_combobox=lambda loc: engine._Run._abandon_combobox(None, loc))
    with pytest.raises(engine._Skip):
        await trusting._combobox(run, await resolve.element(combo.ref), combo, engine._Item(Action(ref=combo.ref, op="select"), combo, None, value="Trois"))
    assert run.combo_options == {}


def test_an_unknown_the_page_does_not_require_is_left_to_the_site():
    optional = field(ref="f1", kind="combobox", label="Bénéficiaire de l'obligation d'emploi ?")
    plan = plan_of(unknown=[Question(ref="f1", label=optional.label, kind="combobox")])
    assert trusting.open_questions(run_of(), plan, snap_of(optional), engine._Validated(), set(), None) == ([], [])
    refused = optional.model_copy(update={"error": "Ce champ est obligatoire"})
    blocking, _ = trusting.open_questions(run_of(), plan, snap_of(refused), engine._Validated(), set(), None)
    assert [q.ref for q in blocking] == ["f1"]
    required = optional.model_copy(update={"required": True})
    blocking, _ = trusting.open_questions(run_of(), plan, snap_of(required), engine._Validated(), set(), None)
    assert [q.ref for q in blocking] == ["f1"]


UNMARKED_LIST = '''<form><label for="code">Code pays</label><x-select id="host"></x-select>
  <label>Numéro de téléphone <input type="tel" name="phone"></label></form>
  <script>
    const root = host.attachShadow({mode: 'open'});
    root.innerHTML = '<input id="box" role="combobox" aria-label="Code pays"><div id="menu"></div>';
    const box = root.getElementById('box'), menu = root.getElementById('menu');
    const ALL = ITEMS;
    const show = () => { menu.innerHTML = ALL.filter((name) => name.toLowerCase().includes(box.value.toLowerCase()))
        .map((name) => `<div class="item"><i class="flag"></i><span>${name}</span></div>`).join('');
      for (const item of menu.children) item.onclick = () => { box.value = item.textContent; menu.innerHTML = ''; }; };
    box.addEventListener('click', show); box.addEventListener('input', show);
  </script>'''


@pytest.mark.parametrize(("items", "shown"), [
    ("['Belgique', 'France', 'Suisse']", "France"),  # a list of country names only: the candidate's country
    ("['Belgique (+32)', 'France (+33)', 'Suisse (+41)']", "France (+33)"),
])
async def test_a_list_without_option_markup_is_read_as_what_appeared(page, items, shown):  # noqa: F811
    from jobhunt.apply.snapshot import take_snapshot

    await page.set_content(UNMARKED_LIST.replace("ITEMS", items))
    snap, resolve = await take_snapshot(page)
    combo = next(f for f in snap.fields if f.kind == "combobox")
    run = SimpleNamespace(page=page, combo_options={}, _combo_value=engine._Run._combo_value, _abandon_combobox=lambda loc: engine._Run._abandon_combobox(None, loc))
    item = engine._Item(Action(ref=combo.ref, op="select", fact="phone"), combo, None, value="+33")
    item.dial_code = "+33"
    assert await trusting._combobox(run, await resolve.element(combo.ref), combo, item) == shown


async def test_a_list_that_offers_nothing_names_the_control_it_could_not_read(page):  # noqa: F811
    from jobhunt.apply.snapshot import take_snapshot

    await page.set_content(UNMARKED_LIST.replace("ITEMS", "[]"))
    snap, resolve = await take_snapshot(page)
    combo = next(f for f in snap.fields if f.kind == "combobox")
    run = SimpleNamespace(page=page, combo_options={}, _combo_value=engine._Run._combo_value, _abandon_combobox=lambda loc: engine._Run._abandon_combobox(None, loc))
    with pytest.raises(engine._Skip, match=r"contrôle : input\[combobox\] < x-select"):
        await trusting._combobox(run, await resolve.element(combo.ref), combo, engine._Item(Action(ref=combo.ref, op="select"), combo, None, value="Ailleurs"))


@pytest.mark.parametrize(("text", "stated"), [
    ("Vous avez déjà un compte ? Connexion", False),  # the prompt of every creation form, not a statement
    ("Déjà inscrit ?\nConnectez-vous", False),
    ("Already registered? Sign in", False),
    ("Un compte existe déjà avec cette adresse e-mail.", True),
    ("This email is already registered. Please sign in.", True),
    ("Cette adresse est déjà utilisée", True),
])
def test_a_sign_in_prompt_does_not_say_the_account_exists(text, stated):
    from jobhunt.apply.answer_check import fold

    assert engine.says_account_exists(fold(text)) is stated


async def test_an_address_box_takes_the_suggestion_closest_to_the_answer(page):  # noqa: F811
    await page.set_content('''<label for="a">Adresse</label><input id="a" role="combobox" aria-autocomplete="list"><div id="pop"></div>
      <script>
        const ALL = ['12 Rue des Tests, Testville, France', '12 Rue des Tests, Fauxbourg, France', '1 Place du Faux, Testville, France'];
        a.addEventListener('input', () => {
          const words = a.value.toLowerCase().split(/[ ,]+/).filter((w) => w.length > 2);
          const hits = a.value.length < 3 ? [] : ALL.filter((name) => words.every((w) => name.toLowerCase().includes(w)));
          pop.innerHTML = hits.map((name) => `<div class="s">${name}</div>`).join('');
          for (const row of pop.children) row.onclick = () => { a.value = row.textContent; pop.innerHTML = ''; }; });
      </script>''')
    from jobhunt.apply.snapshot import take_snapshot

    snap, resolve = await take_snapshot(page)
    combo = next(f for f in snap.fields if f.kind == "combobox")
    run = SimpleNamespace(page=page, combo_options={}, _combo_value=engine._Run._combo_value, _abandon_combobox=lambda loc: engine._Run._abandon_combobox(None, loc))
    item = engine._Item(Action(ref=combo.ref, op="select", fact="address"), combo, None, value="12 rue des Tests 99000 Testville")
    assert await trusting._combobox(run, await resolve.element(combo.ref), combo, item) == "12 Rue des Tests, Testville, France"


async def test_a_button_showing_text_slotted_into_its_component_is_read(page):  # noqa: F811
    from jobhunt.apply.snapshot import take_snapshot

    await page.set_content('''<form><label>Prénom <input type="text" name="first"></label>
      <x-button id="next">Suivant</x-button><x-plain id="add">Ajouter</x-plain></form>
      <script>
        next.attachShadow({mode: 'open'}).innerHTML = '<button type="button"><slot></slot></button>';
        add.attachShadow({mode: 'open'}).innerHTML = '<button type="button"><i class="icon"></i></button>';
      </script>''')
    snap, resolve = await take_snapshot(page)
    assert [f.label for f in snap.fields if f.kind == "button"] == ["Suivant", "Ajouter"]
    await (await resolve.element(next(f.ref for f in snap.fields if f.label == "Suivant"))).click()


async def test_a_slow_list_is_waited_for_and_another_lists_leftovers_are_not_its_options(page):  # noqa: F811
    await page.set_content('''<label for="a">Pays de résidence</label><input id="a" role="combobox">
      <ul role="listbox" id="left"><li role="option">Oui</li></ul>
      <label for="b">Autorisé à travailler ?</label><input id="b" role="combobox"><div id="pop"></div>
      <script>
        b.addEventListener('input', () => setTimeout(() => {
          pop.innerHTML = '<ul role="listbox">' + ['Oui', 'Non'].filter((t) => t.toLowerCase().startsWith(b.value.toLowerCase()))
            .map((t) => `<li role="option">${t}</li>`).join('') + '</ul>';
          for (const li of pop.querySelectorAll('li')) li.onclick = () => { b.value = li.textContent; pop.innerHTML = ''; };
        }, 1800));
      </script>''')
    from jobhunt.apply.snapshot import take_snapshot

    snap, resolve = await take_snapshot(page)
    combo = next(f for f in snap.fields if f.kind == "combobox" and f.label.startswith("Autorisé"))
    run = SimpleNamespace(page=page, combo_options={}, _combo_value=engine._Run._combo_value, _abandon_combobox=lambda loc: engine._Run._abandon_combobox(None, loc))
    item = engine._Item(Action(ref=combo.ref, op="select"), combo, None, value="Non")
    assert await trusting._combobox(run, await resolve.element(combo.ref), combo, item) == "Non"
    assert await page.input_value("#b") == "Non" and run.combo_options[trusting.norm(combo.label)] == ["Non"]


def test_a_password_refused_by_the_sites_policy_is_replaced_once():
    run = run_of()
    calls = []
    run.password = SimpleNamespace(rotate=lambda address: calls.append(address) or "new")
    run._account_address = lambda url: "careers.example.test"
    refused = field(ref="p1", kind="password", label="Choisir un mot de passe", required=True, error="Mot de passe non valide Stratégie de mot de passe")
    confirm = field(ref="p2", kind="password", label="Confirmer le mot de passe", required=True)
    v = engine._Validated()
    run.mode = "submit"
    for f in (refused, confirm, refused):
        assert trusting.check(run, Action(ref=f.ref, op="fill", fact="account_password"), f, v, snap_of(refused, confirm), None) is not None
    assert calls == ["careers.example.test"] and any("nouveau mot de passe" in note for note in v.notes)


def _after_final_click(snap: PageSnapshot, *, sent_empty, exists=False, click_filled=None) -> SimpleNamespace:
    if click_filled is None:  # by default every field of the page held a value when the click was made
        click_filled = {(trusting.norm(f.label), f.kind) for f in snap.fields if f.kind not in ("button", "link")}
    withdrawn = []

    async def unmark():
        withdrawn.append(True)

    async def no_banner():
        return False

    async def says_exists():
        return exists

    return SimpleNamespace(
        last_sent=engine._Sent(final=True, fields={("prenom", "text")}, button="envoyer"), page=SimpleNamespace(url=snap.url),
        _says_account_exists=says_exists, _account_address=lambda url: "careers.example.test",
        account_exists_hosts=engine._Accounts(), login_failed=engine._Accounts(),
        sent_url=snap.url, trusting=True, sent_empty=sent_empty, click_filled=click_filled, _banner_error=no_banner,
        rejected_submits=0, final_awaiting=True,
        final_clicked=True, submit_clicks=1, pending=None, last_click_rec=None, replans={}, diagnostics=SimpleNamespace(receipt_reason=""),
        autonomous=SimpleNamespace(unmark_submit=unmark, final_response=True, foreign_writes=1), withdrawn=withdrawn)


async def test_a_form_still_there_with_its_required_field_empty_was_not_sent():
    name = field(ref="f1", label="Prénom", required=True, value="Camille")
    status = field(ref="f2", kind="combobox", label="Situation RQTH ?", required=True)
    send = field(ref="b1", kind="button", label="Envoyer")
    snap = snap_of(name, status, send)
    run = _after_final_click(snap, sent_empty={("situation rqth", "combobox")})
    assert await engine._Run._submit_rejected(run, snap) and run.withdrawn and run.rejected_submits == 1 and not run.final_clicked
    # a field that a choice brought with it, empty and required, while what was filled keeps its values: refused too
    run = _after_final_click(snap, sent_empty=set())
    assert await engine._Run._submit_rejected(run, snap) and run.withdrawn
    # the form was emptied (what a site does after taking it): the same page proves nothing, the send stays recorded
    emptied = snap_of(name.model_copy(update={"value": ""}), status, send)
    run = _after_final_click(emptied, sent_empty=set())
    assert not await engine._Run._submit_rejected(run, emptied) and not run.withdrawn
    # nothing required is empty: no evidence of a refusal either
    full = snap_of(name, status.model_copy(update={"required": False}), send)
    run = _after_final_click(full, sent_empty=set())
    assert not await engine._Run._submit_rejected(run, full) and not run.withdrawn
    # the field got its answer in the meantime, or the form is gone: no evidence of a refusal
    answered = snap_of(name, status.model_copy(update={"value": "Non"}), send)
    run = _after_final_click(answered, sent_empty={("situation rqth", "combobox")})
    assert not await engine._Run._submit_rejected(run, answered)


async def test_the_fullest_list_seen_is_what_the_planner_is_shown(page):  # noqa: F811
    await page.set_content('''<label for="a">Situation ?</label><input id="a" role="combobox"><div id="pop"></div>
      <script>
        const ALL = ['Je souhaite en faire part', 'Je ne souhaite pas en faire part', 'Non concerné'];
        const show = () => { const hits = ALL.filter((t) => t.toLowerCase().includes(a.value.toLowerCase()));
          pop.innerHTML = '<ul role="listbox">' + (hits.length ? hits : ['Aucun résultat trouvé']).map((t) => `<li role="option">${t}</li>`).join('') + '</ul>'; };
        a.addEventListener('click', show); a.addEventListener('input', show);
      </script>''')
    from jobhunt.apply.snapshot import take_snapshot

    snap, resolve = await take_snapshot(page)
    combo = next(f for f in snap.fields if f.kind == "combobox")
    run = SimpleNamespace(page=page, combo_options={}, _combo_value=engine._Run._combo_value, _abandon_combobox=lambda loc: engine._Run._abandon_combobox(None, loc))
    with pytest.raises(engine._Skip, match="options proposées : Je souhaite en faire part"):
        await trusting._combobox(run, await resolve.element(combo.ref), combo, engine._Item(Action(ref=combo.ref, op="select"), combo, None, value="Zzz"))
    assert run.combo_options[trusting.norm(combo.label)] == ['Je souhaite en faire part', 'Je ne souhaite pas en faire part', 'Non concerné']


STATEMENT_WITH_COOKIES = '''<main><form><input name="email"><button type="button">Postuler</button></form></main>
  <div role="dialog" style="position:fixed;inset:10%;background:#fff"><h2>Déclaration de confidentialité des données</h2>
  <p>Nous utilisons des cookies pour le fonctionnement du site. Nous traitons vos données pour le recrutement.</p>
  <button onclick="this.closest('[role=dialog]').remove()">Accepter</button>
  <button onclick="document.title = 'refused'">Refuser</button><button>Imprimer</button><button>Fermer</button></div>'''


async def test_a_privacy_statement_that_opens_on_cookies_is_still_accepted_by_both_dismissers(page):  # noqa: F811
    from jobhunt.apply.consent import dismiss_consent

    await page.set_content(STATEMENT_WITH_COOKIES)
    assert await dismiss_consent(page) == "Accepter"
    await page.set_content(STATEMENT_WITH_COOKIES)
    assert await trusting.dismiss_blocker(SimpleNamespace(page=page)) == "Accepter"
    assert await page.title() != "refused" and await page.locator("[role=dialog]").count() == 0


@pytest.mark.parametrize(("label", "offers"), [
    ("Se connecter avec une adresse e-mail", True), ("Sign in with Google", True), ("Connexion avec LinkedIn", True),
    ("Connexion", False), ("Postuler", False), ("Sign in", False),
])
def test_a_page_offering_ways_to_sign_in_is_not_past_the_account_pages(label, offers):
    from jobhunt.apply.answer_check import fold

    assert bool(engine.SIGN_IN_WITH_RE.search(fold(label))) is offers


async def test_a_posting_said_gone_on_a_disabled_button_is_found_on_the_whole_page(page):  # noqa: F811
    await page.set_content('<main><h1>Stage test</h1><button disabled>Désolé, cette offre a expiré</button></main>')
    assert await engine._Run._page_says_gone(SimpleNamespace(page=page))
    await page.set_content('<main><h1>Stage test</h1><button>Postuler</button></main>')
    assert not await engine._Run._page_says_gone(SimpleNamespace(page=page))


def test_a_creation_form_that_comes_back_with_its_password_emptied_gets_a_new_password():
    email = field(ref="e1", kind="email", label="Email", required=True, value="camille@example.org", form="0:signup")
    secret = field(ref="p1", kind="password", label="Créez votre mot de passe", required=True, form="0:signup")
    create = field(ref="b1", kind="button", label="Inscription", form="0:signup")
    snap = snap_of(email, secret, create)
    calls = []

    def bounced(last):
        run = run_of()
        run.mode = "submit"
        run.password = SimpleNamespace(rotate=lambda address: calls.append(address) or "new")
        run._account_address = lambda url: "careers.example.test"
        run.last_click_rec = last
        run.last_click_passwords = passwords
        v = engine._Validated()
        assert trusting.check(run, Action(ref="p1", op="fill", fact="account_password"), secret, v, snap, None) is not None
        return v

    passwords = 1
    assert not bounced(None).notes and not calls                              # first visit of the form
    assert not bounced((URL, "connectez vous")).notes and not calls           # another click led here
    passwords = 0  # the same words on the link that led here from another page (a sign-in form): not a bounce
    assert not bounced((URL, "inscription")).notes and not calls
    passwords = 1
    assert any("nouveau mot de passe" in note for note in bounced((URL, "inscription")).notes) and calls == ["careers.example.test"]


def test_workday_account_pages_of_one_career_site_are_one_account():
    apply_page = "https://tenant.wd3.myworkdayjobs.com/fr-FR/acme-careers/job/Testville/Stage-test_JR1/apply/applyManually"
    login_page = "https://tenant.wd3.myworkdayjobs.com/fr-FR/acme-careers/login"
    other_site = "https://tenant.wd3.myworkdayjobs.com/fr-FR/other-careers/login"
    assert engine.account_key(apply_page) == engine.account_key(login_page) != engine.account_key(other_site)
    assert engine.account_key("careers.example.test") == "careers.example.test"
    seen = engine._Accounts()
    seen.add(apply_page)
    assert login_page in seen and other_site not in seen
    seen.discard(login_page)
    assert apply_page not in seen


def test_page_signals_keep_what_the_site_said_without_addresses():
    text = "Connexion Adresse e-mail Mot de passe. Erreur : adresse camille@example.org ou mot de passe incorrect. Suivez-nous"
    assert engine.page_signals(text) == ["Erreur : adresse <e-mail> ou mot de passe incorrect."]
    assert engine.page_signals("Connexion Adresse e-mail Mot de passe") == []


def test_a_photo_nobody_has_does_not_stop_the_send():
    photo = field(ref="f1", kind="file", label="Photo (Choisir un fichier Format autorisé: JPG)", required=True)
    plan = plan_of(unknown=[Question(ref="f1", label="Photo au format JPG", kind="file")])
    blocking, soft = trusting.open_questions(run_of(), plan, snap_of(photo), engine._Validated(), set(), None)
    assert blocking == [] and [q.ref for q in soft] == ["f1"]  # (sent as it is: the site says whether it insists)
    letter = field(ref="f2", kind="file", label="Lettre de recommandation", required=True)
    plan = plan_of(unknown=[Question(ref="f2", label=letter.label, kind="file")])
    blocking, _ = trusting.open_questions(run_of(), plan, snap_of(letter), engine._Validated(), set(), None)
    assert [q.ref for q in blocking] == ["f2"]


@pytest.mark.parametrize(("text", "placeholder"), [
    ("Il n’y a aucun résultat.", True), ("Chargement en cours…", True), ("Loading...", True), ("Aucune sélection", True),
    ("No results found", True), ("Normandie", False), ("Other", False), ("Aucun diplôme", False),
    ("Contient des éléments sélectionnables.", True),
])
def test_what_an_empty_or_loading_list_says_is_not_an_option(text, placeholder):
    assert bool(trusting._NO_RESULT.search(trusting.norm(text))) is placeholder


@pytest.mark.parametrize(("required", "taken"), [("required", "Other"), ("", None)])
async def test_a_required_list_without_the_answer_takes_its_other_entry(page, required, taken):  # noqa: F811
    await page.set_content(f'''<label for="a">Source Detail *</label><input id="a" role="combobox" {required}><div id="pop"></div>
      <script>
        const ALL = ['CareerOne', 'Indeed', 'Other', 'Seek'];
        const show = () => {{ const hits = ALL.filter((t) => t.toLowerCase().includes(a.value.toLowerCase()));
          pop.innerHTML = '<ul role="listbox">' + hits.map((t) => `<li role="option">${{t}}</li>`).join('') + '</ul>';
          for (const li of pop.querySelectorAll('li')) li.onclick = () => {{ a.value = li.textContent; pop.innerHTML = ''; }}; }};
        a.addEventListener('click', show); a.addEventListener('input', show);
      </script>''')
    from jobhunt.apply.snapshot import take_snapshot

    snap, resolve = await take_snapshot(page)
    combo = next(f for f in snap.fields if f.kind == "combobox")
    combo = combo.model_copy(update={"required": bool(required)})
    run = SimpleNamespace(page=page, combo_options={}, _combo_value=engine._Run._combo_value, _abandon_combobox=lambda loc: engine._Run._abandon_combobox(None, loc))
    item = engine._Item(Action(ref=combo.ref, op="select"), combo, None, value="Site carrière de l'entreprise")
    if taken is None:
        with pytest.raises(engine._Skip):
            await trusting._combobox(run, await resolve.element(combo.ref), combo, item)
    else:
        assert await trusting._combobox(run, await resolve.element(combo.ref), combo, item) == taken


@pytest.mark.parametrize(("want", "label"), [("Homme", "Masculin"), ("Femme", "Féminin"), ("Homme", "Male")])
def test_a_gender_answer_finds_its_option_under_another_word(want, label):
    labels = ["Select One", "Féminin", "Female", "Masculin", "Male", "Non déclaré"]
    labels = [text for text in labels if text in ("Select One", "Non déclaré", label)]
    assert labels[trusting.best_option(labels, want)] == label


def test_the_autonomy_rules_tell_the_planner_to_send_from_a_review_page():
    from jobhunt.apply import planner

    assert "review / summary page" in planner.AUTONOMY and "month-first" in planner.AUTONOMY


def test_france_is_found_under_its_name_in_another_language():
    labels = ["Deutschland", "Frankreich", "Französisch-Guayana", "Belgien"]
    assert labels[trusting.best_option(labels, "France")] == "Frankreich"


def test_a_required_message_without_a_letter_takes_a_short_neutral_note():
    run = run_of()
    run.job_title = "Stage ingénieur test"
    note = trusting.short_message(run)
    assert "Stage ingénieur test" in note and note.startswith("Bonjour") and run.facts.get("last_name").value in note
    assert trusting.short_message(run_of(FactSheet(facts=[]))) == ""


async def test_a_choice_that_makes_the_page_replace_the_control_was_taken(page):  # noqa: F811
    await page.set_content('''<div id="box"><label id="l">Pays</label>
      <button id="b" aria-haspopup="listbox" aria-labelledby="l" onclick="pop.innerHTML = '<ul role=listbox><li role=option>France</li><li role=option>Belgique</li></ul>';
        for (const li of pop.querySelectorAll('li')) li.onclick = () => { pop.innerHTML = '';
          box.innerHTML = '<label id=l>Pays</label><button id=b2 aria-haspopup=listbox aria-labelledby=l>' + li.textContent + '</button>'; };">Select One</button></div>
      <div id="pop"></div>''')
    from jobhunt.apply.snapshot import take_snapshot

    snap, resolve = await take_snapshot(page)
    combo = next(f for f in snap.fields if f.kind == "combobox")
    run = SimpleNamespace(page=page, combo_options={}, _combo_value=engine._Run._combo_value, _abandon_combobox=lambda loc: engine._Run._abandon_combobox(None, loc))
    item = engine._Item(Action(ref=combo.ref, op="select", fact="country"), combo, None, value="France")
    assert await trusting._combobox(run, await resolve.element(combo.ref), combo, item) == "France"
    assert await page.inner_text("#b2") == "France"


async def test_a_list_that_takes_its_choice_a_moment_later_is_not_pressed_again(page):  # noqa: F811
    await page.set_content('''<label for="a">Mobile ?</label><input id="a" role="combobox"><div id="pop"></div>
      <script>
        window.picks = 0;
        const show = () => { pop.innerHTML = '<ul role="listbox"><li role="option">Oui</li><li role="option">Non</li></ul>';
          for (const li of pop.querySelectorAll('li')) li.onclick = () => { window.picks++; document.title = 'picks ' + window.picks;
            setTimeout(() => { a.value = li.textContent; pop.innerHTML = ''; }, 1100); }; };
        a.addEventListener('click', show);
      </script>''')
    from jobhunt.apply.snapshot import take_snapshot

    snap, resolve = await take_snapshot(page)
    combo = next(f for f in snap.fields if f.kind == "combobox")
    run = SimpleNamespace(page=page, combo_options={}, _combo_value=engine._Run._combo_value, _abandon_combobox=lambda loc: engine._Run._abandon_combobox(None, loc))
    item = engine._Item(Action(ref=combo.ref, op="select"), combo, None, value="Non")
    assert await trusting._combobox(run, await resolve.element(combo.ref), combo, item) == "Non"
    assert await page.title() == "picks 1"


async def test_an_apply_and_create_form_that_says_the_account_exists_was_not_sent():
    name = field(ref="f1", label="Prénom", required=True)   # (the site emptied the form)
    secret = field(ref="f2", kind="password", label="Choisir un mot de passe", required=True)
    send = field(ref="b1", kind="button", label="Envoyer")
    snap = snap_of(name, secret, send)
    run = _after_final_click(snap, sent_empty=set(), exists=True)
    assert await engine._Run._submit_rejected(run, snap) and run.withdrawn and "careers.example.test" in run.account_exists_hosts
    run = _after_final_click(snap, sent_empty=set(), exists=False)
    assert not await engine._Run._submit_rejected(run, snap) and not run.withdrawn


@pytest.mark.parametrize(("text", "received"), [
    ("Vos données personnelles ont été sauvegardées. Votre candidature pour l'offre 2026-1 STAGE – Test H/F a été envoyée", True),
    ("Your application for the Test Engineer internship has been submitted.", True),
    ("Votre candidature pour l'offre 2026-1 STAGE – Test H/F n'a pas été envoyée", False),
    ("Une fois votre candidature pour l'offre 2026-1 envoyée, vous recevrez un e-mail.", False),
    ("Déposer une candidature à une offre. Les données ont été sauvegardées.", False),
])
def test_a_receipt_that_names_the_posting_between_its_words_is_a_receipt(text, received):
    assert engine.confirmation_evidence(text) is received


async def test_the_answer_to_an_account_click_is_kept_without_values():
    class _Response:
        status = 422
        url = "https://tenant.example.test/fr-FR/acme/createAccount?token=" + "a" * 40

        async def json(self):
            return {"errors": [{"errorCode": "ACCOUNT_EXISTS", "message": "Account for camille@example.org exists"}], "data": {"name": "x"}}

    run = SimpleNamespace(account_pending=[_Response()])
    answers = await engine._Run._account_answers(run)
    assert answers == ["422 /fr-FR/acme/createAccount | errorCode=ACCOUNT_EXISTS ; message=Account for <e-mail> exists"]
    assert run.account_pending == []


async def test_a_live_region_that_announces_the_option_is_not_the_option(page):  # noqa: F811
    await page.set_content('''<label for="a">Mobile ?</label><input id="a" role="combobox">
      <div id="say"></div><div id="pop"></div>
      <script>
        a.addEventListener('click', () => {
          say.innerHTML = '<div role="alert">Non</div>';   // the announcer comes first in the page
          pop.innerHTML = '<div class="row"><span>Oui</span></div><div class="row"><span>Non</span></div>';
          for (const row of pop.children) row.onclick = () => { a.value = row.textContent; pop.innerHTML = ''; say.innerHTML = ''; }; });
      </script>''')
    from jobhunt.apply.snapshot import take_snapshot

    snap, resolve = await take_snapshot(page)
    combo = next(f for f in snap.fields if f.kind == "combobox")
    run = SimpleNamespace(page=page, combo_options={}, _combo_value=engine._Run._combo_value, _abandon_combobox=lambda loc: engine._Run._abandon_combobox(None, loc))
    item = engine._Item(Action(ref=combo.ref, op="select"), combo, None, value="Non")
    assert await trusting._combobox(run, await resolve.element(combo.ref), combo, item) == "Non"
    assert run.combo_options[trusting.norm(combo.label)] == ["Oui", "Non"]


async def test_a_receipt_in_a_dialog_of_the_page_the_site_moved_to_is_found(page):  # noqa: F811
    await page.set_content('''<main><h1>Offres</h1><a href="#">Ingénieur test</a></main>
      <div role="dialog"><h2>Félicitations !</h2><p>Votre candidature a été enregistrée avec succès.</p>
        <label>Adresse e-mail <input type="text"></label><button>Soumettre</button></div>''')
    assert await engine._Run._page_says_received(SimpleNamespace(page=page))
    await page.set_content('<main><h1>Offres</h1><a href="#">Ingénieur test</a><button>Soumettre</button></main>')
    assert not await engine._Run._page_says_received(SimpleNamespace(page=page))


async def test_a_list_nothing_can_be_typed_in_takes_the_entry_closest_to_the_answer(page):  # noqa: F811
    await page.set_content('''<label id="l">Country of citizenship</label>
      <span id="b" role="combobox" tabindex="0" aria-labelledby="l" onclick="pop.innerHTML = '<ul role=listbox>' +
        ['Belgique', 'Burkina Faso', 'Burundi', 'France'].map((t) => '<li role=option>' + t + '</li>').join('') + '</ul>';
        for (const li of pop.querySelectorAll('li')) li.onclick = () => { b.textContent = li.textContent; pop.innerHTML = ''; };">Sélectionner</span>
      <div id="pop"></div>''')
    from jobhunt.apply.snapshot import take_snapshot

    snap, resolve = await take_snapshot(page)
    combo = next(f for f in snap.fields if f.kind == "combobox")
    run = SimpleNamespace(page=page, combo_options={}, _combo_value=engine._Run._combo_value, _abandon_combobox=lambda loc: engine._Run._abandon_combobox(None, loc))
    item = engine._Item(Action(ref=combo.ref, op="select", fact="nationality"), combo, None, value="Burkinabè")
    assert await trusting._combobox(run, await resolve.element(combo.ref), combo, item) == "Burkina Faso"


async def test_an_answer_whose_body_never_ends_does_not_hold_the_run(monkeypatch):
    import asyncio

    class _Hanging:
        status = 200
        url = "https://tenant.example.test/fr-FR/acme/login"

        async def json(self):
            await asyncio.sleep(3600)

    real = asyncio.wait_for
    monkeypatch.setattr(engine.asyncio, "wait_for", lambda aw, timeout: real(aw, 0.05))
    run = SimpleNamespace(account_pending=[_Hanging()])
    assert await engine._Run._account_answers(run) == ["200 /fr-FR/acme/login"]


async def test_a_list_with_its_own_search_box_is_searched_there(page):  # noqa: F811
    await page.set_content('''<label id="l">Country of citizenship</label>
      <span id="b" role="combobox" tabindex="0" aria-labelledby="l">Sélectionner</span><div id="pop"></div>
      <script>
        const ALL = ['Afghanistan', 'Albanie', 'Algérie', 'Belgique', 'Burkina Faso', 'Burundi', 'France'];
        const show = (query) => { const hits = ALL.filter((t) => t.toLowerCase().startsWith(query.toLowerCase())).slice(0, 3);
          list.innerHTML = hits.map((t) => `<li role="option">${t}</li>`).join('');
          for (const li of list.children) li.onclick = () => { b.textContent = li.textContent; pop.innerHTML = ''; }; };
        b.onclick = () => { pop.innerHTML = '<input type="search" id="q"><ul role="listbox" id="list"></ul>';
          window.list = document.getElementById('list'); q.oninput = () => show(q.value); show(''); q.focus(); };
      </script>''')
    from jobhunt.apply.snapshot import take_snapshot

    snap, resolve = await take_snapshot(page)
    combo = next(f for f in snap.fields if f.kind == "combobox")
    run = SimpleNamespace(page=page, combo_options={}, _combo_value=engine._Run._combo_value, _abandon_combobox=lambda loc: engine._Run._abandon_combobox(None, loc))
    item = engine._Item(Action(ref=combo.ref, op="select", fact="nationality"), combo, None, value="Burkina Faso")
    assert await trusting._combobox(run, await resolve.element(combo.ref), combo, item) == "Burkina Faso"


@pytest.mark.parametrize(("want", "label"), [("Français", "French"), ("Français : langue maternelle", "French"), ("Anglais", "English")])
def test_a_language_is_found_under_its_name_in_the_forms_language(want, label):
    labels = ["Dutch", "English", "French", "German", "Other"]
    assert labels[trusting.best_option(labels, want)] == label
    assert label.lower() in trusting.language_names(want)


def test_an_ordinary_answer_has_no_language_names():
    assert trusting.language_names("France") == [] and trusting.language_names("Oui") == [] and trusting.language_names("") == []
    labels = ["Oui", "Non"]
    assert labels[trusting.best_option(labels, "Non")] == "Non"


async def test_an_apply_and_create_form_shown_again_at_a_sibling_address_was_not_sent():
    name = field(ref="f1", label="Prénom", required=True)
    secret = field(ref="f2", kind="password", label="Choisir un mot de passe", required=True)
    send = field(ref="b1", kind="button", label="Envoyer")
    snap = snap_of(name, secret, send)
    run = _after_final_click(snap, sent_empty=set(), exists=True)
    run.sent_url = "https://ats.example.test/careers"   # (the click was made there; the form is shown again at /career)
    assert await engine._Run._submit_rejected(run, snap) and run.withdrawn
    run = _after_final_click(snap, sent_empty=set(), exists=False)
    run.sent_url = "https://ats.example.test/careers"
    assert not await engine._Run._submit_rejected(run, snap) and not run.withdrawn


async def test_required_fields_a_site_unfolds_after_the_click_show_it_refused_the_form():
    send = field(ref="b1", kind="button", label="Envoyer")
    name = field(ref="f1", label="Prénom", required=True)          # (was on the page, empty: nothing had been filled)
    title = field(ref="f2", kind="combobox", label="Civilité", required=True)  # (a section the site unfolded since)
    snap = snap_of(name, title, send)
    run = _after_final_click(snap, sent_empty=set(), click_filled=set())
    assert await engine._Run._submit_rejected(run, snap) and run.withdrawn
    # both held a value when the click was made and are empty now: the form was emptied, which proves nothing
    run = _after_final_click(snap, sent_empty=set(), click_filled={("prenom", "text"), ("civilite", "combobox")})
    assert not await engine._Run._submit_rejected(run, snap) and not run.withdrawn


@pytest.mark.parametrize(("text", "missing"), [
    ("Erreur de saisie - aucune pièce jointe chargée - Je dépose ma candidature", True),
    ("No file was uploaded. Please select a file.", True),
    ("Veuillez joindre votre CV avant d'envoyer", True),
    ("Votre CV : CV.pdf (pièce jointe chargée)", False),
    ("Aucun résultat", False),
])
def test_a_site_saying_no_file_came_with_the_attach_click(text, missing):
    from jobhunt.apply.answer_check import fold

    assert bool(engine.NO_ATTACHMENT_RE.search(fold(text))) is missing


@pytest.mark.parametrize(("label", "bare"), [
    ("Ajouter", True), ("Add file", True), ("Joindre le fichier", True), ("Upload", True),
    ("Ajouter une expérience", False), ("Add another position", False), ("Valider", False),
])
def test_the_bare_attach_button_of_an_upload_block(label, bare):
    from jobhunt.apply.answer_check import fold

    assert bool(engine.BARE_ATTACH_RE.fullmatch(fold(label).strip())) is bare


async def test_an_upload_block_without_a_chosen_file_is_told_apart(page):  # noqa: F811
    await page.set_content('''<div id="cv"><input type="file" id="f1"><input type="text" name="title"><button id="b1">Ajouter</button></div>
      <div id="letter"><input type="file" id="f2"><input type="text" name="title2"><button id="b2">Ajouter</button></div>''')
    await page.set_input_files("#f1", {"name": "CV.pdf", "mimeType": "application/pdf", "buffer": b"%PDF-1.4 test"})
    first = await (await page.query_selector("#b1")).evaluate(engine._ATTACH_JS)
    second = await (await page.query_selector("#b2")).evaluate(engine._ATTACH_JS)
    assert first["found"] and first["filled"] and second["found"] and not second["filled"]


async def test_the_sign_in_link_of_a_page_is_a_control_the_planner_can_click(page):  # noqa: F811
    from jobhunt.apply.snapshot import take_snapshot

    await page.set_content('''<p>Utilisateur déjà enregistré ? <a href="/login">Connectez-vous</a></p>
      <p><a href="/legal">Mentions légales</a> <a href="/account">Sign in</a></p>
      <form><label>E-mail <input name="email"></label><button type="button">Postuler</button></form>''')
    snap, _ = await take_snapshot(page)
    assert [f.label for f in snap.fields if f.kind == "link"] == ["Connectez-vous", "Sign in"]


def test_an_optional_letter_is_attached_once_the_site_refused_the_form_without_it():
    letter = field(ref="f1", kind="file", label="Votre lettre de motivation")
    action = Action(ref="f1", op="upload_letter")
    made, v = do_check(run_of(), action, letter)
    assert made is None and any("facultative" in note for note in v.notes)
    refused = PageSnapshot(url=URL, title="Erreur - Je dépose ma candidature à une offre", fields=[letter])
    made, _ = do_check(run_of(), action, letter, refused)
    assert made is not None and made.used == "letter"


def test_a_phone_is_respelled_after_each_refused_send_without_repeating_itself():
    run = run_of()
    run.rejected_submits = 1  # (the site refused the form: nothing on the field says why)
    typed = ["+33 6 00 00 00 00"]
    for _ in range(3):
        phone = field(ref="p1", kind="text", label="Téléphone", required=True, value=typed[-1])
        made = trusting.check(run, Action(ref="p1", op="fill", fact="phone"), phone, engine._Validated(), snap_of(phone), None)
        assert made is not None and made.forced and made.forced not in typed
        typed.append(made.forced)
    assert not typed[1].startswith("+")  # the other form first: national after international
    # before any refusal, a field that already holds the number is left alone
    calm = run_of()
    phone = field(ref="p1", kind="text", label="Téléphone", required=True, value="+33 6 00 00 00 00")
    assert trusting.check(calm, Action(ref="p1", op="fill", fact="phone"), phone, engine._Validated(), snap_of(phone), None) is None


def test_a_box_acknowledging_the_information_notice_of_an_account_form_is_ticked():
    notice = field(ref="c1", kind="checkbox", required=True, value="false", form="0:signup",
                   label="Merci de prendre connaissance des mentions d'information ci-dessous puis de cocher la case suivante")
    secret = field(ref="p1", kind="password", label="Mot de passe", required=True, form="0:signup")
    snap = snap_of(notice, secret)
    assert trusting.account_consent(notice, snap) and trusting._required_consent(notice)
    made = trusting.validate(run_of(), plan_of(kind="account_signup"), snap, None)
    assert [(item.field.ref, item.act.op) for item in made.items if item.field.ref == "c1"] == [("c1", "check")]


def test_after_an_unexplained_refusal_the_phone_is_respelled_even_when_the_plan_leaves_it_alone():
    run = run_of()
    run.rejected_submits = 1
    name = field(ref="n1", kind="text", label="Nom", required=True, value="Durand")
    phone = field(ref="p1", kind="text", label="Téléphone", required=True, value="+33 6 00 00 00 00")
    send = field(ref="b1", kind="button", label="Envoyer")
    plan = plan_of(Action(ref="b1", op="click", click_role="submit"))
    made = trusting.validate(run, plan, snap_of(name, phone, send), None)
    assert [(item.field.ref, bool(item.forced)) for item in made.items if item.field.ref == "p1"] == [("p1", True)]
    # a page that says what is wrong (another field in error) is not a phone problem
    other = run_of()
    other.rejected_submits = 1
    wrong = name.model_copy(update={"error": "Champ invalide"})
    made = trusting.validate(other, plan, snap_of(wrong, phone, send), None)
    assert not [item for item in made.items if item.field.ref == "p1"]


def test_a_password_is_never_replaced_once_a_creation_form_was_sent_and_left_in_this_run():
    secret = field(ref="p1", kind="password", label="Mot de passe", required=True, form="0:signup",
                   error="Mot de passe non valide")
    create = field(ref="b1", kind="button", label="Créer un compte", form="0:signup")
    calls = []
    run = run_of()
    run.mode = "submit"
    run.password = SimpleNamespace(rotate=lambda address: calls.append(address) or "new")
    run._account_address = lambda url: "careers.example.test"
    run.maybe_created = engine._Accounts()
    run.maybe_created.add("careers.example.test")
    assert trusting.check(run, Action(ref="p1", op="fill", fact="account_password"), secret, engine._Validated(), snap_of(secret, create), None)
    assert calls == []


def test_a_data_use_consent_box_is_ticked_even_when_its_text_reads_like_personal_data():
    box = field(ref="c1", kind="checkbox", value="false",
                label="Vous confirmez avoir pris connaissance et validé les conditions d'utilisation de vos données personnelles.")
    made, v = do_check(run_of(), Action(ref="c1", op="check"), box)
    assert made is not None and made.act.op == "check" and not any("fait dur" in note for note in v.notes)
    licence = field(ref="c2", kind="checkbox", value="false", label="Je suis titulaire du permis de conduire B")
    made, v = do_check(run_of(FactSheet(facts=[])), Action(ref="c2", op="check"), licence)
    assert made is None and any("fait dur" in note for note in v.notes)


_VALUE_HELP = '''<x-box id="h"><button slot="icon" style="width:20px;height:20px"></button></x-box>
  <label for="free">Ville</label><input id="free" aria-autocomplete="list" aria-haspopup="true">
  <script>
    const ALL = %s;
    customElements.define('x-box', class extends HTMLElement { connectedCallback() {
      const root = this.attachShadow({mode: 'open'});
      root.innerHTML = '<input aria-label="%s" aria-autocomplete="list" aria-haspopup="dialog"><slot name="icon"></slot><div id="pop"></div>';
      const input = root.querySelector('input'), pop = root.querySelector('#pop');
      const show = (items) => {
        pop.innerHTML = '<ul role="listbox">' + (items.length ? items : ['Aucune donnée']).map((t) => `<li role="option">${t}</li>`).join('') + '</ul>';
        for (const li of pop.querySelectorAll('li')) li.onclick = () => { if (items.length) { input.value = li.textContent; pop.innerHTML = ''; } }; };
      // (the site's own search: nothing for a year, nothing for a name it spells another way)
      input.addEventListener('input', () => show(ALL.filter((t) => input.value && t.toLowerCase().endsWith(' - ' + input.value.toLowerCase()))));
      this.querySelector('button').onclick = () => show(ALL);
    } });
  </script>'''
_YEARS = [str(year) for year in range(1980, 2027)]
_COUNTRIES = ["AE - EMIRATS ARABES UNIS", "AUTRES", "BE - BELGIQUE", "FR - FRANCE"]


@pytest.mark.parametrize(("label", "listed", "want", "taken"), [
    ("Completion year", _YEARS, "2025", "2025"),  # typing finds nothing: the arrow shows the whole list
    ("Country", _COUNTRIES, "France", "FR - FRANCE"),
    ("Country", _COUNTRIES, "Burkina Faso", "AUTRES"),  # not offered: the list's "other" entry
    ("Completion year", _YEARS, "2027", None),  # not offered, no "other": nothing is chosen, the list is shown to the planner
])
async def test_a_suggesting_box_with_an_arrow_is_a_list_opened_by_its_arrow(page, label, listed, want, taken):  # noqa: F811
    import json

    await page.set_content(_VALUE_HELP % (json.dumps(listed), label))
    from jobhunt.apply.snapshot import take_snapshot

    snap, resolve = await take_snapshot(page)
    assert [(f.label, f.kind) for f in snap.fields if f.kind in ("text", "combobox")] == [(label, "combobox"), ("Ville", "text")]
    combo = next(f for f in snap.fields if f.kind == "combobox").model_copy(update={"required": True})
    run = SimpleNamespace(page=page, combo_options={}, _combo_value=engine._Run._combo_value, _abandon_combobox=lambda loc: engine._Run._abandon_combobox(None, loc))
    item = engine._Item(Action(ref=combo.ref, op="select"), combo, None, value=want)
    if taken is None:
        with pytest.raises(engine._Skip):
            await trusting._combobox(run, await resolve.element(combo.ref), combo, item)
        assert run.combo_options[trusting.norm(label)] == listed
    else:
        assert await trusting._combobox(run, await resolve.element(combo.ref), combo, item) == taken


# A multiple-choice search box of a design system: the text box and the chosen tags live in a shadow root, the list keeps
# 60 entries, its own search only knows each entry's first letters, and a choice becomes a tag (the text box stays empty).
TAGS_LIST = '''<form><label id="q">Mobilité géographique *</label><x-tags></x-tags><button>Envoyer</button></form>
  <script>
    const ALL = ['All France', 'FR - East', 'FR - West', 'All international', ...Array.from({length: 56}, (_, i) => 'Pays ' + (i + 1))];
    const host = document.querySelector('x-tags');
    host.attachShadow({mode: 'open'}).innerHTML = '<div class="box"><span class="tags"></span>'
      + '<input role="combobox" aria-autocomplete="list" aria-label="Mobilité géographique" aria-required="true"></div><ul role="listbox" hidden></ul>';
    const input = host.shadowRoot.querySelector('input'), list = host.shadowRoot.querySelector('ul'), tags = host.shadowRoot.querySelector('.tags');
    const show = () => {
      const typed = input.value.toLowerCase();
      list.innerHTML = ''; list.hidden = false;
      for (const name of ALL.filter((entry) => !typed || entry.toLowerCase().startsWith(typed))) {
        const row = document.createElement('li'); row.setAttribute('role', 'option'); row.textContent = name;
        row.onclick = () => { const tag = document.createElement('span'); tag.className = 'tag'; tag.textContent = name; tags.append(tag); };
        list.append(row);
      }
    };
    input.addEventListener('click', show); input.addEventListener('input', show);
    input.addEventListener('keydown', (event) => { if (event.key === 'Escape') list.hidden = true; });
  </script>'''


async def test_a_tag_box_takes_the_entry_of_its_full_list_when_its_search_finds_nothing(page):  # noqa: F811
    """The stored answer is worded in another language than the list ("Toute la France" for "All France") and the box's own
    search finds nothing for it: the entry is chosen in the whole list, and the tag it becomes is the proof it was taken."""
    from jobhunt.apply.snapshot import take_snapshot

    await page.set_content(TAGS_LIST)
    snap, resolve = await take_snapshot(page)
    combo = next(f for f in snap.fields if f.kind == "combobox")
    run = SimpleNamespace(page=page, combo_options={}, combo_chosen={}, _combo_value=engine._Run._combo_value,
                          _abandon_combobox=lambda loc: engine._Run._abandon_combobox(None, loc))
    item = engine._Item(Action(ref=combo.ref, op="select", fact="mobility"), combo, None, value="Toute la France")
    assert await trusting._combobox(run, await resolve.element(combo.ref), combo, item) == "All France"
    assert await page.evaluate("[...document.querySelector('x-tags').shadowRoot.querySelectorAll('.tag')].map((tag) => tag.textContent)") == ["All France"]
    assert run.combo_chosen == {trusting.norm(combo.label): "All France"}
    assert await page.evaluate("document.querySelector('x-tags').shadowRoot.querySelector('ul').hidden") is True


def test_the_whole_country_is_the_same_entry_in_another_language():
    assert trusting.best_option(["All France", "FR - East France", "All international", "France", "Germany"], "Toute la France") == 0


async def test_an_agreement_dialog_with_boxes_is_to_be_confirmed_not_closed(page):  # noqa: F811
    """A dialog that asks to agree (a box to tick, a confirm button, a close cross): the planner is told to tick and confirm;
    closing it would give the application up. A dialog without boxes keeps the plain "close or accept" wording."""
    await page.set_content(dialog("Please review before continuing", '<button aria-label="Close the dialog">×</button>'
                                  '<label><input type="checkbox" required> I have read and agree to the Privacy Policy.</label>'
                                  '<button>Submit</button>'))
    run = SimpleNamespace(page=page)
    assert await trusting.dismiss_blocker(run) == ""
    hint = await trusting.blocker_hint(run)
    assert "Submit" in hint and "CONFIRMS" in hint and "NEVER its close button" in hint and "ONLY the click that closes" not in hint
    await page.set_content(dialog("Bienvenue sur notre site !", CONTINUE))
    assert "ONLY the click that closes or accepts it" in await trusting.blocker_hint(run)


async def test_an_upload_button_whose_first_click_brought_nothing_is_clicked_once_more(page, tmp_path):  # noqa: F811
    """The page took the first click for something else (it was not listening yet): no chooser, no dialog, no menu. The
    button is clicked once more, and the file goes into the field its dialog then shows."""
    await page.set_content('''<form><button type="button" id="up">Charger un CV</button><div id="slot"></div></form>
      <script>let clicks = 0;
        up.addEventListener('click', () => { if (++clicks < 2) return;
          slot.innerHTML = '<div role="dialog"><span>Charger depuis l’appareil</span><input type="file" id="cv"></div>'; });
      </script>''')
    cv = tmp_path / "CV.pdf"
    cv.write_bytes(b"%PDF-1.4 test")
    run = SimpleNamespace(page=page, uploaded=False)
    await trusting._upload_through(run, await page.query_selector("#up"), cv)
    assert run.uploaded and await page.evaluate("document.getElementById('cv').files[0].name") == "CV.pdf"


async def test_an_upload_button_that_never_brings_a_file_field_says_what_happened(page, tmp_path):  # noqa: F811
    await page.set_content('<form><button type="button" id="up">Charger un CV</button></form>')
    cv = tmp_path / "CV.pdf"
    cv.write_bytes(b"%PDF-1.4 test")
    with pytest.raises(engine._Skip, match=r"clic fait ; rien d'ouvert : second clic ; 0 champ\(s\) fichier, aucun dialogue"):
        await trusting._upload_through(SimpleNamespace(page=page, uploaded=False), await page.query_selector("#up"), cv)


async def test_a_floating_notice_inside_the_frame_that_holds_the_form_is_read(page):  # noqa: F811
    """A career site shows its application form in a frame; the form's own floating notice names the missing field. A cookie
    bar or a help widget floating over the page is not such a notice."""
    await page.set_content('''<title>Candidature à un poste</title><h1>Postuler</h1>
      <div style="position:fixed;bottom:0;left:0;right:0">Nous utilisons des cookies pour améliorer votre visite.</div>
      <iframe style="width:900px;height:400px" srcdoc="<div style='position:fixed;top:0;left:0;right:0'>
        <span>Veuillez renseigner le champ 'Mobilité'.</span><a href='#'>×</a></div><form><input name='a'><button>Enregistrer</button></form>"></iframe>''')
    await page.wait_for_timeout(300)
    run = SimpleNamespace(page=page)
    assert await engine._Run._notice_texts(run, "Candidature à un poste") == ["Veuillez renseigner le champ 'Mobilité'.×"]


def test_a_phone_the_page_message_calls_invalid_is_respelled_without_its_trunk_zero_beside_a_calling_code():
    """An editor's "Save" (not a send) comes back with "The phone number is invalid" in a message, nothing on the field.
    Beside a calling-code list the number is typed without its trunk zero; a page that says nothing leaves it alone."""
    code = field(ref="c1", kind="select", label="Indicatif téléphonique du pays", value="France (+33)",
                 options=[FieldOption(value="FRA_33", label="France (+33)"), FieldOption(value="BEL_32", label="Belgique (+32)")])
    phone = field(ref="p1", kind="tel", label="Numéro de téléphone", required=True, value="0600000000")
    snap = snap_of(code, phone)
    action = Action(ref="p1", op="fill", fact="phone")
    assert trusting.check(run_of(), action, phone, engine._Validated(), snap, None) is None
    run = run_of()
    run.page_messages = ["The phone number is invalid"]
    made = trusting.check(run, action, phone, engine._Validated(), snap, None)
    assert made is not None and made.forced == "600000000"
    assert trusting.phone_refused(["Le numéro de téléphone est incorrect."]) and not trusting.phone_refused(["Veuillez renseigner le champ 'Mobilité'."])


async def test_a_required_source_prompt_that_does_not_list_the_job_board_takes_the_employers_own_site(page):  # noqa: F811
    """"How did you hear about us?" as a search prompt whose list knows neither the board the ad was seen on nor "other":
    the entry for the employer's careers site is taken (the application is made there). Another required prompt gets no
    such answer."""
    from jobhunt.apply.snapshot import take_snapshot

    content = SEARCH_PROMPT.replace("REQUIRED", "required").replace(
        "['École Centrale de Testville', 'Institut Polytechnique de Testville', 'Université de Testville', 'Université de Fauxbourg', 'Autre']",
        "['Salon étudiant', 'Cooptation', 'Site carrière ExempleCo', 'Réseaux sociaux']")
    assert "Site carrière ExempleCo" in content
    await page.set_content(content)
    snap, resolve = await take_snapshot(page)
    box = next(f for f in snap.fields if f.kind == "text")
    target = await resolve.element(box.ref)
    run = SimpleNamespace(page=page)
    with pytest.raises(engine._Skip):
        await trusting._search_pick(run, target, box, "Jobboard Quelconque")
    assert await trusting._search_pick(run, target, box, "Jobboard Quelconque", source=True) == "Site carrière ExempleCo"
    assert trusting.SOURCE_QUESTION.search(trusting.fold("Comment nous avez-vous connus ?"))
    assert trusting.SOURCE_QUESTION.search(trusting.fold("How did you hear about us?"))
    assert not trusting.SOURCE_QUESTION.search(trusting.fold("Dans quelle école êtes-vous ?"))


def test_the_submit_of_an_agreement_dialog_before_the_application_is_not_the_send():
    """A dialog asks to agree (boxes, "Submit") before the form is even reached: its button sends the agreement. Once
    something of the application was entered, a "Submit" is a send again."""
    button = field(ref="b1", kind="button", label="Submit")
    run = run_of(app_touched=True)  # (the required box of the dialog was ticked)
    run.agreement_buttons = {trusting.norm("Close the dialog"), trusting.norm("Submit")}
    assert trusting.effective_role(run, button, "next", snap_of(button)) == "next"
    assert trusting.effective_role(run, button, "submit", snap_of(button)) == "next"
    run.data_typed = True
    assert trusting.effective_role(run, button, "next", snap_of(button)) == "submit"
    assert trusting.effective_role(run_of(app_touched=True), button, "other", snap_of(button)) == "submit"
