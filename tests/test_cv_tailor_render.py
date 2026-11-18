from __future__ import annotations

import json

import pymupdf
import pytest

from jobhunt.cv.master import Bullet, Project
from jobhunt.cv.render import FIT_PREFIX, FitNotes, fit_and_render, render_html, safe_filename
from jobhunt.cv.tailor import DraftBullet, DraftProject, TailorDraft, build_tailored, tailor
from jobhunt.cv.tailored import build_headline
from jobhunt.dedupe import MergedJob
from jobhunt.llm import LLMError
from jobhunt.models import ContractType, Profile

from cv_fakes import fake_llm, good_draft, sample_master

JOB = MergedJob(title="Stage ingénieur logiciel embarqué", company="Exemple SAS", contract_type=ContractType.STAGE,
                description="Nous cherchons un stagiaire pour le firmware C sur STM32, UART, systèmes embarqués, AUTOSAR apprécié.")


def make_photo(path):
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 160, 200), False)
    pix.set_rect(pix.irect, (90, 140, 200))
    pix.save(path)
    return path


# ------------------------------------------------------------------ headline
@pytest.mark.parametrize("contract,expected", [
    (ContractType.ALTERNANCE, "Élève ingénieure - Recherche d'alternance en robotique"),
    (ContractType.STAGE, "Élève ingénieure - Recherche de stage (4e année / assistant ingénieur) en robotique"),
    (ContractType.CDI, "Élève ingénieure - Recherche d'un CDI en robotique"),
    (ContractType.UNKNOWN, "Élève ingénieure - Recherche d'un poste en robotique"),
])
def test_headline_follows_contract(contract, expected):
    assert build_headline(sample_master().identity, contract, "robotique") == expected


def test_headline_without_qualifier_or_title():
    ident = sample_master().identity.model_copy(update={"stage_qualifier": None, "default_title": ""})
    assert build_headline(ident, "stage", "robotique.") == "Recherche de stage en robotique"


# ------------------------------------------------------------------ tailoring
async def test_tailor_one_call_with_master_ids_and_job_in_prompt():
    m = sample_master()
    llm, prov = fake_llm({"TailorDraft": good_draft(m)})
    profile = Profile(name="Camille", headline="Étudiante robotique", email="secret@example.test", interests=["embarqué"])
    cv = await tailor(m, JOB, profile, llm)
    assert len(prov.calls) == 1
    system, user = prov.calls[0]["system"], prov.calls[0]["user"]
    assert "HARD FACTS" in system and "ATS" in system and "p1b1" in system and "Robot suiveur de ligne" in system
    assert "STM32" in user and "Exemple SAS" in user and "stage" in user
    assert "secret@example.test" not in system + user and "camille@example.test" not in system + user  # no contact data to the LLM
    assert cv.headline == "Élève ingénieure - Recherche de stage (4e année / assistant ingénieur) en systèmes embarqués"
    assert [p.id for p in cv.projects] == ["p1", "p2"]
    assert cv.projects[0].title == m.projects[0].title and cv.projects[0].dates == "2025"  # copied by code from the master
    assert cv.projects[0].bullets[0].source_ids == ["p1b1"]
    assert cv.gaps == ["Expérience AUTOSAR", "Anglais courant"] and "capteurs" in cv.matched_keywords
    assert [e.id for e in cv.education] == ["e1", "e2"] and cv.languages[0].name == "Français"


async def test_tailor_ignores_unknown_projects_and_caps_to_three():
    m = sample_master()
    draft = good_draft(m)
    draft.projects.insert(0, DraftProject(id="p99", bullets=[DraftBullet(label="x", text="y", source_ids=["p99b1"])]))
    draft.projects.append(DraftProject(id="p3", bullets=[DraftBullet(label="a", text="b", source_ids=["p3b1"])]))
    m.projects.append(Project(title="Quatrième", bullets=[Bullet(text="t")]))
    m.ensure_ids()
    draft.projects.append(DraftProject(id="p4", bullets=[DraftBullet(label="a", text="b", source_ids=["p4b1"])]))
    draft.education_ids, draft.language_ids = ["e2", "zzz"], []
    cv = build_tailored(draft, m, JOB)
    assert [p.id for p in cv.projects] == ["p1", "p2", "p3"]
    assert any("p99" in a for a in cv.adjustments) and any("au-delà" in a for a in cv.adjustments)
    assert [e.id for e in cv.education] == ["e2"]
    assert [l.id for l in cv.languages] == ["l1", "l2"]  # empty -> keep the master's


async def test_tailor_failure_raises_llm_error_no_fallback():
    llm, _ = fake_llm(error="indisponible")
    with pytest.raises(LLMError):
        await tailor(sample_master(), JOB, None, llm)


async def test_failover_structured_uses_second_provider():
    from jobhunt.llm import FailoverLLM

    from cv_fakes import FakeProvider
    bad, good = FakeProvider(error="down"), FakeProvider({"TailorDraft": good_draft()})
    good.name = "fake2"
    llm = FailoverLLM(providers=[bad, good])
    out = await llm.structured(TailorDraft, system_text="s", user_text="u")
    assert out.domain == "systèmes embarqués" and llm.errors and llm.errors == ["fake: error"]
    assert [c["status"] for c in llm.calls] == ["error", "ok"]


def test_prompt_serialization_is_json():
    from jobhunt.cv.tailor import master_for_llm
    d = json.loads(master_for_llm(sample_master()))
    assert d["identity"] == {"default_title": "Élève ingénieure"} and d["projects"][0]["bullets"][0]["id"] == "p1b1"


# ------------------------------------------------------------------ render
def test_render_html_content_and_escaping(tmp_path):
    m = sample_master()
    cv = build_tailored(good_draft(m), m, JOB)
    cv.summary = "Résumé <script>alert(1)</script> & co"
    html = render_html(cv, m, make_photo(tmp_path / "p.png"))
    assert "<script>alert" not in html and "&lt;script&gt;" in html
    assert "Camille Durand" in html and "camille@example.test" in html and "linkedin.com/in/camille-example" in html
    assert "data:image/png;base64," in html
    assert "Compétences" in html and "Projets" in html and "Formation" in html and "Langues" in html
    assert "<b>Acquisition capteurs :</b>" in html and "2025" in html
    assert "Expérience AUTOSAR" not in html  # gaps are never on the CV
    assert "<img" not in render_html(cv, m, None)


def test_safe_filename():
    assert safe_filename("CV", "Camille Durand", 'Ex/emple: "SA"?') == "CV - Camille Durand - Ex emple SA.pdf"
    assert safe_filename("CV", "A", "") == "CV - A.pdf" and safe_filename("", "") == "CV.pdf"


def test_render_pdf_one_page_no_trim(tmp_path):
    m = sample_master()
    cv = build_tailored(good_draft(m), m, JOB)
    res = fit_and_render(cv, m, tmp_path / "out" / "cv.pdf", photo=make_photo(tmp_path / "p.png"))
    assert res.fits and res.pages == 1 and res.notes.is_empty()
    with pymupdf.open(tmp_path / "out" / "cv.pdf") as doc:
        assert len(doc) == 1 and doc[0].rect.width == pytest.approx(595.28, abs=0.5)
        text = doc[0].get_text()
        assert "CAMILLE DURAND" in text.upper() and "Robot suiveur de ligne" in text
        assert len(doc[0].get_images()) == 1
    assert not list((tmp_path / "out").glob("part-*"))


def overflowing(m):
    cv = build_tailored(good_draft(m), m, JOB)
    long = "lecture et traitement de mesures sur plusieurs voies avec validation croisée et documentation détaillée " * 3
    for p in cv.projects:
        p.bullets += [p.bullets[0].model_copy(update={"text": long, "label": f"Extra {i}"}) for i in range(5)]
    return cv


def test_overflow_is_trimmed_in_a_separate_record_and_untrimmed_content_is_kept(tmp_path):
    m = sample_master()
    cv = overflowing(m)
    before = cv.model_dump()
    res = fit_and_render(cv, m, tmp_path / "cv.pdf")
    assert cv.model_dump() == before  # the untrimmed content is never modified
    assert res.fits
    with pymupdf.open(tmp_path / "cv.pdf") as doc:
        assert len(doc) == 1
    n = res.notes
    assert 0.92 <= n.font_scale < 1.0 and n.dropped_bullets
    assert sum(len(p.bullets) for p in res.final.projects) < sum(len(p.bullets) for p in cv.projects)
    assert res.final.font_scale == n.font_scale
    msgs = [a for a in res.final.adjustments if a.startswith(FIT_PREFIX)]
    assert any("taille de police" in x for x in msgs) and any("retiré" in x for x in msgs)
    assert len(res.final.projects[0].bullets) >= 2  # least relevant content goes first


def test_persisted_trims_apply_again_after_an_edit(tmp_path):
    m = sample_master()
    cv = overflowing(m)
    first = fit_and_render(cv, m, tmp_path / "a.pdf")
    edited = cv.model_copy(deep=True)
    edited.summary = "Résumé modifié."
    second = fit_and_render(edited, m, tmp_path / "b.pdf", notes=first.notes)
    assert second.fits and set(map(tuple, first.notes.dropped_bullets)) <= set(map(tuple, second.notes.dropped_bullets))
    assert second.final.summary == "Résumé modifié."


def test_content_that_cannot_fit_publishes_no_pdf(tmp_path):
    m = sample_master()
    cv = build_tailored(good_draft(m), m, JOB)
    cv.projects = cv.projects[:1]
    cv.projects[0].bullets = [cv.projects[0].bullets[0].model_copy(update={"text": "mot " * 3000})]
    res = fit_and_render(cv, m, tmp_path / "cv.pdf")
    assert not res.fits and res.pages > 1 and not (tmp_path / "cv.pdf").exists() and not list(tmp_path.glob("part-*"))


def test_fit_notes_roundtrip_and_semantics():
    n = FitNotes(font_scale=0.94, dropped_bullets=[("p1", 1)], dropped_projects=["p3"])
    again = FitNotes.model_validate_json(n.model_dump_json())
    assert again.bullet_dropped("p1", 1) and not again.bullet_dropped("p1", 0) and again.bullet_dropped("p3", 0)
