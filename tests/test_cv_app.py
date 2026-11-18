"""Applications storage, service, web UI and CLI for tailored CVs (fake LLM, real headless render)."""

from __future__ import annotations

import hashlib
import sqlite3
import time
from pathlib import Path

import pymupdf
import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from jobhunt import db
from jobhunt.cli import app as cli_app
from jobhunt.config import get_settings
from jobhunt.cv import service as cvs
from jobhunt.cv import store
from jobhunt.cv.check import Flag
from jobhunt.cv.master import confirm_master, load_master, save_master
from jobhunt.cv.tailor import TailorDraft
from jobhunt.cv.tailored import TailoredCV
from jobhunt.dedupe import MergedJob
from jobhunt.llm import LLMError
from jobhunt.models import ContractType
from jobhunt.web.app import GENERATING, app

from cv_fakes import FakeProvider, fake_llm, good_draft, reject, sample_master, save_confirmed


@pytest.fixture
def job(profile):
    j = MergedJob(title="Stage ingénieur logiciel embarqué", company="Exemple/SAS", location="Toulouse (31)",
                  contract_type=ContractType.STAGE, origin_ats="lever",
                  description="Firmware C sur STM32, UART, systèmes embarqués, AUTOSAR apprécié.", score=88)
    db.save_jobs(profile.id, [j])
    return j


@pytest.fixture
def master(profile):
    return save_confirmed(profile.id)


def patch_llm(monkeypatch, draft=None, error=None):
    llm, prov = fake_llm({"TailorDraft": draft or good_draft()}, error)
    monkeypatch.setattr("jobhunt.llm.get_llm", lambda: llm)
    monkeypatch.setattr("jobhunt.web.app.get_llm", lambda: llm)
    return prov


# ------------------------------------------------------------------ helpers
def sha(path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def revs(app_id):
    with db.connect() as c:
        return [r["rev"] for r in c.execute("SELECT rev FROM application_revisions WHERE application_id=? ORDER BY rev", (app_id,))]


# ------------------------------------------------------------------ store
def test_job_deletion_keeps_application_and_revision_is_stored(profile, job):
    a = store.add_revision(profile_id=profile.id, job_id=job.id, job_title="T", company="C", ats=None,
                           tailored=TailoredCV(headline="h", summary="s"), flags=[], verifier=None, fit_notes=cvs.FitNotes(),
                           master_hash=None, tmp_pdf=None, pdf_target=lambda r: Path("/nonexistent"))
    assert a.status == "draft" and a.current_rev == 1 and a.tailored.headline == "h" and a.cv_path is None and a.ats is None
    db.save_jobs(profile.id, [], removed=[db.get_job(job.id)])
    assert db.get_job(job.id) is None and store.get_application(a.id).job_title == "T"


def test_transitions_are_enforced(profile, job):
    a = store.add_revision(profile_id=profile.id, job_id=job.id, job_title="T", company="C", ats=None,
                           tailored=TailoredCV(headline="h", summary="s"), flags=[], verifier=None, fit_notes=cvs.FitNotes(),
                           master_hash=None, tmp_pdf=None, pdf_target=lambda r: Path("/nonexistent"))
    with pytest.raises(cvs.TransitionError):
        store.set_status(a.id, "submitted")  # draft -> submitted
    with pytest.raises(cvs.TransitionError):
        store.set_status(a.id, "draft")  # draft -> draft
    with pytest.raises(ValueError):
        store.set_status(a.id, "bogus")
    assert store.set_status(a.id, "withdrawn").status == "withdrawn"
    with pytest.raises(cvs.TransitionError):
        store.set_status(a.id, "approved")  # withdrawn -> approved
    assert store.set_status(a.id, "draft").status == "draft"  # "restaurer"


# ------------------------------------------------------------------ service
async def test_prepare_application_end_to_end_and_eligibility(profile, job, master):
    llm, prov = fake_llm({"TailorDraft": good_draft()})
    a = await cvs.prepare_application(profile.id, job.id, llm)
    assert a.status == "draft" and a.ats == "lever" and a.errors == [] and a.current_rev == 1
    assert Path(a.cv_path) == get_settings().applications_dir / str(job.id) / "rev-1" / "CV - Camille Durand - Exemple SAS.pdf"
    assert a.pdf_sha256 == sha(a.cv_path) and a.master_hash == master.compute_hash()
    with pymupdf.open(a.cv_path) as doc:
        assert len(doc) == 1
    assert a.tailored.headline == "Élève ingénieure - Recherche de stage (4e année / assistant ingénieur) en systèmes embarqués"
    assert [c["cls"] for c in prov.calls] == ["TailorDraft", "VerifyResult"]  # one tailor call + one verifier call
    assert a.verifier.ran and a.verifier.passed
    ok, reasons = cvs.is_eligible(a)
    assert not ok and reasons == ["CV non approuvé"]
    approved = cvs.approve(a.id, 1)
    assert approved.status == "approved" and approved.approved_rev == 1 and approved.approved_sha256 == a.pdf_sha256
    assert cvs.is_eligible(approved) == (True, [])


async def test_prepare_requires_confirmed_master_and_known_job(profile, job):
    llm, _ = fake_llm({"TailorDraft": good_draft()})
    with pytest.raises(cvs.CVError, match="profil maître"):
        await cvs.prepare_application(profile.id, job.id, llm)
    save_master(profile.id, sample_master())  # active but NOT confirmed
    with pytest.raises(cvs.CVError, match="non confirmé"):
        await cvs.prepare_application(profile.id, job.id, llm)
    assert store.list_applications() == []
    confirm_master(profile.id)
    with pytest.raises(cvs.CVError):
        await cvs.prepare_application(profile.id, 999, llm)


async def test_regenerate_makes_a_new_revision_of_the_same_application_and_keeps_old_files(profile, job, master):
    llm, _ = fake_llm({"TailorDraft": good_draft()})
    a1 = await cvs.prepare_application(profile.id, job.id, llm)
    approved = cvs.approve(a1.id, 1)
    old_hash = sha(a1.cv_path)
    a2 = await cvs.prepare_application(profile.id, job.id, llm)
    assert a2.id == a1.id and a2.current_rev == 2 and revs(a1.id) == [1, 2] and len(store.list_applications()) == 1
    assert a2.cv_path != a1.cv_path and "rev-2" in a2.cv_path and sha(a1.cv_path) == old_hash  # old PDF never overwritten
    assert a2.status == "draft" and a2.approved_rev is None and a2.approved_sha256 is None  # approval cleared
    assert cvs.is_eligible(a2)[0] is False and approved.approved_rev == 1


async def test_failed_regeneration_changes_nothing(profile, job, master, monkeypatch):
    llm, _ = fake_llm({"TailorDraft": good_draft()})
    a1 = await cvs.prepare_application(profile.id, job.id, llm)
    cvs.approve(a1.id, 1)
    bad, _ = fake_llm(error="boom")
    with pytest.raises(Exception):
        await cvs.prepare_application(profile.id, job.id, bad)
    monkeypatch.setattr(cvs, "fit_and_render", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("chromium down")))
    with pytest.raises(cvs.CVError, match="Rendu du PDF impossible"):
        await cvs.prepare_application(profile.id, job.id, llm)
    after = store.get_application(a1.id)
    assert after.status == "approved" and after.current_rev == 1 and revs(a1.id) == [1] and cvs.is_eligible(after)[0]
    assert sorted(p.name for p in (get_settings().applications_dir / str(job.id)).iterdir()) == ["rev-1"]


async def test_stale_edit_creates_nothing_and_leaves_no_file(profile, job, master):
    llm, _ = fake_llm({"TailorDraft": good_draft()})
    a = await cvs.prepare_application(profile.id, job.id, llm)
    await cvs.update_application(a.id, rev=1, llm=llm, summary="Résumé deux.")
    with pytest.raises(cvs.StaleRevision):
        await cvs.update_application(a.id, rev=1, llm=llm, summary="Périmé.")  # form was built on revision 1
    with pytest.raises(cvs.StaleRevision):
        cvs.approve(a.id, 1)
    assert revs(a.id) == [1, 2] and sorted(p.name for p in (get_settings().applications_dir / str(job.id)).iterdir()) == ["rev-1", "rev-2"]


async def test_add_revision_is_atomic_when_the_insert_fails(profile, job, master, monkeypatch):
    llm, _ = fake_llm({"TailorDraft": good_draft()})
    a = await cvs.prepare_application(profile.id, job.id, llm)
    real = store.FitNotes.model_dump_json

    def boom(self, *a, **k):
        raise RuntimeError("db write failed")

    monkeypatch.setattr(store.FitNotes, "model_dump_json", boom)
    with pytest.raises(RuntimeError):
        await cvs.prepare_application(profile.id, job.id, llm)
    monkeypatch.setattr(store.FitNotes, "model_dump_json", real)
    assert store.get_application(a.id).current_rev == 1 and revs(a.id) == [1]
    assert [p.name for p in (get_settings().applications_dir / str(job.id)).iterdir()] == ["rev-1"]  # moved file removed again


async def test_hard_errors_block_approval_unless_overridden_and_override_is_never_eligible(profile, job, master):
    draft = good_draft(master)
    draft.skill_groups[0].items.append("Anglais C1")
    llm, _ = fake_llm({"TailorDraft": draft})
    a = await cvs.prepare_application(profile.id, job.id, llm)
    assert [f.code for f in a.errors] == ["language_level_claim"]
    with pytest.raises(cvs.CVError, match="erreur"):
        cvs.approve(a.id, 1)
    assert store.get_application(a.id).status == "draft"
    ok = cvs.approve(a.id, 1, ignore_errors=True)
    assert ok.status == "approved" and ok.approval_override and "malgré 1 erreur" in ok.notes
    eligible, reasons = cvs.is_eligible(ok)
    assert not eligible and "approbation manuelle malgré des erreurs" in reasons and "1 erreur(s) bloquante(s)" in reasons


async def test_edit_fixes_error_then_approve_and_edit_unapproves(profile, job, master):
    draft = good_draft(master)
    draft.skill_groups[0].items.append("Anglais C1")
    llm, _ = fake_llm({"TailorDraft": draft})
    a = await cvs.prepare_application(profile.id, job.id, llm)
    ticked = {f"sk1|{i}" for i in (0, 1, 3)} | {"sk3|0", "sk3|1", "sk3|2"}
    new = await cvs.update_application(
        a.id, rev=1, llm=llm, headline="Titre modifié", summary="Nouveau résumé.", bullet_texts={(0, 0): "texte réécrit"},
        skills=ticked, extra_skills=set())
    assert new.current_rev == 2 and new.errors == [] and new.tailored.headline == "Titre modifié"
    assert new.tailored.projects[0].bullets[0].text == "texte réécrit" and "Anglais C1" not in str(new.tailored.skill_groups)
    with pymupdf.open(new.cv_path) as doc:
        assert len(doc) == 1 and "texte réécrit" in doc[0].get_text().replace("\n", " ")
    assert cvs.approve(a.id, 2).status == "approved"
    again = await cvs.update_application(a.id, rev=2, llm=llm, summary="Autre résumé.")
    assert again.status == "draft" and again.approved_rev is None and again.current_rev == 3


async def test_verifier_unsupported_claim_blocks_and_is_not_eligible(profile, job, master):
    llm, _ = fake_llm({"TailorDraft": good_draft(), "VerifyResult": reject(("projects[0].bullets[1]", "stage de 6 mois chez Exemple Industrie"))})
    a = await cvs.prepare_application(profile.id, job.id, llm)
    assert not a.verifier.passed
    errs = [f for f in a.errors if f.code == "verifier_hard_fact"]
    assert len(errs) == 1 and errs[0].location == "projects[0].bullets[1]" and "stage de 6 mois" in errs[0].message
    with pytest.raises(cvs.CVError):
        cvs.approve(a.id, 1)
    ok = cvs.approve(a.id, 1, ignore_errors=True)
    assert "vérification IA non réussie" in cvs.is_eligible(ok)[1]


async def test_verifier_embellishment_is_allowed_and_listed_for_interview_prep(profile, job, master, client):
    from cv_fakes import embellish
    llm, _ = fake_llm({"TailorDraft": good_draft(),
                       "VerifyResult": embellish(("projects[0].bullets[1]", "pilotage d'une équipe de 4 personnes"))})
    a = await cvs.prepare_application(profile.id, job.id, llm)
    assert a.verifier.passed and not a.errors
    assert [(f.code, f.hint) for f in a.preps if f.code == "verifier_embellishment"] == [("verifier_embellishment", "révise ce point")]
    ok = cvs.approve(a.id, 1)
    assert cvs.is_eligible(ok)[0], cvs.is_eligible(ok)[1]
    page = client.get(f"/applications/{a.id}").text
    assert "Préparation entretien" in page and "équipe de 4" in page


async def test_verifier_missing_verdict_fails_closed_and_unavailable_verifier_blocks(profile, job, master):
    from jobhunt.cv.verify import ItemVerdict, VerifyResult
    llm, _ = fake_llm({"TailorDraft": good_draft(), "VerifyResult": VerifyResult(items=[ItemVerdict(ref="summary", supported=True)])})
    a = await cvs.prepare_application(profile.id, job.id, llm)
    assert not a.verifier.passed and any("pas de verdict" in f.message for f in a.errors)
    prov = FakeProvider({"TailorDraft": good_draft()})

    async def structured(model_cls, **kw):
        if model_cls.__name__ == "VerifyResult":
            raise LLMError("secret", code="deadline")
        return good_draft()

    prov.structured = structured
    from jobhunt.llm import FailoverLLM
    b = await cvs.prepare_application(profile.id, job.id, FailoverLLM(providers=[prov]))
    assert b.verifier.ran is False and b.verifier.error == "unavailable"
    assert any(f.code == "verifier_not_run" for f in b.errors) and "secret" not in b.model_dump_json()
    assert "vérification IA non effectuée" in cvs.is_eligible(b)[1]


async def test_verifier_prefers_openai_and_records_it(profile, job, master):
    from jobhunt.llm import FailoverLLM
    ant = FakeProvider({"TailorDraft": good_draft()})
    ant.name = "anthropic"
    oai = FakeProvider({"TailorDraft": good_draft()})
    oai.name = "openai"
    from cv_fakes import pass_all
    ant.outputs["VerifyResult"] = oai.outputs["VerifyResult"] = pass_all
    a = await cvs.prepare_application(profile.id, job.id, FailoverLLM(providers=[ant, oai]))
    assert [c["cls"] for c in ant.calls] == ["TailorDraft"] and [c["cls"] for c in oai.calls] == ["VerifyResult"]
    assert a.verifier.provider == "openai"


async def test_cv_failover_can_be_disabled(profile, job, master, monkeypatch):
    from jobhunt.llm import FailoverLLM
    monkeypatch.setattr(get_settings(), "cv_allow_failover", False)
    ant = FakeProvider(error="down")
    ant.name = "anthropic"
    oai = FakeProvider({"TailorDraft": good_draft()})
    oai.name = "openai"
    with pytest.raises(Exception, match="anthropic: error"):
        await cvs.prepare_application(profile.id, job.id, FailoverLLM(providers=[ant, oai]))
    assert oai.calls == []  # the CV never reached the second provider


async def test_eligibility_needs_confirmed_unchanged_master_and_intact_pdf(profile, job, master):
    llm, _ = fake_llm({"TailorDraft": good_draft()})
    a = await cvs.prepare_application(profile.id, job.id, llm)
    a = cvs.approve(a.id, 1)
    assert cvs.is_eligible(a) == (True, [])
    # master edited by hand: unconfirmed
    save_master(profile.id, load_master(profile.id))
    assert "CV maître non confirmé" in cvs.is_eligible(a)[1]
    # re-confirmed after a content change: the CV was tailored from another master
    m = load_master(profile.id)
    m.summary += " Ajout."
    save_master(profile.id, m)
    confirm_master(profile.id)
    assert "CV maître modifié depuis la génération" in cvs.is_eligible(a)[1]
    # restore the master, tamper with the PDF
    save_master(profile.id, master.model_copy(update={"confirmed_at": None, "content_hash": None}))
    confirm_master(profile.id)
    assert cvs.is_eligible(a)[0]
    Path(a.cv_path).write_bytes(b"%PDF-tampered")
    assert "PDF modifié ou différent de celui approuvé" in cvs.is_eligible(a)[1]
    with pytest.raises(cvs.TransitionError):
        store.approve_revision(a.id, 1, file_sha256=store.sha256_file(a.cv_path))  # not a draft any more


async def test_approval_checks_the_file_hash(profile, job, master):
    llm, _ = fake_llm({"TailorDraft": good_draft()})
    a = await cvs.prepare_application(profile.id, job.id, llm)
    Path(a.cv_path).write_bytes(b"%PDF-tampered")
    with pytest.raises(cvs.ApprovalError, match="PDF"):
        cvs.approve(a.id, 1)
    assert store.get_application(a.id).status == "draft"


async def test_cv_that_does_not_fit_gets_no_pdf_and_a_blocking_flag(profile, job, master):
    draft = good_draft(master)
    draft.projects = draft.projects[:1]
    draft.projects[0].bullets = [draft.projects[0].bullets[0].model_copy(update={"text": "lecture de cinq capteurs infrarouges. " * 400})]
    llm, _ = fake_llm({"TailorDraft": draft})
    a = await cvs.prepare_application(profile.id, job.id, llm)
    assert a.cv_path is None and any(f.code == "no_fit" and "ne tient pas sur une page" in f.message for f in a.errors)
    with pytest.raises(cvs.CVError, match="PDF"):
        cvs.approve(a.id, 1, ignore_errors=True)  # an override cannot publish a missing PDF
    assert not (get_settings().applications_dir / str(job.id) / "rev-1").exists()
    # the user can edit; the untrimmed text is still there
    fixed = await cvs.update_application(a.id, rev=1, llm=llm, bullet_texts={(0, 0): "lecture de cinq capteurs infrarouges."})
    assert fixed.cv_path and not [f for f in fixed.errors if f.code == "no_fit"]


async def test_fact_check_runs_on_the_final_trimmed_content_and_trims_persist(profile, job, master):
    draft = good_draft(master)
    long = "lecture de cinq capteurs infrarouges et filtrage des mesures. " * 4
    for dp in draft.projects:
        dp.bullets += [dp.bullets[0].model_copy(update={"text": long, "label": f"Extra {i}"}) for i in range(8)]
    llm, prov = fake_llm({"TailorDraft": draft})
    a = await cvs.prepare_application(profile.id, job.id, llm)
    assert a.cv_path and not a.fit_notes.is_empty() and a.fit_notes.dropped_bullets
    kept = sum(len(p.bullets) for p in cvs.apply_fit(a.tailored, a.fit_notes).projects)
    assert kept < sum(len(p.bullets) for p in a.tailored.projects)  # the stored content is untrimmed
    sent = [i["ref"] for i in __import__("json").loads(prov.calls[1]["user"])["items"] if i["ref"] not in ("summary", "skill_groups")]
    assert len(sent) == kept  # the verifier saw exactly what is on the PDF
    b = await cvs.update_application(a.id, rev=1, llm=llm, summary="Résumé modifié.")
    assert set(map(tuple, a.fit_notes.dropped_bullets)) <= set(map(tuple, b.fit_notes.dropped_bullets))


def test_apply_edits_skill_selection_semantics():
    m = sample_master()
    from jobhunt.cv.tailor import build_tailored
    cv = build_tailored(good_draft(m), m, MergedJob(title="x", contract_type=ContractType.STAGE, description="systèmes embarqués"))
    out = cvs.apply_edits(cv, m, skills={"sk1|0", "sk2|4", "sk3|2"}, extra_skills={"Rust"}, group_labels={0: "Logiciel"})
    groups = {g.label: g.items for g in out.skill_groups}
    assert groups["Logiciel"] == ["C"]
    assert groups["Microcontrôleurs"] == ["PWM"]
    assert groups["Interfaces"] == ["I2C"]
    assert "Rust" not in str(groups) or groups.get("Autres") == ["Rust"]
    assert cv.skill_groups[0].items == ["C", "C++", "programmation embarquée"]
    assert cvs.apply_edits(cv, m, headline="  A   B ").headline == "A B"


async def test_pdf_path_containment(profile, job, master, tmp_path):
    llm, _ = fake_llm({"TailorDraft": good_draft()})
    a = await cvs.prepare_application(profile.id, job.id, llm)
    assert cvs.safe_pdf(a) == Path(a.cv_path).resolve()
    outside = tmp_path / "outside.pdf"
    outside.write_bytes(b"%PDF")
    for bad in (str(outside), str(get_settings().data_dir / "profiles" / "x.pdf"), str(Path(a.cv_path).with_suffix(".txt"))):
        assert cvs.safe_pdf(a.model_copy(update={"cv_path": bad})) is None
    link = get_settings().applications_dir / str(job.id) / "rev-1" / "link.pdf"
    link.symlink_to(outside)
    assert cvs.safe_pdf(a.model_copy(update={"cv_path": str(link)})) is None


# ------------------------------------------------------------------ web
@pytest.fixture
def client():
    GENERATING.clear()
    with TestClient(app) as c:
        yield c


def wait_done(client, job_id, tries=150):
    for _ in range(tries):
        r = client.get(f"/jobs/{job_id}/application/wait", follow_redirects=False)
        if r.status_code == 303:
            return r.headers["location"]
        if "a échoué" in r.text:
            return r
        time.sleep(0.1)
    raise AssertionError("generation did not finish")


def make_app(client, job_id) -> int:
    client.post(f"/jobs/{job_id}/application")
    return int(wait_done(client, job_id).rsplit("/", 1)[1])


def follow(client, r):
    """Follow a redirect that carries a one-shot server-side flash message."""
    assert r.status_code == 303
    return client.get(r.headers["location"]).text


def test_web_generate_wait_view_update_approve(client, profile, job, master, monkeypatch):
    patch_llm(monkeypatch)
    page = client.get(f"/profiles/{profile.id}/shortlist?min_score=0").text
    assert "Préparer le CV" in page and f'formaction="/jobs/{job.id}/application"' in page
    r = client.post(f"/jobs/{job.id}/application", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/jobs/{job.id}/application/wait"
    loc = wait_done(client, job.id)
    assert loc.startswith("/applications/")
    app_id = int(loc.rsplit("/", 1)[1])

    page = client.get(loc).text
    assert "Recherche de stage (4e année" in page and "Mots-clés couverts" in page and "Expérience AUTOSAR" in page
    assert f"/applications/{app_id}/preview" in page and "Télécharger le PDF" in page and "Aucun problème détecté" in page
    assert 'class="flag' not in page and "Vérification IA :" in page and "réussie" in page and "Pas éligible" in page
    assert 'name="rev" value="1"' in page
    prev = client.get(f"/applications/{app_id}/preview")
    assert prev.status_code == 200 and "Camille Durand" in prev.text and "data:image" not in prev.text
    pdf = client.get(f"/applications/{app_id}/pdf")
    assert pdf.headers["content-type"] == "application/pdf" and pdf.content.startswith(b"%PDF") and "CV%20-%20Camille%20Durand" in pdf.headers["content-disposition"]
    assert f'href="/applications/{app_id}"' in client.get(f"/profiles/{profile.id}/shortlist").text
    assert "Brouillon" in client.get("/applications").text
    r = client.post(f"/jobs/{job.id}/application", follow_redirects=False)
    assert r.headers["location"] == f"/applications/{app_id}"  # existing application: no new LLM call

    r = client.post(f"/applications/{app_id}/update", data={
        "rev": "1", "headline": "Titre édité", "summary": "Résumé édité.", "b_0_0": "point édité", "l_0_0": "Libellé",
        "gl_0": "Firmware", "skills_present": "1", "skill": ["sk1|0", "sk3|0"]}, follow_redirects=False)
    assert r.status_code == 303 and "?f=" in r.headers["location"] and "notice" not in r.headers["location"]
    assert "nouvelle version" in follow(client, r)
    a = store.get_application(app_id)
    assert a.current_rev == 2 and a.tailored.headline == "Titre édité" and a.tailored.projects[0].bullets[0].text == "point édité"
    assert [i for g in a.tailored.skill_groups for i in g.items] == ["C", "UART"]
    # approving the page built on revision 1 is refused; revision 2 works
    r = client.post(f"/applications/{app_id}/approve", data={"rev": "1"}, follow_redirects=False)
    assert "a changé" in follow(client, r) and store.get_application(app_id).status == "draft"
    assert client.post(f"/applications/{app_id}/approve", follow_redirects=False).status_code == 303  # missing rev
    assert store.get_application(app_id).status == "draft"
    r = client.post(f"/applications/{app_id}/approve", data={"rev": "2"}, follow_redirects=False)
    assert "CV approuvé" in follow(client, r) and store.get_application(app_id).approved_rev == 2
    page = client.get(f"/applications/{app_id}").text
    assert "Approuvée" in page and "Éligible à l'envoi automatique" in page
    stale = client.post(f"/applications/{app_id}/update", data={"rev": "1", "summary": "x"}, follow_redirects=False)
    assert "a changé" in follow(client, stale) and store.get_application(app_id).current_rev == 2


def test_flash_messages_are_one_shot_and_never_in_the_url(client, profile, job, master, monkeypatch):
    patch_llm(monkeypatch)
    app_id = make_app(client, job.id)
    r = client.post(f"/applications/{app_id}/approve", data={"rev": "9"}, follow_redirects=False)
    loc = r.headers["location"]
    assert loc.startswith(f"/applications/{app_id}?f=") and "a%20chang" not in loc and "changé" not in loc
    assert "a changé" in client.get(loc).text and "a changé" not in client.get(loc).text


def test_web_errors_block_approval_unless_ignored_with_exact_value(client, profile, job, master, monkeypatch):
    draft = good_draft(master)
    draft.skill_groups[0].items.append("Anglais C1")
    patch_llm(monkeypatch, draft)
    app_id = make_app(client, job.id)
    page = client.get(f"/applications/{app_id}").text
    assert 'class="flag error"' in page and "C1" in page and "Ignorer et approuver" in page and "disabled" in page
    r = client.post(f"/applications/{app_id}/approve", data={"rev": "1"}, follow_redirects=False)
    assert "erreur" in follow(client, r) and store.get_application(app_id).status == "draft"
    for bad in ("true", "yes", "0", "ignore"):
        client.post(f"/applications/{app_id}/approve", data={"rev": "1", "ignore": bad})
        assert store.get_application(app_id).status == "draft"
    client.post(f"/applications/{app_id}/approve", data={"rev": "1", "ignore": "on"})
    a = store.get_application(app_id)
    assert a.status == "approved" and a.approval_override
    assert "Pas éligible" in client.get(f"/applications/{app_id}").text


def test_web_regenerate_adds_a_revision_and_failures_change_nothing(client, profile, job, master, monkeypatch):
    patch_llm(monkeypatch, error="clé invalide SECRET-CV-TEXT")
    client.post(f"/jobs/{job.id}/application")
    r = wait_done(client, job.id)
    assert "La génération a échoué" in r.text and "Réessayer" in r.text and "SECRET-CV-TEXT" not in r.text
    GENERATING.clear()
    patch_llm(monkeypatch)
    first = make_app(client, job.id)
    client.post(f"/applications/{first}/approve", data={"rev": "1"})
    GENERATING.clear()
    patch_llm(monkeypatch, error="down")
    client.post(f"/jobs/{job.id}/application", data={"regenerate": "1"})
    assert "La génération a échoué" in wait_done(client, job.id).text
    a = store.get_application(first)
    assert a.status == "approved" and a.current_rev == 1  # the failed regeneration changed nothing
    GENERATING.clear()
    patch_llm(monkeypatch)
    client.post(f"/jobs/{job.id}/application", data={"regenerate": "1"})
    second = int(wait_done(client, job.id).rsplit("/", 1)[1])
    a = store.get_application(first)
    assert second == first and a.current_rev == 2 and a.status == "draft" and revs(first) == [1, 2]


def test_web_unexpected_exception_is_not_shown(client, profile, job, master, monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("SECRET-CV-TEXT")

    monkeypatch.setattr(cvs, "prepare_application", boom)
    client.post(f"/jobs/{job.id}/application")
    r = wait_done(client, job.id)
    assert "SECRET-CV-TEXT" not in r.text and "journaux" in r.text


def test_web_withdraw_restore_and_transitions(client, profile, job, master, monkeypatch):
    patch_llm(monkeypatch)
    app_id = make_app(client, job.id)
    r = client.post(f"/applications/{app_id}/withdraw", follow_redirects=False)
    assert r.headers["location"].startswith("/applications?f=") and store.get_application(app_id).status == "withdrawn"
    page = client.get(f"/applications/{app_id}").text
    assert "Restaurer" in page and "Retirer" not in page
    r = client.post(f"/applications/{app_id}/approve", data={"rev": "1"}, follow_redirects=False)
    assert "Seul un brouillon" in follow(client, r)
    assert "withdrawn" == store.get_application(app_id).status
    client.post(f"/applications/{app_id}/restore")
    assert store.get_application(app_id).status == "draft"
    assert "impossible" in follow(client, client.post(f"/applications/{app_id}/restore", follow_redirects=False))
    # a withdrawn application is reused (new revision, restored) rather than duplicated
    client.post(f"/applications/{app_id}/withdraw")
    GENERATING.clear()
    client.post(f"/jobs/{job.id}/application", data={"regenerate": "1"})
    assert int(wait_done(client, job.id).rsplit("/", 1)[1]) == app_id and store.get_application(app_id).status == "draft"


def test_web_pdf_download_is_contained(client, profile, job, master, monkeypatch, tmp_path):
    patch_llm(monkeypatch)
    app_id = make_app(client, job.id)
    outside = tmp_path / "secret.pdf"
    outside.write_bytes(b"%PDF-secret")
    with db.connect() as c:
        c.execute("UPDATE application_revisions SET pdf_path=? WHERE application_id=?", (str(outside), app_id))
    assert client.get(f"/applications/{app_id}/pdf").status_code == 404


def test_job_links_only_http_and_https(client, profile, master, monkeypatch):
    from jobhunt.dedupe import Sighting
    from datetime import UTC, datetime
    j = MergedJob(title="Stage X", company="Y", contract_type=ContractType.STAGE, origin_url="javascript:alert(1)", score=90,
                  sightings=[Sighting(source="wttj", source_id="1", url="data:text/html,x", seen_at=datetime.now(UTC)),
                             Sighting(source="indeed", source_id="2", url="https://example.test/job", seen_at=datetime.now(UTC))])
    db.save_jobs(profile.id, [j])
    page = client.get(f"/profiles/{profile.id}/shortlist?min_score=0").text
    assert "javascript:" not in page and "data:text/html" not in page and "https://example.test/job" in page
    patch_llm(monkeypatch)
    app_id = make_app(client, j.id)
    page = client.get(f"/applications/{app_id}").text
    assert "javascript:" not in page and "data:text/html" not in page and "https://example.test/job" in page


def test_web_missing_or_unconfirmed_master_reports_french_error(client, profile, job, monkeypatch):
    patch_llm(monkeypatch)
    client.post(f"/jobs/{job.id}/application")
    assert "profil maître" in wait_done(client, job.id).text
    GENERATING.clear()
    save_master(profile.id, sample_master())
    client.post(f"/jobs/{job.id}/application")
    assert "non confirmé" in wait_done(client, job.id).text


def test_web_404s(client):
    for path in ("/applications/99", "/applications/99/preview", "/applications/99/pdf", "/jobs/99/application/wait"):
        assert client.get(path).status_code == 404
    assert client.post("/jobs/99/application").status_code == 404
    assert client.post("/applications/99/restore").status_code == 404


def test_web_master_cv_view_edit_import_and_confirm(client, profile, master, monkeypatch, tmp_path):
    pdir = get_settings().profiles_dir / str(profile.id)
    page = client.get(f"/profiles/{profile.id}/cv").text
    assert "Robot suiveur de ligne" in page and "master.yaml" in page and "CV maître confirmé" in page
    assert "CV maître" in client.get(f"/profiles/{profile.id}").text
    assert "Station météo connectée" in client.get(f"/profiles/{profile.id}/cv/preview").text
    text = page.split('spellcheck="false">')[1].split("</textarea>")[0]
    import html
    edited = html.unescape(text).replace("Courant.", "Bilingue.")
    r = client.post(f"/profiles/{profile.id}/cv", data={"text": edited}, follow_redirects=False)
    assert r.status_code == 303 and "Bilingue." in (pdir / "master.yaml").read_text()
    page = client.get(f"/profiles/{profile.id}/cv").text  # a save unconfirms
    assert "CV maître non confirmé" in page and "Confirmer ce CV maître" in page
    bad = client.post(f"/profiles/{profile.id}/cv", data={"text": "projects:\n  - dates: 1\n"})
    assert bad.status_code == 422 and "invalide" in bad.text and "dates: 1" in bad.text
    # re-import through the UI (fake LLM): staged, the active master is untouched until confirmation
    from test_cv_master_import import imported, make_pdf
    llm, _ = fake_llm({"ImportedMaster": imported()})
    monkeypatch.setattr("jobhunt.web.app.get_llm", lambda: llm)
    pdf = make_pdf(tmp_path / "cv.pdf")
    r = client.post(f"/profiles/{profile.id}/cv/import", files={"file": ("cv.pdf", pdf.read_bytes(), "application/pdf")}, follow_redirects=False)
    assert r.status_code == 303 and "?f=" in r.headers["location"] and "import" not in r.headers["location"]
    assert "Station météo connectée" in (pdir / "master.yaml").read_text() and (pdir / "master.pending.yaml").is_file()
    page = client.get(f"/profiles/{profile.id}/cv").text
    assert "Import en attente de confirmation" in page and "<mark>identity.name</mark>" in page and "Confirmer ce CV maître" in page
    assert not (pdir / "photo.jpg").exists() and (pdir / "pending-photo.jpg").is_file()
    client.post(f"/profiles/{profile.id}/cv/confirm")
    assert "Station météo" not in (pdir / "master.yaml").read_text() and load_master(profile.id).is_confirmed
    assert (pdir / "photo.jpg").is_file() and not (pdir / "master.pending.yaml").exists()
    assert "CV maître confirmé" in client.get(f"/profiles/{profile.id}/cv").text


def test_web_discard_import_and_import_error_is_sanitized(client, profile, master, monkeypatch, tmp_path):
    from test_cv_master_import import imported
    llm, _ = fake_llm({"ImportedMaster": imported()})
    monkeypatch.setattr("jobhunt.web.app.get_llm", lambda: llm)
    r = client.post(f"/profiles/{profile.id}/cv/import", files={"file": ("cv.txt", b"Camille Durand\nCV", "text/plain")}, follow_redirects=False)
    assert (get_settings().profiles_dir / str(profile.id) / "master.pending.yaml").is_file()
    client.post(f"/profiles/{profile.id}/cv/discard")
    assert not (get_settings().profiles_dir / str(profile.id) / "master.pending.yaml").exists()
    bad, _ = fake_llm(error="SECRET-CV-TEXT")
    monkeypatch.setattr("jobhunt.web.app.get_llm", lambda: bad)
    r = client.post(f"/profiles/{profile.id}/cv/import", files={"file": ("cv.txt", b"CV", "text/plain")}, follow_redirects=False)
    assert "SECRET" not in r.headers["location"] and "SECRET" not in follow(client, r)


def test_nav_link(client):
    assert 'href="/applications"' in client.get("/").text and "Candidatures" in client.get("/").text


# ------------------------------------------------------------------ CLI
def test_cli_cv_commands(profile, job, master, monkeypatch, tmp_path):
    patch_llm(monkeypatch)
    runner = CliRunner()
    r = runner.invoke(cli_app, ["cv", "tailor", str(job.id), "--profile", str(profile.id)])
    assert r.exit_code == 0, r.output
    assert "Recherche de stage" in r.output and "Mots-clés ajoutés" in r.output and "0 erreur(s)" in r.output and "PDF :" in r.output
    assert "vérification IA : réussie" in r.output and "Éligible à l'envoi automatique : non — CV non approuvé" in r.output
    r = runner.invoke(cli_app, ["cv", "list"])
    assert r.exit_code == 0 and "draft" in r.output
    assert runner.invoke(cli_app, ["cv", "tailor", "999"]).exit_code != 0
    assert runner.invoke(cli_app, ["cv", "tailor", str(job.id), "--profile", "42"]).exit_code != 0


def test_cli_cv_tailor_errors(profile, job, monkeypatch):
    runner = CliRunner()
    patch_llm(monkeypatch)
    r = runner.invoke(cli_app, ["cv", "tailor", str(job.id)])  # no master yet
    assert r.exit_code == 1 and "profil maître" in r.output
    save_master(profile.id, sample_master())
    r = runner.invoke(cli_app, ["cv", "tailor", str(job.id)])  # not confirmed
    assert r.exit_code == 1 and "non confirmé" in r.output
    confirm_master(profile.id)
    patch_llm(monkeypatch, error="boom")
    r = runner.invoke(cli_app, ["cv", "tailor", str(job.id)])
    assert r.exit_code == 1 and "Appel IA impossible" in r.output


def test_cli_cv_tailor_exit_code_2_on_errors(profile, job, master, monkeypatch):
    draft = good_draft(master)
    draft.skill_groups[0].items.append("Anglais C1")
    patch_llm(monkeypatch, draft)
    r = CliRunner().invoke(cli_app, ["cv", "tailor", str(job.id)])
    assert r.exit_code == 2 and "ERREUR" in r.output and "C1" in r.output


def test_cli_cv_import_text_stages_a_pending_master(profile, monkeypatch, tmp_path):
    from test_cv_master_import import CV_LINES, imported
    from jobhunt.cv.master import load_pending
    llm, _ = fake_llm({"ImportedMaster": imported()})
    monkeypatch.setattr("jobhunt.llm.get_llm", lambda: llm)
    txt = tmp_path / "cv.txt"
    txt.write_text("\n".join(CV_LINES), encoding="utf-8")
    runner = CliRunner()
    r = runner.invoke(cli_app, ["cv", "import", str(profile.id), "--text", str(txt), "--stage-qualifier", "4e année"])
    assert r.exit_code == 0, r.output
    assert "1 groupes de compétences" in r.output and "mot pour mot" in r.output and "À CONFIRMER" in r.output
    assert load_master(profile.id) is None and load_pending(profile.id).identity.stage_qualifier == "4e année"
    assert runner.invoke(cli_app, ["cv", "import", str(profile.id)]).exit_code != 0
    assert runner.invoke(cli_app, ["cv", "import", "99", "--text", str(txt)]).exit_code != 0
