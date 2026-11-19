"""Offline scheduling tests: temporary files, a fake clock and no child process."""

import json
import plistlib
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from threading import Event
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from jobhunt import schedule
from jobhunt.cli import app
from jobhunt.config import get_settings
from jobhunt.daily import DailyReport, daily_lock


class Clock:
    def __init__(self, hour=7, minute=29, second=55):
        self.now = datetime(2026, 11, 19, hour, minute, second).timestamp()

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def test_schedule_lock_serializes_threads(tmp_path, monkeypatch, lock_backend):
    try_lock, unlock = lock_backend
    monkeypatch.setattr(schedule, "try_lock", try_lock)
    monkeypatch.setattr(schedule, "unlock", unlock)
    started, entered = Event(), Event()

    def second_writer():
        started.set()
        with schedule._schedule_lock(tmp_path):
            entered.set()

    with ThreadPoolExecutor(max_workers=1) as pool:
        with schedule._schedule_lock(tmp_path):
            second = pool.submit(second_writer)
            assert started.wait(2)
            assert not entered.wait(0.1)
        second.result(timeout=2)
    assert entered.is_set()


def test_file_lock_retries_cross_process_contention(tmp_path, monkeypatch):
    attempts = iter([False, False, True])
    pauses, releases = [], []
    monkeypatch.setattr(schedule, "try_lock", lambda handle: next(attempts))
    monkeypatch.setattr(schedule, "unlock", releases.append)
    monkeypatch.setattr(schedule, "sleep", pauses.append)
    with schedule._schedule_lock(tmp_path):
        assert len(pauses) == 2
        assert not releases
    assert len(releases) == 1


def test_daily_probe_respects_in_process_claim(tmp_path, monkeypatch, lock_backend):
    from jobhunt import daily

    try_lock, unlock = lock_backend
    for module in (daily, schedule):
        monkeypatch.setattr(module, "try_lock", try_lock)
        monkeypatch.setattr(module, "unlock", unlock)
    with ThreadPoolExecutor(max_workers=1) as pool:
        with daily_lock(tmp_path):
            assert pool.submit(schedule.is_running, tmp_path).result(timeout=2)
        assert not pool.submit(schedule.is_running, tmp_path).result(timeout=2)


@pytest.mark.parametrize(("delay", "runs"), [(4, 0), (5, 1), (30, 1), (604, 1), (606, 0)])
def test_poll_grace(tmp_path, delay, runs):
    clock, launches = Clock(), []
    scheduler = schedule.Scheduler(data_dir=tmp_path, clock=clock, launcher=launches.append)
    scheduler.tick()
    clock.advance(delay)
    scheduler.tick()
    scheduler.tick()
    assert launches == [tmp_path] * runs


def test_no_catchup_when_opened_after_slot(tmp_path):
    clock, launches = Clock(minute=31), []
    scheduler = schedule.Scheduler(data_dir=tmp_path, clock=clock, launcher=launches.append)
    scheduler.tick()
    clock.advance(30)
    scheduler.tick()
    assert not launches


def test_claim_persists_across_restart_and_clock_rewind(tmp_path):
    clock, launches = Clock(), []
    options = dict(data_dir=tmp_path, clock=clock, launcher=launches.append)
    first = schedule.Scheduler(**options)
    clock.advance(30)
    first.tick()
    assert json.loads((tmp_path / "schedule-slots.json").read_text()) == ["2026-11-19T07:30"]
    schedule.Scheduler(**options).tick()
    clock.advance(-30)
    second = schedule.Scheduler(**options)
    clock.advance(30)
    second.tick()
    assert launches == [tmp_path]


def test_lock_skips_slot_permanently_and_logs(tmp_path, caplog):
    clock, launches = Clock(), []
    scheduler = schedule.Scheduler(data_dir=tmp_path, clock=clock, launcher=launches.append)
    with daily_lock(tmp_path):
        clock.advance(30)
        scheduler.tick()
        assert "07:30 ignoré : une recherche est déjà en cours" in caplog.text
        with pytest.raises(schedule.DailyAlreadyRunning):
            schedule.run_now(data_dir=tmp_path, launcher=launches.append)
    scheduler.tick()
    assert not launches
    assert json.loads((tmp_path / "schedule-slots.json").read_text()) == ["2026-11-19T07:30"]


def test_disabled_slot_is_not_replayed_when_enabled(tmp_path):
    clock, launches = Clock(), []
    schedule.set_enabled(False, data_dir=tmp_path)
    scheduler = schedule.Scheduler(data_dir=tmp_path, clock=clock, launcher=launches.append)
    clock.advance(30)
    scheduler.tick()
    schedule.set_enabled(True, data_dir=tmp_path)
    clock.advance(30)
    scheduler.tick()
    assert not launches


def test_crossing_midnight(tmp_path):
    clock, launches = Clock(hour=23, minute=59), []
    schedule.set_times(["23:59", "00:00"], data_dir=tmp_path)
    scheduler = schedule.Scheduler(data_dir=tmp_path, clock=clock, launcher=launches.append)
    clock.advance(30)
    scheduler.tick()
    assert json.loads((tmp_path / "schedule-slots.json").read_text()) == ["2026-11-20T00:00"]
    assert launches == [tmp_path]


@pytest.mark.parametrize(("interval", "expected"), [
    ([{"Hour": 19, "Minute": 0}, {"Hour": 8, "Minute": 5}, {"Hour": 19, "Minute": 0}], ["08:05", "19:00"]),
    ({"Hour": 9, "Minute": 0}, ["09:00"]),
    (None, ["07:30"]),
])
def test_first_use_migrates_once(tmp_path, interval, expected):
    path = schedule.plist_path(tmp_path)
    if interval is not None:
        path.parent.mkdir(parents=True)
        path.write_bytes(plistlib.dumps({"StartCalendarInterval": interval}))
    directory = tmp_path / "data"
    assert schedule.status(data_dir=directory, home=tmp_path).times == expected
    assert json.loads((directory / "schedule.json").read_text()) == {"times": expected, "enabled": True}
    assert path.exists() == (interval is not None)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(plistlib.dumps({"StartCalendarInterval": {"Hour": 12, "Minute": 0}}))
    assert schedule.status(data_dir=directory, home=tmp_path).times == expected


def test_status_next_time_and_result(tmp_path):
    schedule.set_times(["19:00", "7:30"], data_dir=tmp_path)
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "daily-2026-11-19.log").write_text("détail\nRésultat : terminé\n", encoding="utf-8")
    now = datetime(2026, 11, 19, 8)
    status = schedule.status(data_dir=tmp_path, now=now, tail=1)
    assert status.next_run == now.replace(hour=19)
    assert status.log_tail == ["Résultat : terminé"]


def test_command_uses_current_interpreter_or_frozen_entrypoint(monkeypatch):
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    assert schedule.daily_command() == [sys.executable, "-m", "jobhunt", "daily"]
    monkeypatch.setattr(sys, "frozen", True)
    for executable in ("/Apps/Big Penalty.app/Contents/MacOS/Big Penalty", "C:/Big Penalty/big-penalty-cli.exe"):
        monkeypatch.setattr(sys, "executable", executable)
        assert schedule.daily_command() == [executable, "daily"]


@pytest.mark.parametrize("bootout_works", [True, False])
def test_uninstall_legacy_agent_only_after_unloading(tmp_path, bootout_works):
    path = schedule.plist_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.touch()
    seen = []
    loaded = True

    def runner(command):
        nonlocal loaded
        seen.append(command)
        if command[1] == "bootout" and bootout_works:
            loaded = False
        return SimpleNamespace(returncode=0 if loaded else 113, stderr="")

    if bootout_works:
        assert schedule.uninstall_launchd(home=tmp_path, runner=runner, uid=501)
    else:
        with pytest.raises(schedule.ScheduleError, match="plist conservé"):
            schedule.uninstall_launchd(home=tmp_path, runner=runner, uid=501)
    assert path.exists() is not bootout_works
    assert seen == [["launchctl", action, "gui/501/com.bigpenalty.daily"] for action in ("print", "bootout", "print")]


def test_schedule_cli(monkeypatch):
    launches = []
    monkeypatch.setattr(schedule, "_launch_daily", launches.append)
    runner = CliRunner()
    installed = runner.invoke(app, ["schedule", "install", "--time", "19:00", "--time", "7:30"])
    assert installed.exit_code == 0 and "Aucun agent installé" in installed.output
    for args in (["set", "12:00", "08:00"], ["add", "9:05"], ["remove", "12:00"], ["run"]):
        result = runner.invoke(app, ["schedule", *args])
        assert result.exit_code == 0, result.output
    assert "08:00, 09:05" in runner.invoke(app, ["schedule", "status"]).output
    assert launches == [get_settings().data_dir]
    assert runner.invoke(app, ["schedule", "set", "xx"]).exit_code == 1


def test_daily_cli_records_result_without_running_search(monkeypatch):
    from jobhunt import daily

    monkeypatch.setattr(daily, "run_daily", lambda *args, **kwargs: DailyReport())
    assert CliRunner().invoke(app, ["daily"]).exit_code == 0
    assert schedule.status().log_tail[0].endswith("Résultat : aucun profil")
