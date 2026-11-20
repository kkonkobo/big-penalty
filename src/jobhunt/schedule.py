"""Local daily schedule, shared by the web server and CLI."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import plistlib
import re
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from threading import Lock, Thread
from time import monotonic, sleep, time
from typing import Callable, Iterable

from jobhunt.config import get_settings
from jobhunt.daily import LOCK_NAME, append_log, log_line
from jobhunt.filelock import local_file_lock, try_lock, unlock

LABEL = "com.bigpenalty.daily"
DEFAULT_TIME = "07:30"
POLL_SECONDS = 30
GRACE_SECONDS = 10 * 60
LOCK_TIMEOUT = 2.0
_SCHEDULE_MUTEX = Lock()
log = logging.getLogger(__name__)


class ScheduleError(Exception):
    pass


class ScheduleBusy(ScheduleError):
    pass


class DailyAlreadyRunning(ScheduleError):
    pass


@dataclass
class Schedule:
    times: list[str]
    enabled: bool


def parse_time(value: str) -> tuple[int, int]:
    match = re.fullmatch(r"([0-9]{1,2}):([0-9]{2})", value.strip())
    if not match or int(match[1]) > 23 or int(match[2]) > 59:
        raise ScheduleError(f"heure invalide : {value!r} (format HH:MM, ex. 07:30)")
    return int(match[1]), int(match[2])


def normalize_times(times: str | Iterable[str]) -> list[str]:
    return sorted({"%02d:%02d" % parse_time(value) for value in ([times] if isinstance(times, str) else times)})


def plist_path(home: Path | None = None) -> Path:
    return (home or Path.home()) / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def _legacy_times(home: Path | None) -> list[str]:
    try:
        interval = plistlib.loads(plist_path(home).read_bytes())["StartCalendarInterval"]
        entries = [interval] if isinstance(interval, dict) else interval
        return normalize_times("%02d:%02d" % (entry["Hour"], entry["Minute"]) for entry in entries)
    except (OSError, KeyError, TypeError, ValueError, plistlib.InvalidFileException, ScheduleError):
        return []


def _write_json(path: Path, value) -> None:
    stream = tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False)
    temp = Path(stream.name)
    try:
        with stream:
            json.dump(value, stream)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


@contextmanager
def _schedule_lock(data_dir: Path):
    """Serialize migration, edits and slot claims across the CLI and server processes."""
    deadline = monotonic() + LOCK_TIMEOUT
    if not _SCHEDULE_MUTEX.acquire(timeout=LOCK_TIMEOUT):
        raise ScheduleBusy("planification occupée, réessayez")
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        with (data_dir / "schedule.lock").open("a+b") as stream:
            fd = stream.fileno()
            while not try_lock(fd):
                remaining = deadline - monotonic()
                if remaining <= 0:
                    raise ScheduleBusy("planification occupée, réessayez")
                sleep(min(remaining, 0.025))
            try:
                yield
            finally:
                unlock(fd)
    finally:
        _SCHEDULE_MUTEX.release()


def _read_snapshot(path: Path) -> Schedule:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        raise ScheduleError("schedule.json invalide") from None
    if (not isinstance(value, dict) or type(value.get("enabled")) is not bool
            or not isinstance(value.get("times"), list) or not all(isinstance(item, str) for item in value["times"])):
        raise ScheduleError("schedule.json invalide")
    return Schedule(normalize_times(value["times"]), value["enabled"])


def _initial_schedule(home: Path | None) -> Schedule:
    return Schedule(_legacy_times(home) or [DEFAULT_TIME], True)


def _read_schedule(data_dir: Path, home: Path | None) -> Schedule:
    path = data_dir / "schedule.json"
    try:
        return _read_snapshot(path)
    except FileNotFoundError:
        settings = _initial_schedule(home)
        _write_json(path, vars(settings))
        return settings


def _edit(change, *, data_dir: Path | None = None, home: Path | None = None) -> Schedule:
    directory = data_dir or get_settings().data_dir
    try:
        with _schedule_lock(directory):
            settings = _read_schedule(directory, home)
            change(settings)
            _write_json(directory / "schedule.json", vars(settings))
            return settings
    except OSError:
        raise ScheduleError("impossible d’enregistrer la planification") from None


def set_times(times: Iterable[str], **kwargs) -> list[str]:
    return _edit(lambda settings: setattr(settings, "times", normalize_times(times)), **kwargs).times


def add_time(value: str, **kwargs) -> list[str]:
    def change(settings: Schedule) -> None:
        settings.times = normalize_times([*settings.times, value])

    return _edit(change, **kwargs).times


def remove_time(value: str, **kwargs) -> list[str]:
    target = normalize_times(value)[0]

    def change(settings: Schedule) -> None:
        if target not in settings.times:
            raise ScheduleError(f"{target} n'est pas planifié")
        settings.times.remove(target)

    return _edit(change, **kwargs).times


def set_enabled(enabled: bool, **kwargs) -> None:
    _edit(lambda settings: setattr(settings, "enabled", enabled), **kwargs)


def is_running(data_dir: Path) -> bool:
    """Probe, but never create or unlink, the persistent daily lock."""
    path = data_dir / LOCK_NAME
    with local_file_lock(path) as acquired:
        if not acquired:
            return True
        try:
            with path.open("r+b") as stream:
                fd = stream.fileno()
                if not try_lock(fd, shared=True):
                    return True
                unlock(fd)
                return False
        except FileNotFoundError:
            return False


def daily_command(*, executable: str | None = None, frozen: bool | None = None) -> list[str]:
    executable = executable or sys.executable
    if frozen is None:
        frozen = getattr(sys, "frozen", False)
    if frozen:
        return [executable, "daily"]
    return [executable, "-m", "jobhunt", "daily"]


def _launch_daily(data_dir: Path) -> None:
    env = {**os.environ, "JOBHUNT_DATA_DIR": str(data_dir.resolve())}
    if getattr(sys, "frozen", False):
        env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    options = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS}
               if os.name == "nt" else {"start_new_session": True})
    process = subprocess.Popen(daily_command(), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, env=env, **options)
    Thread(target=process.wait, name="jobhunt-daily-reaper", daemon=True).start()


def run_now(*, data_dir: Path | None = None, launcher: Callable[[Path], None] | None = None) -> None:
    directory = data_dir or get_settings().data_dir
    try:
        if is_running(directory):
            raise DailyAlreadyRunning("une recherche est déjà en cours")
        (launcher or _launch_daily)(directory)
    except OSError:
        raise ScheduleError("impossible de lancer la recherche") from None


class Scheduler:
    """Use elapsed real time for the grace window, and local wall time for slot IDs."""

    def __init__(self, *, data_dir: Path | None = None, home: Path | None = None,
                 clock: Callable[[], float] = time, launcher: Callable[[Path], None] | None = None):
        self.data_dir = data_dir or get_settings().data_dir
        self.home = home
        self.clock = clock
        self.launcher = launcher
        self.started = self.checked = clock()

    def tick(self) -> None:
        now = self.clock()
        try:
            with _schedule_lock(self.data_dir):
                settings = _read_schedule(self.data_dir, self.home)
                if settings.enabled and settings.times:
                    self._trigger(settings.times, now)
        except ScheduleBusy:
            return
        self.checked = now

    def _trigger(self, times: list[str], now: float) -> None:
        slots = {}
        cutoff = max(self.started, self.checked, now - GRACE_SECONDS)
        first_minute = (int(cutoff) // 60 + 1) * 60
        for instant in range(first_minute, int(now) + 1, 60):
            local = datetime.fromtimestamp(instant)
            if local.strftime("%H:%M") in times:
                slots[local.isoformat(timespec="minutes")] = local
        if not slots:
            return
        path = self.data_dir / "schedule-slots.json"
        try:
            previous = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(previous, list) or not all(isinstance(item, str) for item in previous):
                raise ValueError
            consumed = set(previous)
        except FileNotFoundError:
            consumed = set()
        except ValueError:
            raise ScheduleError("schedule-slots.json invalide") from None
        pending = slots.keys() - consumed
        if not pending:
            return
        _write_json(path, sorted(consumed | pending))
        latest = max(pending, key=lambda slot: slots[slot].timestamp())
        for slot in sorted(pending - {latest}):
            log.warning("Créneau %s ignoré : créneau plus récent dans la fenêtre de grâce", slot)
        try:
            run_now(data_dir=self.data_dir, launcher=self.launcher)
        except DailyAlreadyRunning:
            log.warning("Créneau %s ignoré : une recherche est déjà en cours", latest)
        except ScheduleError:
            log.error("Créneau %s : impossible de lancer la recherche", latest)

    async def run(self, stopped: asyncio.Event) -> None:
        while not stopped.is_set():
            try:
                await asyncio.to_thread(self.tick)
            except (ScheduleError, OSError):
                log.error("Impossible de vérifier la planification")
            try:
                await asyncio.wait_for(stopped.wait(), timeout=POLL_SECONDS)
            except TimeoutError:
                pass


def next_run_of(times: Iterable[str], now: datetime) -> datetime | None:
    """Find the next real local occurrence (including clock changes), not a nonexistent wall time."""
    timestamp = now.timestamp()
    local_now = datetime.fromtimestamp(timestamp)
    candidates = []
    for value in times:
        hour, minute = parse_time(value)
        for days in range(3):
            day = local_now + timedelta(days=days)
            wall = day.replace(hour=hour, minute=minute, second=0, microsecond=0)
            for fold in (0, 1):
                instant = wall.replace(fold=fold).timestamp()
                if instant > timestamp and datetime.fromtimestamp(instant) == wall:
                    candidates.append(instant)
    return datetime.fromtimestamp(min(candidates)) if candidates else None


@dataclass
class Status:
    times: list[str]
    enabled: bool
    running: bool
    next_run: datetime | None
    last_log: Path | None
    log_tail: list[str]


def status(*, data_dir: Path | None = None, home: Path | None = None,
           now: datetime | None = None, tail: int = 5) -> Status:
    directory = data_dir or get_settings().data_dir
    try:
        try:
            with _schedule_lock(directory):
                settings = _read_schedule(directory, home)
        except ScheduleBusy:
            try:
                settings = _read_snapshot(directory / "schedule.json")
            except FileNotFoundError:
                settings = _initial_schedule(home)
        last_log = max((directory / "logs").glob("daily-*.log"), default=None, key=lambda path: path.name)
        lines = last_log.read_text(encoding="utf-8", errors="replace").splitlines()[-tail:] if last_log and tail else []
        return Status(settings.times, settings.enabled, is_running(directory),
                      next_run_of(settings.times, now or datetime.now()) if settings.enabled else None, last_log, lines)
    except OSError:
        raise ScheduleError("impossible de lire la planification ou le journal") from None


def uninstall_launchd(*, home: Path | None = None, runner: Callable | None = None, uid: int | None = None) -> bool:
    """Remove only the legacy agent, and keep its plist if unloading cannot be confirmed."""
    if runner is None:
        if sys.platform != "darwin":
            raise ScheduleError("launchd n'est disponible que sur macOS")

        def runner(command):
            return subprocess.run(command, capture_output=True, text=True, timeout=10)

    service = f"gui/{os.getuid() if uid is None else uid}/{LABEL}"
    path = plist_path(home)

    def loaded() -> bool:
        result = runner(["launchctl", "print", service])
        if result.returncode == 0:
            return True
        if result.returncode == 113 or "Could not find service" in (result.stderr or ""):
            return False
        raise ScheduleError("impossible de vérifier l'agent launchd ; plist conservé")

    try:
        was_loaded = loaded()
        if was_loaded:
            runner(["launchctl", "bootout", service])
            if loaded():
                raise ScheduleError("launchctl bootout a échoué ; plist conservé")
        existed = path.exists()
        path.unlink(missing_ok=True)
        return was_loaded or existed
    except (OSError, subprocess.TimeoutExpired):
        raise ScheduleError("impossible de désinstaller l'agent launchd") from None
