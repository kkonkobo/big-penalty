"""service.auto_approve + store.mark_submitted / mark_failed + approved_by bookkeeping."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from jobhunt import db
from jobhunt.cv import service as cvs
from jobhunt.cv import store
from jobhunt.cv.master import load_master, save_master

from apply_cv_helpers import ERROR, make_app, make_job
from cv_fakes import sample_master, save_confirmed


@pytest.fixture
def master(profile):
    return save_confirmed(profile.id)


@pytest.fixture
def job(profile, master):
    return make_job(profile)


def test_auto_approve_clean_draft_is_approved_by_auto_and_eligible(profile, job, master, tmp_path):
    a = make_app(profile, job, tmp_path, master_hash=master.content_hash)
    assert a.approved_by is None and a.submitted_at is None
    b = cvs.auto_approve(a.id)
    assert b.status == "approved" and b.approved_by == "auto" and b.approved_rev == 1 and not b.approval_override
    assert cvs.is_eligible(b) == (True, [])
    assert store.get_application(a.id).approved_by == "auto"


def test_manual_approval_records_user_and_new_revision_clears_it(profile, job, master, tmp_path):
    a = make_app(profile, job, tmp_path, master_hash=master.content_hash)
    assert cvs.approve(a.id, 1).approved_by == "user"
    again = store.add_revision(profile_id=profile.id, job_id=job.id, job_title=None, company=None, ats=None, tailored=a.tailored,
                               flags=[], verifier=a.verifier, fit_notes=a.fit_notes, master_hash=a.master_hash, tmp_pdf=None,
                               pdf_target=lambda r: Path("/nonexistent"), app_id=a.id, base_rev=1)
    assert again.status == "draft" and again.approved_by is None


def test_leaving_approved_clears_approved_by(profile, job, master, tmp_path):
    a = cvs.auto_approve(make_app(profile, job, tmp_path, master_hash=master.content_hash).id)
    assert store.set_status(a.id, "draft").approved_by is None
    b = cvs.auto_approve(a.id)
    assert store.set_status(b.id, "withdrawn").approved_by is None


@pytest.mark.parametrize("kw, text", [
    ({"flags": [ERROR]}, "erreur(s) bloquante(s)"),
    ({"verifier": None}, "vérification IA non effectuée"),
    ({"with_pdf": False}, "PDF absent"),
])
def test_auto_approve_refuses_unclean_revisions(profile, job, master, tmp_path, kw, text):
    a = make_app(profile, job, tmp_path, master_hash=master.content_hash, **kw)
    with pytest.raises(cvs.ApprovalError, match=re.escape(text)):
        cvs.auto_approve(a.id)
    b = store.get_application(a.id)
    assert b.status == "draft" and b.approved_by is None and not b.approval_override


def test_auto_approve_refuses_failed_verifier(profile, job, master, tmp_path):
    from jobhunt.cv.verify import VerifierReport
    a = make_app(profile, job, tmp_path, master_hash=master.content_hash, verifier=VerifierReport(ran=True, passed=False))
    with pytest.raises(cvs.ApprovalError, match="vérification IA non réussie"):
        cvs.auto_approve(a.id)


def test_auto_approve_refuses_unconfirmed_or_changed_master(profile, job, master, tmp_path):
    a = make_app(profile, job, tmp_path, master_hash=master.content_hash)
    m = load_master(profile.id)
    m.summary += " Ajout."
    save_master(profile.id, m)  # saving unconfirms
    with pytest.raises(cvs.ApprovalError, match="CV maître non confirmé"):
        cvs.auto_approve(a.id)
    save_confirmed(profile.id, m)  # confirmed again but with a different hash than the revision was tailored from
    with pytest.raises(cvs.ApprovalError, match="modifié depuis la génération"):
        cvs.auto_approve(a.id)
    assert store.get_application(a.id).status == "draft"


def test_auto_approve_refuses_tampered_pdf(profile, job, master, tmp_path):
    a = make_app(profile, job, tmp_path, master_hash=master.content_hash)
    Path(a.cv_path).write_bytes(b"%PDF-tampered")
    with pytest.raises(cvs.ApprovalError, match="PDF modifié"):
        cvs.auto_approve(a.id)


def test_auto_approve_refuses_pdf_outside_applications_dir(profile, job, master, tmp_path):
    a = make_app(profile, job, tmp_path, master_hash=master.content_hash)
    outside = tmp_path / "outside.pdf"
    outside.write_bytes(Path(a.cv_path).read_bytes())
    with db.connect() as c:
        c.execute("UPDATE application_revisions SET pdf_path=? WHERE application_id=?", (str(outside), a.id))
    with pytest.raises(cvs.ApprovalError, match="PDF absent"):
        cvs.auto_approve(a.id)


def test_auto_approve_only_drafts_and_unknown_ids(profile, job, master, tmp_path):
    a = make_app(profile, job, tmp_path, master_hash=master.content_hash)
    cvs.auto_approve(a.id)
    with pytest.raises(cvs.ApprovalError, match="seul un brouillon"):
        cvs.auto_approve(a.id)  # already approved
    with pytest.raises(cvs.CVError, match="introuvable"):
        cvs.auto_approve(999)


def test_manual_override_stays_non_eligible_and_auto_approve_never_overrides(profile, job, master, tmp_path):
    a = make_app(profile, job, tmp_path, master_hash=master.content_hash, flags=[ERROR])
    ok = cvs.approve(a.id, 1, ignore_errors=True)
    assert ok.approval_override and ok.approved_by == "user"
    eligible, reasons = cvs.is_eligible(ok)
    assert not eligible and "approbation manuelle malgré des erreurs" in reasons


def test_is_eligible_of_a_draft_is_unchanged(profile, job, master, tmp_path):
    a = make_app(profile, job, tmp_path, master_hash=master.content_hash)
    assert cvs.is_eligible(a) == (False, ["CV non approuvé"])


# ------------------------------------------------------------------ mark_submitted / mark_failed
def test_mark_submitted_requires_approval_of_the_exact_current_revision(profile, job, master, tmp_path):
    a = make_app(profile, job, tmp_path, master_hash=master.content_hash)
    with pytest.raises(cvs.TransitionError):
        store.mark_submitted(a.id, 1)  # still a draft
    cvs.auto_approve(a.id)
    with pytest.raises(cvs.StaleRevision):
        store.mark_submitted(a.id, 2)
    s = store.mark_submitted(a.id, 1)
    assert s.status == "submitted" and s.submitted_at is not None and s.approved_rev == 1 and s.approved_by == "auto"
    for call in (lambda: store.mark_submitted(a.id, 1), lambda: store.set_status(a.id, "draft"),
                 lambda: store.set_status(a.id, "withdrawn"), lambda: store.mark_failed(a.id, "x")):
        with pytest.raises(cvs.TransitionError):
            call()  # a submitted application is immutable
    with pytest.raises(cvs.TransitionError, match="déjà envoyée"):
        store.add_revision(profile_id=profile.id, job_id=job.id, job_title=None, company=None, ats=None, tailored=a.tailored,
                           flags=[], verifier=None, fit_notes=a.fit_notes, master_hash=None, tmp_pdf=None,
                           pdf_target=lambda r: Path("/nonexistent"), app_id=a.id)


def test_mark_submitted_unknown_application(profile):
    with pytest.raises(cvs.CVError, match="introuvable"):
        store.mark_submitted(404, 1)


def test_mark_failed_only_from_approved_and_keeps_the_note(profile, job, master, tmp_path):
    a = make_app(profile, job, tmp_path, master_hash=master.content_hash)
    with pytest.raises(cvs.TransitionError):
        store.mark_failed(a.id, "boom")  # draft -> failed is not allowed
    cvs.auto_approve(a.id)
    f = store.mark_failed(a.id, "Le formulaire a refusé le CV")
    assert f.status == "failed" and f.notes == "Le formulaire a refusé le CV" and f.approved_rev is None and f.approved_by is None
    assert store.set_status(a.id, "draft").status == "draft"  # failed -> draft stays possible
