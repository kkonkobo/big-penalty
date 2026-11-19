"""Periodic portal sync uses only fake runners and temporary journals."""

import asyncio
from datetime import UTC, datetime
from threading import Event

import pytest

from jobhunt.portal.periodic import INTERVAL_SECONDS, PeriodicSync, SyncOutcome, last_outcome, run_pass
from jobhunt.portal.journal import Journal, pass_lock
from jobhunt.portal.sync import ProfileSyncReport

NOW = datetime(2026, 11, 19, 12, tzinfo=UTC)


def configure(*, paired=True, enabled=True, paused=False):
    journal = Journal()
    if paired:
        journal.set_setting("origin", "https://example.invalid")
    journal.save_profile(1, profile_key="fictional", enabled=enabled)
    journal.set_paused(paused)
    return journal


@pytest.mark.parametrize("settings", [{"paired": False}, {"enabled": False}, {"paused": True}, None])
def test_ineligible_pass_does_not_call_runner(settings):
    if settings is not None:
        journal = configure(**settings)
    calls = []

    async def runner(*args):
        calls.append(args)
        return []

    run_pass(runner=runner, clock=lambda: NOW)
    assert not calls
    if settings is not None:
        assert last_outcome(journal) is None


def test_portal_lock_skips_external_sync_without_overwriting_result():
    journal = configure()
    journal.set_setting("automatic_sync_outcome", f"ok {NOW.isoformat()}")
    calls = []

    async def runner(*args):
        calls.append(args)
        return []

    with pass_lock() as acquired:
        assert acquired
        run_pass(runner=runner, clock=lambda: NOW)
    assert not calls
    assert last_outcome(journal) == SyncOutcome(NOW, "Réussie")


@pytest.mark.parametrize("success", [True, False])
def test_outcome_persisted_and_log_contains_no_secret(success, caplog):
    journal = configure()
    journal.save_profile(2, profile_key="not-enabled", enabled=False)
    calls = []

    async def runner(_journal, ids, when):
        calls.append((ids, when))
        if not success:
            raise RuntimeError("do-not-log-this-token")
        return [ProfileSyncReport(ids[0], ok=True)]

    run_pass(runner=runner, clock=lambda: NOW)
    assert calls == [([1], NOW)]
    assert last_outcome(journal) == SyncOutcome(NOW, "Réussie" if success else "Échec")
    assert "do-not-log-this-token" not in caplog.text


@pytest.mark.asyncio
async def test_tick_skips_while_worker_running_and_does_not_catch_up():
    clock = 0
    calls = []
    started, release = Event(), Event()

    def runner():
        calls.append(1)
        started.set()
        release.wait(5)

    scheduler = PeriodicSync(clock=lambda: clock, runner=runner)
    scheduler.tick()
    assert scheduler.pending is None
    clock += INTERVAL_SECONDS
    scheduler.tick()
    first = scheduler.pending
    try:
        assert await asyncio.to_thread(started.wait, 2)
        clock += INTERVAL_SECONDS
        scheduler.tick()
        assert scheduler.pending is first
    finally:
        release.set()
        await first
    scheduler.tick()
    assert calls == [1]

    clock += INTERVAL_SECONDS * 4
    scheduler.tick()
    assert scheduler.pending is first
    clock += INTERVAL_SECONDS
    scheduler.tick()
    await scheduler.pending
    assert calls == [1, 1]
