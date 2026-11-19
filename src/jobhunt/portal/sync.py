"""One sync pass (DESIGN §5.2) under the non-blocking file lock `data/portal/sync.lock`.

Per enabled profile: interrupted sweep, pull the inbox (journal + cursor in one transaction), apply, push the snapshot, upload
missing documents; then ONE executor batch for every profile of the pass and, if anything ran, push again. Errors are caught
per profile: the pass never raises for one profile and is a no-op when the server is unreachable. Safe to kill at any point
and to run every five minutes forever."""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass, field
from datetime import datetime

from jobhunt import db
from jobhunt.portal import apply_events, executor, protocol, secrets
from jobhunt.portal.client import InvalidEvent, PortalClient, PortalError, PortalOffline
from jobhunt.portal.journal import Journal, NewEvent, StreamError, pass_lock
from jobhunt.portal.snapshot import build_snapshot

BUSY = "une synchronisation est déjà en cours"
STREAM_MSG = "Flux d'événements incohérent : lancez « portal resync » pour ce profil."
OFFLINE_MSG = "Serveur injoignable : réessai à la prochaine synchronisation."
INVALID_MSG = "Demande illisible."


@dataclass
class ProfileSyncReport:
    profile_id: int | None
    ok: bool = False
    error: str | None = None
    pulled: int = 0
    applied: apply_events.ApplyReport | None = None
    pushed: bool = False
    uploaded: int = 0
    executed: int = 0
    message: str = ""


def _error_text(e: PortalError) -> str:
    return OFFLINE_MSG if isinstance(e, PortalOffline) else str(e)


def _to_new(ev) -> NewEvent:
    if isinstance(ev, InvalidEvent):
        return NewEvent(seq=ev.seq, event_id=ev.event_id, kind=ev.kind, payload={}, at=ev.at, state="rejected", message=INVALID_MSG)
    payload = ev.model_dump(mode="json", exclude={"seq", "event_id", "at", "kind"})
    return NewEvent(seq=ev.seq, event_id=ev.event_id, kind=ev.kind, payload=payload, at=ev.at)


def _pull(journal: Journal, client: PortalClient, pid: int, now: datetime, rep: ProfileSyncReport) -> None:
    p = journal.get_profile(pid)
    cursor, confirmed_ack = p.cursor_seq, p.confirmed_ack
    page = client.get_inbox(p.profile_key, cursor)
    target = page.last_seq
    if target - cursor > protocol.MAX_PENDING_EVENTS + protocol.CANCEL_RESERVE:
        raise StreamError("boîte de réception trop volumineuse")
    cutoff, held = page.revoked_through_seq, page.held
    buffered = []
    while True:
        validate_controls(p, page)
        if (page.revoked_through_seq, page.held) != (cutoff, held):
            raise StreamError("contrôles modifiés pendant la lecture")
        if page.last_seq < max(cursor, target) or not confirmed_ack <= page.acked_seq <= cursor:
            raise StreamError("le serveur est revenu en arrière")
        confirmed_ack = page.acked_seq
        if any(event.seq != cursor + offset or event.seq > page.last_seq
               for offset, event in enumerate(page.events, start=1)):
            raise StreamError("trou dans la séquence")
        events = [event for event in page.events if event.seq <= target]
        if cursor < target and not events:
            raise StreamError("page manquante")
        buffered.extend(_to_new(event) for event in events)
        cursor += len(events)
        if cursor == target:
            break
        page = client.get_inbox(p.profile_key, cursor)
    with journal.transaction() as scoped:
        current = scoped.get_profile(pid)
        if current != p or scoped.pending_config(pid):
            raise StreamError("contrôles locaux modifiés pendant la lecture")
        scoped.update_profile(pid, held=held, confirmed_ack=confirmed_ack, revoked_through_seq=cutoff, control_time=now)
        if buffered:
            scoped.insert_page(pid, p.stream_id, buffered, now)
    rep.pulled += len(buffered)


def validate_controls(profile, page) -> None:
    if page.stream_id != profile.stream_id:
        raise StreamError("flux inattendu")
    if (not page.enabled or page.sessions_epoch != profile.epoch or page.config_rev != profile.config_rev
            or not profile.revoked_through_seq <= page.revoked_through_seq <= page.last_seq
            or not profile.confirmed_ack <= page.acked_seq <= profile.cursor_seq
            or page.last_seq < profile.cursor_seq):
        raise StreamError("version d’accès inattendue")


def _push(journal: Journal, client: PortalClient, pid: int, now: datetime, rep: ProfileSyncReport) -> None:
    p = journal.get_profile(pid)
    snap, docs = build_snapshot(journal, pid, now)
    results = _results_for_stream(journal, pid, p.stream_id)
    rev = p.snapshot_rev + 1
    for attempt in (1, 2):
        if journal.get_profile(pid) != p or journal.pending_config(pid):
            raise StreamError("contrôles locaux modifiés pendant la préparation")
        push = protocol.SnapshotPush(stream_id=p.stream_id, sessions_epoch=p.epoch, revoked_through_seq=p.revoked_through_seq,
                                     rev=rev, acked_seq=p.cursor_seq, results=results, snapshot=snap)
        try:
            resp = client.put_snapshot(p.profile_key, push)
            break
        except PortalError as e:
            if e.code == "rev" and e.current is not None and attempt == 1:
                rev = e.current + 1
                continue
            raise
    journal.update_profile(pid, expected_stream=p.stream_id, expected_epoch=p.epoch, snapshot_rev=resp.rev,
                           confirmed_ack=push.acked_seq)
    rep.pushed = True
    for doc_id in resp.missing_documents:
        path = docs.get(doc_id)
        if path is None:
            continue
        try:
            data = path.read_bytes()
        except OSError:
            continue
        ref = next((d for d in snap.documents if d.doc_id == doc_id), None)
        if ref is None or len(data) != ref.size or hashlib.sha256(data).hexdigest() != ref.sha256:
            continue  # the file changed since the snapshot: the next pass describes it again
        try:
            client.put_document(p.profile_key, doc_id, data)
        except PortalError as e:
            if e.code == "throttled":  # the server's 120 requests per minute: the rest is listed as missing again next pass
                break
            raise
        rep.uploaded += 1


def _results_for_stream(journal: Journal, profile_id: int, stream_id: str) -> list[protocol.EventResult]:
    targets = {"answer": ("a", "path"), "custom_edit": ("c", "custom_id"), "custom_delete": ("c", "custom_id"),
               "question_answer": ("q", "qid"), "intent": ("g", "grant")}
    wire_results = []
    for result in journal.latest_results(profile_id, protocol.MAX_RESULTS):
        event = journal.get_event(result.event_id)
        if event is None or event.stream_id != stream_id:
            continue
        source = event
        if event.kind == "cancel":
            source = journal.get_event(event.payload.get("target", ""))
            if source is None or source.profile_id != profile_id or source.stream_id != stream_id or source.kind != "intent":
                source = None
        if source is not None and source.kind in targets:
            prefix, field = targets[source.kind]
            value = source.payload.get(field)
            if isinstance(value, str) and value:
                result = result.model_copy(update={"key": f"{prefix}:{value}"[:120]})
        wire_results.append(result)
    return wire_results


def _guard(journal: Journal, pid: int, now: datetime, rep: ProfileSyncReport, fn) -> bool:
    """Run one stage; record the error of this profile instead of raising. True on success."""
    try:
        fn()
        return True
    except StreamError:
        rep.error = STREAM_MSG
    except PortalError as e:
        rep.error = _error_text(e)
    except Exception as e:  # noqa: BLE001 - never a secret: the type name only
        rep.error = f"Erreur inattendue ({type(e).__name__})."
    journal.update_profile(pid, last_error=rep.error)
    return False


def _sync_one(journal: Journal, client: PortalClient, pid: int, now: datetime) -> ProfileSyncReport:
    rep = ProfileSyncReport(pid)
    p = journal.get_profile(pid)
    if p is None or not p.enabled:
        rep.message = "profil non activé"
        return rep
    if not _guard(journal, pid, now, rep, lambda: _pull(journal, client, pid, now, rep)):
        return rep
    journal.interrupted_sweep(pid, now)

    def apply_stage():
        rep.applied = apply_events.apply_pending(journal, pid, now)

    if not _guard(journal, pid, now, rep, apply_stage):
        return rep
    if not _guard(journal, pid, now, rep, lambda: _push(journal, client, pid, now, rep)):
        return rep
    journal.update_profile(pid, last_error=None, last_sync_at=now)
    rep.ok = True
    return rep


async def _async_pass(journal: Journal, client: PortalClient, ids: list[int], now: datetime, execute: bool) -> list[ProfileSyncReport]:
    reports = [_sync_one(journal, client, pid, now) for pid in ids]
    if execute:
        try:
            batch = await executor.run_batch(journal, [r.profile_id for r in reports if r.ok], now)
        except Exception as e:  # noqa: BLE001
            for r in reports:
                r.message = f"Envoi non lancé ({type(e).__name__})."
            return reports
        for r in reports:
            r.executed = sum(1 for o in batch.outcomes if o.profile_id == r.profile_id)
            if batch.locked:
                r.message = "Un autre envoi est en cours : les envois restent en attente."
            if r.executed:
                _guard(journal, r.profile_id, now, r, lambda r=r: _push(journal, client, r.profile_id, now, r))
    return reports


def _client_from_journal(journal: Journal) -> PortalClient:
    origin = journal.get_setting("origin")
    if not origin:
        raise PortalError("no_token")
    kind = journal.get_setting("token_store", "keyring")
    token = secrets.load_token(origin, kind)
    if not token:
        raise PortalError("no_token")
    return PortalClient(origin + (journal.get_setting("base_path", "") or ""), token)


async def async_sync_all(profile_ids: list[int] | None = None, execute: bool = True, *, journal: Journal | None = None,
                         client: PortalClient | None = None, now: datetime | None = None) -> list[ProfileSyncReport]:
    now = now or db.now()
    with pass_lock("sync.lock") as got:
        if not got:
            return [ProfileSyncReport(None, error=BUSY, message=BUSY)]
        journal = journal or Journal()
        ids = [p.profile_id for p in journal.list_profiles(enabled_only=True) if profile_ids is None or p.profile_id in profile_ids]
        if not ids:
            return []
        own = client is None
        try:
            client = client or _client_from_journal(journal)
        except (PortalError, secrets.PortalSecretError) as e:
            msg = str(e)
            return [ProfileSyncReport(pid, error=msg) for pid in ids]
        try:
            return await _async_pass(journal, client, ids, now, execute)
        finally:
            if own:
                client.close()


def sync_all(profile_ids: list[int] | None = None, execute: bool = True, *, journal: Journal | None = None,
             client: PortalClient | None = None, now: datetime | None = None) -> list[ProfileSyncReport]:
    return asyncio.run(async_sync_all(profile_ids, execute, journal=journal, client=client, now=now))


async def async_sync_profile(journal: Journal, client: PortalClient, profile_id: int, now: datetime, execute: bool = True) -> ProfileSyncReport:
    if journal.get_profile(profile_id) is None:
        return ProfileSyncReport(profile_id, message="profil inconnu")
    reports = await async_sync_all([profile_id], execute, journal=journal, client=client, now=now)
    return reports[0] if reports else ProfileSyncReport(profile_id, message="profil non activé")


def sync_profile(journal: Journal, client: PortalClient, profile_id: int, now: datetime, execute: bool = True) -> ProfileSyncReport:
    return asyncio.run(async_sync_profile(journal, client, profile_id, now, execute))
