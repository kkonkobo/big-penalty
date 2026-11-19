"""Read-only projection of potentially reusable answers for the pilot."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Collection, Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from jobhunt.apply.prompt_rules import decision_key, norm_question, technical_key
from jobhunt.apply.types import FormField, Question

if TYPE_CHECKING:
    from jobhunt.apply.service import QueueItem


ALIASES = {
    "city": ("Ville de résidence", "Ville de résidence actuelle", "Votre ville de résidence",
             "City of residence", "Current city of residence", "Residential city", "Ville", "City", "Stadt", "Wohnort"),
    "postal_code": ("Code postal de résidence", "Code postal de votre domicile", "Code postal de votre adresse actuelle",
                    "Residential postal code", "Residential ZIP code", "Postal code of your current address",
                    "ZIP code of your current address", "Code postal", "Postal Code", "ZIP Code", "Zipcode", "Postleitzahl", "PLZ"),
    "country": ("Pays de résidence", "Pays de résidence actuel", "Votre pays de résidence",
                "Country of residence", "Current country of residence", "Country you currently live in",
                "Pays/région de résidence", "Country/Region of Residence", "Wohnsitzland", "Land/Region des Wohnsitzes"),
    "graduation_date": ("Date d'obtention du diplôme (prévue)", "Date prévue d'obtention du diplôme",
                        "Date prévisionnelle d'obtention du diplôme", "Expected graduation date",
                        "Anticipated graduation date", "When do you expect to graduate?"),
    "internship_agreement": ("L'école fournit une convention de stage",
                             "Votre école peut-elle fournir une convention de stage ?",
                             "Can your school provide an internship agreement?",
                             "Does your school provide an internship agreement?",
                             "Can your university provide an internship agreement?"),
    "first_name": ("Prénom", "Votre prénom", "First name", "Given name", "Vorname"),
    "last_name": ("Nom de famille", "Last name", "Family name", "Surname", "Nachname"),
    "full_name": ("Nom complet", "Full name", "Vollständiger Name"),
    "email": ("E-mail", "Email address", "E-mail address", "Adresse e-mail", "Courriel", "E-Mail-Adresse"),
    "phone": ("Téléphone", "Numéro de téléphone", "Phone", "Phone number", "Telephone number", "Telefonnummer"),
    "phone_calling_code": ("Indicatif de pays", "Indicatif téléphonique", "Indicatif du pays", "Country calling code",
                           "Phone country code", "Telephone country code", "Ländervorwahl", "Telefonländervorwahl"),
    "phone_device_type": ("Phone Device Type", "Phone Type", "Type d’appareil téléphonique", "Type de téléphone",
                          "Type de périphérique téléphonique", "Telefon-Gerätetyp", "Telefongerätetyp", "Telefontyp",
                          "Telefon Gerätetyp", "Telefon Gerätetyp (privat)"),
    "work_authorization": ("Décrivez votre autorisation de travail en France", "Describe your work authorization in France",
                           "Beschreiben Sie Ihre Arbeitserlaubnis in Frankreich"),
    "needs_visa_sponsorship": ("Do you need visa sponsorship?", "Do you require visa sponsorship?",
                              "Avez-vous besoin d'un visa ou d'un permis de travail ?"),
}
_LABEL_KEYS = {norm_question(label): key for key, labels in ALIASES.items() for label in labels}
_KINDS = {
    "city": {"text", "select", "combobox"},
    "postal_code": {"text", "number"},
    "country": {"text", "select", "combobox"},
    "graduation_date": {"text", "date", "select", "combobox"},
    "internship_agreement": {"text", "select", "combobox", "radio"},
    "first_name": {"text"}, "last_name": {"text"}, "full_name": {"text"}, "email": {"text", "email"},
    "phone": {"text", "tel"}, "phone_calling_code": {"select", "combobox"},
    "phone_device_type": {"select", "combobox", "radio"}, "work_authorization": {"text", "textarea"},
    "needs_visa_sponsorship": {"select", "combobox", "radio"},
}


def fact_key(question: Question) -> str | None:
    """An exact label and a compatible control; no matching from an ATS ref or options."""
    if question.reason is not None:
        key = question.fact_id if question.reason == "fact" else None
    else:
        key = _LABEL_KEYS.get(norm_question(question.label))
    return key if key in _KINDS and question.required and question.kind in _KINDS[key] else None


def capture(field: FormField, label: str) -> Question:
    blocker = technical_key(label, field.kind)
    question = Question(ref=field.ref, label=label, kind=field.kind,
                        options=[] if blocker else [option.label for option in field.options])
    if blocker:
        return question.model_copy(update={"reason": "technical", "category": blocker})
    group = norm_question(field.group)
    key = fact_key(question) if field.label else None
    if key in {"city", "postal_code", "country"} and any(term in group for term in (
            "birth", "naissance", "geburt", "previous", "ancien", "school", "ecole", "preferred", "souhaite", "employer", "arbeitgeber")):
        key = None
    choice = decision_key(label)
    if choice == "ambiguous_country" and group in {"current address", "residential address", "adresse de residence", "wohnanschrift"} \
            and norm_question(label) in {"pays", "country", "land", "country region", "pays region"}:
        key = "country"
    if key:
        return question.model_copy(update={"reason": "fact", "fact_id": key})
    if choice:
        return question.model_copy(update={"reason": "decision", "category": choice})
    return question.model_copy(update={"reason": "unmapped"})


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


DECISIONS = {
    "ai_opt_in": "Autoriser l’assistance par IA", "ai_opt_out": "Refuser l’assistance par IA",
    "ai_review": "Assistance par IA : vérifier le choix proposé",
    "conflict": "Déclaration de conflit d’intérêts (par employeur)",
    "former_employer": "Emploi antérieur (par employeur)", "marketing": "Préférences marketing (par site)",
    "recruiting": "Préférences de recrutement (par site)",
    "ambiguous_country": "Pays ou indicatif : vérifier ce que le formulaire demande",
    "work_authorization_review": "Autorisation de travail ou parrainage : vérifier la question exacte",
}
TECHNICAL = {
    "credentials": "Mot de passe ou confirmation", "account": "Compte ou connexion", "cv_upload": "Dépôt du CV",
    "letter_upload": "Dépôt de la lettre de motivation", "photo_upload": "Dépôt de la photo",
    "file_upload": "Dépôt d’un fichier", "recheck": "Vérifier le champ ou le format proposé",
}


def review_groups(items: Iterable[QueueItem], recorded_fact_keys: Collection[str], answered_labels: Collection[str]):
    """Return IDs only, including paused prompts with an existing value that still need inspection."""
    decisions: dict[str, set[int]] = defaultdict(set)
    technical: dict[str, set[int]] = defaultdict(set)
    for item in items:
        for question in item.questions:
            key = fact_key(question)
            label = norm_question(question.label)
            blocker = question.category if question.reason == "technical" else technical_key(question.label, question.kind)
            if blocker:
                technical[blocker].add(item.app.id)
            elif key and (key in recorded_fact_keys or label in answered_labels):
                technical[f"recheck:{key}"].add(item.app.id)
            elif not key and (choice := decision_key(question.label)):
                decisions[choice].add(item.app.id)
            elif not key and not question.choice_binding and (label in _LABEL_KEYS or label in answered_labels
                    or question.kind in {"button", "link", "select", "radio", "combobox", "checkbox"}):
                technical["recheck"].add(item.app.id)

    def rows(groups):
        return sorted((MissingAnswer(key, tuple(sorted(ids))) for key, ids in groups.items()),
                      key=lambda group: (-group.application_count, group.fact_key))

    return rows(decisions), rows(technical)


def editable_custom(question: Question) -> bool:
    label = norm_question(question.label)
    return bool(label and question.reason not in {"fact", "decision", "technical"} and not question.choice_binding
                and label not in _LABEL_KEYS and not technical_key(question.label, question.kind)
                and not decision_key(question.label)
                and ((question.reason is None and bool(question.options)) or question.kind in {"text", "textarea", "email", "tel", "number", "date"}))
