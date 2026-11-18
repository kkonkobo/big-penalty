import os
import re
from pathlib import Path

from jobhunt.filelock import try_lock, unlock


def test_second_descriptor_conflicts_until_unlocked(tmp_path):
    path = tmp_path / "x.lock"
    a = os.open(path, os.O_RDWR | os.O_CREAT)
    b = os.open(path, os.O_RDWR | os.O_CREAT)
    try:
        assert try_lock(a)
        assert not try_lock(b)
        assert not try_lock(b, shared=True)
        os.write(a, b"content is still writable\n")
        unlock(a)
        assert try_lock(b, shared=True)
        unlock(b)
    finally:
        os.close(a)
        os.close(b)
    assert path.read_bytes().startswith(b"content is still writable")


def test_only_filelock_imports_fcntl():
    """fcntl doesn't exist on Windows: importing it anywhere else crashes the Windows app at start-up."""
    src = Path(__file__).resolve().parents[1] / "src" / "jobhunt"
    offenders = [
        str(p.relative_to(src)) for p in src.rglob("*.py")
        if p.name != "filelock.py" and re.search(r"^\s*(import fcntl|from fcntl )", p.read_text(encoding="utf-8"), re.M)
    ]
    assert offenders == []
