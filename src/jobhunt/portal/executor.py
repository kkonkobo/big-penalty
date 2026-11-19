"""Consume portal approvals without opening a browser or starting an attempt."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Collection

from jobhunt import db
from jobhunt.apply import service
from jobhunt.apply import store as apply_store
from jobhunt.cv import service as cvs
from jobhunt.cv import store as cvstore
from jobhunt.cv.errors import CVError
from jobhunt.portal.journal import GrantRow, Journal, ProfileRow
from jobhunt.portal.settings import MAX_SEND_MINUTES
from jobhunt.portal.snapshot import APPROVAL_MODE, interrupted_after_click

CHANGED = "La candidature a changé depuis votre accord : rechargez la page."
APPROVAL_REFUSED = "CV à corriger : cette candidature ne peut pas être envoyée telle quelle."
APPROVED = "Accord enregistré. La candidature est prête pour le prochain passage sur le Mac."
LOCAL_ONLY = "Reprise sur le Mac uniquement."

_monotonic = time.monotonic


@dataclass
class Outcome:
    profile_id: int
    app_id: int
    event_id: str
    state: str
    message: str


@dataclass
class BatchReport:
    outcomes: list[Outcome] = field(default_factory=list)
    locked: bool = False
    skipped: dict[int, str] = field(default_factory=dict)
    stopped_for_time: bool = False

    @property
    def did_anything(self) -> bool:
        return bool(self.outcomes)

    def profiles(self) -> set[int]:
        return {outcome.profile_id for outcome in self.outcomes}


def _matches(profile: ProfileRow, grant: GrantRow) -> bool:
    app = cvstore.get_application(grant.app_id)
    if app is None or app.profile_id != profile.profile_id or app.status not in ("draft", "approved") or app.current_rev != grant.rev:
        return False
    if (apply_store.has_site_handoff(app.id) or apply_store.requires_manual_retry(app.id)
            or interrupted_after_click(apply_store.last_run(app.id))):
        return False
    job = db.get_job(app.job_id)
    if job is None or service.origin_digest(job.origin_url) != grant.origin_sha:
        return False
    if cvstore.sha256_file(cvs.safe_pdf(app)) != grant.pdf_sha256:
        return False
    item = next((item for item in service.queue(profile.profile_id) if item.app.id == app.id), None)
    return bool(item and item.bucket == "to_validate" and (item.selectable or item.supervised))


async def run_batch(journal: Journal, profile_ids: Collection[int], now: datetime) -> BatchReport:
    report = BatchReport()
    work = []
    if journal.paused:
        return report
    for profile_id in profile_ids:
        profile = journal.get_profile(profile_id)
        if profile is None or not profile.enabled:
            continue
        if profile.held:
            report.skipped[profile_id] = "profil suspendu"
        elif journal.events(profile_id, ("received",)):
            report.skipped[profile_id] = "événements non traités"
        else:
            work.extend((profile, event) for event in journal.events(profile_id, ("queued",), ("intent",)))
    if not work:
        return report
    from jobhunt import schedule
    from jobhunt.config import get_settings

    if schedule.is_running(data_dir=get_settings().data_dir):
        for profile, _event in work:
            report.skipped[profile.profile_id] = "une recherche quotidienne est en cours"
        return report
    started = _monotonic()
    try:
        with service.send_lock():
            for profile, event in work:
                if _monotonic() - started >= MAX_SEND_MINUTES * 60:
                    report.stopped_for_time = True
                    break
                _approve_one(journal, profile, event, report)
    except service.SendLocked:
        report.locked = True
    return report


def _approve_one(journal, profile, event, report):
    profile_id = profile.profile_id
    grant_id = event.payload.get("grant", "")
    grant = journal.get_grant(grant_id)
    refusal = LOCAL_ONLY if grant and grant.action == "resume" else CHANGED
    try:
        claim = journal.claim_send(profile_id, event.event_id, grant_id, db.now, 0, expected=profile,
                                   approval_only=True, refusal=refusal,
                                   validate=lambda: bool(grant and grant.action == "send" and grant.mode == APPROVAL_MODE
                                                         and _matches(profile, grant)))
    except Exception:
        report.skipped[profile_id] = "Erreur technique."
        return
    if claim == "inbox":
        report.skipped[profile_id] = "événements non traités"
    if claim not in ("claimed", "refused"):
        return
    state, message = "refused", refusal
    if claim == "claimed":
        try:
            cvs.approve(grant.app_id, grant.rev, expected_sha256=grant.pdf_sha256, portal=True)
        except CVError:
            message = APPROVAL_REFUSED
        except Exception:
            state, message = "failed", "Erreur technique."
        else:
            state, message = "done", APPROVED
        journal.update_event(event.event_id, db.now(), only_from=("running",), state=state, message=message)
    report.outcomes.append(Outcome(profile_id, grant.app_id if grant else 0, event.event_id, state, message))
