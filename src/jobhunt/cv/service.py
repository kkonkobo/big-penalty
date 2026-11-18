"""Orchestration: tailor -> fit one page -> fact check (rules + LLM verifier) on the FINAL content -> immutable revision;
edits (new revision); approval bound to a revision; eligibility for automatic submission."""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from jobhunt import db
from jobhunt.config import get_settings
from jobhunt.cv import store
from jobhunt.cv.check import Flag, blocking, check
from jobhunt.cv.errors import ApprovalError, CVError, StaleRevision, TransitionError  # noqa: F401 (re-exported)
from jobhunt.cv.master import Master, MasterError, load_master, norm_text, photo_path
from jobhunt.cv.render import FitNotes, apply_fit, fit_and_render, render_html, safe_filename
from jobhunt.cv.store import Application
from jobhunt.cv.tailor import tailor
from jobhunt.cv.tailored import TailoredCV, TailoredSkillGroup
from jobhunt.cv.verify import VerifierReport, verify

NO_FIT_FLAG = Flag(level="error", code="no_fit", location="layout",
                   message="Le CV ne tient pas sur une page : raccourcir le texte (aucun PDF produit)")


def application_dir(job_id: int) -> Path:
    return get_settings().applications_dir / str(job_id)


def revision_pdf_path(master: Master, company: str | None, job_id: int, rev: int) -> Path:
    """Every revision has its own directory: a published PDF is never overwritten."""
    return application_dir(job_id) / f"rev-{rev}" / safe_filename("CV", master.identity.name, company or "")


def run_check(tailored: TailoredCV, master: Master) -> list[Flag]:
    return check(tailored, master, extra_allowed_text=master.identity.stage_qualifier or "")


def _load(profile_id: int, *, require_confirmed: bool = False) -> tuple[Master, Path | None]:
    try:
        master = load_master(profile_id)
    except MasterError as e:
        raise CVError(str(e)) from e
    if master is None:
        raise CVError(f"Pas de profil maître pour le profil {profile_id} : lancer `big-penalty cv import {profile_id} --pdf CV.pdf`.")
    if require_confirmed and not master.is_confirmed:
        raise CVError("CV maître non confirmé : le vérifier et cliquer « Confirmer ce CV maître » "
                      f"(/profiles/{profile_id}/cv) avant de générer un CV sur mesure.")
    return master, photo_path(profile_id, master)


async def _build_revision(master: Master, photo: Path | None, untrimmed: TailoredCV, notes: FitNotes, llm, *, profile_id: int,
                          job_id: int, job_title: str | None, company: str | None, ats: str | None,
                          app_id: int | None = None, base_rev: int | None = None) -> Application:
    """Fit -> check the exact final content -> verify it -> publish atomically. A render crash raises (nothing is stored);
    a CV that cannot fit is stored WITHOUT a PDF and flagged."""
    with tempfile.TemporaryDirectory(prefix="cv-") as tmp:
        tmp_pdf = Path(tmp) / "cv.pdf"
        try:
            fit = await asyncio.to_thread(fit_and_render, untrimmed, master, tmp_pdf, photo=photo, notes=notes)
        except Exception as e:  # noqa: BLE001 - browser problems: keep the previous revision untouched
            raise CVError(f"Rendu du PDF impossible ({type(e).__name__}) : aucune nouvelle version créée") from e
        flags = run_check(fit.final, master)  # AFTER trimming, on what is on the PDF
        report = await verify(fit.final, master, llm)
        flags += report.flags()
        if not fit.fits:
            flags.append(NO_FIT_FLAG)
        return store.add_revision(
            profile_id=profile_id, job_id=job_id, job_title=job_title, company=company, ats=ats, tailored=untrimmed,
            flags=flags, verifier=report, fit_notes=fit.notes, master_hash=master.compute_hash(),
            tmp_pdf=tmp_pdf if fit.fits else None, pdf_target=lambda rev: revision_pdf_path(master, company, job_id, rev),
            app_id=app_id, base_rev=base_rev)


async def prepare_application(profile_id: int, job_id: int, llm=None) -> Application:
    """Generate (one tailor call + one verifier call) a tailored CV for the job: a NEW REVISION of the job's application
    (created on first use). Needs a confirmed master. On any failure nothing is stored and the previous revision stays."""
    from jobhunt import llm as llm_module

    profile = db.get_profile(profile_id)
    job = db.get_job(job_id)
    if profile is None or job is None or job.profile_id != profile_id:
        raise CVError("Profil ou offre introuvable")
    master, photo = _load(profile_id, require_confirmed=True)
    own = llm is None
    llm = llm or llm_module.get_llm()
    try:
        tailored = await tailor(master, job, profile, llm)
        return await _build_revision(master, photo, tailored, FitNotes(), llm, profile_id=profile_id, job_id=job_id,
                                     job_title=job.title, company=job.company, ats=job.origin_ats)
    finally:
        if own:
            await llm.aclose()


def apply_edits(tailored: TailoredCV, master: Master, *, headline: str | None = None, summary: str | None = None,
                bullet_texts: dict[tuple[int, int], str] | None = None,
                bullet_labels: dict[tuple[int, int], str] | None = None,
                skills: set[str] | None = None, extra_skills: set[str] | None = None,
                group_labels: dict[int, str] | None = None) -> TailoredCV:
    """Return an edited copy. `skills` = checked master items as "<master group id>|<index>"; `extra_skills` = checked
    items that are not in the master (kept only if still ticked). skills=None leaves the skills untouched."""
    cv = tailored.model_copy(deep=True)
    if headline is not None:
        cv.headline = norm_text(headline)
    if summary is not None:
        cv.summary = norm_text(summary)
    for (pi, bi), text in (bullet_texts or {}).items():
        if pi < len(cv.projects) and bi < len(cv.projects[pi].bullets) and norm_text(text):
            cv.projects[pi].bullets[bi].text = norm_text(text)
    for (pi, bi), label in (bullet_labels or {}).items():
        if pi < len(cv.projects) and bi < len(cv.projects[pi].bullets):
            cv.projects[pi].bullets[bi].label = norm_text(label).rstrip(":").strip()
    for gi, label in (group_labels or {}).items():
        if gi < len(cv.skill_groups) and norm_text(label):
            cv.skill_groups[gi].label = norm_text(label)
    if skills is not None:
        origin: dict[str, tuple[str, str]] = {}  # item key -> (master group id, label)
        wanted: dict[str, str] = {}
        for g in master.skill_groups:
            for idx, item in enumerate(g.items):
                origin.setdefault(norm_text(item).lower(), (g.id, g.label))
                if f"{g.id}|{idx}" in skills:
                    wanted[norm_text(item).lower()] = item
        for x in extra_skills or set():
            wanted.setdefault(norm_text(x).lower(), x)
        groups: list[TailoredSkillGroup] = []
        placed: set[str] = set()
        for g in cv.skill_groups:  # keep existing groups/order, drop unticked items
            keep = [i for i in g.items if norm_text(i).lower() in wanted and norm_text(i).lower() not in placed]
            placed.update(norm_text(i).lower() for i in keep)
            groups.append(TailoredSkillGroup(label=g.label, items=keep))
        by_master: dict[str, TailoredSkillGroup] = {}
        for g in groups:
            for i in g.items:
                by_master.setdefault(origin.get(norm_text(i).lower(), ("", ""))[0], g)
        for key, item in wanted.items():  # newly ticked items go to the group holding their master siblings
            if key in placed:
                continue
            gid, label = origin.get(key, ("", "Autres"))
            target = by_master.get(gid)
            if target is None:
                target = TailoredSkillGroup(label=label, items=[])
                groups.append(target)
                by_master[gid] = target
            target.items.append(item)
            placed.add(key)
        cv.skill_groups = [g for g in groups if g.items]
    return cv


async def update_application(app_id: int, *, rev: int, llm=None, **edits) -> Application:
    """Apply the user's edits to revision `rev` (must be the current one) as a NEW revision: persisted page trims are kept,
    the CV is re-fitted, re-checked and re-verified. Approval is cleared."""
    from jobhunt import llm as llm_module

    app = store.get_application(app_id)
    if app is None:
        raise CVError("Candidature introuvable")
    if app.current_rev != rev:
        raise StaleRevision("Le CV a changé entre-temps : rouvrir la page et recommencer")
    master, photo = _load(app.profile_id)
    cv = apply_edits(app.tailored, master, **edits)
    own = llm is None
    llm = llm or llm_module.get_llm()
    try:
        return await _build_revision(master, photo, cv, app.fit_notes, llm, profile_id=app.profile_id, job_id=app.job_id,
                                     job_title=None, company=app.company, ats=None, app_id=app_id, base_rev=rev)
    finally:
        if own:
            await llm.aclose()


def approve(app_id: int, rev: int, *, ignore_errors: bool = False) -> Application:
    """Approve exactly revision `rev` (the current one) after re-hashing its PDF on disk."""
    app = store.get_application(app_id)
    if app is None:
        raise CVError("Candidature introuvable")
    return store.approve_revision(app_id, rev, file_sha256=store.sha256_file(safe_pdf(app)), ignore_errors=ignore_errors)


def withdraw(app_id: int) -> Application:
    return store.set_status(app_id, "withdrawn")


def restore(app_id: int) -> Application:
    return store.set_status(app_id, "draft")


def safe_pdf(app: Application) -> Path | None:
    """The revision PDF if it is a .pdf file inside settings.applications_dir (symlinks resolved), else None."""
    if not app.cv_path:
        return None
    try:
        real = Path(app.cv_path).resolve(strict=True)
        real.relative_to(get_settings().applications_dir.resolve())
    except (OSError, ValueError):
        return None
    return real if real.is_file() and real.suffix.lower() == ".pdf" else None


def is_eligible(app: Application) -> tuple[bool, list[str]]:
    """Automatic submission gate. ALL must hold: no blocking error, verifier ran and passed, master confirmed and unchanged
    since the CV was tailored, exactly this revision approved by hand without override, one-page PDF intact on disk."""
    reasons: list[str] = []
    if blocking(app.flags):
        reasons.append(f"{len(blocking(app.flags))} erreur(s) bloquante(s)")
    if app.verifier is None or not app.verifier.ran:
        reasons.append("vérification IA non effectuée")
    elif not app.verifier.passed:
        reasons.append("vérification IA non réussie")
    try:
        master = load_master(app.profile_id)
    except MasterError:
        master = None
    if master is None or not master.is_confirmed:
        reasons.append("CV maître non confirmé")
    elif app.master_hash != master.content_hash:
        reasons.append("CV maître modifié depuis la génération")
    if app.status != "approved" or app.approved_rev is None:
        reasons.append("CV non approuvé")
    elif app.approved_rev != app.current_rev:
        reasons.append("la version approuvée n'est plus la version courante")
    if app.approval_override:
        reasons.append("approbation manuelle malgré des erreurs")
    pdf = safe_pdf(app)
    if pdf is None or not app.pdf_sha256:
        reasons.append("PDF absent")
    elif store.sha256_file(pdf) != app.pdf_sha256 or (app.status == "approved" and app.approved_sha256 != app.pdf_sha256):
        reasons.append("PDF modifié ou différent de celui approuvé")
    return not reasons, reasons


def preview_html(app: Application) -> str:
    """The CV as published: untrimmed content with the persisted page trims applied."""
    master, photo = _load(app.profile_id)
    return render_html(apply_fit(app.tailored, app.fit_notes), master, photo)


def master_as_tailored(master: Master) -> TailoredCV:
    """The untailored master laid out like a tailored CV (HTML preview of the master; may exceed one page)."""
    from jobhunt.cv.tailored import (TailoredBullet, TailoredEducation, TailoredLanguage, TailoredProject)

    return TailoredCV(
        headline=master.identity.default_title, summary=master.summary,
        skill_groups=[TailoredSkillGroup(label=g.label, items=list(g.items)) for g in master.skill_groups],
        projects=[TailoredProject(id=p.id, title=p.title, dates=p.dates, context=p.context,
                                  bullets=[TailoredBullet(label=b.label, text=b.text, source_ids=[b.id]) for b in p.bullets])
                  for p in master.projects],
        education=[TailoredEducation(**e.model_dump()) for e in master.education],
        languages=[TailoredLanguage(**l.model_dump()) for l in master.languages],
    )


def master_preview_html(profile_id: int) -> str:
    master, photo = _load(profile_id)
    return render_html(master_as_tailored(master), master, photo)
