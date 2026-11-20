from datetime import UTC, datetime

import pytest

from jobhunt.apply.missing_answers import MissingAnswer, capture, fact_key, project
from jobhunt.apply.prompt_rules import decision_key, international_calling_code, technical_key
from jobhunt.apply.service import QueueItem
from jobhunt.apply.store import ApplyRun
from jobhunt.apply.types import FormField, Question
from jobhunt.cv.store import Application
from jobhunt.cv.tailored import TailoredCV


def pause(app_id, *labels, profile_id=1, rev=2, status="needs_input", bucket="paused", reason="input", app_status="draft"):
    app = Application(id=app_id, profile_id=profile_id, job_id=app_id, current_rev=2, status=app_status,
                      tailored=TailoredCV(headline="Fictional CV", summary="Fictional summary"))
    run = ApplyRun(id=100 + app_id, application_id=app_id, rev=rev, mode="dry_run", trigger="user",
                   status=status, started_at=datetime(2026, 1, 1, tzinfo=UTC))
    return QueueItem(app=app, last_run=run, bucket=bucket, pause_reason=reason,
                     questions=[Question(label=label) for label in labels])


@pytest.mark.parametrize(("label", "kind", "expected"), [
    (" VILLE de RÉSIDENCE * ", "text", "city"),
    ("Residential ZIP code", "number", "postal_code"),
    ("Country of residence?", "combobox", "country"),
    ("Date prévue d’obtention du diplôme", "date", "graduation_date"),
    ("Can your school provide an internship agreement?", "radio", "internship_agreement"),
    ("City", "text", "city"),
    ("Pays", "select", "country"),
    ("Postal code", "text", "postal_code"),
    ("Postleitzahl", "text", "postal_code"),
    ("Pays/région de résidence", "select", "country"),
    ("Country/Region of Residence", "select", "country"),
    ("Land", "select", None),
    ("Telefon-Gerätetyp", "select", "phone_device_type"),
    ("Postal code of your previous address", "text", None),
    ("Ville de naissance", "text", None),
    ("Preferred city of residence", "text", None),
    ("Country code", "select", None),
    ("Country of residence or nationality?", "select", None),
    ("Graduation date", "date", None),
    ("Expected graduation year", "number", None),
    ("Internship agreement required by employer?", "radio", None),
    ("Avez-vous besoin d'un visa ou d'un permis de travail ?", "radio", None),
    ("Can your school provide an internship agreement?", "file", None),
])
def test_only_explicit_required_fact_questions_match(label, kind, expected):
    assert fact_key(Question(label=label, kind=kind, ref="city", options=["Fictional option"])) == expected
    assert fact_key(Question(label=label, kind=kind, required=False)) is None


@pytest.mark.parametrize("label", ["One-time code", "Code de vérification", "Bestätigungscode"])
def test_verification_codes_are_technical(label):
    assert technical_key(label, "text") == "credentials"


def test_projection_deduplicates_distinct_apps_and_uses_only_current_input_pauses():
    items = [
        pause(8, "City of residence", "Ville de résidence", "Expected graduation date"),
        pause(3, "Ville de résidence", "Expected graduation date", "Country of residence", "Pays de résidence",
              "Residential postal code", "Can your school provide an internship agreement?"),
        pause(2, "City of residence", profile_id=2),
        pause(4, "City of residence", rev=1),
        pause(5, "City of residence", status="needs_email", reason="email"),
        pause(6, "City of residence", bucket="blocked"),
        pause(7, "City of residence", app_status="withdrawn"),
        pause(9, "City of residence", status="needs_human", reason="human"),
        pause(10, "City of residence", app_status="failed"),
    ]
    groups = project(1, [*items, items[0]], recorded_fact_keys={"postal_code"}, answered_labels={"country of residence"})
    assert groups == [MissingAnswer("city", (3, 8)), MissingAnswer("graduation_date", (3, 8)),
                      MissingAnswer("country", (3,)), MissingAnswer("internship_agreement", (3,))]
    assert [group.application_count for group in groups] == [2, 2, 1, 1]


def test_capture_distinguishes_context_and_choice_polarity():
    def country(group):
        return capture(FormField(ref="field", kind="select", label="Land", group=group), "Land")

    assert country("Wohnanschrift").fact_id == "country"
    assert country("Geburtsort").category == "ambiguous_country"
    assert capture(FormField(ref="field", kind="text", label="City", group="Place of birth"), "City").fact_id is None
    for phrase in ("Consent to AI assistance", "J'autorise l'assistance par IA", "KI-Unterstützung zustimmen"):
        assert decision_key(phrase) == "ai_opt_in"
    for phrase in ("Opt out of AI assistance", "Je refuse l’assistance par IA", "KI-Unterstützung nicht zulassen"):
        assert decision_key(phrase) == "ai_opt_out"


@pytest.mark.parametrize(("phone", "expected"), [("+33 6 00 00 00 00", "+33"), ("0049 151 00000000", "+49"),
                                                 ("06 00 00 00 00", None), ("+33 (0)6 00 00 00 00", None)])
def test_calling_code_does_not_infer_international_number(phone, expected):
    assert international_calling_code(phone) == expected


@pytest.mark.parametrize(('postal', 'country', 'expected'), [
    ('31000', None, '31'), ('20137', 'France', '2A'), ('20200', 'FR', '2B'),
    ('20600', 'France', '2B'), ('97400', 'France', '974'), ('97600', None, '976'),
    ('10115', 'Allemagne', None), ('98000', None, None), ('7500', None, None), ('SW1A 1AA', None, None),
])
def test_inferred_residence_facts(profile, postal, country, expected):
    from jobhunt.apply.facts import candidate_answers

    answers = {"postal_code": postal, "country": country, "city": "Ville-Test", "address": "1 rue Fictive",
               "address_line_2": "Bâtiment B"}
    known = candidate_answers(profile, None, answers)
    assert known['department'] == expected
    assert known['country'] == (country or ('France' if expected else None))
    assert [known[key] for key in ('postal_code', 'city', 'address', 'address_line_2')] == [postal, 'Ville-Test', '1 rue Fictive', 'Bâtiment B']


@pytest.mark.parametrize(('phone', 'code', 'device'), [
    ('06 11 11 11 11', '+33', 'Mobile'), ('+33 7 11 11 11 11', '+33', 'Mobile'),
    ('01 11 11 11 11', '+33', None), ('+49 151 00000000', '+49', None), ('061234', None, None),
])
def test_inferred_phone_facts(profile, phone, code, device):
    from jobhunt.apply.facts import candidate_answers

    values = candidate_answers(profile.model_copy(update={'phone': phone}), None, {})
    assert (values['phone_calling_code'], values['phone_device_type']) == (code, device)


@pytest.mark.parametrize(('key', 'value', 'options', 'expected'), [
    ('country', 'France', ['Belgique', 'France'], 1), ('country', 'France', ['France métropolitaine'], None),
    ('city', 'Toulouse', ['Toulouse', 'Toulouse (préférée)'], 0), ('city', 'Toulouse', ['Toulouse Centre'], None),
    ('department', '31', ['Paris', 'Haute-Garonne'], 1), ('department', '2A', ['2B', '2A - Corse-du-Sud'], 1),
    ('department', '974', ['La Réunion'], 0), ('department', '31', ['Haute-Garonne', '31'], None),
    ('phone_calling_code', '+33', ['France (+33)', 'Luxembourg (+352)'], 0),
    ('phone_calling_code', '+33', ['Pays fictif (+330)', 'Non précisé'], None),
    ('phone_calling_code', '+33', ['Belgique', 'France', 'Suisse'], 1), ('phone_calling_code', '+33', ['Belgique', 'Suisse'], None),
    ('phone_calling_code', '+33', ['France', 'France (+33)'], None),
    ('phone_device_type', 'Mobile', ['Cell phone'], 0), ('phone_device_type', 'Mobile', ['Fixe'], None),
    ('internship_agreement', 'Oui', ['No', 'Yes'], 1), ('internship_agreement', 'Oui', ['Yes, under conditions'], None),
])
def test_standing_option_mapping_requires_unique_equivalence(key, value, options, expected):
    from jobhunt.apply.missing_answers import exact_option

    assert exact_option(key, value, options, '') == expected


def test_phone_country_prompt_is_the_calling_code_question():
    from jobhunt.apply.missing_answers import standing_key

    assert standing_key('Phone number country') == 'phone_calling_code'


def test_master_graduation_and_identity_are_not_guessed(profile):
    from cv_fakes import sample_master
    from jobhunt.apply.facts import candidate_answers
    from jobhunt.cv.master import Education

    master = sample_master()
    master.education = [Education(title='Fictional MSc', dates='2025-09-01 – 2027-06-30')]
    known = candidate_answers(profile, master, {})
    assert [known[key] for key in ('first_name', 'last_name', 'email', 'phone', 'graduation_date')] == [
        'Camille', 'Durand', 'camille@example.test', '+33 6 00 00 00 00', '30 juin 2027']
    master.education[0].dates = '2025 – en cours'
    unknown = candidate_answers(profile, master, {})
    assert unknown['graduation_date'] is unknown['country'] is unknown['internship_agreement'] is None
    assert unknown['politically_exposed'] is unknown['conflict_of_interest'] is unknown['recruiter_ai'] is None


def test_live_engine_standing_options_and_ai_polarity(tmp_path):
    from jobhunt.apply.engine import Uploads, _Run
    from jobhunt.apply.types import Fact, FactSheet, FieldOption

    run = _Run(None, url='https://fictional.test/apply', facts=FactSheet(facts=[
        Fact(key='country', label='Pays', value='France'), Fact(key='recruiter_ai', label='IA', value='Non'),
    ]), planner=None, uploads=Uploads(cv_pdf=None), mode='submit', out_dir=tmp_path, job_title='', company='', password=None,
        before_submit=None, on_submit_click=None, on_nav_click=None, verifier=None, host_allowed=None, max_steps=1, goto=None)
    country = FormField(ref='country', kind='select', label='Country', required=True,
                        options=[FieldOption(value='FR', label='France')], choice_options=[('FR', 'France', False, '')])
    assert run._standing_item(country).option_index == 0
    assert run._standing_item(country.model_copy(update={'choice_options': [('FR', 'France', True, '')]})) is None
    assert run._standing_item(country.model_copy(update={'group': 'Country of birth'})) is None
    ai = FormField(ref='ai', kind='checkbox', label='J’accepte que le recruteur soit assisté par un outil doté d’IA')
    assert run._standing_item(ai).act.op == 'uncheck'
    assert run._standing_item(ai.model_copy(update={'label': 'Je refuse que le recruteur soit assisté par un outil doté d’IA'})).act.op == 'check'
    assert run._standing_item(ai.model_copy(update={'label': 'J’accepte la politique de confidentialité'})) is None


@pytest.mark.parametrize("label,expected", [("Ville", None), ("Numéro de téléphone", None), ("Code de vérification", "credentials"),
                                            ("Code", "credentials"), ("", "credentials")])
def test_one_time_code_autocomplete_only_makes_a_credential_of_a_code_field(label, expected):
    from jobhunt.apply.prompt_rules import technical_key
    assert technical_key(label, "text", autocomplete="one-time-code") == expected
