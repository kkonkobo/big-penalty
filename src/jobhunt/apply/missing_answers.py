"""Read-only projection of potentially reusable answers for the pilot."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Collection, Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from jobhunt.apply.facts import norm_question
from jobhunt.apply.types import Question

if TYPE_CHECKING:
    from jobhunt.apply.service import QueueItem


ALIASES = {
    "city": ("Ville de résidence", "Ville de résidence actuelle", "Votre ville de résidence",
             "City of residence", "Current city of residence", "Residential city"),
    "postal_code": ("Code postal de résidence", "Code postal de votre domicile", "Code postal de votre adresse actuelle",
                    "Residential postal code", "Residential ZIP code", "Postal code of your current address",
                    "ZIP code of your current address"),
    "country": ("Pays de résidence", "Pays de résidence actuel", "Votre pays de résidence",
                "Country of residence", "Current country of residence", "Country you currently live in"),
    "graduation_date": ("Date d'obtention du diplôme (prévue)", "Date prévue d'obtention du diplôme",
                        "Date prévisionnelle d'obtention du diplôme", "Expected graduation date",
                        "Anticipated graduation date", "When do you expect to graduate?"),
    "internship_agreement": ("L'école fournit une convention de stage",
                             "Votre école peut-elle fournir une convention de stage ?",
                             "Can your school provide an internship agreement?",
                             "Does your school provide an internship agreement?",
                             "Can your university provide an internship agreement?"),
}
_LABEL_KEYS = {norm_question(label): key for key, labels in ALIASES.items() for label in labels}
_KINDS = {
    "city": {"text", "select", "combobox"},
    "postal_code": {"text", "number"},
    "country": {"text", "select", "combobox"},
    "graduation_date": {"text", "date", "select", "combobox"},
    "internship_agreement": {"text", "select", "combobox", "radio"},
}


def fact_key(question: Question) -> str | None:
    """No substring matches, ref/option heuristics, or unqualified address components."""
    key = _LABEL_KEYS.get(norm_question(question.label))
    return key if key and question.required and question.kind in _KINDS[key] else None


@dataclass(frozen=True)
class MissingAnswer:
    fact_key: str
    application_ids: tuple[int, ...]

    @property
    def application_count(self) -> int:
        return len(self.application_ids)


def input_pauses(profile_id: int, items: Iterable[QueueItem]) -> Iterable[QueueItem]:
    """Select the active candidate's current input pauses from a latest-run queue snapshot."""
    for item in items:
        app, run = item.app, item.last_run
        if (app.profile_id == profile_id and app.status in {"draft", "approved"}
                and item.bucket == "paused" and item.pause_reason == "input" and run is not None
                and run.status == "needs_input" and run.application_id == app.id and run.rev == app.current_rev):
            yield item


def project(profile_id: int, items: Iterable[QueueItem], *, recorded_fact_keys: Collection[str],
            answered_labels: Collection[str]) -> list[MissingAnswer]:
    """Group open questions by fact key, without receiving any answer values.

    `answered_labels` contains normalized labels with a nonempty saved custom answer. Counts are per fact, not
    additive across facts, and indicate candidates for review, not readiness or permission to resume.
    """
    affected: dict[str, set[int]] = defaultdict(set)
    for item in input_pauses(profile_id, items):
        for question in item.questions:
            key = fact_key(question)
            if key and key not in recorded_fact_keys and norm_question(question.label) not in answered_labels:
                affected[key].add(item.app.id)
    groups = [MissingAnswer(key, tuple(sorted(app_ids))) for key, app_ids in affected.items()]
    return sorted(groups, key=lambda group: (-group.application_count, group.fact_key))
