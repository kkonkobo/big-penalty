"""Server configuration, read once from the environment (Dokploy env). Errors never contain a secret value."""

from __future__ import annotations

import base64
import binascii
import ipaddress
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from urllib.parse import urlsplit

PAIRING_CODE_MIN = 32
_HEX64 = re.compile(r"^[0-9a-fA-F]{64}$")


class ConfigError(ValueError):
    """Invalid or missing configuration. The message names the variable, never its value."""


def parse_key(name: str, raw: str) -> bytes:
    """A 32-byte key written as 64 hex characters or as base64url (padding optional)."""
    text = (raw or "").strip()
    if _HEX64.match(text):
        return bytes.fromhex(text)
    try:
        data = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (binascii.Error, ValueError):
        data = b""
    if len(data) != 32:
        raise ConfigError(f"{name}: expected exactly 32 bytes, written as 64 hex characters or base64url")
    return data


def _origin(raw: str, allow_insecure: bool) -> str:
    text = (raw or "").strip()
    try:
        parts = urlsplit(text)
        port = parts.port
    except ValueError:
        raise ConfigError("PORTAL_PUBLIC_ORIGIN: not a valid origin") from None
    host = (parts.hostname or "").lower()
    if parts.path not in ("", "/") or parts.query or parts.fragment or parts.username or parts.password or not host:
        raise ConfigError("PORTAL_PUBLIC_ORIGIN: must be scheme://host[:port] with no path, query or credentials")
    if parts.scheme == "https":
        pass
    elif parts.scheme == "http" and allow_insecure and host in ("127.0.0.1", "localhost") and port:
        pass
    else:
        raise ConfigError("PORTAL_PUBLIC_ORIGIN: must be https://host[:port] "
                          "(http://127.0.0.1:PORT only with PORTAL_ALLOW_INSECURE_LOCAL=1)")
    return f"{parts.scheme}://{host}" + (f":{port}" if port else "")


def _base_path(raw: str) -> str:
    if not raw.startswith("/") or raw.endswith("/") or "//" in raw or not re.fullmatch(r"/[A-Za-z0-9._~/-]+", raw):
        raise ConfigError("PORTAL_BASE_PATH: must start with '/', have no trailing '/' and contain only URL-safe characters")
    return raw


def _int(name: str, raw: str, low: int, high: int) -> int:
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(f"{name}: not an integer") from None
    if not low <= value <= high:
        raise ConfigError(f"{name}: out of range")
    return value


@dataclass(frozen=True)
class Config:
    data_key: bytes = field(repr=False)
    public_origin: str
    data_key_old: bytes | None = field(default=None, repr=False)
    base_path: str = "/app"
    db_path: str = "/data/portal.sqlite3"
    trusted_proxies: tuple = ()  # tuple of ipaddress networks
    pairing_code: str | None = field(default=None, repr=False)
    pairing_expires: float | None = None  # epoch seconds
    max_devices: int = 1
    version: str = "dev"
    allow_insecure_local: bool = False
    max_total_bytes: int = 1 << 30
    hold_all_on_start: bool = False

    @property
    def pairing_enabled(self) -> bool:
        return bool(self.pairing_code) and len(self.pairing_code or "") >= PAIRING_CODE_MIN and self.pairing_expires is not None

    @property
    def allowed_hosts(self) -> frozenset[str]:
        """Host header values accepted: the public origin's host[:port] (default ports may be omitted)."""
        parts = urlsplit(self.public_origin)
        netloc = parts.netloc.lower()
        hosts = {netloc}
        if parts.scheme == "https" and parts.port == 443 or parts.scheme == "http" and parts.port == 80:
            hosts.add((parts.hostname or "").lower())
        return frozenset(hosts)

    @property
    def cookie_prefix_path(self) -> str:
        return self.base_path + "/"

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> Config:
        env = {k: v for k, v in environ.items() if k.startswith("PORTAL_")}

        def get(name: str, default: str | None = None) -> str | None:
            value = env.get(name)
            return value if value not in (None, "") else default

        raw_key = get("PORTAL_DATA_KEY")
        if not raw_key:
            raise ConfigError("PORTAL_DATA_KEY is required")
        raw_old = get("PORTAL_DATA_KEY_OLD")
        raw_origin = get("PORTAL_PUBLIC_ORIGIN")
        if not raw_origin:
            raise ConfigError("PORTAL_PUBLIC_ORIGIN is required")
        insecure = get("PORTAL_ALLOW_INSECURE_LOCAL", "") == "1"
        proxies = []
        for item in (get("PORTAL_TRUSTED_PROXIES", "") or "").split(","):
            if item.strip():
                try:
                    proxies.append(ipaddress.ip_network(item.strip(), strict=False))
                except ValueError:
                    raise ConfigError("PORTAL_TRUSTED_PROXIES: not a comma-separated list of CIDRs") from None
        code, expires = get("PORTAL_PAIRING_CODE"), get("PORTAL_PAIRING_EXPIRES")
        expires_at = None
        if expires:
            try:
                parsed = datetime.fromisoformat(expires.strip())
            except ValueError:
                raise ConfigError("PORTAL_PAIRING_EXPIRES: not an ISO datetime") from None
            if parsed.tzinfo is None:
                raise ConfigError("PORTAL_PAIRING_EXPIRES: the ISO datetime needs a timezone")
            expires_at = parsed.astimezone(UTC).timestamp()
        key = parse_key("PORTAL_DATA_KEY", raw_key)
        old = parse_key("PORTAL_DATA_KEY_OLD", raw_old) if raw_old else None
        if old is not None and old == key:
            raise ConfigError("PORTAL_DATA_KEY_OLD: must differ from PORTAL_DATA_KEY")
        return cls(
            data_key=key, data_key_old=old, public_origin=_origin(raw_origin, insecure),
            base_path=_base_path(get("PORTAL_BASE_PATH", "/app") or "/app"),
            db_path=get("PORTAL_DB_PATH", "/data/portal.sqlite3") or "/data/portal.sqlite3",
            trusted_proxies=tuple(proxies), pairing_code=code, pairing_expires=expires_at,
            max_devices=_int("PORTAL_MAX_DEVICES", get("PORTAL_MAX_DEVICES", "1") or "1", 1, 100),
            version=(get("PORTAL_VERSION", "dev") or "dev")[:80], allow_insecure_local=insecure,
            max_total_bytes=_int("PORTAL_MAX_TOTAL_BYTES", get("PORTAL_MAX_TOTAL_BYTES") or str(1 << 30), 1, 2**63 - 1),
            hold_all_on_start=get("PORTAL_HOLD_ALL_ON_START") == "1")
