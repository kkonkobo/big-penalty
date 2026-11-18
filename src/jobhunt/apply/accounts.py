"""ATS candidate accounts: one stored credential per (full hostname of the account page, e-mail).

Storage backends (see `get_backend`): the OS credential store through the `keyring` library (macOS Keychain, Windows
Credential Manager) or, as a fallback, a local JSON file `data/accounts.json` (gitignored). A non-secret index
`data/accounts-index.json` lists every account so they can be listed / exported whatever the backend.

Passwords are only ever returned by `get_password` / `create_password` / the export file: never logged, never put in an
exception message (errors are fixed French sentences, at most the exception TYPE name) and never in a process argument list."""

from __future__ import annotations

import contextlib
import importlib
import ipaddress
import json
import os
import re
import secrets
import string
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse

from jobhunt.config import get_settings
from jobhunt.apply.errors import ApplyError

SPECIALS = "-_+."
PASSWORD_LENGTH = 20
_SECOND_LEVEL = {"co", "com", "org", "net", "gov", "ac", "edu"}  # co.uk, com.au ... (heuristic for 2-letter ccTLDs)
# Suffixes treated as PUBLIC (the registrable domain is the suffix + one more label): common ccSLDs and multi-tenant hosting
# domains where unrelated tenants share a suffix. Built in: no public-suffix-list dependency.
PUBLIC_SUFFIXES = frozenset({
    "co.uk", "org.uk", "ac.uk", "gov.uk", "me.uk", "ltd.uk", "plc.uk", "net.uk", "sch.uk", "com.au", "net.au", "org.au", "edu.au",
    "gov.au", "co.nz", "org.nz", "net.nz", "co.jp", "or.jp", "ne.jp", "ac.jp", "com.br", "net.br", "org.br", "co.in", "net.in",
    "org.in", "co.za", "org.za", "com.mx", "org.mx", "com.tr", "org.tr", "com.cn", "net.cn", "org.cn", "com.hk", "com.sg", "com.ar",
    "com.co", "co.kr", "co.il", "co.id", "com.my", "com.ph", "com.pl", "com.ua", "com.ng", "com.eg", "com.sa", "com.pk",
    "asso.fr", "gouv.fr", "com.fr", "nom.fr", "prd.fr", "tm.fr", "avocat.fr", "notaires.fr", "cci.fr",
    "github.io", "gitlab.io", "netlify.app", "vercel.app", "herokuapp.com", "web.app", "firebaseapp.com", "pages.dev",
    "workers.dev", "azurewebsites.net", "cloudfront.net", "amazonaws.com", "appspot.com", "blogspot.com", "wordpress.com",
    "wixsite.com", "webflow.io", "notion.site", "carrd.co", "myshopify.com", "typeform.com", "onrender.com", "fly.dev",
    "surge.sh", "glitch.me", "repl.co", "ngrok.io", "ngrok-free.app", "trycloudflare.com", "azurestaticapps.net",
    "blob.core.windows.net", "s3.amazonaws.com", "cloudapp.azure.com", "myftpupload.com", "weebly.com", "squarespace.com",
    "wixstudio.io", "framer.app", "framer.website", "bubbleapps.io", "softr.app", "super.site", "gitbook.io", "readthedocs.io",
})
_SAFE_FIELD = re.compile(r"[A-Za-z0-9 ._@:+\u2014-]{1,200}")  # service / account / label: no quote, backslash, newline...


_LABEL = re.compile(r"[a-z0-9_](?:[a-z0-9_-]{0,61}[a-z0-9_])?")


def _hostname(host: str) -> str:
    """Canonical host of a host / "host:port" / "user@host" / URL: lowercase, no userinfo / port / scheme, ONE trailing dot
    dropped, unicode labels IDNA-encoded (punycode), IPv6 in its compressed form. "" when it is not a valid host (empty
    label, forbidden character, IDNA failure): callers treat that as "not allowed"."""
    host = (host or "").strip().lower()
    if not host or any(ch in host for ch in "\\ \t\r\n"):
        return ""
    with contextlib.suppress(ValueError):  # bare IP literal ("::1", "10.0.0.1")
        return str(ipaddress.ip_address(host))
    try:
        host = urlparse(host if "//" in host else "//" + host).hostname or ""
    except ValueError:
        return ""
    host = host.removesuffix(".")
    if not host:
        return ""
    with contextlib.suppress(ValueError):
        return str(ipaddress.ip_address(host))
    try:
        host = host.encode("idna").decode("ascii").lower()
    except (UnicodeError, ValueError):
        return ""
    return host if all(_LABEL.fullmatch(lab) for lab in host.split(".")) else ""


hostname = _hostname  # public: canonical host (see _hostname); "" when invalid


def is_ip(host: str) -> bool:
    return _is_ip(_hostname(host))


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def public_suffix_len(host: str) -> int:
    """Number of labels of the public suffix of a canonical host (1 for a plain TLD)."""
    labels = host.split(".")
    for n in range(min(len(labels), 4), 1, -1):
        if ".".join(labels[-n:]) in PUBLIC_SUFFIXES:
            return n
    if len(labels) >= 3 and len(labels[-1]) == 2 and labels[-2] in _SECOND_LEVEL:
        return 2
    return 1


def is_public_suffix(host: str) -> bool:
    """The host IS a public suffix (a TLD, "co.uk", "github.io"): nobody owns it."""
    h = _hostname(host)
    return bool(h) and not _is_ip(h) and len(h.split(".")) <= public_suffix_len(h)


def registrable_host(host: str) -> str:
    """"jobs.example.co.uk:443" / "https://careers.Example.com/x" -> "example.co.uk" / "example.com"; for a multi-tenant
    suffix ("a.github.io") the tenant. The host is canonicalised first (see `hostname`). IP addresses, "localhost" and a
    host that is itself a public suffix are returned unchanged; an invalid host gives "". Names the browser profile directory
    and decides "same site"."""
    host = _hostname(host)
    labels = host.split(".")
    if not host or _is_ip(host) or len(labels) <= 1:
        return host
    n = public_suffix_len(host) + 1
    return host if len(labels) <= n - 1 else ".".join(labels[-n:])


def service_name(host: str) -> str:
    """Keychain service of an account: scoped to the FULL hostname of the account page (lowercase, no port), so a password
    created for one site is never offered to another subdomain / tenant of the same registrable domain."""
    return "big-penalty:" + _hostname(host)


KEYRING_NAME = "trousseau du système"
FILE_NAME = "fichier local data/accounts.json"
ENV_BACKEND = "JOBHUNT_ACCOUNTS_BACKEND"
EXPORT_VERSION = 1
_BAD_NAME = "Nom de compte non pris en charge par le trousseau"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Backend(Protocol):
    name: str

    def get(self, service: str, account: str) -> str | None: ...

    def set(self, service: str, account: str, password: str, label: str) -> None: ...

    def delete(self, service: str, account: str) -> None: ...


def _load_keyring() -> Any | None:
    """The `keyring` module, imported lazily; None when it is not installed. (Test seam.)"""
    try:
        return importlib.import_module("keyring")
    except ImportError:
        return None


def _keyring_usable(kr: Any | None) -> bool:
    """A real backend is active: not the `fail` / `null` ones, priority > 0."""
    if kr is None:
        return False
    try:
        backend = kr.get_keyring()
        module = type(backend).__module__ or ""
        if module.startswith(("keyring.backends.fail", "keyring.backends.null")):
            return False
        return float(backend.priority) > 0
    except Exception:  # noqa: BLE001 - NoKeyringError & co: not usable
        return False


class KeyringBackend:
    name = KEYRING_NAME

    def __init__(self, kr: Any) -> None:
        self._kr = kr

    @staticmethod
    def _call(fn: Any, *args: str, ignore: str = "") -> Any:
        failure = ""
        try:
            return fn(*args)
        except Exception as e:  # noqa: BLE001
            failure = type(e).__name__  # keep the TYPE only: the exception may hold the password
        if failure != ignore:
            raise ApplyError(f"Trousseau indisponible : {failure}")  # raised outside `except`: no __context__ either
        return None

    def get(self, service: str, account: str) -> str | None:
        return self._call(self._kr.get_password, service, account) or None

    def set(self, service: str, account: str, password: str, label: str) -> None:  # the label has no place in keyring
        self._call(self._kr.set_password, service, account, password)

    def delete(self, service: str, account: str) -> None:
        self._call(self._kr.delete_password, service, account, ignore="PasswordDeleteError")  # already absent: fine


def _write_json_atomic(path: Path, data: Any, *, private: bool = True) -> None:
    """Write `data` as JSON to a temp file in the same folder (mode 0600 where supported), then `os.replace` it."""
    tmp = ""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            if private:
                with contextlib.suppress(OSError, NotImplementedError):
                    os.chmod(tmp, 0o600)
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except OSError as e:
        if tmp:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
        raise ApplyError(f"Écriture impossible ({type(e).__name__}) : {path.name}") from None


def _read_json(path: Path, label: str) -> Any | None:
    """Parsed JSON of `path`; None when the file does not exist; ApplyError when unreadable / corrupt (never overwritten)."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError) as e:
        raise ApplyError(f"{label} illisible ({type(e).__name__}) : {path.name}") from None
    try:
        return json.loads(text)
    except ValueError:
        raise ApplyError(f"{label} corrompu : {path.name} (le corriger ou le supprimer à la main)") from None


class FileBackend:
    """`data/accounts.json`: {service: {account: {"password", "label", "updated_at"}}}. Passwords are in clear text there."""

    name = FILE_NAME

    @property
    def path(self) -> Path:
        return get_settings().data_dir / "accounts.json"

    def _load(self) -> dict[str, dict[str, dict[str, Any]]]:
        data = _read_json(self.path, "Fichier de comptes")
        if data is None:
            return {}
        if not isinstance(data, dict) or not all(isinstance(v, dict) and all(isinstance(e, dict) for e in v.values())
                                                 for v in data.values()):
            raise ApplyError(f"Fichier de comptes corrompu : {self.path.name} (le corriger ou le supprimer à la main)")
        return data

    def get(self, service: str, account: str) -> str | None:
        pw = self._load().get(service, {}).get(account, {}).get("password")
        return pw if isinstance(pw, str) and pw else None

    def set(self, service: str, account: str, password: str, label: str) -> None:
        data = self._load()
        data.setdefault(service, {})[account] = {"password": password, "label": label, "updated_at": _now()}
        _write_json_atomic(self.path, data)

    def delete(self, service: str, account: str) -> None:
        data = self._load()
        if account in data.get(service, {}):
            del data[service][account]
            if not data[service]:
                del data[service]
            _write_json_atomic(self.path, data)


_backend_override: Backend | None = None  # tests inject an in-memory backend here


def get_backend() -> Backend:
    """JOBHUNT_ACCOUNTS_BACKEND = keyring | file | auto (default). auto: the system keyring when a real backend is usable,
    else the local file. The choice is made BEFORE any access: an error at call time never falls back to the file."""
    if _backend_override is not None:
        return _backend_override
    choice = (os.environ.get(ENV_BACKEND) or "auto").strip().lower()
    if choice not in {"auto", "keyring", "file"}:
        raise ApplyError(f"{ENV_BACKEND} doit valoir keyring, file ou auto")
    if choice == "file":
        return FileBackend()
    kr = _load_keyring()
    if _keyring_usable(kr):
        return KeyringBackend(kr)
    if choice == "keyring":
        raise ApplyError("Trousseau indisponible : aucun trousseau du système utilisable")
    return FileBackend()


def backend_name() -> str:
    return get_backend().name


# ---- non-secret index: data/accounts-index.json -> [{service, account, host, label, created_at}] ----

def _index_path() -> Path:
    return get_settings().data_dir / "accounts-index.json"


def _load_index() -> list[dict[str, Any]]:
    data = _read_json(_index_path(), "Index des comptes")
    if data is None:
        return []
    if not isinstance(data, list) or not all(isinstance(e, dict) and isinstance(e.get("service"), str)
                                             and isinstance(e.get("account"), str) for e in data):
        raise ApplyError("Index des comptes corrompu : accounts-index.json (le corriger ou le supprimer à la main)")
    return data


STATES = ("pending", "active")
DEFAULT_STATE = "active"          # an index entry without `state` (older versions, imports) is a working account
PENDING_GRACE_S = 600             # a signup younger than this is never trusted to exist (the run may still be on that page)


def _index_add(host: str, email: str, state: str = DEFAULT_STATE) -> None:
    service = service_name(host)
    entries = _load_index()
    if any(e["service"] == service and e["account"] == email for e in entries):
        return
    entries.append({"service": service, "account": email, "host": _hostname(host), "label": _label(host), "created_at": _now(),
                    "state": state, "state_at": _now()})
    _write_json_atomic(_index_path(), entries, private=False)


def set_state(host: str, email: str, state: str) -> bool:
    """Record whether the account really exists on the site: `pending` (password created for a signup that may have failed)
    or `active` (a run went past the account pages, or the site now waits for the e-mail link). False when the account is
    not in the index. Unknown states are refused."""
    if state not in STATES:
        raise ApplyError("État de compte inconnu")
    if not _hostname(host):
        raise ApplyError(_BAD_NAME)
    service = service_name(host)
    entries = _load_index()
    for e in entries:
        if e["service"] == service and e["account"] == email:
            if e.get("state", DEFAULT_STATE) != state:
                e["state"], e["state_at"] = state, _now()
                _write_json_atomic(_index_path(), entries, private=False)
            return True
    return False


def account_state(host: str, email: str) -> str | None:
    """`pending` / `active`, None when the index does not know the account."""
    service = service_name(host)
    for e in _load_index():
        if e["service"] == service and e["account"] == email:
            return e.get("state") if e.get("state") in STATES else DEFAULT_STATE
    return None


def account_known(host: str, email: str) -> bool:
    """Does an account really exist on `host` for `email`? A stored password AND state `active`. A `pending` account (its
    signup was never confirmed by a later page) is not known, so the engine signs up again with the stored password instead of
    logging in to an account that does not exist. A credential without index entry counts as active (it predates the index).
    Any error -> False."""
    try:
        if get_password(host, email) is None:
            return False
        state = account_state(host, email)
        return state in (None, "active")
    except ApplyError:
        return False


def _label(host: str) -> str:
    return f"Big Penalty — {_hostname(host)}"


def _check_name(host: str, email: str) -> None:
    """Hosts / accounts that would need quoting are refused before any backend is touched."""
    if not _hostname(host):
        raise ApplyError(_BAD_NAME)
    if not all(_SAFE_FIELD.fullmatch(v) for v in (service_name(host), email, _label(host))):
        raise ApplyError(_BAD_NAME)


def get_password(host: str, email: str) -> str | None:
    """The stored password, or None when there is no credential for this host and e-mail."""
    if not _hostname(host):
        raise ApplyError(_BAD_NAME)
    return get_backend().get(service_name(host), email)

def generate_password() -> str:
    rnd = secrets.SystemRandom()
    chars = [secrets.choice(string.ascii_uppercase), secrets.choice(string.ascii_lowercase), secrets.choice(string.digits),
             secrets.choice(SPECIALS)]
    chars += [secrets.choice(string.ascii_letters + string.digits) for _ in range(PASSWORD_LENGTH - len(chars))]
    rnd.shuffle(chars)
    if chars[0] in SPECIALS:  # never start with "-" / "+" (option-like)
        k = next(i for i, c in enumerate(chars) if c not in SPECIALS)
        chars[0], chars[k] = chars[k], chars[0]
    return "".join(chars)


def create_password(host: str, email: str) -> str:
    """The existing password if any (never regenerated), else a new random one stored in the backend (and returned), read back
    to verify and recorded in the index."""
    _check_name(host, email)
    backend = get_backend()
    service = service_name(host)
    existing = backend.get(service, email)
    if existing:
        _index_add(host, email)  # a credential that was not indexed yet is an older, working one
        return existing
    pw = generate_password()
    backend.set(service, email, pw, _label(host))
    if backend.get(service, email) != pw:
        raise ApplyError("Enregistrement dans le trousseau impossible")
    _index_add(host, email, "pending")  # until a run gets past the account pages
    return pw


def list_accounts() -> list[dict[str, Any]]:
    """Index entries (no password) plus `stored` (the backend still has a password for it) and `state` (pending / active)."""
    backend = get_backend()
    return [{**e, "state": e.get("state") if e.get("state") in STATES else DEFAULT_STATE,
             "stored": backend.get(e["service"], e["account"]) is not None} for e in _load_index()]


def delete_account(host: str, email: str) -> None:
    """Remove the credential from the backend and the index entry."""
    if not _hostname(host):
        raise ApplyError(_BAD_NAME)
    service = service_name(host)
    get_backend().delete(service, email)
    entries = _load_index()
    kept = [e for e in entries if not (e["service"] == service and e["account"] == email)]
    if len(kept) != len(entries):
        _write_json_atomic(_index_path(), kept, private=False)


def export_accounts(path: Path, *, overwrite: bool = False) -> int:
    """Write every stored account WITH its password (clear text, mode 0600) to `path`; returns the count."""
    path = Path(path)
    if path.exists() and not overwrite:
        raise ApplyError(f"Le fichier existe déjà : {path.name} (--overwrite pour l'écraser)")
    backend = get_backend()
    out = []
    for e in _load_index():
        pw = backend.get(e["service"], e["account"])
        if pw:
            out.append({"service": e["service"], "account": e["account"], "host": e.get("host", ""),
                        "label": e.get("label", ""), "password": pw, "state": e.get("state", DEFAULT_STATE)})
    _write_json_atomic(path, {"version": EXPORT_VERSION, "exported_at": _now(), "accounts": out})
    return len(out)


def _parse_import(path: Path) -> list[dict[str, str]]:
    data = _read_json(Path(path), "Fichier d'import")
    if data is None:
        raise ApplyError(f"Fichier d'import introuvable : {Path(path).name}")
    bad = ApplyError("Fichier d'import invalide : format inattendu")
    if not isinstance(data, dict) or data.get("version") != EXPORT_VERSION or not isinstance(data.get("accounts"), list):
        raise bad
    entries = []
    for e in data["accounts"]:
        if not isinstance(e, dict):
            raise bad
        host, account, pw = e.get("host"), e.get("account"), e.get("password")
        if not all(isinstance(v, str) for v in (host, account, pw)) or not pw or len(pw) > 1000 or not pw.isprintable():
            raise bad
        h = _hostname(host)
        if not h or e.get("service") != service_name(h):
            raise bad
        _check_name(h, account)  # same allow-list as create_password
        entries.append({"host": h, "account": account, "password": pw,
                        "state": e.get("state") if e.get("state") in STATES else DEFAULT_STATE})
    return entries


def import_accounts(path: Path, *, overwrite: bool = False) -> tuple[int, int]:
    """Import an export file. Returns (imported, skipped). An account that already exists with a DIFFERENT password is
    skipped unless `overwrite`; one that already has the same password counts as imported. Everything is validated first."""
    entries = _parse_import(path)
    backend = get_backend()
    imported = skipped = 0
    for e in entries:
        service = service_name(e["host"])
        current = backend.get(service, e["account"])
        if current is not None and current != e["password"] and not overwrite:
            skipped += 1
            continue
        if current != e["password"]:
            backend.set(service, e["account"], e["password"], _label(e["host"]))
        _index_add(e["host"], e["account"], e["state"])
        imported += 1
    return imported, skipped
