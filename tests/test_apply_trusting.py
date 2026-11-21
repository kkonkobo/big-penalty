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
