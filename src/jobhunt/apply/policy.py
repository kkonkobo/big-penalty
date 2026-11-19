"""Guardrails of automatic and manual sending. Pure functions over the database and the CV stores (no browser, no LLM)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from functools import cached_property
from typing import Literal

from jobhunt import db
from jobhunt.apply import store as apply_store
from jobhunt.config import get_settings
from jobhunt.cv import service, store
from jobhunt.cv.master import MasterError, load_master
from jobhunt.cv.store import Application
from jobhunt.dedupe import MergedJob, canonical_url, normalize_company
from jobhunt.freshness import MAX_JOB_AGE_DAYS, expired_by_runs, over_age_limit, start_of_today_paris
from jobhunt.origin import MIN_CONFIDENCE, is_aggregator
from jobhunt.origin import normalize_title as _normalize_role_title

CLOSED_JOB_STATUSES = {"rejected", "applied"}
BLOCKING_APPLICATION_STATUSES = {"submitted", "failed", "withdrawn"}


def normalize_role(company: str | None, title: str | None) -> str:
    """Company + title without case, accents, punctuation or gender markers ("(H/F)", "F/H", "H/F/X")."""
    return f"{normalize_company(company)}|{_normalize_role_title(title or '')}"


def _is_http(url: str | None) -> bool:
    return bool(url) and url.lower().startswith(("http://", "https://"))


def _usable_origin(job: MergedJob) -> bool:
    return _is_http(job.origin_url) and not is_aggregator(job.origin_url)


def _canon(url: str | None) -> str:
    return canonical_url(url) if _is_http(url) else ""


def _app_keys(other: Application, other_job: MergedJob | None) -> tuple[str, str]:
    """(role key, canonical URL) of an application, from its job when it still exists, else from what it stored."""
    company = other.company or (other_job.company if other_job else None)
    title = other.job_title or (other_job.title if other_job else None)
    url = other.sent_origin_url or _canon(other_job.origin_url if other_job else None)
    return normalize_role(company, title), url


def duplicate_of(app: Application, job: MergedJob) -> Application | None:
    """Another application of the same profile already SENT (submitted, or a send attempt that reached the final click and
    was not released) for the same job, company + role, or origin URL. Works when the job row of the other application was
    deleted or merged: its reservation and `sent_origin_url` keep the role and the URL."""
    reserved = apply_store.active_reservations(app.profile_id)
    reserved_ids = {r["application_id"] for r in reserved}
    return _find_duplicate(app, job, reserved, store.get_application,
                           lambda: (other for other in store.list_applications(app.profile_id)
                                    if other.status == "submitted" or other.id in reserved_ids),
                           lambda other: _app_keys(other, db.get_job(other.job_id)))


def _find_duplicate(app, job, reserved, lookup_app, candidates, keys):
    role = normalize_role(job.company, job.title)
    url = _canon(job.origin_url)
    for r in reserved:  # role / URL recorded at click time
        if r["application_id"] != app.id and ((role != "|" and r["role_key"] == role) or (url and r["url_key"] == url)):
            return lookup_app(r["application_id"])
    for other in candidates():
        if other.id == app.id:
            continue
        if other.job_id == job.id:
            return other
        other_role, other_url = keys(other)
        if (url and other_url == url) or (role != "|" and other_role == role):
            return other
    return None


def _confirmed_master_hash(profile_id: int) -> str | None:
    try:
        master = load_master(profile_id)
    except MasterError:
        return None
    return master.content_hash if master is not None and master.is_confirmed else None


@dataclass
class QueuePolicyReads:
    """Inputs shared only while displaying one profile's queue; never consulted by send or resume gates."""

    profile_id: int
    apps: dict[int, Application]
    jobs: dict[int, MergedJob]
    reservations: list[dict]
    unconfirmed: set[int]

    @cached_property
    def master_hash(self) -> str | None:
        return _confirmed_master_hash(self.profile_id)

    @cached_property
    def duplicate_keys(self) -> dict[int, tuple[str, str]]:
        reserved_ids = {row["application_id"] for row in self.reservations}
        return {app.id: _app_keys(app, self.jobs.get(app.job_id)) for app in self.apps.values()
                if app.status == "submitted" or app.id in reserved_ids}

    def duplicate(self, app: Application, job: MergedJob) -> Application | None:
        return _find_duplicate(app, job, self.reservations, self.apps.get,
                               lambda: (self.apps[app_id] for app_id in self.duplicate_keys),
                               lambda other: self.duplicate_keys[other.id])


def _expired(job: MergedJob, now: datetime | None) -> bool:
    """Same notion as `db.query_jobs(include_expired=False)`: not seen for a week by sources checked OK since."""
    ref = now or db.now()
    return expired_by_runs(job, ref, db.recent_ok_runs(job.profile_id, ref))


def submit_problems(app: Application, job: MergedJob | None, *, trigger: Literal["auto", "user"], now: datetime | None = None,
                    run_id: int | None = None) -> list[str]:
    """French reasons why this application must NOT be sent now (empty = OK). `user`: a manual override approval is allowed
    and neither the score, the expiry nor the provenance of the link applies. `auto`: also needs the master switch, a clean
    eligible approval, a known age, a trusted origin link, the minimum score and room under the daily cap. Both: age <= 14.0 days
    (strict, no tolerance), job not rejected / applied, no earlier unconfirmed send attempt. `run_id`: the run that is asking (its
    own pending / firm reservation neither counts against the daily cap nor as an unconfirmed earlier attempt)."""
    return _submit_problems(app, job, trigger=trigger, now=now, run_id=run_id)


def queue_problems(app: Application, job: MergedJob | None, reads: QueuePolicyReads) -> list[str]:
    """Display the manual admission checks using this queue's shared reads."""
    return _submit_problems(app, job, trigger="user", queue_reads=reads)


def _submit_problems(app: Application, job: MergedJob | None, *, trigger: Literal["auto", "user"], now: datetime | None = None,
                     run_id: int | None = None, queue_reads: QueuePolicyReads | None = None) -> list[str]:
    s = get_settings()
    out: list[str] = []
    if job is None or job.profile_id != app.profile_id or job.id != app.job_id:
        return ["offre introuvable pour ce profil"]
    if not _is_http(job.origin_url):
        out.append("pas de lien http(s) vers le site de l'entreprise")
    elif is_aggregator(job.origin_url):
        out.append("le lien est celui d'un agrégateur, pas du site de l'entreprise")
    over = over_age_limit(job, s.apply_max_age_days, now)
    if over:
        out.append(f"offre de plus de {s.apply_max_age_days} jours")
    elif over is None and trigger == "auto":
        out.append("date de publication de l'offre inconnue")
    if job.status in CLOSED_JOB_STATUSES:
        out.append("offre déjà rejetée ou déjà postulée")
    if trigger == "auto":
        if _expired(job, now):
            out.append("offre expirée (plus vue depuis plus d'une semaine)")
        if (job.origin_confidence or 0) < MIN_CONFIDENCE:
            out.append("lien de l'offre non vérifié (origine incertaine)")
    if app.status != "submitted" and (app.id in queue_reads.unconfirmed if queue_reads is not None
                                      else apply_store.has_unconfirmed_click(app.id, except_run_id=run_id)):
        out.append("un envoi précédent n'a pas été confirmé : vérifier sur le site de l'entreprise")
    dup = queue_reads.duplicate(app, job) if queue_reads is not None else duplicate_of(app, job)
    if dup is not None:
        out.append(f"candidature déjà envoyée pour la même offre (n°{dup.id})")
    if app.status != "approved" or app.approved_rev is None:
        out.append("CV non approuvé")
    elif app.approved_rev != app.current_rev:
        out.append("la version approuvée n'est plus la version courante")
    master_hash = queue_reads.master_hash if queue_reads is not None else _confirmed_master_hash(app.profile_id)
    if master_hash is None:
        out.append("CV maître non confirmé")
    elif app.master_hash != master_hash:
        out.append("CV maître modifié depuis la génération")
    pdf = service.safe_pdf(app)
    if pdf is None or not app.pdf_sha256:
        out.append("PDF absent")
    elif store.sha256_file(pdf) != app.pdf_sha256 or app.approved_sha256 != app.pdf_sha256:
        out.append("PDF modifié ou différent de celui approuvé")
    if trigger == "auto":
        if apply_store.requires_manual_retry(app.id):
            out.append("nouvel envoi manuel requis après vérification")
        if apply_store.has_site_handoff(app.id):
            out.append("intervention manuelle requise sur le site")
        if not s.apply_auto:
            out.append("envoi automatique désactivé")
        ok, reasons = service.is_eligible(app)
        if not ok:
            out.append("CV non éligible à l'envoi automatique : " + " ; ".join(reasons))
        if job.score is None or job.score < s.apply_auto_min_score:
            out.append(f"score inférieur à {s.apply_auto_min_score:g}")
        if apply_store.count_auto_submitted(app.profile_id, start_of_today_paris(now), except_run_id=run_id) >= s.apply_daily_cap:
            out.append(f"plafond de {s.apply_daily_cap} envois automatiques par jour atteint")
    return list(dict.fromkeys(out))


def auto_candidates(profile_id: int, now: datetime | None = None) -> list[MergedJob]:
    """Jobs the daily run should prepare / send, best score first: scored at least `apply_prepare_min_score`, fresh, not
    expired, real company link, job still open, and no submitted / failed / withdrawn application for it."""
    s = get_settings()
    done = {a.job_id for a in store.list_applications(profile_id) if a.status in BLOCKING_APPLICATION_STATUSES}
    # include_stale: the shortlist hides jobs older than its 14-day window, sending has its own limit (apply_max_age_days)
    jobs = db.query_jobs(profile_id, min_score=s.apply_prepare_min_score, sort="score", as_of=now,
                         include_stale=s.apply_max_age_days > MAX_JOB_AGE_DAYS)
    jobs = [j for j in jobs if over_age_limit(j, s.apply_max_age_days, now) is not True]
    return [j for j in jobs if j.status not in CLOSED_JOB_STATUSES and _usable_origin(j) and j.id not in done]
