"""Regressions from the PR #10 real-harm review: off switch parsing, draft JSON receipts, no guessed key facts."""

from __future__ import annotations

import json

import pytest

from jobhunt.apply import autonomous
from jobhunt.apply.engine import _UNATTENDED_NO_GUESS
from jobhunt.config import get_settings


@pytest.mark.parametrize(("raw", "enabled"), [("0", False), ("false", False), ("FALSE", False), ("off", False),
                                              ("no", False), ("0 ", False), ("1", True), ("", True)])
def test_autonomous_off_switch_accepts_the_usual_words(raw, enabled, monkeypatch):
    monkeypatch.setenv("JOBHUNT_APPLY_AUTONOMOUS", raw)
    get_settings.cache_clear()
    try:
        assert get_settings().apply_autonomous is enabled
    finally:
        get_settings.cache_clear()


@pytest.mark.parametrize("body", [{"applicationId": "d-1", "status": "in_progress"}, {"applicationId": "d-1", "status": "draft"},
                                  {"id": 3, "status": "saved"}, {"applicationId": "d-1", "status": "started"}])
def test_draft_like_json_is_never_a_receipt(body):
    assert not autonomous.confirmation_json(json.dumps(body).encode())


@pytest.mark.parametrize("label", ["Highest degree obtained", "Diplôme le plus élevé", "Niveau d'études", "Date de début souhaitée",
                                   "Available start date", "Disponibilité", "Expected salary", "Prétentions salariales",
                                   "Gehaltsvorstellung", "Höchster Abschluss"])
def test_key_facts_are_never_guessed_unattended(label):
    assert _UNATTENDED_NO_GUESS.search(label)


@pytest.mark.parametrize("label", ["Prénom", "Pourquoi nous rejoindre ?", "LinkedIn", "Ville"])
def test_ordinary_questions_are_not_caught(label):
    assert not _UNATTENDED_NO_GUESS.search(label)
