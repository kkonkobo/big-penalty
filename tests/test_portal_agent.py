"""launchd agent of the portal sync: pure builders, then install / uninstall / status with a fake launchctl and a temp home."""

from __future__ import annotations

import plistlib
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from jobhunt.portal import agent

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="launchd plist and /bin/sh wrapper are macOS-only")
REPO = Path("/Volumes/Example/Projects/portal-demo")
HOME = Path("/Users/example")
KW = dict(uid=501, check_platform=False)


class Runner:
    def __init__(self, loaded=False, bootstrap_rc=0, bootout_rc=0):
        self.calls, self.loaded, self.bootstrap_rc, self.bootout_rc = [], loaded, bootstrap_rc, bootout_rc

    def __call__(self, cmd):
        self.calls.append(cmd)
        rc = 0
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


@posix_only
def test_plist_content():
    plist = plistlib.loads(agent.build_plist(REPO, home=HOME))
    assert plist["Label"] == "com.bigpenalty.portal"
    assert plist["StartInterval"] == 300 and plist["RunAtLoad"] is True and plist["ProcessType"] == "Background"
    assert plist["ProgramArguments"][:2] == ["/bin/zsh", "-lc"] and len(plist["ProgramArguments"]) == 3
    assert plist["ProgramArguments"][2] == agent.build_shell_command(REPO)
    assert plist["StandardOutPath"] == f"{HOME}/Library/Logs/big-penalty/portal.out.log"
    assert plist["StandardErrorPath"] == f"{HOME}/Library/Logs/big-penalty/portal.err.log"
    assert plist["EnvironmentVariables"]["PATH"].startswith(f"{HOME}/.local/bin:")
    assert plistlib.loads(agent.build_plist(REPO, interval_s=600, home=HOME))["StartInterval"] == 600
    with pytest.raises(agent.AgentError):
        agent.build_plist(REPO, interval_s=5, home=HOME)


@posix_only
def test_command_content():
    cmd = agent.build_shell_command(REPO)
    assert cmd.startswith(f"[ -d {REPO} ] || exit 0; cd {REPO} && mkdir -p data/logs && ")
    assert "exec >> data/logs/portal-sync.log 2>&1" in cmd and cmd.endswith(". ./env.sh && uv run big-penalty portal sync")
    assert "/Volumes/My SSD" in agent.build_shell_command(Path("/Volumes/My SSD/x")).replace("'", "")


@posix_only
def test_command_exits_quietly_when_the_ssd_is_not_mounted(tmp_path):
    res = subprocess.run(["/bin/sh", "-c", agent.build_shell_command(tmp_path / "not mounted")], capture_output=True, text=True)
    assert res.returncode == 0 and res.stdout == "" and res.stderr == ""
    assert not (tmp_path / "not mounted").exists()


def _run_command(repo: Path):
    cmd = agent.build_shell_command(repo).replace("uv run big-penalty portal sync", "echo hello; echo oops >&2")
    return subprocess.run(["/bin/sh", "-c", cmd], capture_output=True, text=True)


@posix_only
def test_command_appends_output_to_the_sync_log(tmp_path):
    (tmp_path / "env.sh").write_text("")
    for _ in range(2):
        res = _run_command(tmp_path)
        assert res.returncode == 0 and res.stdout == "" and res.stderr == ""
    assert (tmp_path / "data/logs/portal-sync.log").read_text() == "hello\noops\n" * 2


@posix_only
def test_command_rotates_a_large_log(tmp_path):
    (tmp_path / "env.sh").write_text("")
    logs = tmp_path / "data/logs"
    logs.mkdir(parents=True)
    (logs / "portal-sync.log").write_bytes(b"x" * (agent.MAX_LOG_BYTES + 1))
    assert _run_command(tmp_path).returncode == 0
    assert (logs / "portal-sync.log").read_text() == "hello\noops\n"
    assert (logs / "portal-sync.log.1").stat().st_size == agent.MAX_LOG_BYTES + 1
    (logs / "portal-sync.log").write_bytes(b"x" * 100)  # a small log is kept
    assert _run_command(tmp_path).returncode == 0 and (logs / "portal-sync.log").stat().st_size > 100


def test_install_writes_plist_and_bootstraps(tmp_path):
    repo, home, runner = tmp_path / "repo", tmp_path / "home", Runner()
    path = agent.install(repo=repo, home=home, runner=runner, **KW)
    assert path == home / "Library/LaunchAgents/com.bigpenalty.portal.plist"
    assert plistlib.loads(path.read_bytes())["StartInterval"] == 300 and agent.read_interval(home) == 300
    assert (repo / "data" / "logs").is_dir() and (home / "Library/Logs/big-penalty").is_dir()
    assert runner.calls == [["launchctl", "print", "gui/501/com.bigpenalty.portal"], ["launchctl", "bootstrap", "gui/501", str(path)]]


def test_install_replaces_a_loaded_agent(tmp_path):
    runner = Runner(loaded=True)
    agent.install(repo=tmp_path, home=tmp_path, runner=runner, **KW)
    assert [c[1] for c in runner.calls] == ["print", "bootout", "print", "bootstrap"] and runner.loaded


def test_install_reports_bootstrap_failure(tmp_path):
    with pytest.raises(agent.AgentError, match="bootstrap"):
        agent.install(repo=tmp_path, home=tmp_path, runner=Runner(bootstrap_rc=5), **KW)


def test_install_aborts_without_touching_the_plist_when_bootout_fails(tmp_path):
    agent.install(repo=tmp_path, home=tmp_path, runner=Runner(loaded=True), **KW)
    path = agent.plist_path(tmp_path)
    before = path.read_bytes()
    runner = Runner(loaded=True, bootout_rc=5)
    with pytest.raises(agent.AgentError, match="bootout"):
        agent.install(repo=tmp_path, home=tmp_path, runner=runner, interval_s=900, **KW)
    assert path.read_bytes() == before and not any(c[1] == "bootstrap" for c in runner.calls)


def test_install_rejects_a_bad_interval_before_touching_anything(tmp_path):
    runner = Runner()
    with pytest.raises(agent.AgentError):
        agent.install(repo=tmp_path, home=tmp_path, runner=runner, interval_s=1, **KW)
    assert runner.calls == [] and not (tmp_path / "Library").exists()


def test_install_is_refused_outside_macos(tmp_path, monkeypatch):
    monkeypatch.setattr(agent.sys, "platform", "linux")
    runner = Runner()
    with pytest.raises(agent.AgentError, match="macOS"):
        agent.install(repo=tmp_path, home=tmp_path, runner=runner, uid=501)
    with pytest.raises(agent.AgentError, match="macOS"):
        agent.uninstall(home=tmp_path, runner=runner, uid=501)
    with pytest.raises(agent.AgentError, match="macOS"):
        agent.status(repo=tmp_path, home=tmp_path, runner=runner, uid=501)
    assert runner.calls == [] and not (tmp_path / "Library").exists()


def test_uninstall(tmp_path):
    runner = Runner(loaded=True)
    agent.install(repo=tmp_path, home=tmp_path, runner=runner, **KW)
    runner.calls.clear()
    assert agent.uninstall(home=tmp_path, runner=runner, **KW) is True
    assert [c[1] for c in runner.calls] == ["print", "bootout", "print"] and not agent.plist_path(tmp_path).exists()
    assert agent.uninstall(home=tmp_path, runner=Runner(), **KW) is False


def test_uninstall_keeps_the_plist_when_still_loaded(tmp_path):
    agent.install(repo=tmp_path, home=tmp_path, runner=Runner(), **KW)
    with pytest.raises(agent.AgentError, match="toujours chargé"):
        agent.uninstall(home=tmp_path, runner=Runner(loaded=True, bootout_rc=5), **KW)
    assert agent.plist_path(tmp_path).exists()


def test_status(tmp_path):
    runner = Runner(loaded=True)
    agent.install(repo=tmp_path, home=tmp_path, runner=runner, **KW)
    (tmp_path / "data/logs/portal-sync.log").write_text("\n".join(f"l{i}" for i in range(8)) + "\n")
    st = agent.status(repo=tmp_path, home=tmp_path, runner=runner, **KW)
    assert st.installed and st.loaded and st.interval_s == 300
    assert st.log_path.name == "portal-sync.log" and st.log_tail == ["l3", "l4", "l5", "l6", "l7"]


def test_status_when_nothing_is_installed(tmp_path):
    st = agent.status(repo=tmp_path, home=tmp_path, runner=Runner(), **KW)
    assert not st.installed and not st.loaded and st.interval_s is None and st.log_path is None
