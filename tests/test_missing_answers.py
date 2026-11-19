from datetime import UTC, datetime

import pytest

from jobhunt.apply.missing_answers import MissingAnswer, fact_key, project
from jobhunt.apply.service import QueueItem
from jobhunt.apply.store import ApplyRun
from jobhunt.apply.types import Question
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
    ("City", "text", None),
    ("Pays", "select", None),
    ("Postal code", "text", None),
    ("Postal code of your previous address", "text", None),
    ("Ville de naissance", "text", None),
    ("Preferred city of residence", "text", None),
    ("Country code", "select", None),
    ("Country of residence or nationality?", "select", None),
    ("Graduation date", "date", None),
    ("Expected graduation year", "number", None),
    ("Internship agreement required by employer?", "radio", None),
    ("Can your school provide an internship agreement?", "file", None),
])
def test_only_explicit_required_fact_questions_match(label, kind, expected):
    assert fact_key(Question(label=label, kind=kind, ref="city", options=["Fictional option"])) == expected
    assert fact_key(Question(label=label, kind=kind, required=False)) is None


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
