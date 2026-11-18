from __future__ import annotations

from jobhunt.cv.check import Flag, blocking, check, technical_terms
from jobhunt.cv.tailor import build_tailored
from jobhunt.models import ContractType

from cv_fakes import good_draft, sample_master


class _Job:
    contract_type = ContractType.ALTERNANCE
    title = "Alternance ingénieur systèmes embarqués"
    description = "Firmware C, UART."


def tailored():
    m = sample_master()
    return m, build_tailored(good_draft(m), m, _Job())


def codes(flags, level=None):
    return {f.code for f in flags if level in (None, f.level)}


def test_clean_cv_has_no_errors():
    m, cv = tailored()
    flags = check(cv, m)
    assert blocking(flags) == []
    assert [f for f in flags if f.level == "warning"] == []


def test_added_skill_is_a_prep_item_with_hint_not_an_error():
    m, cv = tailored()
    cv.skill_groups[0].items.append("Rust")
    cv.skill_groups[1].items[0] = "uart"  # case difference is fine
    flags = check(cv, m)
    preps = [f for f in flags if f.code == "skill_not_in_master"]
    assert [(f.location, f.term) for f in preps] == [("skill_groups[0].items[3]", "Rust")]
    assert preps[0].level == "prep" and "Rust" in preps[0].hint
    assert not [f for f in flags if f.level == "error"]


def test_language_level_claims_are_hard_facts():
    m, cv = tailored()
    cv.skill_groups[0].items.append("Anglais C1")
    cv.summary += " Anglais bilingue."
    errs = [f for f in check(cv, m) if f.code == "language_level_claim"]
    assert {f.term for f in errs} == {"C1", "bilingue"} and all(f.level == "error" for f in errs)


def test_bullet_without_or_with_invalid_sources_is_error():
    m, cv = tailored()
    cv.projects[0].bullets[0].source_ids = []
    cv.projects[0].bullets[1].source_ids = ["zzz"]
    flags = check(cv, m)
    assert {(f.code, f.location) for f in flags if f.level == "error"} == {
        ("bullet_no_source", "projects[0].bullets[0]"), ("bullet_bad_source", "projects[0].bullets[1]")}


def test_bullet_citing_another_project_is_an_error():
    m, cv = tailored()
    cv.projects[1].bullets[0].source_ids = [m.projects[1].bullets[0].id, m.projects[0].bullets[0].id]
    assert "bullet_other_project" in codes(check(cv, m), "error")


def test_altered_dates_titles_schools_are_errors():
    m, cv = tailored()
    cv.projects[0].dates = "2023"
    cv.projects[1].title = "Station météo industrielle"
    cv.projects[0].context = "Projet académique, École Prestigieuse"
    cv.education[0].school = "École Prestigieuse"
    cv.education[1].dates = "2019–2024"
    cv.languages[1].level = "Niveau C1."
    cv.education.append(cv.education[0].model_copy(update={"id": "e99"}))
    flags = check(cv, m)
    assert {f.code for f in flags if f.level == "error"} == {
        "project_altered", "education_altered", "language_altered", "education_unknown"}
    assert {f.location for f in flags if f.code == "project_altered"} == {"projects[0].dates", "projects[1].title", "projects[0].context"}


def test_unknown_project_is_error():
    m, cv = tailored()
    cv.projects[0].id = "p42"
    assert "project_unknown" in codes(check(cv, m), "error")


def test_whitespace_differences_are_not_alterations():
    m, cv = tailored()
    cv.projects[0].title = "  Robot   suiveur de ligne "
    assert blocking(check(cv, m)) == []


def test_terms_absent_from_master_are_prep_items_not_errors():
    m, cv = tailored()
    cv.summary += " Sensibilisée à ISO 26262, AUTOSAR et ROS2 avec MATLAB chez Google."
    cv.projects[0].bullets[0].text += " Gain de 40 % sur FreeRTOS."
    all_flags = check(cv, m)
    assert not [f for f in all_flags if f.level == "error"]
    flags = [f for f in all_flags if f.level == "prep"]
    summary_terms = {f.term for f in flags if f.location == "summary"}
    assert {"ISO 26262", "AUTOSAR", "ROS2", "MATLAB", "Google"} <= summary_terms
    bullet_terms = {f.term for f in flags if f.location == "projects[0].bullets[0]"}
    assert {"40", "FreeRTOS"} <= bullet_terms
    assert {f.code for f in flags} >= {"summary_term_not_in_master", "term_not_in_sources"}


def test_bullet_terms_must_be_in_the_CITED_sources_not_just_the_master():
    m, cv = tailored()
    # "UART" is in the master (project 2) but not in the sources cited by this project-1 bullet
    cv.projects[0].bullets[0].text = "lecture de cinq capteurs par UART."
    errs = [f for f in check(cv, m) if f.code == "term_not_in_sources"]
    assert [f.term for f in errs] == ["UART"]
    cv.projects[0].bullets[0].source_ids = ["p1b1", "p1b2"]
    assert [f for f in check(cv, m) if f.code == "term_not_in_sources"] == [f for f in errs]  # still: p1b2 has no UART either


def test_numbers_quantities_and_proper_nouns_in_bullets():
    m, cv = tailored()
    b = cv.projects[0].bullets[0]
    b.text = "lecture de 5 capteurs infrarouges et filtrage des mesures."  # cinq == 5: supported
    assert [f for f in check(cv, m) if f.level == "error"] == []
    b.text = "lecture de sept capteurs infrarouges pour 30 % de précision chez Airbus."
    terms = {f.term for f in check(cv, m) if f.code == "term_not_in_sources"}
    assert {"sept", "30", "Airbus"} <= terms
    b.text = "lecture de cinq capteurs infrarouges. Pilotage par Toulouse"  # sentence start is not a proper noun, mid-sentence is
    assert {f.term for f in check(cv, m) if f.code == "term_not_in_sources"} == {"Toulouse"}


def test_case_accents_and_plurals_are_normalised():
    m, cv = tailored()
    cv.projects[0].bullets[0].text = "LECTURE de cinq CAPTEUR infrarouges et filtrage des mesure."
    assert [f for f in check(cv, m) if f.level == "error"] == []
    cv.projects[1].bullets[0].text = "lecture d'un capteur par i2c."
    assert [f for f in check(cv, m) if f.level == "error"] == []


def test_skill_group_rename_cannot_introduce_tools_or_numbers():
    m, cv = tailored()
    cv.skill_groups[0].label = "Firmware ROS2"
    cv.skill_groups[1].label = "Interfaces UART"  # in the master: fine
    errs = [f for f in check(cv, m) if f.code == "label_term_not_in_master"]
    assert [(f.location, f.term) for f in errs] == [("skill_groups[0].label", "ROS2")]


def test_headline_tool_absent_from_master_is_an_error():
    m, cv = tailored()
    cv.headline += " Python Rust"
    assert [f.term for f in check(cv, m) if f.code == "headline_term_not_in_master"] == ["Rust"]


def test_terms_present_in_master_do_not_error():
    m, cv = tailored()
    cv.summary = "Python, C++, STM32 (ARM Cortex-M), UART/SPI/I2C et PWM."
    assert [f for f in check(cv, m) if f.code == "summary_term_not_in_master"] == []


def test_french_words_are_not_technical():
    terms = technical_terms("C'est une Étudiante en Robotique. Les systèmes embarqués. Je vais en ville.")
    assert terms == {}
    assert set(technical_terms("Python, C++, C# et Go")) >= {"python", "c++", "c#", "go"}
    assert "std:iso26262" in technical_terms("conforme à la norme ISO 26262")
    assert "std:en2025" not in technical_terms("en 2025")  # lowercase French "en" is not the EN standard prefix


def test_stage_qualifier_headline_allowed():
    m, cv = tailored()
    qualifier, m.identity.stage_qualifier = m.identity.stage_qualifier, None
    cv.headline = "Élève ingénieure - Recherche de stage (4e année / assistant ingénieur) en systèmes embarqués"
    assert [f for f in check(cv, m, extra_allowed_text=qualifier) if f.location == "headline"] == []
    assert any(f.location == "headline" for f in check(cv, m))  # "4e" would be flagged without the allowance


def test_long_summary_warning_and_flag_model():
    m, cv = tailored()
    cv.summary = "mot " * 120
    assert "summary_long" in codes(check(cv, m), "warning")
    assert Flag(level="error", code="x", location="y", message="z").term is None


def test_domain_is_restricted_to_ad_words_and_master_tools():
    from jobhunt.cv.tailor import sanitize_domain
    m = sample_master()
    assert sanitize_domain("systèmes embarqués", m, _Job())[0] == "systèmes embarqués"
    dom, note = sanitize_domain("intelligence artificielle", m, _Job())
    assert dom == "" and "annonce" in note
    class J2(_Job):
        description = "Firmware pour systèmes embarqués sous Zephyr"
    dom, note = sanitize_domain("systèmes embarqués Zephyr", m, J2())
    assert dom == "systèmes embarqués Zephyr" and note is None  # ad keywords are allowed even if absent from the master
