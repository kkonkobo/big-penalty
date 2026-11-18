"""launchd scheduling: plist generation is pure; install/uninstall/status use injected paths and runner."""

import plistlib
import shlex
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from jobhunt import schedule
from jobhunt.cli import app

# launchd scheduling only installs on macOS (schedule._require_macos). These tests check the macOS plist and the
# /bin/sh wrapper it runs, which use POSIX paths and a POSIX shell, so they don't apply on Windows.
posix_only = pytest.mark.skipif(sys.platform == "win32", reason="launchd plist and /bin/sh wrapper are macOS-only")

REPO = Path("/Volumes/SSD/Dev/big-penalty")
HOME = Path("/Users/test")


@posix_only
def test_plist_content_for_default_time():
    plist = plistlib.loads(schedule.build_plist(REPO, ["07:30"], home=HOME))
    assert plist["Label"] == "com.bigpenalty.daily"
    assert plist["StartCalendarInterval"] == [{"Hour": 7, "Minute": 30}]
    assert plist["ProgramArguments"][:2] == ["/bin/zsh", "-lc"] and len(plist["ProgramArguments"]) == 3
    # launchd's own logs are on the always-mounted home volume, never on the SSD
    assert plist["StandardOutPath"] == "/Users/test/Library/Logs/big-penalty/launchd.out.log"
    assert plist["StandardErrorPath"] == "/Users/test/Library/Logs/big-penalty/launchd.err.log"
    assert plist["EnvironmentVariables"]["PATH"].startswith("/Users/test/.local/bin:")
    assert plist["RunAtLoad"] is False


@posix_only
def test_wrapper_command_skips_quietly_when_repo_missing(tmp_path):
    cmd = schedule.build_shell_command(REPO)
    assert cmd == ("[ -d /Volumes/SSD/Dev/big-penalty ] || exit 0; cd /Volumes/SSD/Dev/big-penalty && "
                   "mkdir -p data/logs && exec >> data/logs/launchd.out.log 2>> data/logs/launchd.err.log && "
                   ". ./env.sh && uv run jobhunt daily")
    missing = schedule.build_shell_command(tmp_path / "not mounted")  # real shell: exits 0, prints nothing
    res = subprocess.run(["/bin/sh", "-c", missing], capture_output=True, text=True)
    assert res.returncode == 0 and res.stdout == "" and res.stderr == ""


@posix_only
def test_wrapper_command_quotes_paths_with_spaces():
    cmd = schedule.build_shell_command(Path("/Volumes/My SSD/job hunt"))
    assert shlex.split(cmd)[:4] == ["[", "-d", "/Volumes/My SSD/job hunt", "]"]


def test_repo_root_is_derived_from_package():
    assert (schedule.repo_root() / "pyproject.toml").exists() and (schedule.repo_root() / "env.sh").exists()


@pytest.mark.parametrize("value,expected", [("07:30", (7, 30)), ("7:05", (7, 5)), ("23:59", (23, 59)), ("00:00", (0, 0))])
def test_parse_time_ok(value, expected):
    assert schedule.parse_time(value) == expected


@pytest.mark.parametrize("value", ["24:00", "7:60", "0730", "7h30", "", "ab:cd"])
def test_parse_time_rejects(value):
    with pytest.raises(schedule.ScheduleError):
        schedule.parse_time(value)


def test_next_run():
    now = datetime(2026, 11, 17, 7, 0).astimezone()
    assert schedule.next_run(7, 30, now) == now.replace(hour=7, minute=30)
    assert schedule.next_run(7, 0, now).day == 18  # strictly after now


class Runner:
    def __init__(self, loaded=False, bootstrap_rc=0, bootout_rc=0):
        self.calls, self.loaded, self.bootstrap_rc, self.bootout_rc = [], loaded, bootstrap_rc, bootout_rc

    def __call__(self, cmd):
        self.calls.append(cmd)
        rc = 0
        if cmd[1] == "kickstart":
            rc = 0 if self.loaded else 113
        if cmd[1] == "bootstrap":
            rc = self.bootstrap_rc
            self.loaded = self.loaded or rc == 0
        elif cmd[1] == "bootout":
            rc = self.bootout_rc if self.loaded else 113
            if rc == 0:
                self.loaded = False
        elif cmd[1] == "print":
            rc = 0 if self.loaded else 113
        return SimpleNamespace(returncode=rc, stdout="", stderr="Bootstrap failed: 5" if rc else "")


def test_install_writes_plist_and_bootstraps(tmp_path):
    repo, home = tmp_path / "repo", tmp_path / "home"
    runner = Runner()
    path = schedule.install("08:15", repo=repo, home=home, runner=runner, uid=501, check_platform=False)
    assert path == home / "Library/LaunchAgents/com.bigpenalty.daily.plist"
    assert plistlib.loads(path.read_bytes())["StartCalendarInterval"] == [{"Hour": 8, "Minute": 15}]
    assert (repo / "data" / "logs").is_dir() and (home / "Library/Logs/big-penalty").is_dir()
    assert runner.calls == [["launchctl", "print", "gui/501/com.bigpenalty.daily"],
                            ["launchctl", "bootstrap", "gui/501", str(path)]]


def test_install_replaces_a_loaded_agent(tmp_path):
    runner = Runner(loaded=True)
    path = schedule.install("08:15", repo=tmp_path, home=tmp_path, runner=runner, uid=501, check_platform=False)
    assert [c[1] for c in runner.calls] == ["print", "bootout", "print", "bootstrap"] and runner.loaded
    assert plistlib.loads(path.read_bytes())["StartCalendarInterval"] == [{"Hour": 8, "Minute": 15}]


def test_install_reports_bootstrap_failure(tmp_path):
    with pytest.raises(schedule.ScheduleError, match="bootstrap"):
        schedule.install(repo=tmp_path, home=tmp_path, runner=Runner(bootstrap_rc=5), uid=501, check_platform=False)


def test_install_stops_without_touching_plist_when_bootout_fails(tmp_path):
    runner = Runner(loaded=True)
    schedule.install("07:30", repo=tmp_path, home=tmp_path, runner=runner, uid=501, check_platform=False)
    path = schedule.plist_path(tmp_path)
    before = path.read_bytes()
    runner = Runner(loaded=True, bootout_rc=5)
    with pytest.raises(schedule.ScheduleError, match="bootout"):
        schedule.install("09:00", repo=tmp_path, home=tmp_path, runner=runner, uid=501, check_platform=False)
    assert path.read_bytes() == before and not any(c[1] == "bootstrap" for c in runner.calls)


def test_install_rejects_bad_time_before_touching_anything(tmp_path):
    runner = Runner()
    with pytest.raises(schedule.ScheduleError):
        schedule.install("25:00", repo=tmp_path, home=tmp_path, runner=runner, check_platform=False)
    assert runner.calls == [] and not (tmp_path / "Library").exists()


def test_uninstall_boots_out_and_removes_plist(tmp_path):
    runner = Runner(loaded=True)
    schedule.install(repo=tmp_path, home=tmp_path, runner=runner, uid=501, check_platform=False)
    path = schedule.plist_path(tmp_path)
    runner.calls.clear()
    assert schedule.uninstall(home=tmp_path, runner=runner, uid=501, check_platform=False) is True
    assert [c[1] for c in runner.calls] == ["print", "bootout", "print"] and not path.exists()
    assert schedule.uninstall(home=tmp_path, runner=Runner(), uid=501, check_platform=False) is False  # nothing left


def test_uninstall_fails_and_keeps_plist_when_still_loaded(tmp_path):
    schedule.install(repo=tmp_path, home=tmp_path, runner=Runner(), uid=501, check_platform=False)
    path = schedule.plist_path(tmp_path)
    with pytest.raises(schedule.ScheduleError, match="toujours chargé"):
        schedule.uninstall(home=tmp_path, runner=Runner(loaded=True, bootout_rc=5), uid=501, check_platform=False)
    assert path.exists()


@posix_only
def test_wrapper_redirects_output_into_data_logs(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "env.sh").write_text("")
    cmd = schedule.build_shell_command(repo).replace("uv run jobhunt daily", "echo hello; echo oops >&2")
    res = subprocess.run(["/bin/sh", "-c", cmd], capture_output=True, text=True)
    assert res.returncode == 0 and res.stdout == "" and res.stderr == ""
    assert (repo / "data/logs/launchd.out.log").read_text() == "hello\n"
    assert (repo / "data/logs/launchd.err.log").read_text() == "oops\n"


def test_status_loaded_with_log_tail(tmp_path):
    runner = Runner(loaded=True)
    schedule.install("07:30", repo=tmp_path, home=tmp_path, runner=runner, uid=501, check_platform=False)
    logs = tmp_path / "data" / "logs"
    (logs / "daily-2026-11-16.log").write_text("old\n")
    (logs / "daily-2026-11-17.log").write_text("\n".join(f"l{i}" for i in range(8)) + "\n")
    now = datetime(2026, 11, 17, 9, 0).astimezone()
    st = schedule.status(repo=tmp_path, home=tmp_path, runner=runner, uid=501, now=now, check_platform=False)
    assert st.installed and st.loaded and st.times == ["07:30"]
    assert st.next_run == datetime(2026, 11, 18, 7, 30).astimezone()
    assert st.last_log.name == "daily-2026-11-17.log" and st.log_tail == ["l3", "l4", "l5", "l6", "l7"]


def test_status_when_nothing_installed(tmp_path):
    st = schedule.status(repo=tmp_path, home=tmp_path, runner=Runner(), uid=501, check_platform=False)
    assert not st.installed and not st.loaded and st.times is None and st.last_log is None


def test_cli_schedule_never_touches_real_launchd_for_bad_input(monkeypatch):
    monkeypatch.setattr(schedule.sys, "platform", "darwin")
    res = CliRunner().invoke(app, ["schedule", "install", "--time", "99:99"])
    assert res.exit_code == 1 and "heure invalide" in res.output
    assert CliRunner().invoke(app, ["schedule", "frobnicate"]).exit_code == 2


# ---------------------------------------------------------------- multiple times
KW = dict(uid=501, check_platform=False)


def test_normalize_times_dedupes_sorts_and_pads():
    assert schedule.normalize_times(["19:00", "7:30", "07:30", "12:05"]) == ["07:30", "12:05", "19:00"]
    with pytest.raises(schedule.ScheduleError):
        schedule.normalize_times([])
    with pytest.raises(schedule.ScheduleError):
        schedule.normalize_times(["07:30", "25:00"])


def test_plist_always_an_array_sorted():
    plist = plistlib.loads(schedule.build_plist(REPO, ["19:00", "07:30", "19:00"], home=HOME))
    assert plist["StartCalendarInterval"] == [{"Hour": 7, "Minute": 30}, {"Hour": 19, "Minute": 0}]


def test_set_add_remove_roundtrip_through_plist(tmp_path):
    runner = Runner()
    kw = dict(repo=tmp_path, home=tmp_path, runner=runner, **KW)
    schedule.set_times(["18:00", "07:30"], **kw)
    assert schedule.read_times(tmp_path) == ["07:30", "18:00"] and runner.loaded
    assert schedule.add_time("12:00", **kw) == ["07:30", "12:00", "18:00"]
    assert schedule.remove_time("07:30", **kw) == ["12:00", "18:00"]
    assert schedule.read_times(tmp_path) == ["12:00", "18:00"] and runner.loaded
    assert [c[1] for c in runner.calls].count("bootout") == 2  # add and remove each reload the loaded agent


def test_add_installs_when_missing(tmp_path):
    assert schedule.add_time("09:00", repo=tmp_path, home=tmp_path, runner=Runner(), **KW) == ["09:00"]


def test_remove_errors(tmp_path):
    kw = dict(repo=tmp_path, home=tmp_path, runner=Runner(), **KW)
    with pytest.raises(schedule.ScheduleError, match="installée"):
        schedule.remove_time("07:30", **kw)
    schedule.install("07:30", **kw)
    with pytest.raises(schedule.ScheduleError, match="pas planifié"):
        schedule.remove_time("08:00", **kw)
    with pytest.raises(schedule.ScheduleError, match="uninstall"):
        schedule.remove_time("07:30", **kw)
    assert schedule.read_times(tmp_path) == ["07:30"]


def test_change_keeps_plist_when_bootout_fails(tmp_path):
    schedule.install("07:30", repo=tmp_path, home=tmp_path, runner=Runner(), **KW)
    with pytest.raises(schedule.ScheduleError, match="bootout"):
        schedule.add_time("08:00", repo=tmp_path, home=tmp_path, runner=Runner(loaded=True, bootout_rc=5), **KW)
    assert schedule.read_times(tmp_path) == ["07:30"]


def test_next_run_of_picks_earliest():
    now = datetime(2026, 11, 17, 9, 0).astimezone()
    assert schedule.next_run_of(["07:30", "12:00", "19:00"], now) == now.replace(hour=12, minute=0)
    assert schedule.next_run_of(["07:30", "08:00"], now).day == 18


def test_run_now_kickstarts_when_loaded(tmp_path):
    runner = Runner(loaded=True)
    schedule.run_now(repo=tmp_path, home=tmp_path, runner=runner, **KW)
    assert runner.calls[-1] == ["launchctl", "kickstart", "gui/501/com.bigpenalty.daily"]


def test_run_now_not_installed(tmp_path):
    runner = Runner()
    with pytest.raises(schedule.ScheduleError, match="daily"):
        schedule.run_now(repo=tmp_path, home=tmp_path, runner=runner, **KW)
    assert not any(c[1] == "kickstart" for c in runner.calls)


def test_is_running_probes_lock_without_holding_or_creating_it(tmp_path):
    assert schedule.is_running(repo=tmp_path) is False and not (tmp_path / "data").exists()
    from jobhunt.daily import daily_lock

    (tmp_path / "data").mkdir()
    with daily_lock(tmp_path / "data"):
        assert schedule.is_running(repo=tmp_path) is True
        with pytest.raises(schedule.ScheduleError, match="déjà en cours"):
            schedule.run_now(repo=tmp_path, home=tmp_path, runner=Runner(loaded=True), **KW)
    assert schedule.is_running(repo=tmp_path) is False
    with daily_lock(tmp_path / "data"):  # the probe left no lock behind
        pass


def test_status_multiple_times_and_running(tmp_path):
    runner = Runner(loaded=True)
    schedule.set_times(["07:30", "19:00"], repo=tmp_path, home=tmp_path, runner=runner, **KW)
    now = datetime(2026, 11, 17, 9, 0).astimezone()
    st = schedule.status(repo=tmp_path, home=tmp_path, runner=runner, now=now, uid=501, check_platform=False)
    assert st.times == ["07:30", "19:00"] and st.next_run == now.replace(hour=19, minute=0) and not st.running


def test_cli_multi_commands(monkeypatch):
    calls = []
    monkeypatch.setattr(schedule, "install", lambda times, **kw: calls.append(list(times)) or Path("/p"))
    monkeypatch.setattr(schedule, "run_now", lambda **kw: calls.append("run"))
    r = CliRunner()
    assert r.invoke(app, ["schedule", "install", "--time", "19:00", "--time", "7:30"]).exit_code == 0
    assert r.invoke(app, ["schedule", "set", "12:00", "08:00", "08:00"]).exit_code == 0
    assert r.invoke(app, ["schedule", "run"]).exit_code == 0
    assert calls == [["07:30", "19:00"], ["08:00", "12:00"], "run"]
    res = r.invoke(app, ["schedule", "set", "xx"])
    assert res.exit_code == 1 and "heure invalide" in res.output
