"""AES-256-GCM sealing of everything the portal stores about a person, plus the keyed hashes used for throttling and CSRF.

Subkeys come from the data key with HKDF-SHA256, one per purpose. Every `seal` draws a fresh 12-byte nonce. The associated
data is exactly `bp1|<purpose>|<profile_row_id>|<row_id>` so a ciphertext moved to another row or profile does not open.
A `key_id` (a hash of a derived value, never key material) tells which configured key sealed a row; the old key stays
readable while `PORTAL_DATA_KEY_OLD` is configured."""

from __future__ import annotations

import hashlib
import hmac
import os
from dataclasses import dataclass

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

SEAL_PURPOSES = ("snapshot", "results", "event", "document")
MAC_PURPOSES = ("address", "csrf", "login")


class CryptoError(Exception):
    """A row cannot be opened (wrong key, tampering, row moved). Carries no data."""


@dataclass(frozen=True)
class Sealed:
    key_id: str
    nonce: bytes
    ciphertext: bytes


def _hkdf(key: bytes, info: bytes) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=info).derive(key)


class _KeySet:
    def __init__(self, key: bytes) -> None:
        self.key_id = hashlib.sha256(_hkdf(key, b"bp1|key-id")).hexdigest()[:8]
        self.aead = {p: AESGCM(_hkdf(key, b"bp1|seal|" + p.encode())) for p in SEAL_PURPOSES}
        self.macs = {p: _hkdf(key, b"bp1|mac|" + p.encode()) for p in MAC_PURPOSES}


def aad(purpose: str, profile_row_id: int | str, row_id: int | str) -> bytes:
    return f"bp1|{purpose}|{profile_row_id}|{row_id}".encode()


class Cipher:
    def __init__(self, key: bytes, old_key: bytes | None = None) -> None:
        if len(key) != 32 or (old_key is not None and len(old_key) != 32):
            raise ValueError("keys must be 32 bytes")
        self._current = _KeySet(key)
        self._by_id = {self._current.key_id: self._current}
        if old_key is not None:
            old = _KeySet(old_key)
            self._by_id.setdefault(old.key_id, old)

    @property
    def key_id(self) -> str:
        return self._current.key_id

    def seal(self, purpose: str, profile_row_id: int | str, row_id: int | str, plaintext: bytes) -> Sealed:
        nonce = os.urandom(12)
        data = self._current.aead[purpose].encrypt(nonce, plaintext, aad(purpose, profile_row_id, row_id))
        return Sealed(self._current.key_id, nonce, data)

    def open(self, purpose: str, profile_row_id: int | str, row_id: int | str, sealed: Sealed) -> bytes:
        keys = self._by_id.get(sealed.key_id)
        if keys is None or purpose not in keys.aead:
            raise CryptoError("unknown key")
        try:
            return keys.aead[purpose].decrypt(sealed.nonce, sealed.ciphertext, aad(purpose, profile_row_id, row_id))
        except (InvalidTag, ValueError):
            raise CryptoError("cannot open") from None

    def reseal(self, purpose: str, profile_row_id: int | str, row_id: int | str, sealed: Sealed) -> Sealed:
        """Re-encrypt a row under the current key (key rotation). Fresh nonce."""
        return self.seal(purpose, profile_row_id, row_id, self.open(purpose, profile_row_id, row_id, sealed))

    def needs_rotation(self, sealed: Sealed) -> bool:
        return sealed.key_id != self._current.key_id

    def mac(self, purpose: str, data: str | bytes) -> str:
        """Keyed hash (hex) for `address`, `csrf` and `login` values. Never reversible to the input."""
        raw = data.encode() if isinstance(data, str) else data
        return hmac.new(self._current.macs[purpose], raw, hashlib.sha256).hexdigest()
