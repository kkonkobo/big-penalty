"""Known-facts catalogue: sources, contract-dependent answers, secrets, answers.yaml round trip."""

from __future__ import annotations

import pytest
import yaml

from jobhunt.apply import facts as F
from jobhunt.apply.errors import ApplyError
from jobhunt.config import get_settings
from jobhunt.cv.master import Education, Master
from jobhunt.models import ContractType

from apply_cv_helpers import make_job
from cv_fakes import sample_master

ANSWERS = {
    "provisional": True,
    "work_authorization": "Titre de séjour étudiant en cours de validité",
    "stage": {"available_from": "janvier 2027", "duration": "3 à 4 mois"},
    "alternance": {"start": "septembre 2027", "duration": "12 mois", "rhythm": "selon le calendrier de l'école"},
    "driving_licence": None,
    "salary_expectation": {"alternance": "selon grille légale", "stage": "gratification légale"},
    "mobility": "Toute la France",
    "languages": "Français courant ; anglais B2",
    "custom": [{"question": "Avez-vous le permis B ?", "answer": "Non"}],
}


def sheet(profile, job, answers=None, master=None, **kw):
    return F.build_facts(profile, master or sample_master(), ANSWERS if answers is None else answers, job, **kw)


def val(s, key):
    return s.get(key).value


def test_identity_facts_are_exact_and_name_is_split(profile):
    m = sample_master()
    s = sheet(profile, make_job(profile), master=m)
    assert val(s, "full_name") == "Camille Durand" and val(s, "first_name") == "Camille" and val(s, "last_name") == "Durand"
    assert val(s, "email") == "camille@example.test" and val(s, "phone") == "+33 6 00 00 00 00"
    assert all(s.get(k).exact for k in ("full_name", "first_name", "last_name", "email", "phone", "linkedin", "account_email"))
    assert s.get("phone").variants == ["+33600000000", "0600000000", "06 00 00 00 00"]
    # typed as a full URL (forms validate it); the shorter spellings are accepted variants
    assert val(s, "linkedin") == "https://www.linkedin.com/in/camille-example"
    assert s.get("linkedin").variants == ["linkedin.com/in/camille-example", "www.linkedin.com/in/camille-example",
                                          "https://linkedin.com/in/camille-example"]


def test_name_split_multiple_tokens_and_answers_override(profile):
    m = sample_master()
    m.identity.name = "Anne Marie de la Tour"
    s = sheet(profile, make_job(profile), master=m)
    assert (val(s, "first_name"), val(s, "last_name")) == ("Anne", "Marie de la Tour")
    s = sheet(profile, make_job(profile, title="Autre"), answers={"first_name": "Anne-Marie", "last_name": "de la Tour"}, master=m)
    assert (val(s, "first_name"), val(s, "last_name")) == ("Anne-Marie", "de la Tour")


def test_email_phone_fall_back_to_the_profile(profile):
    profile = profile.model_copy(update={"email": "lea@example.test", "phone": "06 12 34 56 78"})
    m = sample_master()
    m.identity.email = m.identity.phone = m.identity.linkedin = None
    s = sheet(profile, make_job(profile), master=m)
    assert val(s, "email") == "lea@example.test" and val(s, "account_email") == "lea@example.test"
    assert val(s, "phone") == "06 12 34 56 78" and s.get("phone").variants == ["+33 6 12 34 56 78", "+33612345678", "0612345678"]
    assert val(s, "linkedin") is None and s.get("linkedin").variants == []


@pytest.mark.parametrize("raw", ["+33 6 12 34 56 78", "0033612345678", "06.12.34.56.78", "0612345678", "+33612345678"])
def test_phone_variants_cover_the_french_formats(raw):
    got = set(F.phone_variants(raw)) | {raw}
    assert {"+33 6 12 34 56 78", "+33612345678", "0612345678", "06 12 34 56 78"} <= got


def test_phone_variants_empty_for_foreign_numbers():
    assert F.phone_variants("+1 415 555 0100") == [] and F.phone_variants(None) == []


def test_account_email_argument_and_password_placeholder(profile):
    s = sheet(profile, make_job(profile), account_email="ats@example.test")
    assert val(s, "account_email") == "ats@example.test" and s.get("account_email").exact
    pw = s.get("account_password")
    assert pw.secret and pw.exact and pw.value == ""


def test_answers_facts_and_unknowns(profile):
    s = sheet(profile, make_job(profile))
    assert val(s, "work_authorization").startswith("Titre de séjour") and val(s, "mobility") == "Toute la France"
    assert val(s, "languages_summary") == "Français courant ; anglais B2"
    assert s.get("driving_licence") is not None and val(s, "driving_licence") is None and not s.known("driving_licence")
    for k in ("civility", "gender", "birth_date", "nationality", "address", "postal_code", "city", "country", "disability"):
        assert s.get(k) is not None and val(s, k) is None
    s = sheet(profile, make_job(profile, title="Autre"), answers={"civility": "Mme", "birth_date": "01/02/2003", "driving_licence": "Permis B"})
    assert (val(s, "civility"), val(s, "birth_date"), val(s, "driving_licence")) == ("Mme", "01/02/2003", "Permis B")


def test_languages_school_degree_and_current_title(profile):
    s = sheet(profile, make_job(profile))
    assert val(s, "language_1") == "Français : Courant" and val(s, "language_2") == "Anglais : B2"
    assert val(s, "school") == "École Exemple" and val(s, "degree") == "Cycle ingénieur - Robotique"
    assert val(s, "education_level") == "Cycle ingénieur - Robotique, École Exemple (2024–en cours)"
    assert "année" not in val(s, "education_level")  # no invented year of study
    assert val(s, "current_title") == "Élève ingénieure"


def test_most_recent_education_is_chosen_whatever_the_order(profile):
    m = sample_master()
    m.education = [Education(title="Licence", school="Ancienne", dates="2021–2024"),
                   Education(title="Master", school="Récente", dates="2024–2026")]
    s = sheet(profile, make_job(profile), master=m)
    assert val(s, "school") == "Récente"
    m.education = [Education(title="Licence", school="A", dates="2021–2024"), Education(title="Cycle", school="B", dates="2024–en cours")]
    assert val(sheet(profile, make_job(profile, title="Z"), master=m), "school") == "B"
    m.education = []
    s = sheet(profile, make_job(profile, title="Y"), master=m)
    assert val(s, "school") is None and val(s, "degree") is None and val(s, "education_level") is None


def test_stage_job_exposes_only_the_stage_answers(profile):
    s = sheet(profile, make_job(profile, contract=ContractType.STAGE))
    assert val(s, "contract_sought") == "Stage" and val(s, "available_from") == "janvier 2027" and val(s, "duration") == "3 à 4 mois"
    assert val(s, "salary_expectation") == "gratification légale" and s.get("rhythm") is None and s.get("alternance_start") is None


def test_alternance_job_exposes_only_the_alternance_answers(profile):
    s = sheet(profile, make_job(profile, contract=ContractType.ALTERNANCE))
    assert val(s, "contract_sought") == "Alternance" and val(s, "available_from") == "septembre 2027" and val(s, "duration") == "12 mois"
    assert val(s, "rhythm") == "selon le calendrier de l'école" and val(s, "salary_expectation") == "selon grille légale"


def test_other_contract_exposes_both_sets_with_prefixed_keys(profile):
    s = sheet(profile, make_job(profile, contract=ContractType.UNKNOWN))
    assert s.get("contract_sought") is None and s.get("available_from") is None
    assert val(s, "stage_contract_sought") == "Stage" and val(s, "stage_available_from") == "janvier 2027"
    assert val(s, "alternance_contract_sought") == "Alternance" and val(s, "alternance_available_from") == "septembre 2027"
    assert val(s, "alternance_rhythm") and val(s, "stage_salary_expectation") == "gratification légale"


def test_missing_answers_file_values_are_unknown(profile):
    s = sheet(profile, make_job(profile), answers={})
    assert not s.known("available_from") and not s.known("work_authorization") and s.known("email")


def test_job_facts_and_consents(profile):
    s = sheet(profile, make_job(profile, source="linkedin"))
    assert val(s, "job_source") == "LinkedIn" and val(s, "job_title").startswith("Stage ingénieur") and val(s, "company") == "Exemple SAS"
    assert s.get("consent_required") is None and val(s, "consent_optional") == "non"
    assert "refuser" in s.get("consent_optional").label
    no_sighting = make_job(profile, title="Sans source")
    no_sighting.sightings = []
    assert val(sheet(profile, no_sighting), "job_source") is None


def test_custom_answers_become_facts(profile):
    s = sheet(profile, make_job(profile))
    assert s.get("custom_1").label == "Avez-vous le permis B ?" and val(s, "custom_1") == "Non"


def test_facts_for_llm_never_contains_secrets(profile):
    s = sheet(profile, make_job(profile))
    s.get("account_password").value = "Sup3r-Secret_pw"  # even if a caller mistakenly filled it
    out = F.facts_for_llm(s)
    assert "Sup3r-Secret_pw" not in repr(out)
    pw = next(d for d in out if d["key"] == "account_password")
    assert pw["value"] == "(mot de passe géré par le système)"
    by = {d["key"]: d for d in out}
    assert by["email"] == {"key": "email", "label": "Adresse e-mail", "value": "camille@example.test", "known": True}
    assert by["civility"]["known"] is False and by["civility"]["value"] is None


# ------------------------------------------------------------------ answers.yaml
def test_load_answers_missing_and_invalid(profile):
    assert F.load_answers(profile.id) == {}
    p = F.answers_path(profile.id)
    assert p == get_settings().profiles_dir / str(profile.id) / "answers.yaml"
    p.parent.mkdir(parents=True)
    p.write_text("- une liste", encoding="utf-8")
    with pytest.raises(ApplyError):
        F.load_answers(profile.id)
    p.write_text("a: [unclosed", encoding="utf-8")
    with pytest.raises(ApplyError):
        F.load_answers(profile.id)


def test_save_custom_answers_round_trip_keeps_other_keys(profile):
    p = F.answers_path(profile.id)
    p.parent.mkdir(parents=True)
    p.write_text(yaml.safe_dump(ANSWERS, allow_unicode=True, sort_keys=False), encoding="utf-8")
    F.save_custom_answers(profile.id, [("Quelle est votre prétention ?", "Gratification légale"), ("  ", "ignoré"), ("Vide ?", "")])
    F.save_custom_answers(profile.id, [("avez-vous le PERMIS B", "Oui"), ("Autre question", "Réponse é")])
    got = F.load_answers(profile.id)
    assert {k: v for k, v in got.items() if k != "custom"} == {k: v for k, v in ANSWERS.items() if k != "custom"}
    assert got["custom"] == [{"question": "Avez-vous le permis B ?", "answer": "Oui"},
                             {"question": "Quelle est votre prétention ?", "answer": "Gratification légale"},
                             {"question": "Autre question", "answer": "Réponse é"}]
    assert not list(p.parent.glob("*.tmp"))  # atomic write leaves no temp file
    assert "é" in p.read_text(encoding="utf-8")  # not escaped


def test_save_custom_answers_creates_the_file(profile):
    F.save_custom_answers(profile.id, [("Q ?", "R")])
    assert F.load_answers(profile.id) == {"custom": [{"question": "Q ?", "answer": "R"}]}


# ------------------------------------------------------------------ Fact.label_re: identity facts only fit matching fields
def _fits(key: str, text: str) -> bool:
    """What the engine does: re.search, IGNORECASE, on the accent-stripped lowercase label + name + placeholder."""
    import re
    import unicodedata

    plain = "".join(c for c in unicodedata.normalize("NFKD", text.lower()) if not unicodedata.combining(c))
    return bool(re.search(F.LABEL_RE[key], plain, re.I))


NAME_TABLE = {  # label -> the name facts that may fill it
    "Prénom": {"first_name"}, "Nom": {"last_name"}, "Nom de famille": {"last_name"}, "Nom complet": {"full_name"},
    "Nom et prénom": {"full_name"}, "Prénom et nom": {"full_name"}, "First name": {"first_name"}, "Last name": {"last_name"},
    "Full name": {"full_name"}, "Given name": {"first_name"}, "Family name": {"last_name"}, "Surname": {"last_name"},
    "Nom d'usage": set(), "Nom de naissance": set(), "Nom de jeune fille": set(), "Birth name": set(), "Maiden name": set(),
    "Your name": {"full_name"}, "Votre nom": {"full_name"}, "Name": {"full_name"}, "Last name / Nom": {"last_name"},
    "Prénom(s)": {"first_name"}, "Supervisor name": set(), "Nom du superviseur": set(), "Nom de l'école": set(),
    "Name of reference": set(), "Nom du recruteur": set(), "Nom du conjoint": set(),
    "Nom d'utilisateur": set(), "Nom de l'entreprise": set(), "Nom du parrain": set(), "Prénom du parrain": set(),
    "Nom de la société": set(), "Company name": set(), "Username": set(), "Referrer name": set(), "Nom du contact d'urgence": set(),
    "Nom du manager": set(), "Nom du tuteur": set(), "Name of your referee": set(), "Mentor name": set(), "Nom du garant": set(),
    "Witness name": set(), "Nom du témoin": set(), "Nom d'un proche": set(), "Sponsor name": set(), "Guardian name": set(), "Ville": set(), "Code postal": set(), "File name": set(),
}


@pytest.mark.parametrize("label, expected", sorted(NAME_TABLE.items()))
def test_name_facts_fit_only_name_fields(label, expected):
    assert {k for k in ("first_name", "last_name", "full_name") if _fits(k, label)} == expected


@pytest.mark.parametrize("key, yes, no", [
    ("email", ["E-mail", "Adresse e-mail", "Courriel", "Email address", "Mail professionnel"],
     ["E-mail du parrain", "Email de la personne qui vous a recommandé", "E-mail de l'entreprise", "Contact d'urgence : e-mail", "Ville"]),
    ("email", ["E-mail professionnel ou personnel", "Adresse e-mail"],
     ["Supervisor Email", "E-mail de l'école", "E-mail du responsable", "Reference email", "E-mail du recruteur", "School email"]),
    ("phone", ["Numéro de téléphone portable", "Tél."],
     ["Reference Phone", "Téléphone de la personne à contacter", "Téléphone du superviseur", "Phone of your manager", "Téléphone de l'école"]),
    ("linkedin", ["URL LinkedIn"], ["LinkedIn du superviseur", "LinkedIn du recruteur"]),
    ("email", ["Email", "Adresse e-mail"],
     ["Email of your referee", "Email of your mentor", "E-mail du garant", "Witness email", "E-mail du témoin", "Sponsor email",
      "Guardian email", "E-mail du tuteur légal", "Next of kin email", "E-mail d'un proche"]),
    ("phone", ["Téléphone", "Phone"],
     ["Witness phone", "Téléphone du garant", "Referee phone", "Mentor phone number", "Téléphone du témoin", "Phone of next of kin",
      "Téléphone d'un proche", "Sponsor phone", "Guardian phone"]),
    ("linkedin", ["LinkedIn"], ["LinkedIn of your mentor", "LinkedIn du garant", "Referee LinkedIn"]),
    ("account_email", ["Email", "Courriel"], ["Referral email", "Mail du tuteur"]),
    ("phone", ["Téléphone", "Tel.", "Mobile", "Phone number", "Numéro de portable"],
     ["Téléphone du parrain", "Téléphone de l'entreprise", "Contact d'urgence (téléphone)", "Télécharger votre CV", "Ville"]),
    ("linkedin", ["Profil LinkedIn", "LinkedIn URL"], ["LinkedIn du parrain", "Page LinkedIn de l'entreprise"]),
    ("account_password", ["Mot de passe", "Password", "Confirmer le mot de passe", "Passwort", "MDP"],
     ["Ville", "E-mail", "Nom"]),
])
def test_other_identity_facts_label_regexes(key, yes, no):
    assert all(_fits(key, t) for t in yes), [t for t in yes if not _fits(key, t)]
    assert not any(_fits(key, t) for t in no), [t for t in no if _fits(key, t)]


def test_field_name_and_placeholder_count_too():
    assert _fits("last_name", "Nom lastName Dupont")
    assert _fits("email", "Votre adresse input_mail vous@exemple.fr")
    assert not _fits("last_name", "Ville city_name Toulouse")


def test_the_exact_identity_facts_carry_their_label_re(profile):
    s = sheet(profile, make_job(profile))
    for key in ("first_name", "last_name", "full_name", "email", "account_email", "phone", "linkedin", "account_password"):
        assert s.get(key).label_re == F.LABEL_RE[key] and s.get(key).label_re, key
    assert s.get("city").label_re is None and s.get("job_title").label_re is None


# ------------------------------------------------------------------ trusted hosts, profile digest, today
def test_trusted_hosts_round_trip_validation_and_other_keys_kept(profile):
    F.save_custom_answers(profile.id, [("Permis ?", "Oui")])
    assert F.trusted_hosts(profile.id) == []
    assert F.add_trusted_host(profile.id, " Jobs.Exemple.FR. ") == "jobs.exemple.fr"
    assert F.add_trusted_host(profile.id, "jobs.exemple.fr") == "jobs.exemple.fr"  # no duplicate
    F.add_trusted_host(profile.id, "recrutement.autre.test")
    assert F.trusted_hosts(profile.id) == ["jobs.exemple.fr", "recrutement.autre.test"]
    assert F.load_answers(profile.id)["custom"][0]["answer"] == "Oui"
    assert not F.answers_path(profile.id).with_name("answers.yaml.tmp").exists()


@pytest.mark.parametrize("bad", ["", "localhost", "127.0.0.1", "1.2.3.4", "https://jobs.exemple.fr", "jobs.exemple.fr/x", "a b.fr",
                                 "jobs.exemple.fr:8080", "-a.fr", "a..fr", "é.fr", "x@y.fr", "a.b"])
def test_trusted_host_must_be_a_plain_hostname(profile, bad):
    with pytest.raises(ApplyError):
        F.add_trusted_host(profile.id, bad)
    assert F.trusted_hosts(profile.id) == []


def test_invalid_entries_in_the_file_are_ignored(profile):
    p = F.answers_path(profile.id)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("trusted_hosts: [Jobs.Exemple.FR, 'http://x', 12, 'localhost']\n", encoding="utf-8")
    assert F.trusted_hosts(profile.id) == ["jobs.exemple.fr"]


def test_candidate_profile_and_today_facts(profile):
    master = sample_master()
    from datetime import date

    s = F.build_facts(profile, master, {"civility": "Mme"}, make_job(profile), today=date(2026, 11, 17))
    cp, today = s.get("candidate_profile"), s.get("today")
    assert cp.label.startswith("Profil du candidat (CV maître)") and not cp.exact and cp.label_re is None and not cp.secret
    assert master.identity.default_title in cp.value and master.education[0].school in cp.value
    assert len(cp.value) <= F.PROFILE_DIGEST_MAX
    assert today.label == "Date du jour" and today.value == "2026-11-17" and today.label_re is None
    assert s.get("civility").value == "Mme"
    assert F.build_facts(profile, master, {"civility": "M."}, make_job(profile)).get("civility").value == "M."
    assert F.build_facts(profile, None, {}, make_job(profile)).get("candidate_profile").value is None
    assert len(F.build_facts(profile, master, {}, make_job(profile)).get("today").value) == 10  # real date by default


def test_profile_digest_is_capped():
    master = sample_master()
    big = master.model_copy(update={"summary": "mot " * 2000})
    d = F.profile_digest(big)
    assert len(d) == F.PROFILE_DIGEST_MAX and d.endswith("…")


# ------------------------------------------------------------------ editable answers (the "Réponses aux formulaires" table)
def test_every_field_is_declared_once_with_a_section_and_the_requested_selects():
    paths = [f.path for f in F.ANSWER_FIELDS]
    assert len(paths) == len(set(paths)) and {f.section for f in F.ANSWER_FIELDS} == set(F.ANSWER_SECTIONS)
    assert [f.section for f in F.ANSWER_FIELDS] == sorted((f.section for f in F.ANSWER_FIELDS), key=F.ANSWER_SECTIONS.index)
    by = F.ANSWER_BY_PATH
    assert by["civility"].options == ("", "M.", "Mme") and by["driving_licence"].options == ("", "Oui (permis B)", "Non")
    assert by["disability"].options == ("", "Non", "Oui (RQTH)", "Ne souhaite pas répondre")
    assert by["criminal_record"].options == ("", "Casier judiciaire vierge", "Ne souhaite pas répondre")
    assert by["needs_visa_sponsorship"].options == ("", "Non", "Oui") and by["vehicle"].options == ("", "Oui", "Non")
    assert {p for p, f in by.items() if f.widget == "date"} == {"birth_date", "stage.available_from", "alternance.start", "graduation_date"}
    assert by["work_authorization"].widget == "textarea" and by["work_authorization"].max_len == 1000


def test_date_conversion_round_trip_and_legacy_text():
    assert F.to_french_date("2027-01-04") == "4 janvier 2027" and F.to_french_date("2027-09-01") == "1er septembre 2027"
    assert F.to_french_date("2027-02-28") == "28 février 2027" and F.to_french_date("2027-08-15") == "15 août 2027"
    for iso in ("2027-01-04", "2027-09-01", "2000-12-31", "2027-08-15", "2027-02-28"):
        assert F.from_french_date(F.to_french_date(iso)) == iso
    assert F.from_french_date("4 aout 2027") == "2027-08-04" and F.from_french_date("1 janvier 2027") == "2027-01-01"
    for legacy in ("janvier 2027", "dès que possible", "", None, "31 février 2027", "4 janvier", "2027-01-04", "4 foo 2027"):
        assert F.from_french_date(legacy) is None
    with pytest.raises(ValueError):
        F.to_french_date("pas une date")


def test_save_answers_round_trip_reaches_build_facts(profile):
    values = {"civility": "Mme", "birth_date": "1999-03-04", "birth_place": " Toulouse ", "nationality": "Française",
              "address": "12 rue des Lilas", "postal_code": "31000", "city": "Toulouse", "department": "Haute-Garonne",
              "country": "France", "work_authorization": "Citoyenne française", "needs_visa_sponsorship": "Non",
              "driving_licence": "Oui (permis B)", "vehicle": "Oui", "disability": "Non", "criminal_record": "Casier judiciaire vierge",
              "stage.available_from": "2027-01-04", "stage.duration": "6 mois", "alternance.start": "2027-09-01",
              "alternance.duration": "24 mois", "alternance.rhythm": "3/1", "notice_period": "Aucun (étudiant)",
              "graduation_date": "2028-06-30", "internship_agreement": "Oui",
              "salary_expectation.stage": "Gratification légale", "salary_expectation.alternance": "Grille légale",
              "current_salary": "Aucun (étudiant)", "mobility": "Toute la France", "languages": "Français, anglais B2",
              "first_name": "Léa-Marie", "last_name": "Durand", "phone_device_type": "Mobile"}
    assert set(values) == {f.path for f in F.ANSWER_FIELDS}
    F.save_answers(profile.id, values)
    data = F.load_answers(profile.id)
    assert data["birth_date"] == "4 mars 1999" and data["alternance"]["start"] == "1er septembre 2027"
    assert data["stage"] == {"available_from": "4 janvier 2027", "duration": "6 mois"} and data["birth_place"] == "Toulouse"
    s = sheet(profile, make_job(profile), answers=data)
    expected = {"civility": "Mme", "birth_date": "4 mars 1999", "birth_place": "Toulouse", "nationality": "Française",
                "address": "12 rue des Lilas", "postal_code": "31000", "city": "Toulouse", "department": "Haute-Garonne",
                "country": "France", "work_authorization": "Citoyenne française", "needs_visa_sponsorship": "Non",
                "driving_licence": "Oui (permis B)", "vehicle": "Oui", "disability": "Non",
                "criminal_record": "Casier judiciaire vierge", "notice_period": "Aucun (étudiant)", "current_salary": "Aucun (étudiant)",
                "graduation_date": "30 juin 2028", "internship_agreement": "Oui", "phone_device_type": "Mobile",
                "mobility": "Toute la France", "languages_summary": "Français, anglais B2", "first_name": "Léa-Marie", "last_name": "Durand"}
    for key, value in expected.items():
        assert val(s, key) == value, key
        assert s.get(key).label and not s.get(key).secret
    pick = lambda base: (s.get(base) or s.get("stage_" + base)).value  # noqa: E731 - key depends on the job's contract type
    assert pick("available_from") == "4 janvier 2027" and pick("salary_expectation") == "Gratification légale"
    assert pick("duration") == "6 mois"


def test_every_new_fact_key_exists_and_is_unknown_without_an_answer(profile):
    s = sheet(profile, make_job(profile), answers={})
    for key in ("birth_place", "department", "needs_visa_sponsorship", "vehicle", "criminal_record", "notice_period", "current_salary",
                "civility", "birth_date", "nationality", "address", "postal_code", "city", "country", "driving_licence", "disability"):
        assert s.get(key) is not None and s.get(key).value is None and s.get(key).label, key


def test_an_empty_value_removes_the_key_and_empty_blocks(profile):
    F.save_answers(profile.id, {"birth_place": "Albi", "stage.duration": "3 mois", "stage.available_from": "2027-01-04"})
    F.save_answers(profile.id, {"birth_place": "  ", "stage.duration": "", "stage.available_from": ""})
    data = F.load_answers(profile.id)
    assert "birth_place" not in data and "stage" not in data
    F.save_answers(profile.id, {"stage.duration": "3 mois", "stage.available_from": "2027-01-04"})
    F.save_answers(profile.id, {"stage.duration": ""})
    assert F.load_answers(profile.id)["stage"] == {"available_from": "4 janvier 2027"}  # the sibling stays


def test_paths_left_out_are_untouched_and_unknown_keys_custom_and_hosts_are_preserved(profile):
    p = F.answers_path(profile.id)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(yaml.safe_dump({"provisional": True, "gender": "F", "education_level": "Bac+4", "trusted_hosts": ["jobs.autre-site.test"],
                                 "custom": [{"question": "Q ?", "answer": "R"}, "bizarre"],
                                 "stage": {"duration": "6 mois", "note": "libre"}}, allow_unicode=True), encoding="utf-8")
    F.save_answers(profile.id, {"city": "Albi", "stage.available_from": "2027-01-04"})
    data = F.load_answers(profile.id)
    assert data["provisional"] is True and data["gender"] == "F" and data["education_level"] == "Bac+4"
    assert data["trusted_hosts"] == ["jobs.autre-site.test"] and data["custom"] == [{"question": "Q ?", "answer": "R"}, "bizarre"]
    assert data["stage"] == {"duration": "6 mois", "note": "libre", "available_from": "4 janvier 2027"} and data["city"] == "Albi"
    assert not p.with_name(p.name + ".tmp").exists()


def test_select_values_must_be_options_and_nothing_is_written_on_error(profile):
    F.save_answers(profile.id, {"city": "Albi"})
    for bad in ({"civility": "Dr", "city": "Paris"}, {"driving_licence": "oui"}, {"disability": "Peut-être"}):
        with pytest.raises(ApplyError, match="non autorisée"):
            F.save_answers(profile.id, bad)
    for bad in ({"birth_date": "pas une date"}, {"stage.available_from": "2027-02-31"}, {"unknown.path": "x"}):
        with pytest.raises(ApplyError):
            F.save_answers(profile.id, bad)
    assert F.load_answers(profile.id) == {"city": "Albi"}


def test_values_are_stripped_and_capped(profile):
    F.save_answers(profile.id, {"address": "  12   rue \n des Lilas  ", "city": "x" * 900, "work_authorization": "y" * 2000})
    data = F.load_answers(profile.id)
    assert data["address"] == "12 rue des Lilas" and len(data["city"]) == 500 and len(data["work_authorization"]) == 1000


def test_a_legacy_free_text_date_is_kept_unless_a_date_is_picked(profile):
    p = F.answers_path(profile.id)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(yaml.safe_dump({"stage": {"available_from": "janvier 2027"}}), encoding="utf-8")
    F.save_answers(profile.id, {"stage.available_from": ""})
    assert F.get_answer(F.load_answers(profile.id), "stage.available_from") == "janvier 2027"
    F.save_answers(profile.id, {"stage.available_from": "2027-01-11"})
    assert F.get_answer(F.load_answers(profile.id), "stage.available_from") == "11 janvier 2027"
    F.save_answers(profile.id, {"stage.available_from": ""})  # a complete date can be cleared
    assert F.get_answer(F.load_answers(profile.id), "stage.available_from") is None


def test_get_answer_paths():
    a = {"stage": {"duration": "6 mois"}, "city": " Albi ", "empty": " "}
    assert F.get_answer(a, "stage.duration") == "6 mois" and F.get_answer(a, "city") == "Albi"
    assert F.get_answer(a, "stage.nope") is None and F.get_answer(a, "city.deeper") is None and F.get_answer(a, "empty") is None


def test_custom_answer_ids_are_stable_and_derived_from_the_normalized_question(profile):
    assert F.custom_id("Quelle est votre disponibilité ?") == F.custom_id("quelle est votre DISPONIBILITÉ")
    assert len(F.custom_id("x")) == 16 and F.custom_id("a") != F.custom_id("b")
    F.save_custom_answers(profile.id, [("Q1 ?", "A1"), ("Q2 ?", "A2")])
    ids = [c["id"] for c in F.custom_answers(profile.id)]
    assert ids == [F.custom_id("Q1 ?"), F.custom_id("Q2 ?")]
    F.delete_custom_answer(profile.id, ids[0], "A1")
    assert [c["id"] for c in F.custom_answers(profile.id)] == [ids[1]]  # the other id did not move


def test_custom_answers_update_delete_and_trusted_host_removal(profile):
    F.save_custom_answers(profile.id, [("Q1 ?", "A1"), ("Q2 ?", "A2"), ("Q3 ?", "A3")])
    F.add_trusted_host(profile.id, "jobs.autre-site.test")
    F.add_trusted_host(profile.id, "recrute.exemple.test")
    q1, q2, q3 = (c["id"] for c in F.custom_answers(profile.id))
    assert F.update_custom_answer(profile.id, q2, "  Nouvelle   réponse ", "A2") is True
    assert [c["answer"] for c in F.custom_answers(profile.id)] == ["A1", "Nouvelle réponse", "A3"]
    assert F.update_custom_answer(profile.id, q2, "autre", "A2") is False  # the stored answer is no longer what was displayed
    assert F.delete_custom_answer(profile.id, q2, "A2") is False
    assert F.delete_custom_answer(profile.id, q1, "A1") is True
    assert [c["question"] for c in F.custom_answers(profile.id)] == ["Q2 ?", "Q3 ?"]
    assert F.delete_custom_answer(profile.id, q1, "A1") is False and F.update_custom_answer(profile.id, q1, "x", "A1") is False  # gone
    with pytest.raises(ApplyError, match="vide"):
        F.update_custom_answer(profile.id, q3, "  ", "A3")
    assert F.remove_trusted_host(profile.id, "JOBS.autre-site.test.") is True and F.remove_trusted_host(profile.id, "jobs.autre-site.test") is False
    assert F.trusted_hosts(profile.id) == ["recrute.exemple.test"] and F.remove_trusted_host(profile.id, "x") is False
    assert F.remove_trusted_host(profile.id, "recrute.exemple.test") and "trusted_hosts" not in F.load_answers(profile.id)
    assert len(F.custom_answers(profile.id)) == 2


@pytest.mark.parametrize("label", ["Kennwort", "Passwort bestätigen", "Contraseña", "Senha", "Wachtwoord", "Hasło", "Parola", "密码",
                                   "Mot de passe", "Password"])
def test_the_password_label_matches_other_languages(label):
    from jobhunt.apply.facts import LABEL_RE
    import re
    import unicodedata
    stripped = "".join(c for c in unicodedata.normalize("NFKD", label) if not unicodedata.combining(c)).lower()
    assert re.search(LABEL_RE["account_password"], stripped, re.I)


# ------------------------------------------------------------------ answers_lock
import os  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402


def test_answers_lock_is_reentrant_and_releases(profile):
    with F.answers_lock(profile.id):
        with F.answers_lock(profile.id):  # a public function called from inside a wrapped block
            F.save_answers(profile.id, {"city": "Albi"})
            assert F.add_trusted_host(profile.id, "jobs.autre-site.test")
        assert F.get_answer(F.load_answers(profile.id), "city") == "Albi"
    assert F.answers_lock and F._held == {}
    with F.answers_lock(profile.id, timeout_s=0.1):  # released: free again
        pass


def test_answers_lock_times_out_with_a_clear_error_when_held_by_another_thread(profile):
    held, release = threading.Event(), threading.Event()

    def hold():
        with F.answers_lock(profile.id):
            held.set()
            release.wait(5)

    t = threading.Thread(target=hold)
    t.start()
    assert held.wait(5)
    start = time.monotonic()
    with pytest.raises(ApplyError, match="Fichier de réponses occupé : réessayer"):
        with F.answers_lock(profile.id, timeout_s=0.3):
            pass
    assert 0.25 <= time.monotonic() - start < 3
    with pytest.raises(ApplyError, match="occupé"):  # the public writers wait the default 10 s: shortened here
        with F.answers_lock(profile.id, timeout_s=0.05):
            F.save_answers(profile.id, {"city": "Albi"})
    release.set()
    t.join(5)
    F.save_answers(profile.id, {"city": "Albi"})  # free again


def test_a_waiting_writer_gets_the_lock_when_it_is_released(profile):
    held, release = threading.Event(), threading.Event()

    def hold():
        with F.answers_lock(profile.id):
            held.set()
            release.wait(5)

    t = threading.Thread(target=hold)
    t.start()
    assert held.wait(5)
    threading.Timer(0.2, release.set).start()
    F.save_answers(profile.id, {"city": "Albi"})  # blocks ~0.2 s, then writes
    t.join(5)
    assert F.get_answer(F.load_answers(profile.id), "city") == "Albi"


def test_two_threads_writing_different_fields_both_survive(profile):
    errors = []

    def writer(kind):
        try:
            for i in range(40):
                if kind == "a":
                    F.save_answers(profile.id, {"city": f"Ville{i}"})
                    F.save_custom_answers(profile.id, [(f"Question {i} ?", "R")])
                else:
                    F.save_answers(profile.id, {"birth_place": f"Lieu{i}"})
                    F.add_trusted_host(profile.id, f"site{i}.exemple.test")
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=writer, args=(k,)) for k in "ab"]
    [t.start() for t in threads]
    [t.join(60) for t in threads]
    assert not errors
    data = F.load_answers(profile.id)
    assert data["city"] == "Ville39" and data["birth_place"] == "Lieu39"
    assert len(F.custom_answers(profile.id)) == 40 and len(F.trusted_hosts(profile.id)) == 40  # no lost update


def test_two_processes_writing_different_fields_both_survive(profile, tmp_path):
    code = ("import sys\nfrom jobhunt.apply import facts as F\n"
            "kind, pid = sys.argv[1], int(sys.argv[2])\n"
            "for i in range(25):\n"
            "    if kind == 'a':\n        F.save_answers(pid, {'city': f'Ville{i}'}); F.save_custom_answers(pid, [(f'Q{i} ?', 'R')])\n"
            "    else:\n        F.save_answers(pid, {'birth_place': f'Lieu{i}'}); F.add_trusted_host(pid, f'site{i}.exemple.test')\n")
    env = {**os.environ, "JOBHUNT_DATA_DIR": str(get_settings().data_dir)}
    procs = [subprocess.Popen([sys.executable, "-c", code, k, str(profile.id)], env=env, stderr=subprocess.PIPE) for k in "ab"]
    outs = [p.communicate(timeout=120) for p in procs]
    assert [p.returncode for p in procs] == [0, 0], [o[1].decode()[-400:] for o in outs]
    data = F.load_answers(profile.id)
    assert data["city"] == "Ville24" and data["birth_place"] == "Lieu24"
    assert len(F.custom_answers(profile.id)) == 25 and len(F.trusted_hosts(profile.id)) == 25


def test_temp_files_never_collide_and_stale_ones_are_cleaned(profile):
    p = F.answers_path(profile.id)
    p.parent.mkdir(parents=True, exist_ok=True)
    old, fresh = p.with_name("answers.crash1.tmp"), p.with_name("answers.crash2.tmp")
    old.write_text("x")
    fresh.write_text("x")
    long_ago = time.time() - 2 * 3600
    os.utime(old, (long_ago, long_ago))
    seen = set()
    real = os.replace

    def spy(src, dst):
        seen.add(src)
        return real(src, dst)

    import os as _os
    errors = []
    _os.replace = spy
    try:
        def w(i):
            try:
                F._write_answers(profile.id, {"city": f"v{i}"})  # deliberately WITHOUT the lock: names must still not collide
            except Exception as e:  # noqa: BLE001
                errors.append(e)
        ts = [threading.Thread(target=w, args=(i,)) for i in range(20)]
        [t.start() for t in ts]
        [t.join(30) for t in ts]
    finally:
        _os.replace = real
    assert not errors and len(seen) == 20  # twenty distinct temp names
    assert not old.exists() and fresh.exists()  # only the stale leftover is removed
    assert F.load_answers(profile.id)["city"].startswith("v")
    assert sorted(x.name for x in p.parent.glob("answers.*.tmp")) == ["answers.crash2.tmp"]
