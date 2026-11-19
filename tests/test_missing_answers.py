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
    ("Pays", "select", None),
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
