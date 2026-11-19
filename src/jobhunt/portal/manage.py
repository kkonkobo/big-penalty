"""Operator actions of the portal (called by the CLI and the local web page). DESIGN §9.

Every function takes optional keyword arguments `journal` and `client` (tests); by default they open the journal and build the
client from the pairing stored in it. Configuration is staged locally before sending it; unfinished changes block portal work
and can be retried (a 409 on the configuration revision is adopted once)."""

from __future__ import annotations

import contextlib
import os
import warnings
from dataclasses import dataclass, field
from datetime import datetime
from functools import wraps
from urllib.parse import urlsplit

from jobhunt import db
from jobhunt.portal import apply_events, protocol, secrets
from jobhunt.portal.client import PortalClient, PortalError, check_base_url, origin_of
from jobhunt.portal.journal import EventRow, Journal, pass_lock


class ManageError(Exception):
    """French message for the operator."""


@dataclass
class Credentials:
    address: str
    handle: str
    passcode: str  # shown once, never stored

    @property
    def handle_shown(self) -> str:
        return protocol.grouped(self.handle, 5)

    @property
    def passcode_shown(self) -> str:
        return protocol.grouped(self.passcode, 4)


@dataclass
class PairInfo:
    origin: str
    base_path: str
    device_id: str
    device_name: str
    token_store: str


@dataclass
class DisableResult:
    server_updated: bool
    error: str | None = None


@dataclass
class ProfileState:
    profile_id: int
    name: str
    enabled: bool
    mode: str
    handle: str
    last_sync_at: datetime | None
    last_error: str | None
    pending: int            # received, not applied yet
    awaiting_review: int
    queued: int
    held: bool
    sends_today: int = 0


@dataclass
class PortalStatus:
    paired: bool
    origin: str = ""
    base_path: str = ""
    device_name: str = ""
    paused: bool = False
    profiles: list[ProfileState] = field(default_factory=list)


@dataclass
class RecoveredProfile:
    profile_key: str
    enabled: bool
    held: bool
    config_rev: int
    snapshot_rev: int


# ---------------------------------------------------------------- plumbing
def _journal(journal: Journal | None) -> Journal:
    return journal or Journal()


def _pairing(j: Journal) -> tuple[str, str, str]:
    origin = j.get_setting("origin")
    if not origin:
        raise ManageError("Cet appareil n'est pas appairé : lancez d'abord « portal pair ».")
    return origin, j.get_setting("base_path", "") or "", j.get_setting("token_store", "keyring") or "keyring"


@contextlib.contextmanager
def _client(j: Journal, client: PortalClient | None):
    if client is not None:
        yield client
        return
    origin, base, kind = _pairing(j)
    try:
        token = secrets.load_token(origin, kind)
    except secrets.PortalSecretError as e:
        raise ManageError(str(e)) from None
    if not token:
        raise ManageError("Jeton de l'appareil introuvable : appairez-le de nouveau (« portal unpair » puis « portal pair »).")
    c = PortalClient(origin + base, token)
    try:
        yield c
    finally:
        c.close()


def _address(j: Journal) -> str:
    origin, base, _ = _pairing(j)
    return f"{origin}{base}/"


def _verifier(passcode: str) -> protocol.PasscodeVerifier:
    salt = os.urandom(protocol.SCRYPT_SALT_BYTES)
    return protocol.PasscodeVerifier(salt=salt.hex(), hash=protocol.passcode_hash(passcode, salt).hex())


def _push_config(client: PortalClient, key: str, *, config_rev: int, enabled: bool, handle: str, passcode: protocol.PasscodeVerifier | None,
                 epoch: int, stream_id: str, save_revision) -> int:
    rev = config_rev
    for attempt in (0, 1):
        cfg = protocol.ProfileConfig(config_rev=rev, enabled=enabled, handle_sha256=protocol.handle_digest(handle), passcode=passcode,
                                     sessions_epoch=epoch, stream_id=stream_id)
        try:
            client.put_profile(key, cfg)
            return rev
        except PortalError as e:
            if e.code == "config_rev" and e.current is not None and attempt == 0:
                rev = max(rev, e.current + 1)
                save_revision(rev)
                continue
            raise ManageError(str(e)) from None
    raise ManageError("Configuration refusée par le portail.")


def _row(j: Journal, profile_id: int, *, enabled: bool = True):
    r = j.get_profile(profile_id)
    if r is None or (enabled and not r.enabled):
        raise ManageError("Ce profil n'est pas activé sur le portail.")
    return r


def _check_chosen(passcode: str) -> None:
    n = protocol.normalize_secret(passcode)
    if len(n) < protocol.PASSCODE_MIN_CHOSEN or len(passcode) > protocol.SECRET_MAX_INPUT:
        raise ManageError(f"Le code choisi doit faire au moins {protocol.PASSCODE_MIN_CHOSEN} caractères.")


def _serialized(fn):
    @wraps(fn)
    def run(*args, **kwargs):
        with pass_lock("manage.lock") as acquired:
            if not acquired:
                raise ManageError("Une modification des réglages est en cours : réessayez dans un instant.")
            return fn(*args, **kwargs)
    return run


def _retry(journal: Journal, profile_id: int, operation: str) -> bool:
    pending = journal.pending_config(profile_id)
    if pending and pending != operation:
        raise ManageError("Une modification est en attente : répétez la commande précédente ou désactivez le profil.")
    return pending == operation


def _publish(journal: Journal, row, client: PortalClient | None, *, enabled: bool, passcode: str | None = None) -> None:
    with _client(journal, client) as remote:
        rev = _push_config(remote, row.profile_key, config_rev=row.config_rev, enabled=enabled, handle=row.handle,
                           passcode=_verifier(passcode) if passcode is not None else None, epoch=row.epoch, stream_id=row.stream_id,
                           save_revision=lambda value: journal.save_profile(row.profile_id, config_rev=value))
    journal.complete_config(row.profile_id, db.now(), enabled=enabled, config_rev=rev, last_error=None)


# ---------------------------------------------------------------- pairing
@_serialized
def pair(url: str, pairing_code: str, device_name: str, token_store: str = "keyring", *, journal: Journal | None = None,
         client: PortalClient | None = None) -> PairInfo:
    """Pair with the configured portal URL; store the device token locally and send only its digest."""
    j = _journal(journal)
    if j.get_setting("origin"):
        raise ManageError("Cet appareil est déjà appairé : lancez « portal unpair » avant de l'appairer de nouveau.")
    if token_store not in secrets.KINDS:
        raise ManageError("Le stockage du jeton doit être « keyring » ou « file ».")
    try:
        base = check_base_url(url)
        origin = origin_of(base)
    except PortalError as e:
        raise ManageError(str(e)) from None
    base_path = urlsplit(base).path.rstrip("/")
    try:
        token = secrets.load_token(origin, token_store)  # left by an interrupted pairing: the server accepts a retry of the same token
        if not token:
            token = protocol.new_token()
            secrets.store_token(origin, token, token_store)
    except secrets.PortalSecretError as e:
        raise ManageError(str(e)) from None
    own = client is None
    c = client or PortalClient(base, None)
    try:
        resp = c.pair(pairing_code, device_name, protocol.sha256_hex(token))
    except PortalError as e:
        if 400 <= e.status < 500:
            with contextlib.suppress(secrets.PortalSecretError):
                secrets.delete_token(origin, token_store)
        raise ManageError(str(e)) from None
    finally:
        if own:
            c.close()
    j.set_settings(dict(origin=origin, base_path=base_path, device_id=resp.device_id, device_name=device_name, token_store=token_store))
    return PairInfo(origin, base_path, resp.device_id, device_name, token_store)


@_serialized
def unpair(force: bool = False, *, journal: Journal | None = None) -> None:
    j = _journal(journal)
    origin, _, kind = _pairing(j)
    active = any(row.enabled or j.pending_config(row.profile_id) for row in j.list_profiles())
    if active and not force:
        raise ManageError("Des profils sont encore activés ou en attente : désactivez-les d'abord (ou utilisez --force).")
    j.disable_for_unpair(db.now())
    try:
        secrets.delete_token(origin, kind)
    except secrets.PortalSecretError as e:
        raise ManageError(str(e)) from None
    j.delete_settings("origin", "base_path", "device_id", "device_name", "token_store")
    if force:
        warnings.warn("Appairage supprimé sur le Mac uniquement : les profils, sessions et données restent sur le serveur.",
                      stacklevel=2)


# ---------------------------------------------------------------- profiles
@_serialized
def enable(profile_id: int, passcode: str | None = None, review: bool = False, *, profile_key: str | None = None,
           journal: Journal | None = None, client: PortalClient | None = None) -> Credentials:
    """Enable the portal for a local profile: new handle, new stream, a generated (or chosen) code. The code is returned once.
    `profile_key`: reuse an existing server key (after `recover`)."""
    j = _journal(journal)
    _pairing(j)
    if db.get_profile(profile_id) is None:
        raise ManageError("Profil introuvable.")
    row = j.get_profile(profile_id)
    if row and row.enabled:
        raise ManageError("Ce profil est déjà activé : utilisez « portal passcode » pour changer le code.")
    if passcode is not None:
        _check_chosen(passcode)
    else:
        passcode = protocol.new_passcode()
    if not _retry(j, profile_id, "enable"):
        key = profile_key or (row.profile_key if row else protocol.new_id())
        j.stage_config(profile_id, "enable", db.now(), profile_key=key, handle=protocol.new_handle(), stream_id=protocol.new_id(),
                       epoch=row.epoch + 1 if row else 0, config_rev=(row.config_rev if row else 0) + 1, enabled=False,
                       review_mode=review, held=False)
    elif profile_key is not None and row.profile_key != profile_key:
        raise ManageError("Ce profil a déjà une activation en attente avec une autre clé.")
    row = _row(j, profile_id, enabled=False)
    _publish(j, row, client, enabled=True, passcode=passcode)
    return Credentials(_address(j), row.handle, passcode)


@_serialized
def rotate_passcode(profile_id: int, passcode: str | None = None, *, journal: Journal | None = None,
                    client: PortalClient | None = None) -> Credentials:
    """New code: sessions die (epoch + 1), grants and queued intents are voided. Returned once."""
    j = _journal(journal)
    row = _row(j, profile_id)
    if passcode is not None:
        _check_chosen(passcode)
    else:
        passcode = protocol.new_passcode()
    if not _retry(j, profile_id, "rotate"):
        j.stage_config(profile_id, "rotate", db.now(), epoch=row.epoch + 1, config_rev=row.config_rev + 1)
    _publish(j, _row(j, profile_id), client, enabled=True, passcode=passcode)
    return Credentials(_address(j), row.handle, passcode)


@_serialized
def revoke(profile_id: int, *, journal: Journal | None = None, client: PortalClient | None = None) -> None:
    """Kill every session of the profile without changing the code."""
    j = _journal(journal)
    row = _row(j, profile_id)
    if not _retry(j, profile_id, "revoke"):
        j.stage_config(profile_id, "revoke", db.now(), epoch=row.epoch + 1, config_rev=row.config_rev + 1)
    _publish(j, _row(j, profile_id), client, enabled=True)


@_serialized
def set_mode(profile_id: int, mode: str, *, journal: Journal | None = None) -> None:
    if mode not in ("direct", "review"):
        raise ManageError("Le mode doit être « direct » ou « review ».")
    j = _journal(journal)
    _row(j, profile_id)
    j.save_profile(profile_id, review_mode=mode == "review", control_time=db.now())


@_serialized
def disable(profile_id: int, purge: bool = False, *, journal: Journal | None = None, client: PortalClient | None = None) -> DisableResult:
    """Local first (nothing more is applied or started), then the server (disabled, or its data deleted with `purge`)."""
    j = _journal(journal)
    row = _row(j, profile_id, enabled=False)
    operation = "purge" if purge else "disable"
    if j.pending_config(profile_id) != operation:
        j.stage_config(profile_id, operation, db.now(), enabled=False, config_rev=row.config_rev + 1)
    try:
        if purge:
            with _client(j, client) as c:
                try:
                    c.delete_profile(row.profile_key)
                except PortalError as e:
                    if e.code != "not_found":
                        raise
            j.complete_config(profile_id, db.now())
        else:
            _publish(j, _row(j, profile_id, enabled=False), client, enabled=False)
    except (ManageError, PortalError) as e:
        return DisableResult(False, str(e))
    return DisableResult(True)


def pause(*, journal: Journal | None = None) -> None:
    _journal(journal).set_paused(True, db.now())


def resume(*, journal: Journal | None = None) -> None:
    _journal(journal).set_paused(False)


@_serialized
def resync(profile_id: int, *, journal: Journal | None = None, client: PortalClient | None = None) -> None:
    """New stream: cursor 0, grants void, non-final local events `lost` (the server marks its own as lost too)."""
    j = _journal(journal)
    row = _row(j, profile_id)
    if not _retry(j, profile_id, "resync"):
        j.stage_config(profile_id, "resync", db.now(), stream_id=protocol.new_id(), config_rev=row.config_rev + 1)
    _publish(j, _row(j, profile_id), client, enabled=True)


@_serialized
def recover(url: str, token_store: str = "keyring", *, journal: Journal | None = None,
            client: PortalClient | None = None) -> list[RecoveredProfile]:
    """After losing data/portal/: pair again from the keyring token (it survives) and list the server's profiles of this device.
    LIMITS: the server does not know which local profile each key belongs to, and the handles and codes cannot be recovered;
    re-enable each local profile (`enable(..., profile_key=<key>)` reuses a listed key, else a new one is created and the old
    one can be removed with `forget_server_profile`). Approvals that were queued are lost and must be given again."""
    j = _journal(journal)
    if j.get_setting("origin"):
        raise ManageError("Cet appareil est déjà appairé : rien à récupérer.")
    try:
        base = check_base_url(url)
        origin = origin_of(base)
        token = secrets.load_token(origin, token_store)
    except (PortalError, secrets.PortalSecretError) as e:
        raise ManageError(str(e)) from None
    if not token:
        raise ManageError("Jeton introuvable : l'appareil doit être appairé de nouveau (nouveau code d'appairage).")
    own = client is None
    c = client or PortalClient(base, token)
    try:
        st = c.status()
    except PortalError as e:
        raise ManageError(str(e)) from None
    finally:
        if own:
            c.close()
    j.set_settings(dict(origin=origin, base_path=urlsplit(base).path.rstrip("/"), device_id="", device_name="", token_store=token_store))
    return [RecoveredProfile(p.profile_key, p.enabled, p.held, p.config_rev, p.snapshot_rev) for p in st.profiles]


def forget_server_profile(profile_key: str, *, journal: Journal | None = None, client: PortalClient | None = None) -> None:
    j = _journal(journal)
    with _client(j, client) as c:
        try:
            c.delete_profile(profile_key)
        except PortalError as e:
            raise ManageError(str(e)) from None


# ---------------------------------------------------------------- status, review, journal
def status(*, journal: Journal | None = None) -> PortalStatus:
    j = _journal(journal)
    origin = j.get_setting("origin")
    st = PortalStatus(paired=bool(origin), origin=origin or "", base_path=j.get_setting("base_path", "") or "",
                      device_name=j.get_setting("device_name", "") or "", paused=j.paused)
    now = db.now()
    for p in j.list_profiles():
        counts = j.event_counts(p.profile_id)
        local = db.get_profile(p.profile_id)
        st.profiles.append(ProfileState(
            profile_id=p.profile_id, name=local.name if local else "", enabled=p.enabled, mode="review" if p.review_mode else "direct",
            handle=p.handle, last_sync_at=p.last_sync_at, last_error=p.last_error, pending=counts.get("received", 0),
            awaiting_review=counts.get("awaiting_review", 0), queued=counts.get("queued", 0), held=p.held,
            sends_today=j.sends_today(p.profile_id, now)))
    return st


def review_list(profile_id: int, *, journal: Journal | None = None) -> list[EventRow]:
    return _journal(journal).events(profile_id, ("awaiting_review",))


def review_apply(profile_id: int, event_ids: list[str] | None = None, *, journal: Journal | None = None) -> apply_events.ApplyReport:
    j = _journal(journal)
    with pass_lock("sync.lock") as got:
        if not got:
            raise ManageError("Une synchronisation est en cours : réessayez dans un instant.")
        return apply_events.apply_reviewed(j, profile_id, event_ids, True)


def review_reject(profile_id: int, event_ids: list[str] | None = None, *, journal: Journal | None = None) -> int:
    j = _journal(journal)
    with pass_lock("sync.lock") as got:
        if not got:
            raise ManageError("Une synchronisation est en cours : réessayez dans un instant.")
        return sum(apply_events.apply_reviewed(j, profile_id, event_ids, False).states.values())


def journal_tail(profile_id: int, n: int = 50, *, journal: Journal | None = None) -> list[EventRow]:
    return _journal(journal).tail(profile_id, n)
