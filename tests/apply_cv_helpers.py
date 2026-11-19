"""Build applications / jobs for the apply tests without a browser or an LLM (fictional candidate only)."""

from __future__ import annotations

from datetime import datetime, timedelta, UTC
from pathlib import Path

from jobhunt import db
from jobhunt.cv import service as cvs
from jobhunt.cv import store
from jobhunt.cv.check import Flag
from jobhunt.cv.render import FitNotes
from jobhunt.cv.store import Application
from jobhunt.cv.tailored import TailoredCV
from jobhunt.cv.verify import VerifierReport
from jobhunt.dedupe import MergedJob, Sighting
from jobhunt.models import ContractType

ERROR = Flag(level="error", code="language_level_claim", location="skills", message="niveau de langue non attesté")
OK_VERIFIER = VerifierReport(ran=True, passed=True, provider="fake", model="fake")


def make_job(profile, *, title="Stage ingénieur logiciel embarqué (H/F)", company="Exemple SAS", score=88.0,
             url="https://careers.exemple.test/jobs/42", age_days=1, contract=ContractType.STAGE, source="linkedin", **kw) -> MergedJob:
    j = MergedJob(title=title, company=company, location="Toulouse (31)", contract_type=contract, origin_url=url,
                  origin_ats="lever", origin_confidence=0.9, score=score, posted_at=datetime.now(UTC) - timedelta(days=age_days),
                  sightings=[Sighting(source=source, source_id=f"{title}-{company}", url="https://example.test/x")], **kw)
    db.save_jobs(profile.id, [j])
    return j


def make_app(profile, job, tmp_path: Path, *, master_hash: str | None, flags: list[Flag] | None = None,
             verifier: VerifierReport | None = OK_VERIFIER, with_pdf: bool = True) -> Application:
    """A draft application with ONE revision whose PDF is a tiny file inside applications_dir (no render, no LLM)."""
    tmp = tmp_path / "cv-tmp.pdf"
    tmp.write_bytes(b"%PDF-1.4 fake one page")
    from jobhunt.cv.master import load_master
    master = load_master(profile.id)
    return store.add_revision(
        profile_id=profile.id, job_id=job.id, job_title=job.title, company=job.company, ats=job.origin_ats,
        tailored=TailoredCV(headline="h", summary="s"), flags=flags or [], verifier=verifier, fit_notes=FitNotes(),
        master_hash=master_hash, tmp_pdf=tmp if with_pdf else None,
        pdf_target=lambda rev: cvs.revision_pdf_path(master, job.company, job.id, rev))
