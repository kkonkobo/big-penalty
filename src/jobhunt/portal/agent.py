"""launchd agent `com.bigpenalty.portal`: runs `big-penalty portal sync` every five minutes (DESIGN §9).

Modelled on `jobhunt.schedule` (same safety rules): the wrapper command exits 0 quietly when the external SSD is not
mounted, launchd's own stdout / stderr live under ~/Library/Logs/big-penalty (always mounted, so launchd never fails to
spawn the job), an agent that is loaded and cannot be booted out is never rewritten. `RunAtLoad` makes one pass happen as
soon as the agent is loaded, then every `StartInterval` seconds. A pass that is still running (an approved send can take
minutes) is never doubled: `portal sync` holds the non-blocking lock `data/portal/sync.lock` and a second run exits 0.

Pure builders (`build_shell_command`, `build_plist`) are separate from the side effects (`install`, `uninstall`,
`status`), which take an injectable `home`, `uid`, `repo` and command `runner`: tests never touch the real LaunchAgents
directory or launchctl."""

from __future__ import annotations

import plistlib
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path

from jobhunt import schedule
from jobhunt.schedule import Runner

LABEL = "com.bigpenalty.portal"
DEFAULT_INTERVAL_S = 300
MIN_INTERVAL_S = 60
SYNC_LOG = "portal-sync.log"
# The sync log is bounded without any daemon: when a pass starts and the file is over this size, it is moved to
# `portal-sync.log.1` (replacing the previous one) and a fresh file is started. At most about twice this size on disk,
# and the last megabyte is always kept for `agent status`.
MAX_LOG_BYTES = 1_000_000


class AgentError(Exception):
    """French message for the operator."""


def plist_path(home: Path | None = None) -> Path:
    return (home or Path.home()) / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def build_shell_command(repo: Path) -> str:
    """Exits 0 quietly when the repo (external SSD) is not mounted; otherwise rotates the sync log if it is large, appends
    the pass's output to data/logs/portal-sync.log and runs `portal sync`."""
    r = shlex.quote(str(repo))
    log = f"data/logs/{SYNC_LOG}"
    rotate = (f'{{ [ ! -f {log} ] || [ "$(wc -c < {log} | tr -d \' \')" -le {MAX_LOG_BYTES} ] || mv -f {log} {log}.1; }}')
    return (f"[ -d {r} ] || exit 0; cd {r} && mkdir -p data/logs && {rotate} && "
            f"exec >> {log} 2>&1 && . ./env.sh && uv run big-penalty portal sync")


def build_plist(repo: Path, *, interval_s: int = DEFAULT_INTERVAL_S, home: Path | None = None) -> bytes:
    """The launchd property list (XML bytes). Pure."""
    if interval_s < MIN_INTERVAL_S:
        raise AgentError(f"L'intervalle doit être d'au moins {MIN_INTERVAL_S} secondes.")
    logs = schedule.launchd_log_dir(home)
    return plistlib.dumps({
        "Label": LABEL,
        "ProgramArguments": ["/bin/zsh", "-lc", build_shell_command(repo)],
        "StartInterval": interval_s,
        "RunAtLoad": True,
        "ProcessType": "Background",
        "StandardOutPath": str(logs / "portal.out.log"),
        "StandardErrorPath": str(logs / "portal.err.log"),
        "EnvironmentVariables": {"PATH": schedule.launch_path(home)},
    })


def _require_macos() -> None:
    if sys.platform != "darwin":
        raise AgentError("launchd n'existe que sous macOS : l'agent de synchronisation n'est pas disponible ici "
                         "(lancez « big-penalty portal sync » à la main ou avec le planificateur de votre système).")


def is_loaded(runner: Runner = schedule._run, uid: int | None = None) -> bool:
    return runner(["launchctl", "print", f"{schedule._domain(uid)}/{LABEL}"]).returncode == 0


def _unload(runner: Runner, uid: int | None) -> bool:
    """Boot the agent out if it is loaded. Returns whether it was loaded; raises if it is still loaded afterwards."""
    if not is_loaded(runner, uid):
        return False
    res = runner(["launchctl", "bootout", f"{schedule._domain(uid)}/{LABEL}"])
    if is_loaded(runner, uid):
        raise AgentError("launchctl bootout a échoué, l'agent est toujours chargé (rien n'a été modifié) : "
                         f"{(res.stderr or res.stdout or '').strip() or 'code ' + str(res.returncode)}")
    return True


def read_interval(home: Path | None = None) -> int | None:
    """StartInterval stored in the plist (the source of truth); None when there is no readable plist."""
    try:
        return int(plistlib.loads(plist_path(home).read_bytes())["StartInterval"])
    except (OSError, KeyError, TypeError, ValueError, plistlib.InvalidFileException):
        return None


def install(*, repo: Path | None = None, home: Path | None = None, runner: Runner = schedule._run, uid: int | None = None,
            interval_s: int = DEFAULT_INTERVAL_S, check_platform: bool = True) -> Path:
    """Write the plist, create the log directories and load the agent (replacing a loaded one). If the loaded agent
    cannot be booted out, nothing is changed and AgentError is raised."""
    if check_platform:
        _require_macos()
    repo = repo or schedule.repo_root()
    content = build_plist(repo, interval_s=interval_s, home=home)  # validates before anything is touched
    path = plist_path(home)
    _unload(runner, uid)
    (repo / "data" / "logs").mkdir(parents=True, exist_ok=True)
    schedule.launchd_log_dir(home).mkdir(parents=True, exist_ok=True)  # launchd does not create log directories
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    res = runner(["launchctl", "bootstrap", schedule._domain(uid), str(path)])
    if res.returncode != 0:
        raise AgentError(f"launchctl bootstrap a échoué : {(res.stderr or res.stdout or '').strip()}")
    return path


def uninstall(*, home: Path | None = None, runner: Runner = schedule._run, uid: int | None = None,
              check_platform: bool = True) -> bool:
    """Unload the agent and remove the plist. True if something was there. Still loaded after bootout: AgentError, plist kept."""
    if check_platform:
        _require_macos()
    path = plist_path(home)
    was_loaded = _unload(runner, uid)
    existed = path.exists()
    path.unlink(missing_ok=True)
    return existed or was_loaded


@dataclass
class Status:
    installed: bool
    loaded: bool
    interval_s: int | None = None
    log_path: Path | None = None
    log_tail: list[str] | None = None


def status(*, repo: Path | None = None, home: Path | None = None, runner: Runner = schedule._run, uid: int | None = None,
           tail: int = 5, check_platform: bool = True) -> Status:
    if check_platform:
        _require_macos()
    repo = repo or schedule.repo_root()
    st = Status(installed=plist_path(home).exists(), loaded=is_loaded(runner, uid))
    if st.installed:
        st.interval_s = read_interval(home)
    log = repo / "data" / "logs" / SYNC_LOG
    if log.is_file():
        st.log_path = log
        try:
            with log.open("rb") as f:
                f.seek(max(0, log.stat().st_size - 16384))
                st.log_tail = f.read().decode("utf-8", errors="replace").splitlines()[-tail:]
        except OSError:
            st.log_tail = []
    return st


__all__ = ["LABEL", "AgentError", "build_plist", "build_shell_command", "install", "is_loaded", "plist_path", "read_interval",
           "status", "uninstall"]
