"""Run portal sync off the local server event loop, only during its lifespan."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from time import monotonic

from jobhunt import db
from jobhunt.config import get_settings
from jobhunt.portal import sync
from jobhunt.portal.journal import Journal, fmt, parse, pass_lock

INTERVAL_SECONDS = 5 * 60
MAX_TICK_DELAY = 30
RESULT_LABELS = {"ok": "Réussie", "partial": "Partiellement réussie", "failed": "Échec"}
log = logging.getLogger("uvicorn.error.jobhunt.portal")


@dataclass(frozen=True)
class SyncOutcome:
    at: datetime
    label: str


def last_outcome(journal: Journal | None = None) -> SyncOutcome | None:
    journal = journal or Journal()
    result, _, stamp = (journal.get_setting("automatic_sync_outcome") or "").partition(" ")
    if result in RESULT_LABELS:
        try:
            at = parse(stamp)
        except ValueError:
            return None
        if at is not None:
            return SyncOutcome(at, RESULT_LABELS[result])
    return None


def run_pass(*, runner=sync.sync_selected, clock=db.now) -> None:
    """Use the same sync lock and code as a manual pass; never record a skipped tick as a result."""
    path = get_settings().data_dir / "portal" / "portal.sqlite3"
    if not path.is_file():
        return
    with pass_lock() as acquired:
        if not acquired:
            return
        journal = Journal(path)
        if not journal.get_setting("origin") or journal.paused:
            return
        ids = [profile.profile_id for profile in journal.list_profiles(enabled_only=True)]
        if not ids:
            return
        try:
            reports = asyncio.run(runner(journal, ids, clock()))
        except Exception:
            result = "failed"
        else:
            successful = sum(report.ok for report in reports)
            result = "ok" if successful == len(ids) else "partial" if successful else "failed"
        finished = clock()
        log.log(logging.INFO if result == "ok" else logging.WARNING,
                "Synchronisation automatique du portail : %s", RESULT_LABELS[result])
        journal.set_setting("automatic_sync_outcome", f"{result} {fmt(finished)}")


class PeriodicSync:
    def __init__(self, *, clock=monotonic, runner=None):
        self.clock = clock
        self.runner = runner or run_pass
        self.deadline = clock() + INTERVAL_SECONDS
        self.pending: asyncio.Task | None = None

    async def _work(self) -> None:
        try:
            await asyncio.to_thread(self.runner)
        except Exception:
            log.warning("Impossible de vérifier ou d’enregistrer la synchronisation automatique du portail.")

    def tick(self) -> None:
        now = self.clock()
        if now < self.deadline:
            return
        late = now - self.deadline
        self.deadline = now + INTERVAL_SECONDS
        if late <= MAX_TICK_DELAY and (self.pending is None or self.pending.done()):
            self.pending = asyncio.create_task(self._work())

    async def run(self, stopped: asyncio.Event) -> None:
        try:
            while not stopped.is_set():
                self.tick()
                try:
                    await asyncio.wait_for(stopped.wait(), timeout=max(0, self.deadline - self.clock()))
                except TimeoutError:
                    pass
        finally:
            if self.pending is not None:
                await self.pending
