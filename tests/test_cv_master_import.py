from __future__ import annotations

import pymupdf
import pytest

from jobhunt.config import get_settings
from jobhunt.cv.importer import ImportedMaster, _Bullet, _Education, _Language, _Project, _Skill, extract_pdf, import_cv, verify_import
from jobhunt.cv.master import (Bullet, Master, MasterError, Project, confirm_master, discard_pending, dump_master, load_master,
                               load_pending, load_report, parse_master, photo_path, save_master)
from jobhunt.llm import LLMError

from cv_fakes import fake_llm, sample_master

CV_LINES = [
    "CAMILLE DURAND", "Élève ingénieure - Recherche d'alternance en robotique",
    "Étudiante en cycle ingénieur en robotique, je conçois des prototypes.", "COMPÉTENCES",
    "Développement logiciel : C, C++, Python.", "PROJETS", "Robot suiveur de ligne 2025",
    "Projet académique, École Exemple", "Acquisition : lecture de cinq capteurs infrarouges.", "FORMATION",
    "Cycle ingénieur - Robotique 2024–en cours", "École Exemple", "LANGUES", "Français : courant.",
]


def make_pdf(path):
    doc = pymupdf.open()
    page = doc.new_page()
    y = 60
    for line in CV_LINES:
        page.insert_text((50, y), line, fontsize=10)
        y += 18
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 900, 1100), False)
    pix.set_rect(pix.irect, (200, 120, 90))
    page.insert_image(pymupdf.Rect(420, 40, 500, 140), pixmap=pix)
    doc.save(path)
    return path


def imported() -> ImportedMaster:
    return ImportedMaster(
        name="Camille Durand", default_title="Élève ingénieure",
        summary="Étudiante en cycle ingénieur en robotique, je conçois des prototypes.",
        skill_groups=[_Skill(label="Développement logiciel", items=["C", "C++", "Python."])],
        projects=[_Project(title="Robot suiveur de ligne", dates="2025", context="Projet académique, École Exemple",
                           bullets=[_Bullet(label="Acquisition", text="lecture de cinq capteurs infrarouges.")])],
        education=[_Education(title="Cycle ingénieur - Robotique", school="École Exemple", dates="2024–en cours")],
        languages=[_Language(name="Français", level="courant.")],
    )


def test_ensure_ids_is_stable_and_yaml_roundtrips():
    m = sample_master()
    assert [g.id for g in m.skill_groups] == ["sk1", "sk2", "sk3", "sk4"]
    assert m.projects[0].id == "p1" and [b.id for b in m.projects[0].bullets] == ["p1b1", "p1b2"]
    again = parse_master(dump_master(m))
    assert again == m
    m.projects.append(Project(title="Nouveau", bullets=[Bullet(text="x")]))
    ids = [p.id for p in m.ensure_ids().projects]
    assert ids[:3] == ["p1", "p2", "p3"] and ids[3] == "p4"  # existing ids untouched, new ones appended


def test_parse_master_errors():
    with pytest.raises(MasterError):
        parse_master("identity: [unclosed")
    with pytest.raises(MasterError):
        parse_master("- a list")
    with pytest.raises(MasterError):
        parse_master("projects:\n  - dates: 2025\n")  # title missing


def test_save_load_master_and_photo_path(tmp_path):
    assert load_master(7) is None
    m = sample_master()
    p = save_master(7, m)
    assert p == get_settings().profiles_dir / "7" / "master.yaml" and load_master(7) == m
    assert photo_path(7, m) is None
    (p.parent / "photo.jpg").write_bytes(b"x")
    assert photo_path(7).name == "photo.jpg"


def test_photo_path_rejects_symlink_escape_and_non_images(tmp_path):
    m = save_master(8, sample_master()).parent
    secret = tmp_path / "secret.jpg"
    secret.write_bytes(b"x")
    (m / "photo.jpg").symlink_to(secret)  # points outside the profile dir
    assert photo_path(8) is None
    (m / "photo.jpg").unlink()
    (m / "photo.txt").write_text("x")
    assert photo_path(8) is None
    inside = m / "real.png"
    inside.write_bytes(b"x")
    (m / "photo.png").symlink_to(inside)  # a symlink that stays inside is fine
    assert photo_path(8).name == "real.png"
    mm = sample_master().model_copy(update={"identity": sample_master().identity.model_copy(update={"photo": "../../x.jpg"})})
    assert photo_path(8, mm) is None


def test_confirmation_lifecycle_and_hash():
    m = sample_master()
    save_master(5, m)
    assert not load_master(5).is_confirmed
    c = confirm_master(5)
    assert c.is_confirmed and load_master(5).is_confirmed and load_master(5).content_hash == c.compute_hash()
    # a hand edit on disk invalidates the confirmation even if confirmed_at is still there
    path = get_settings().profiles_dir / "5" / "master.yaml"
    path.write_text(path.read_text().replace("Courant.", "Bilingue."))
    assert not load_master(5).is_confirmed
    # saving through the API unconfirms too
    confirm_master(5)
    save_master(5, load_master(5))
    assert not load_master(5).is_confirmed
    with pytest.raises(MasterError):
        confirm_master(99)


def test_confirmed_fields_are_not_master_content():
    m = sample_master()
    assert "confirmed" not in m.all_text() and m.compute_hash() == m.model_copy(update={"confirmed_at": "x", "content_hash": "y"}).compute_hash()


def test_extract_pdf_text_and_photo(tmp_path):
    pdf = make_pdf(tmp_path / "cv.pdf")
    text, photo = extract_pdf(pdf, tmp_path / "out")
    assert "COMPÉTENCES" in text and "Robot suiveur de ligne" in text
    assert photo and photo.name == "photo.jpg" and photo.stat().st_size > 100
    img = pymupdf.Pixmap(str(photo))  # shrunk (<= 700 px wide) and readable
    assert img.width <= 700


async def test_import_cv_pdf_stages_pending_and_never_touches_the_active_master(tmp_path, profile):
    pdf = make_pdf(tmp_path / "cv.pdf")
    save_master(profile.id, sample_master())
    active_before = (get_settings().profiles_dir / str(profile.id) / "master.yaml").read_text()
    llm, prov = fake_llm({"ImportedMaster": imported()})
    profile = profile.model_copy(update={"email": "p@example.test", "phone": "+33 1 00 00 00 00"})
    res = await import_cv(profile.id, profile, llm, pdf=pdf)
    master, path = res.master, res.path
    pdir = get_settings().profiles_dir / str(profile.id)
    assert len(prov.calls) == 1 and "verbatim" in prov.calls[0]["system"].lower() and "COMPÉTENCES" in prov.calls[0]["user"]
    assert path == pdir / "master.pending.yaml" and (pdir / "master.yaml").read_text() == active_before
    assert (pdir / "pending-photo.jpg").is_file() and not (pdir / "photo.jpg").exists()
    assert master.identity.email == "p@example.test" and master.projects[0].bullets[0].id == "p1b1"
    assert res.unverified == []
    assert master.skill_groups[0].items == ["C", "C++", "Python"]
    # the contacts taken from the Profile are not in the CV text: disputed; so is a name differing from the Profile's
    fields = {(d["field"], d["reason"]) for d in res.disputed}
    assert ("identity.email", "absent du CV source") in fields and ("identity.phone", "absent du CV source") in fields
    assert ("identity.name", "différent du nom du profil") in fields
    assert load_report(profile.id)["disputed"] and load_pending(profile.id) == master.model_copy(update={})
    # confirming moves it to master.yaml (with the photo) and clears the staging files
    confirmed = confirm_master(profile.id)
    assert confirmed.is_confirmed and load_master(profile.id).projects[0].title == "Robot suiveur de ligne"
    assert (pdir / "photo.jpg").is_file() and not (pdir / "pending-photo.jpg").exists() and load_pending(profile.id) is None
    assert not (pdir / "master.pending.report.json").exists()


async def test_import_identity_matching_source_and_profile_is_not_disputed(tmp_path):
    txt = tmp_path / "cv.txt"
    txt.write_text("\n".join(CV_LINES) + "\ncamille@example.test +33 6 00 00 00 00 linkedin.com/in/camille", encoding="utf-8")
    d = imported()
    d.email, d.phone, d.linkedin = "Camille@Example.test", "+33 6 00 00 00 00", "https://www.linkedin.com/in/camille"
    llm, _ = fake_llm({"ImportedMaster": d})
    from jobhunt.models import Profile
    res = await import_cv(1, Profile(name="Camille", headline="x", email="camille@example.test"), llm, text_file=txt)
    assert res.disputed == []
    d.email = "other@example.test"
    res = await import_cv(1, Profile(name="Camille", headline="x", email="camille@example.test"), llm, text_file=txt)
    assert {x["reason"] for x in res.disputed} == {"absent du CV source", "différent de l'e-mail du profil"}


async def test_discard_pending_and_reimport_keeps_stage_qualifier_and_flags_non_verbatim(tmp_path, profile):
    save_master(profile.id, Master(identity={"name": "X", "stage_qualifier": "4e année"}))
    txt = tmp_path / "cv.txt"
    txt.write_text("\n".join(CV_LINES), encoding="utf-8")
    draft = imported()
    draft.skill_groups[0].items.append("Rust")  # not in the source text: invented
    llm, _ = fake_llm({"ImportedMaster": draft})
    res = await import_cv(profile.id, profile, llm, text_file=txt)
    assert res.master.identity.stage_qualifier == "4e année"
    assert len(res.unverified) == 1 and "Rust" in res.unverified[0]
    discard_pending(profile.id)
    assert load_pending(profile.id) is None and load_master(profile.id).identity.name == "X"


async def test_import_argument_and_llm_errors(tmp_path, profile):
    llm, _ = fake_llm({"ImportedMaster": imported()})
    with pytest.raises(ValueError):
        await import_cv(profile.id, profile, llm)
    empty = tmp_path / "e.txt"
    empty.write_text("  ")
    with pytest.raises(ValueError):
        await import_cv(profile.id, profile, llm, text_file=empty)
    bad, _ = fake_llm(error="boom")
    txt = tmp_path / "cv.txt"
    txt.write_text("CV")
    with pytest.raises(LLMError):
        await import_cv(profile.id, profile, bad, text_file=txt)
    assert load_master(profile.id) is None and load_pending(profile.id) is None  # nothing written on failure


def test_verify_import_ignores_case_accents_spacing():
    m = parse_master("summary: 'Étudiante  en robotique'\n")
    assert verify_import(m, "etudiante en ROBOTIQUE") == []
    assert verify_import(m, "autre texte")
