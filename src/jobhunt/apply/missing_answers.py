"""Read-only projection of potentially reusable answers for the pilot."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Collection, Iterable
from dataclasses import dataclass
import re
from typing import TYPE_CHECKING

from jobhunt.apply.prompt_rules import DIAL_COUNTRY_NAMES, decision_key, device_type, norm_question, recruiter_ai_polarity, technical_key
from jobhunt.apply.types import FieldOption, FormField, Question

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
    "address": ("Adresse", "Adresse postale", "Adresse (numéro et rue)", "Street Name and Type", "Street address", "Address Line 1"),
    "address_line_2": ("Address Line 2", "Ligne d'adresse 2", "Complément d'adresse"),
    "department": ("Département", "Department", "Département de résidence", "State (département de résidence)"),
    "politically_exposed": ("Êtes-vous une personne politiquement exposée ?", "Personne politiquement exposée",
                            "Are you a politically exposed person?", "Politically exposed person"),
    "graduation_date": ("Date d'obtention du diplôme (prévue)", "Date prévue d'obtention du diplôme",
                        "Date prévisionnelle d'obtention du diplôme", "Expected graduation date",
                        "Anticipated graduation date", "When do you expect to graduate?"),
    "internship_agreement": ("L'école fournit une convention de stage",
                             "Votre école peut-elle fournir une convention de stage ?",
                             "Can your school provide an internship agreement?",
                             "Does your school provide an internship agreement?",
                             "Can your university provide an internship agreement?"),
    "first_name": ("Prénom", "Votre prénom", "First name", "Given name", "Vorname"),
    "last_name": ("Nom de famille", "Votre nom de famille", "Last name", "Family name", "Surname", "Nachname"),
    "full_name": ("Nom complet", "Full name", "Vollständiger Name"),
    "email": ("E-mail", "Email address", "E-mail address", "Adresse e-mail", "Courriel", "E-Mail-Adresse"),
    "phone": ("Téléphone", "Numéro de téléphone", "Phone", "Phone number", "Telephone number", "Telefonnummer"),
    "phone_calling_code": ("Indicatif de pays", "Indicatif téléphonique", "Indicatif du pays", "Country calling code", "Code de région/pays",
                           "Phone country code", "Telephone country code", "Phone number country", "Phone country", "Country of phone number", "Ländervorwahl", "Telefonländervorwahl"),
    "phone_device_type": ("Phone Device Type", "Phone Type", "Type d’appareil téléphonique", "Type de téléphone",
                          "Type de périphérique téléphonique", "Telefon-Gerätetyp", "Telefongerätetyp", "Telefontyp",
                          "Telefon Gerätetyp", "Telefon Gerätetyp (privat)"),
    "work_authorization": ("Décrivez votre autorisation de travail en France", "Describe your work authorization in France",
                           "Beschreiben Sie Ihre Arbeitserlaubnis in Frankreich"),
    "needs_visa_sponsorship": ("Do you need visa sponsorship?", "Do you require visa sponsorship?"),
}
_LABEL_KEYS = {norm_question(label): key for key, labels in ALIASES.items() for label in labels}
_LABEL_KEYS.update(country="country", pays="country")
YES_NO = {True: {"oui", "yes", "ja"}, False: {"non", "no", "nein"}}
IDENTITY = {"first_name", "last_name", "full_name", "email", "phone"}
POLICY = {"marketing", "recruiting", "consent"}
_NEW_KEYS = {"address", "address_line_2", "department", "phone_calling_code", "politically_exposed"}
_PROMOTED_DECISIONS = {"recruiter_ai", "former_employer", "conflict_of_interest", "relatives_at_employer", "country"}
_DEPARTMENTS = dict(line.split(" ", 1) for line in """01 Ain
02 Aisne
03 Allier
04 Alpes-de-Haute-Provence
05 Hautes-Alpes
06 Alpes-Maritimes
07 Ardèche
08 Ardennes
09 Ariège
10 Aube
11 Aude
12 Aveyron
13 Bouches-du-Rhône
14 Calvados
15 Cantal
16 Charente
17 Charente-Maritime
18 Cher
19 Corrèze
2A Corse-du-Sud
2B Haute-Corse
21 Côte-d’Or
22 Côtes-d’Armor
23 Creuse
24 Dordogne
25 Doubs
26 Drôme
27 Eure
28 Eure-et-Loir
29 Finistère
30 Gard
31 Haute-Garonne
32 Gers
33 Gironde
34 Hérault
35 Ille-et-Vilaine
36 Indre
37 Indre-et-Loire
38 Isère
39 Jura
40 Landes
41 Loir-et-Cher
42 Loire
43 Haute-Loire
44 Loire-Atlantique
45 Loiret
46 Lot
47 Lot-et-Garonne
48 Lozère
49 Maine-et-Loire
50 Manche
51 Marne
52 Haute-Marne
53 Mayenne
54 Meurthe-et-Moselle
55 Meuse
56 Morbihan
57 Moselle
58 Nièvre
59 Nord
60 Oise
61 Orne
62 Pas-de-Calais
63 Puy-de-Dôme
64 Pyrénées-Atlantiques
65 Hautes-Pyrénées
66 Pyrénées-Orientales
67 Bas-Rhin
68 Haut-Rhin
69 Rhône
70 Haute-Saône
71 Saône-et-Loire
72 Sarthe
73 Savoie
74 Haute-Savoie
75 Paris
76 Seine-Maritime
77 Seine-et-Marne
78 Yvelines
79 Deux-Sèvres
80 Somme
81 Tarn
82 Tarn-et-Garonne
83 Var
84 Vaucluse
85 Vendée
86 Vienne
87 Haute-Vienne
88 Vosges
89 Yonne
90 Territoire de Belfort
91 Essonne
92 Hauts-de-Seine
93 Seine-Saint-Denis
94 Val-de-Marne
95 Val-d’Oise
971 Guadeloupe
972 Martinique
973 Guyane
974 La Réunion
976 Mayotte""".splitlines())
_KINDS = {
    "city": {"text", "select", "combobox"},
    "postal_code": {"text", "number"},
    "country": {"text", "select", "combobox"},
    "address": {"text", "textarea"}, "address_line_2": {"text", "textarea"},
    "department": {"text", "select", "combobox"},
    "politically_exposed": {"text", "select", "combobox", "radio"},
    "conflict_of_interest": {"text", "select", "combobox", "radio"},
    "former_employer": {"text", "select", "combobox", "radio"},
    "relatives_at_employer": {"text", "select", "combobox", "radio"},
    "recruiter_ai": {"select", "radio", "checkbox"},
    "graduation_date": {"text", "date", "select", "combobox"},
    "internship_agreement": {"text", "select", "combobox", "radio"},
    "first_name": {"text"}, "last_name": {"text"}, "full_name": {"text"}, "email": {"text", "email"},
    "phone": {"text", "tel"}, "phone_calling_code": {"text", "tel", "select", "combobox"},
    "phone_device_type": {"select", "combobox", "radio"}, "work_authorization": {"text", "textarea"},
    "needs_visa_sponsorship": {"select", "combobox", "radio"},
}


def standing_key(label: str) -> str | None:
    normalized = norm_question(label)
    if recruiter_ai_polarity(label) is not None:
        return "recruiter_ai"
    if (decision_key(label) == "former_employer"
            and re.search(r"\b(?:us|this company|this employer|cet employeur|l employeur|nous|notre entreprise|notre groupe)\b", normalized)
            and not re.search(r"\b(?:never|not|jamais|depuis|since|last \d+|\d+ ans)\b", normalized)):
        return "former_employer"
    if decision_key(label) == "conflict" and re.search(r"\b(?:employe|salarie|employee|client|customer)\b", normalized) \
            and not re.search(r"\b(?:no|not|aucun|aucune|absence|declarez ne)\b", normalized):
        return "conflict_of_interest"
    if decision_key(label) == "relatives":  # owner rule: the candidate has no relative at the employer -> always "No"
        return "relatives_at_employer"
    return _LABEL_KEYS.get(normalized)


def fact_key(question: Question) -> str | None:
    """An exact label and a compatible control; no matching from an ATS ref or options."""
    inferred = standing_key(question.label)
    key = question.fact_id if question.reason == "fact" else inferred if (
        question.reason is None or (question.reason == "decision" and inferred in _PROMOTED_DECISIONS
                                    and question.category != "address_context")
        or (question.reason == "unmapped" and inferred in _NEW_KEYS and question.category is None)) else None
    return key if key in _KINDS and (question.required or key == "recruiter_ai") and question.kind in _KINDS[key] else None


def capture(field: FormField, label: str) -> Question:
    blocker = technical_key(label, field.kind, group=field.group, autocomplete=field.autocomplete)
    question = Question(ref=field.ref, label=label, kind=field.kind,
                        options=[] if blocker else [option.label for option in field.options], required=field.required)
    if blocker:
        return question.model_copy(update={"reason": "technical", "category": blocker})
    group = norm_question(field.group)
    key = fact_key(question.model_copy(update={"required": True})) if field.label else None
    if standing_key(label) in IDENTITY and field.label:
        key = standing_key(label)
    if key in {"city", "postal_code", "country", "address", "address_line_2", "department"} and any(term in group for term in (
            "birth", "naissance", "geburt", "previous", "ancien", "school", "ecole", "preferred", "souhaite", "employer", "arbeitgeber")):
        return question.model_copy(update={"reason": "unmapped", "category": "address_context"})
    choice = decision_key(label) or decision_key(field.group)
    if choice == "ambiguous_country" and group in {"current address", "residential address", "adresse de residence", "wohnanschrift"} \
            and norm_question(label) in {"pays", "country", "land", "country region", "pays region"}:
        key = "country"
    if key:
        return question.model_copy(update={"reason": "fact", "fact_id": key})
    if choice:
        return question.model_copy(update={"reason": "decision", "category": choice})
    return question.model_copy(update={"reason": "unmapped"})


def exact_option(key: str, value: str, options: list[str], label: str) -> int | None:
    """Map only unique equivalent labels. The caller validates the actual control and option identity."""
    wanted = norm_question(value)
    equivalent = {wanted}
    if key == "country" and wanted in {"france", "fr", "fra", "frankreich"}:
        equivalent = {"france", "fr", "fra", "frankreich"}
    if key == "department":
        code = next((code for code, name in _DEPARTMENTS.items() if wanted in {norm_question(code), norm_question(name)}), None)
        if code:
            name = norm_question(_DEPARTMENTS[code])
            equivalent = {code.lower(), name, f"{code.lower()} {name}", f"{name} {code.lower()}"}
            if code.startswith("0"):
                equivalent.add(code[1:])
    if key in {"politically_exposed", "internship_agreement", "recruiter_ai", "conflict_of_interest", "former_employer",
               "relatives_at_employer", "needs_visa_sponsorship"}:
        polarity = next((yes for yes, words in YES_NO.items() if wanted in words), None)
        if polarity is None:
            return None
        if key == "recruiter_ai" and recruiter_ai_polarity(label) is False:
            polarity = not polarity
        equivalent = YES_NO[polarity]
    matches = [index for index, option in enumerate(options) if (
        device_type(option) == value if key == "phone_device_type" else
        (option.strip() == value or re.findall(r"\+[1-9][0-9]*", option) == [value]
         or (not re.search(r"\+[1-9]", option) and norm_question(option) in DIAL_COUNTRY_NAMES.get(value, ()))) if key == "phone_calling_code" else
        norm_question(option) in equivalent)]
    if key == "recruiter_ai" and not matches:
        matches = [index for index, option in enumerate(options)
                   if recruiter_ai_polarity(option) is (wanted in YES_NO[True])
                   or norm_question(option) in ({"j accepte", "i accept", "i agree"} if wanted in YES_NO[True]
                                               else {"je refuse", "i decline", "i do not agree"})]
    return matches[0] if len(matches) == 1 else None


def policy_key(question: Question) -> str | None:
    from jobhunt.apply.autonomous import blocked_consent, eeo_field

    if standing_key(question.label) == "recruiter_ai":
        return None
    category = decision_key(question.label) or question.category
    if category in POLICY or blocked_consent(norm_question(question.label)) or eeo_field(norm_question(question.label)):
        return category or "policy"
    return None


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
    "consent": "Confidentialité et conditions : répondre sur le site",
    "ambiguous_country": "Pays ou indicatif : vérifier ce que le formulaire demande",
    "work_authorization_review": "Autorisation de travail ou parrainage : vérifier la question exacte",
}
TECHNICAL = {
    "credentials": "Mot de passe ou code de vérification", "account": "Compte ou connexion", "cv_upload": "Dépôt du CV",
    "letter_upload": "Dépôt de la lettre de motivation", "photo_upload": "Dépôt de la photo",
    "file_upload": "Dépôt d’un fichier", "recheck": "Vérifier le champ ou le format proposé",
    "application_material": "Lettre de motivation ou message",
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
                and not standing_key(question.label) and not policy_key(question) and not technical_key(question.label, question.kind)
                and not decision_key(question.label)
                and ((question.reason is None and bool(question.options)) or question.kind in {"text", "textarea", "email", "tel", "number", "date"}))


@dataclass(frozen=True)
class Decision:
    item: QueueItem
    question: Question
    options: tuple[tuple[int, str], ...] = ()

    @property
    def saved(self) -> bool:
        from jobhunt.apply import scoped_choices

        return bool(self.options and self.question.choice_binding in scoped_choices.load(
            self.item.app.profile_id, self.item.app.id, self.item.app.current_rev, self.item.origin_sha))


@dataclass(frozen=True)
class AnswerProjection:
    groups: list[MissingAnswer]
    decisions: list[Decision]
    technical: list[MissingAnswer]
    pauses: list[QueueItem]


def project_queue(profile, items: Iterable[QueueItem], answers: dict) -> AnswerProjection:
    """Project only current input pauses, using the same exact mappings as form filling."""
    from jobhunt.apply import facts, scoped_choices
    from jobhunt.cv.master import MasterError, load_master

    try:
        master = load_master(profile.id)
    except MasterError:
        master = None
    values = facts.candidate_answers(profile, master if master and master.is_confirmed else None, answers)
    custom = {norm_question(entry["question"]) for entry in (answers.get("custom") or [])
              if isinstance(entry, dict) and entry.get("question") and entry.get("answer")}
    groups: dict[str, set[int]] = defaultdict(set)
    technical: dict[str, set[int]] = defaultdict(set)
    decisions: list[Decision] = []
    seen = set()
    pauses = list(input_pauses(profile.id, items))
    for item in pauses:
        for question in item.questions:
            label = norm_question(question.label)
            if not label:
                continue
            blocker = technical_key(question.label, question.kind) or (
                question.category or "recheck" if question.reason == "technical" else None)
            key = fact_key(question)
            path = facts.employer_path(item.app.company) if key == "former_employer" else key
            if blocker:
                technical[blocker].add(item.app.id)
            elif policy_key(question):
                continue
            elif key in IDENTITY or standing_key(question.label) in IDENTITY:
                if not values.get(key or standing_key(question.label)):
                    technical["recheck"].add(item.app.id)
            elif path:
                value = facts.get_answer(answers, path) if key == "former_employer" else values.get(path)
                if not value:
                    groups[path].add(item.app.id)
                elif question.kind in {"select", "radio", "combobox"} and exact_option(key, value, question.options, question.label) is None:
                    technical["recheck"].add(item.app.id)
                elif question.kind == "date" and key == "graduation_date" and not facts.from_french_date(value):
                    groups[path].add(item.app.id)
            elif question.kind in {"select", "radio", "combobox"}:
                options = tuple(scoped_choices.labels(question))
                if not options or not scoped_choices.run_current(item.app, item.last_run, item.origin_sha) or item.outstanding:
                    technical["recheck"].add(item.app.id)
                elif (token := (item.app.id, question.choice_binding)) not in seen:
                    seen.add(token)
                    decisions.append(Decision(item, question, options))
            elif editable_custom(question) and label not in custom:
                if label not in seen:
                    seen.add(label)
                    decisions.append(Decision(item, question))
            elif label not in custom:
                technical["recheck"].add(item.app.id)

    def rows(mapping):
        return sorted((MissingAnswer(key, tuple(sorted(ids))) for key, ids in mapping.items()),
                      key=lambda group: (-group.application_count, group.fact_key))

    return AnswerProjection(rows(groups), decisions, rows(technical), pauses)
