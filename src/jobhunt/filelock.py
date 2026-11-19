"""Non-blocking advisory lock on an open file descriptor, on macOS, Linux and Windows.

macOS / Linux: flock(2) on the whole file, exclusive or shared. Two descriptors conflict even inside one process.
Windows: there is no fcntl; a one-byte lock (msvcrt.locking) far past the file's content stands in for the whole
file. It is always exclusive, which is enough for "is another run holding it?" probes, and it never blocks reads or
writes of the content itself. Like flock, the OS drops it when the process dies.
"""

from __future__ import annotations

import os
import sys
from contextlib import contextmanager
from pathlib import Path
from threading import Lock
from typing import Iterator

_active_paths: set[Path] = set()
_active_mutex = Lock()


@contextmanager
def local_file_lock(path: Path) -> Iterator[bool]:
    """Nonblocking thread guard for callers that share a persistent file lock."""
    key = path.resolve()
    with _active_mutex:
        acquired = key not in _active_paths
        if acquired:
            _active_paths.add(key)
    try:
        yield acquired
    finally:
        if acquired:
            with _active_mutex:
                _active_paths.remove(key)

if sys.platform == "win32":
    import msvcrt

    _LOCK_OFFSET = 1 << 30  # beyond any real content; Windows allows locking past end of file

    def _at_lock_byte(fd: int, mode: int) -> None:
        position = os.lseek(fd, 0, os.SEEK_CUR)
        os.lseek(fd, _LOCK_OFFSET, os.SEEK_SET)
        try:
            msvcrt.locking(fd, mode, 1)
        finally:
            os.lseek(fd, position, os.SEEK_SET)

    def try_lock(fd: int, shared: bool = False) -> bool:
        try:
            _at_lock_byte(fd, msvcrt.LK_NBLCK)
        except OSError:
            return False
        return True

    def unlock(fd: int) -> None:
        _at_lock_byte(fd, msvcrt.LK_UNLCK)

else:
    import fcntl

    def try_lock(fd: int, shared: bool = False) -> bool:
        try:
            fcntl.flock(fd, (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB)
        except OSError:
            return False
        return True

    def unlock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)
