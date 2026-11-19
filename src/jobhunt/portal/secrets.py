"""The device token: OS keyring (service `big-penalty-portal`, account = server origin) or, by explicit choice, a 0600 file.

No silent fallback: a missing keyring backend is an error that says how to choose the file store. The token is never logged,
never in an exception message and never in the journal."""

from __future__ import annotations

import contextlib
import importlib
import json
import os
import stat
from pathlib import Path
from typing import Any

from jobhunt.portal.journal import portal_dir

SERVICE = "big-penalty-portal"
TOKEN_FILE = "device-token"
KINDS = ("keyring", "file")


class PortalSecretError(Exception):
    """French, safe-to-display error about the token store (never carries the token)."""


def _load_keyring() -> Any | None:
    """The `keyring` module, None when not installed. (Test seam.)"""
    try:
        return importlib.import_module("keyring")
    except ImportError:
        return None


def _keyring() -> Any:
    kr = _load_keyring()
    usable = False
    if kr is not None:
        try:
            backend = kr.get_keyring()
            module = type(backend).__module__ or ""
            usable = not module.startswith(("keyring.backends.fail", "keyring.backends.null")) and float(backend.priority) > 0
        except Exception:  # noqa: BLE001
            usable = False
    if not usable:
        raise PortalSecretError("Trousseau indisponible : aucun service de trousseau utilisable sur cette machine. "
                                "Choisissez explicitement le stockage par fichier protégé (--token-store file).")
    return kr


def _check_kind(kind: str) -> None:
    if kind not in KINDS:
        raise PortalSecretError("Mode de stockage du jeton inconnu (keyring ou file).")


def _file() -> Path:
    return portal_dir() / TOKEN_FILE


def store_token(origin: str, token: str, kind: str) -> None:
    _check_kind(kind)
    if kind == "keyring":
        kr = _keyring()
        failure = ""
        try:
            kr.set_password(SERVICE, origin, token)
        except Exception as e:  # noqa: BLE001
            failure = type(e).__name__
        if failure:
            raise PortalSecretError(f"Trousseau indisponible ({failure}) : le jeton n'a pas été enregistré.")
        return
    path = _file()
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(fd, 0o600)
        os.write(fd, json.dumps({"origin": origin, "token": token}).encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)


def load_token(origin: str, kind: str) -> str | None:
    _check_kind(kind)
    if kind == "keyring":
        kr = _keyring()
        failure = ""
        value = None
        try:
            value = kr.get_password(SERVICE, origin)
        except Exception as e:  # noqa: BLE001
            failure = type(e).__name__
        if failure:
            raise PortalSecretError(f"Trousseau indisponible ({failure}).")
        return value or None
    path = _file()
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except FileNotFoundError:
        return None
    if os.name == "posix" and mode & 0o077:  # Windows has no POSIX mode bits; the file lives in the user profile
        raise PortalSecretError("Le fichier du jeton est lisible par d'autres utilisateurs : corrigez ses droits (chmod 600) avant de continuer.")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise PortalSecretError("Fichier du jeton illisible.") from None
    if not isinstance(data, dict) or data.get("origin") != origin or not isinstance(data.get("token"), str):
        return None  # bound to the origin it was paired with
    return data["token"] or None


def delete_token(origin: str, kind: str) -> None:
    _check_kind(kind)
    if kind == "keyring":
        kr = _keyring()
        failure = ""
        try:
            kr.delete_password(SERVICE, origin)
        except Exception as e:  # noqa: BLE001
            failure = type(e).__name__
        if failure and failure != "PasswordDeleteError":
            raise PortalSecretError(f"Trousseau indisponible ({failure}).")
        return
    with contextlib.suppress(FileNotFoundError):
        _file().unlink()
