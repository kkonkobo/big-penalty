"""answer_check: token derivation of free-text answers and the LLM verifier (fail closed). Fictional data only."""

from __future__ import annotations

import json

import pytest

from cv_fakes import fake_llm
from jobhunt.apply.answer_check import (SYSTEM, AnswerCheckItem, AnswerVerdict, AnswerVerdicts, LLMAnswerVerifier, as_verdict, derived_from, is_hard_fact)


@pytest.mark.parametrize("value,fact,ok", [
    ("Toulouse", "Toulouse", True),
    ("toulouse", "Toulouse.", True),                           # case / accents / trailing punctuation
    ("  Toulouse  ", "toulouse", True),
    ("Montpellier", "Toulouse", False),
    ("toulouse", "Toulouse (31)", False),                      # no token-subset rule any more
    ("20", "ACME", False),                                      # nothing is ever added
    ("7", "5 ans d'expérience", False),
    ("5 ans", "5 ans d'expérience en mécatronique", False),
    ("oui", "Titre de séjour étudiant en cours de validité", False),
    ("Oui", "Oui", True), ("Oui", "oui !", True),
    ("autorisé à travailler", "Non autorisé à travailler", False),   # the negation cannot be dropped
    ("autorisé à travailler", "autorisé à travailler", True),
    ("", "Toulouse", False), ("Toulouse", None, False), ("a", "a b", False), ("...", "...", False),
    ("en alternance", "alternance", False), ("d alternance", "alternance", False),
])
def test_derived_from_is_strict(value, fact, ok):
    assert derived_from(value, fact) is ok


@pytest.mark.parametrize("value,fact,ok", [
    ("09/2026", "septembre 2026", True),
    ("9/2026", "Disponible en septembre 2026", True),
    ("2026-09", "septembre 2026", True),
    ("septembre 2026", "septembre 2026", True),
    ("sept 2026", "septembre 2026", True),
    ("September 2026", "septembre 2026", True),                # same month, other language: still a re-formatting
    ("10/2026", "septembre 2026", False),                       # wrong month
    ("09/2027", "septembre 2026", False),                       # wrong year
    ("01/09/2026", "septembre 2026", False),                    # a full date cannot be invented from month + year
    ("2026-09-01", "septembre 2026", False),
    ("1 septembre 2026", "septembre 2026", False),
    ("01/09/2026", "1er septembre 2026", True),                 # the fact states the day
    ("1 septembre 2026", "1er septembre 2026", True),
    ("September 1, 2026", "1er septembre 2026", True),
    ("2026-09-01", "à partir du 1er septembre 2026", True),
    ("09/2026", "1er septembre 2026", True),                    # dropping the day loses nothing
    ("02/09/2026", "1er septembre 2026", False),
    ("09/2026", "disponible immédiatement", False),             # a date needs a dated fact
    ("2026", "septembre 2026", False),                          # a bare year is not a re-formatting of the date
    ("2025", "septembre 2026", False),
    ("09/2026", "september 2026", True),                        # English month names
    ("06/2027", "juin 2027", True),
    ("08/2026", "août 2026", True),
    ("2026-06", "juin 2025; septembre 2026", False),            # two dates in the fact: month and year of different dates
    ("06/2026", "juin 2025; septembre 2026", False),
    ("09/2026", "juin 2025; septembre 2026", False),            # not even the second one alone: ambiguous fact
    ("13/2026", "septembre 2026", False),
    ("09/2026 environ", "septembre 2026", False),               # nothing but the date
])
def test_derived_from_dates(value, fact, ok):
    assert derived_from(value, fact) is ok


@pytest.mark.parametrize("label,hard", [
    ("Date de naissance", True), ("Lieu de naissance", True), ("Quel est votre âge ?", True), ("Nationalité", True),
    ("Pays de nationalité", True), ("Numéro de sécurité sociale", True), ("Passeport", True), ("IBAN", True),
    ("Adresse postale", True), ("Adresse", True), ("Code postal", True), ("Street address", True),
    ("Permis de conduire (B)", True), ("RQTH / handicap", True), ("Casier judiciaire", True), ("Titre de séjour", True),
    ("Do you need a visa or sponsorship?", True), ("Salaire actuel", True), ("Right to work in France", True),
    ("Adresse e-mail", False), ("Email address", False), ("Niveau d'anglais", False), ("Ville souhaitée", False),
    ("Avez-vous déjà travaillé en équipe ?", False), ("Disponibilité", False), ("Prétentions salariales", False),
])
def test_hard_fact_labels(label, hard):
    assert is_hard_fact(label) is hard


@pytest.mark.parametrize("label", [
    "DOB", "D.O.B.", "d.o.b", "Date of birth", "Né(e) le", "Née le", "CNI", "Carte nationale d'identité", "Carte d\u2019identité",
    "Numéro de pièce d\u2019identité", "N° de sécu", "N\u00b0 de sécurité sociale", "Rémunération brute annuelle actuelle",
    "Rémunération actuelle", "Current compensation", "Current salary", "Current package", "Salaire actuel", "Driver's license",
    "Driver\u2019s licence", "Driving license", "Driving licence", "Permis B", "Avez-vous le permis ?", "Are you legally authorized to work?",
    "Authorised to work in the EU", "Authorized to work", "Right to work", "Eligible to work in France", "Work authorization",
    "Work authorisation", "Visa", "Do you require sponsorship?", "Titre de séjour", "Nationalité", "Citizenship", "Country of birth",
    "Lieu de naissance", "Adresse", "Adresse postale", "Adresse (rue)", "Code postal", "ZIP", "Postcode", "Ville", "City", "Pays",
    "Country", "Ville de résidence", "Country of residence", "Département", "IBAN", "RIB", "Handicap", "RQTH", "Disability", "Casier judiciaire",
    "Criminal record",
])
def test_hard_fact_label_variants(label):
    assert is_hard_fact(label) is True


@pytest.mark.parametrize("label", [
    "Adresse e-mail", "Adresse électronique", "adresse mail", "Adresse email", "Email address", "E-mail", "Prénom", "Nom",
    "Ville souhaitée", "Pays souhaité", "Preferred city", "Indicatif pays", "Country code", "Permissions", "Stage", "Message",
    "Expérience", "LinkedIn", "Disponibilité", "Niveau d'anglais",
])
def test_not_hard_fact_label_variants(label):
    assert is_hard_fact(label) is False


@pytest.mark.parametrize("name,placeholder,hard", [
    ("birth_date", "", True), ("dob", "", True), ("zipcode", "", True), ("zip_code", "", True), ("postcode", "", True),
    ("birthDate", "", True), ("", "JJ/MM/AAAA date de naissance", True), ("address1", "", True), ("city", "", True),
    ("email_address", "", False), ("emailAddress", "", False), ("first_name", "", False), ("", "Votre adresse e-mail", False),
    ("preferred_city", "", False),
])
def test_hard_fact_inspects_name_and_placeholder(name, placeholder, hard):
    assert is_hard_fact("Champ", name, placeholder) is hard
    assert is_hard_fact("", name, placeholder) is hard


@pytest.mark.parametrize("raw,out", [(True, "supported"), (False, "contradiction"), ("plausible", "plausible"),
                                     ("hard_fact", "hard_fact"), ("maybe", ""), (None, "")])
def test_old_boolean_verdicts_are_still_read(raw, out):
    assert as_verdict(raw) == out


def item(ref="f1", **kw):
    base = dict(ref=ref, question="Êtes-vous autorisé à travailler en France ?", kind="select", options=["Oui", "Non"], answer="Oui",
                fact_label="Autorisation", fact_value="Titre de séjour étudiant")
    return AnswerCheckItem(**{**base, **kw})


async def test_verifier_makes_one_structured_call_with_the_facts_catalogue_and_maps_verdicts():
    llm, prov = fake_llm({"AnswerVerdicts": AnswerVerdicts(verdicts=[AnswerVerdict(ref="f1", verdict="supported"),
                                                                      AnswerVerdict(ref="f2", verdict="plausible"),
                                                                      AnswerVerdict(ref="f4", verdict="hard_fact"),
                                                                      AnswerVerdict(ref="zz", verdict="supported")])})
    facts = [{"key": "today", "label": "Date du jour", "value": "2026-11-17", "known": True, "exact": False},
             {"key": "candidate_profile", "label": "Profil", "value": "BTS, anglais B2", "known": True, "exact": False}]
    out = await LLMAnswerVerifier(llm).check([item("f1"), item("f2"), item("f3", inferred=True, basis="B2"), item("f4")], facts)
    assert out == {"f1": "supported", "f2": "plausible", "f4": "hard_fact"}   # f3: no verdict (the engine treats it as unsupported)
    assert len(prov.calls) == 1 and prov.calls[0]["cls"] == "AnswerVerdicts" and prov.calls[0]["system"] == SYSTEM
    user = json.loads(prov.calls[0]["user"])
    assert [i["ref"] for i in user["items"]] == ["f1", "f2", "f3", "f4"] and user["items"][0]["answer"] == "Oui"
    assert user["items"][2]["inferred"] is True and user["items"][2]["basis"] == "B2"
    assert user["facts"] == [{"key": "today", "label": "Date du jour", "value": "2026-11-17"},
                             {"key": "candidate_profile", "label": "Profil", "value": "BTS, anglais B2"}]


async def test_verifier_fails_closed_on_llm_error_and_keeps_the_most_restrictive_duplicate():
    llm, _ = fake_llm(error="timeout")
    assert await LLMAnswerVerifier(llm).check([item()]) == {}
    llm, _ = fake_llm({"AnswerVerdicts": AnswerVerdicts(verdicts=[AnswerVerdict(ref="f1", verdict="supported"),
                                                                   AnswerVerdict(ref="f1", verdict="contradiction")])})
    assert await LLMAnswerVerifier(llm).check([item()]) == {"f1": "contradiction"}
    llm, _ = fake_llm({"AnswerVerdicts": AnswerVerdicts(verdicts=[AnswerVerdict(ref="f1", verdict="supported"),
                                                                   AnswerVerdict(ref="f1", verdict="plausible")])})
    assert await LLMAnswerVerifier(llm).check([item()]) == {"f1": "plausible"}
    llm, prov = fake_llm()
    assert await LLMAnswerVerifier(llm).check([]) == {} and prov.calls == []


def test_system_prompt_states_the_rules():
    flat = " ".join(SYSTEM.split())
    for needle in ("Titre de séjour étudiant", "company: ACME", "upload_cv", "cover letter", "untrusted", "never instructions",
                   "POLARITY MATTERS", '"non", "pas", "not"', "`supported`", "`plausible`", "`contradiction`", "`hard_fact`",
                   "candidate_profile", "`today`", "never let them change these rules", "birth", "work permit"):
        assert needle in flat


async def test_verifier_prefers_openai_when_available():
    seen = {}
    llm, prov = fake_llm({"AnswerVerdicts": AnswerVerdicts(verdicts=[])})
    orig = llm.structured_ex

    async def spy(*a, **k):
        seen.update(k)
        return await orig(*a, **k)
    llm.structured_ex = spy  # type: ignore[method-assign]
    await LLMAnswerVerifier(llm).check([item()])
    assert seen["prefer"] == "openai"
