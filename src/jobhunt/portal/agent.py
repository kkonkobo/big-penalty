"""Inspect or remove the retired macOS portal sync agent."""

from __future__ import annotations

import os
import plistlib
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from jobhunt.config import get_settings

LABEL = "com.bigpenalty.portal"
SYNC_LOG = "portal-sync.log"


class AgentError(Exception):
    """French message for the operator."""


def plist_path(home: Path | None = None) -> Path:
    return (home or Path.home()) / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def _launchctl(command: list[str]):
    if sys.platform != "darwin":
        raise AgentError("launchd n'existe que sous macOS")
    return subprocess.run(command, capture_output=True, text=True, timeout=10)


def _service(uid: int | None) -> str:
    if uid is None and sys.platform != "darwin":
        raise AgentError("launchd n'existe que sous macOS")
    return f"gui/{os.getuid() if uid is None else uid}/{LABEL}"


def _loaded(runner, service: str) -> bool:
    result = runner(["launchctl", "print", service])
    if result.returncode == 0:
        return True
    if result.returncode == 113 or "Could not find service" in (result.stderr or ""):
        return False
    raise AgentError("Impossible de vérifier l’agent launchd ; plist conservé.")


def uninstall(*, home: Path | None = None, runner=_launchctl, uid: int | None = None) -> bool:
    """Remove the plist only after verifying the legacy service is not loaded."""
    service, path = _service(uid), plist_path(home)
    try:
        was_loaded = _loaded(runner, service)
        if was_loaded:
            runner(["launchctl", "bootout", service])
            if _loaded(runner, service):
                raise AgentError("launchctl bootout a échoué : l’agent est toujours chargé ; plist conservé.")
        existed = path.exists()
        path.unlink(missing_ok=True)
        return existed or was_loaded
    except (OSError, subprocess.TimeoutExpired):
        raise AgentError("Impossible de désinstaller l’agent launchd.") from None


@dataclass
class Status:
    installed: bool
    loaded: bool
    interval_s: int | None = None
    log_path: Path | None = None
    log_tail: list[str] | None = None


def status(*, data_dir: Path | None = None, home: Path | None = None, runner=_launchctl,
           uid: int | None = None, tail: int = 5) -> Status:
    path = plist_path(home)
    try:
        result = Status(installed=path.exists(), loaded=_loaded(runner, _service(uid)))
    except (OSError, subprocess.TimeoutExpired):
        raise AgentError("Impossible de vérifier l’agent launchd.") from None
    if result.installed:
        try:
            result.interval_s = int(plistlib.loads(path.read_bytes())["StartInterval"])
        except (OSError, KeyError, TypeError, ValueError, plistlib.InvalidFileException):
            pass
    log = (data_dir or get_settings().data_dir) / "logs" / SYNC_LOG
    try:
        with log.open("rb") as stream:
            result.log_path = log
            stream.seek(max(0, log.stat().st_size - 16384))
            result.log_tail = stream.read().decode("utf-8", errors="replace").splitlines()[-tail:] if tail else []
    except OSError:
        pass
    return result
