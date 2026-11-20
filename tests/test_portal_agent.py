"""Legacy agent inspection and removal never call the real launchctl."""

import plistlib
from types import SimpleNamespace

import pytest

from jobhunt.portal import agent


def write_plist(home):
    path = agent.plist_path(home)
    path.parent.mkdir(parents=True)
    path.write_bytes(plistlib.dumps({"StartInterval": 300}))
    return path


@pytest.mark.parametrize("can_unload", [True, False])
def test_uninstall_checks_before_deleting(tmp_path, can_unload):
    path = write_plist(tmp_path)
    calls, loaded = [], True

    def runner(command):
        nonlocal loaded
        calls.append(command)
        if command[1] == "bootout" and can_unload:
            loaded = False
        return SimpleNamespace(returncode=0 if loaded else 113, stderr="")

    if can_unload:
        assert agent.uninstall(home=tmp_path, runner=runner, uid=501)
    else:
        with pytest.raises(agent.AgentError, match="toujours chargé"):
            agent.uninstall(home=tmp_path, runner=runner, uid=501)
    assert path.exists() is not can_unload
    assert calls == [["launchctl", action, "gui/501/com.bigpenalty.portal"] for action in ("print", "bootout", "print")]


def test_uninstall_keeps_plist_if_agent_state_is_unknown(tmp_path):
    path = write_plist(tmp_path)
    with pytest.raises(agent.AgentError, match="plist conservé"):
        agent.uninstall(home=tmp_path, runner=lambda _: SimpleNamespace(returncode=5, stderr=""), uid=501)
    assert path.exists()


def test_status_reads_legacy_interval_and_log(tmp_path):
    write_plist(tmp_path)
    logs = tmp_path / "logs"
    logs.mkdir()
    path = logs / "portal-sync.log"
    path.write_text("première ligne\ndernière ligne\n")
    status = agent.status(data_dir=tmp_path, home=tmp_path, runner=lambda _: SimpleNamespace(returncode=0), uid=501, tail=1)
    assert status.installed and status.loaded and status.interval_s == 300
    assert status.log_path == path and status.log_tail == ["dernière ligne"]
