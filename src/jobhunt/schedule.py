"""Daily scheduling with launchd (macOS): `jobhunt schedule install|set|add|remove|run|status|uninstall`.

The agent `com.bigpenalty.daily` has a StartCalendarInterval: always an ARRAY of {Hour, Minute} dicts (one per
time of day). The plist is the source of truth for the times (read back with plistlib). If the Mac is asleep at that time,
launchd runs the missed job as soon as it wakes: that is the catch-up behaviour we rely on. If the Mac is
powered off, nothing runs (and nothing is replayed at boot): the next day's run finds the extra days anyway
because searches ask for the last 14 days and "new today" is measured since the latest completed run.

The external SSD may be unmounted: the wrapper command exits 0 silently when the repo directory is missing.
launchd's own StandardOut/ErrorPath live in ~/Library/Logs/big-penalty (always mounted), so launchd never fails to
spawn the job; once the repo is known to be there, the wrapper redirects the run's output to data/logs.

Pure functions (`build_plist`, `build_shell_command`, `next_run`) are separate from the side effects
(`install`, `uninstall`, `status`), which take injectable paths and a command runner so tests never touch the real
LaunchAgents directory or launchctl.
"""

from __future__ import annotations

import os
import plistlib
import fcntl
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable

LABEL = "com.bigpenalty.daily"
DEFAULT_TIME = "07:30"
Runner = Callable[[list[str]], Any]  # returns an object with .returncode / .stdout


class ScheduleError(Exception):
    pass


def repo_root() -> Path:
    """Repository root, derived from the package location (src/jobhunt/schedule.py -> repo)."""
    return Path(__file__).resolve().parents[2]


def parse_time(value: str) -> tuple[int, int]:
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", value.strip())
    if not m or int(m[1]) > 23 or int(m[2]) > 59:
        raise ScheduleError(f"heure invalide : {value!r} (format HH:MM, ex. 07:30)")
    return int(m[1]), int(m[2])


def plist_path(home: Path | None = None) -> Path:
    return (home or Path.home()) / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def launchd_log_dir(home: Path | None = None) -> Path:
    return (home or Path.home()) / "Library" / "Logs" / "big-penalty"


def build_shell_command(repo: Path) -> str:
    """Exits 0 quietly when the repo (external SSD) is not mounted; otherwise sends the run's output to data/logs
    and runs the daily command."""
    r = shlex.quote(str(repo))
    return (f"[ -d {r} ] || exit 0; cd {r} && mkdir -p data/logs && "
            "exec >> data/logs/launchd.out.log 2>> data/logs/launchd.err.log && . ./env.sh && uv run jobhunt daily")


def launch_path(home: Path | None = None) -> str:
    """PATH for the agent: `zsh -l` does not read ~/.zshrc, and uv usually lives in ~/.local/bin or Homebrew."""
    h = home or Path.home()
    return ":".join([str(h / ".local" / "bin"), "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin",
                     "/usr/sbin", "/sbin"])


def normalize_times(times: str | Iterable[str]) -> list[str]:
    """Validate HH:MM strings, dedupe and sort them; returns zero-padded "HH:MM". Raises on empty input."""
    if isinstance(times, str):
        times = [times]
    out = sorted({"%02d:%02d" % parse_time(t) for t in times})
    if not out:
        raise ScheduleError("aucune heure indiquée (format HH:MM, ex. 07:30)")
    return out


def build_plist(repo: Path, times: str | Iterable[str], *, home: Path | None = None) -> bytes:
    """The launchd property list (XML bytes) for the daily agent. Pure. StartCalendarInterval is always an array."""
    intervals = [{"Hour": h, "Minute": m} for h, m in map(parse_time, normalize_times(times))]
    logs = launchd_log_dir(home)
    return plistlib.dumps({
        "Label": LABEL,
        "ProgramArguments": ["/bin/zsh", "-lc", build_shell_command(repo)],
        "StartCalendarInterval": intervals,
        "StandardOutPath": str(logs / "launchd.out.log"),
        "StandardErrorPath": str(logs / "launchd.err.log"),
        "EnvironmentVariables": {"PATH": launch_path(home)},
        "RunAtLoad": False,
    })


def next_run_of(times: Iterable[str], now: datetime) -> datetime:
    """Earliest next local occurrence among several HH:MM times."""
    return min(next_run(*parse_time(t), now) for t in times)


def next_run(hour: int, minute: int, now: datetime) -> datetime:
    """Next local occurrence of HH:MM strictly after `now`."""
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return candidate if candidate > now else candidate + timedelta(days=1)


def _domain(uid: int | None = None) -> str:
    return f"gui/{os.getuid() if uid is None else uid}"


def _run(cmd: list[str]) -> Any:
    return subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=60)


def _require_macos() -> None:
    if sys.platform != "darwin":
        raise ScheduleError("launchd n'existe que sous macOS")


def is_loaded(runner: Runner = _run, uid: int | None = None) -> bool:
    return runner(["launchctl", "print", f"{_domain(uid)}/{LABEL}"]).returncode == 0


def _unload(runner: Runner, uid: int | None) -> bool:
    """Boot the agent out if it is loaded. Returns whether it was loaded; raises if it is still loaded after."""
    if not is_loaded(runner, uid):
        return False
    res = runner(["launchctl", "bootout", f"{_domain(uid)}/{LABEL}"])
    if is_loaded(runner, uid):
        raise ScheduleError(f"launchctl bootout a échoué, l'agent est toujours chargé (rien n'a été modifié) : "
                            f"{(res.stderr or res.stdout or '').strip() or 'code ' + str(res.returncode)}")
    return True


def read_times(home: Path | None = None) -> list[str] | None:
    """Times stored in the plist (the source of truth); None if there is no readable plist."""
    path = plist_path(home)
    try:
        interval = plistlib.loads(path.read_bytes())["StartCalendarInterval"]
        if isinstance(interval, dict):  # tolerate a hand-edited single dict
            interval = [interval]
        return sorted({"%02d:%02d" % (int(i["Hour"]), int(i["Minute"])) for i in interval}) or None
    except (OSError, KeyError, TypeError, ValueError, plistlib.InvalidFileException):
        return None


def install(times: str | Iterable[str] = DEFAULT_TIME, *, repo: Path | None = None, home: Path | None = None,
            runner: Runner = _run, uid: int | None = None, check_platform: bool = True) -> Path:
    """Write the plist, create the log directories and load the agent (replacing a previously loaded one).
    `times`: one or several HH:MM. If the loaded agent cannot be booted out, nothing is changed and ScheduleError
    is raised."""
    if check_platform:
        _require_macos()
    normalized = normalize_times(times)
    repo = repo or repo_root()
    path = plist_path(home)
    _unload(runner, uid)  # before any change: a failure leaves the old plist and agent untouched
    (repo / "data" / "logs").mkdir(parents=True, exist_ok=True)
    launchd_log_dir(home).mkdir(parents=True, exist_ok=True)  # launchd does not create log directories
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(build_plist(repo, normalized, home=home))
    res = runner(["launchctl", "bootstrap", _domain(uid), str(path)])
    if res.returncode != 0:
        raise ScheduleError(f"launchctl bootstrap a échoué : {(res.stderr or res.stdout or '').strip()}")
    return path


def set_times(times: str | Iterable[str], **kw: Any) -> Path:
    """`schedule set`: replace all times (installs when not installed)."""
    return install(times, **kw)


def add_time(time: str, **kw: Any) -> list[str]:
    """Add one time to the installed schedule (installs with just that time if nothing is installed)."""
    new = normalize_times(list(read_times(kw.get("home")) or []) + [time])
    install(new, **kw)
    return new


def remove_time(time: str, **kw: Any) -> list[str]:
    cur = read_times(kw.get("home"))
    if cur is None:
        raise ScheduleError("aucune planification installée")
    target = normalize_times(time)[0]
    if target not in cur:
        raise ScheduleError(f"{target} n'est pas planifié (heures : {', '.join(cur)})")
    new = [t for t in cur if t != target]
    if not new:
        raise ScheduleError("c'est la dernière heure : utilisez `schedule uninstall` pour tout désactiver")
    install(new, **kw)
    return new


def is_running(data_dir: Path | None = None, repo: Path | None = None) -> bool:
    """Whether a daily run holds data/daily.lock. Non-blocking probe; the lock is never kept nor created."""
    path = (data_dir or (repo or repo_root()) / "data") / "daily.lock"
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except OSError:
            return True
        return False  # closing the fd below drops our shared lock
    finally:
        os.close(fd)


def run_now(*, repo: Path | None = None, home: Path | None = None, runner: Runner = _run,
            uid: int | None = None, check_platform: bool = True) -> None:
    """On-demand run through launchd (`kickstart`, returns immediately). The daily lock prevents any overlap."""
    if check_platform:
        _require_macos()
    if not is_loaded(runner, uid):
        raise ScheduleError("planification non installée : lancez `big-penalty daily` directement, "
                            "ou `big-penalty schedule install` d'abord")
    if is_running(repo=repo):
        raise ScheduleError("une recherche est déjà en cours")
    res = runner(["launchctl", "kickstart", f"{_domain(uid)}/{LABEL}"])
    if res.returncode != 0:
        raise ScheduleError(f"launchctl kickstart a échoué : {(res.stderr or res.stdout or '').strip()}")


def uninstall(*, home: Path | None = None, runner: Runner = _run, uid: int | None = None,
              check_platform: bool = True, repo: Path | None = None) -> bool:
    """Unload the agent and remove the plist. Returns True if something was there. If the agent is still loaded
    after bootout, raises ScheduleError and keeps the plist."""
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
    times: list[str] | None = None
    running: bool = False
    next_run: datetime | None = None
    last_log: Path | None = None
    log_tail: list[str] | None = None


def latest_daily_log(logs: Path) -> Path | None:
    return max(logs.glob("daily-*.log"), default=None, key=lambda p: p.name) if logs.is_dir() else None


def status(*, repo: Path | None = None, home: Path | None = None, runner: Runner = _run, uid: int | None = None,
           now: datetime | None = None, tail: int = 5, check_platform: bool = True) -> Status:
    if check_platform:
        _require_macos()
    repo = repo or repo_root()
    path = plist_path(home)
    st = Status(installed=path.exists(), loaded=is_loaded(runner, uid))
    st.running = is_running(repo=repo)
    if st.installed:
        st.times = read_times(home)
        if st.times:
            st.next_run = next_run_of(st.times, now or datetime.now().astimezone())
    st.last_log = latest_daily_log(repo / "data" / "logs")
    if st.last_log:
        st.log_tail = st.last_log.read_text(encoding="utf-8", errors="replace").splitlines()[-tail:]
    return st
