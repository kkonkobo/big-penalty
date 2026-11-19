"""The Mac API under `<base>/api/v1` (DESIGN §4). Bodies are the models of `jobhunt.portal.protocol`.

Every route authenticates first (bearer token, sha256 lookup, 120 requests per minute per device), then resolves
`(device_id, profile_key)`: another device's profile is a plain 404. Errors are `{"error": code, "current": n?}` and never
echo the request. Database work runs in the thread pool; bodies are read here (the ASGI middleware already enforces the
size limits on the declared length and on the stream)."""

from __future__ import annotations

import hashlib
import hmac
import sqlite3
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Annotated, Any

from fastapi import APIRouter, Path, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ValidationError
from starlette.concurrency import run_in_threadpool
from starlette.requests import ClientDisconnect

from jobhunt.portal import protocol
from jobhunt.portal.protocol import (EventResult, Inbox, PairRequest, PairResponse, ProfileConfig, ProfileConfigResponse,
                                     ProfileStatus, ServerStatus, SnapshotPush, SnapshotPushResponse)

from . import ops, throttle
from .auth import client_address, delete_profile_sessions
from .crypto import Sealed
from .db import purge_profile_data

if TYPE_CHECKING:
    from .app import Portal

router = APIRouter(prefix=protocol.API_PREFIX)
PROFILE_KEY = Annotated[str, Path(pattern=r"^[0-9a-f]{32}$")]
DOC_ID = Annotated[str, Path(pattern=r"^[0-9a-f]{32}$")]
API_RATE = (120, 60)
PAIR_PER_ADDRESS = (5, 3600)
PAIR_GLOBAL = (20, 86400)


class ApiError(Exception):
    def __init__(self, status: int, code: str, current: int | None = None) -> None:
        self.status, self.code, self.current = status, code, current


def error_response(status: int, code: str, current: int | None = None) -> JSONResponse:
    body: dict[str, Any] = {"error": code}
    if current is not None:
        body["current"] = current
    return JSONResponse(body, status_code=status)


def _json(model: BaseModel, status: int = 200) -> JSONResponse:
    return JSONResponse(model.model_dump(mode="json"), status_code=status)


def _portal(request: Request) -> Portal:
    return request.app.state.portal


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _now(portal: Portal) -> float:
    return portal.clock()


# ---------------------------------------------------------------- shared helpers
def _authenticate(portal: Portal, header: str | None) -> str:
    """The device id of a valid, unrevoked bearer token; rate-limited. Raises 401 / 429."""
    scheme, _, token = (header or "").partition(" ")
    if scheme.lower() != "bearer" or not token or len(token) > 300:
        raise ApiError(401, "unauthorized")
    digest = _sha(token.strip())
    row = portal.db.conn.execute("SELECT id, token_sha256 FROM devices WHERE token_sha256=? AND revoked_at IS NULL", (digest,)).fetchone()
    if row is None or not hmac.compare_digest(row["token_sha256"].encode(), digest.encode()):
        raise ApiError(401, "unauthorized")
    if not throttle.reserve(portal.db, [(f"api:{row['id']}", *API_RATE)], _now(portal)):
        raise ApiError(429, "rate_limited")
    return row["id"]


def _active_device(conn, device_id: str) -> None:
    if conn.execute("SELECT 1 FROM devices WHERE id=? AND revoked_at IS NULL", (device_id,)).fetchone() is None:
        raise ApiError(401, "unauthorized")


def _profile(conn, device_id: str, key: str):
    _active_device(conn, device_id)
    row = conn.execute("SELECT * FROM profiles WHERE device_id=? AND profile_key=?", (device_id, key)).fetchone()
    if row is None:
        raise ApiError(404, "not_found")
    return row


async def _json_body(request: Request, model: type[BaseModel]):
    media = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if media != "application/json":
        raise ApiError(415, "content_type")
    try:
        return await run_in_threadpool(model.model_validate_json, await request.body())
    except ValidationError:
        raise ApiError(422, "invalid") from None
    except ClientDisconnect:
        raise ApiError(400, "aborted") from None


async def _authed(request: Request) -> tuple[Portal, str]:
    portal = _portal(request)
    return portal, await run_in_threadpool(_authenticate, portal, request.headers.get("authorization"))


def _dt(epoch: float) -> datetime:
    return datetime.fromtimestamp(epoch, UTC)


# ---------------------------------------------------------------- pairing
def _pair(portal: Portal, body: PairRequest, address: str) -> PairResponse:
    cfg, now = portal.config, _now(portal)
    if not cfg.pairing_enabled or now > (cfg.pairing_expires or 0):
        raise ApiError(403, "refused")
    given = hashlib.sha256(body.pairing_code.encode()).digest()
    expected = hashlib.sha256((cfg.pairing_code or "").encode()).digest()
    if not hmac.compare_digest(given, expected):
        addr_key = portal.cipher.mac("address", address)
        if not throttle.reserve(portal.db, [(f"pair:a:{addr_key}", *PAIR_PER_ADDRESS), ("pair:all", *PAIR_GLOBAL)], now):
            raise ApiError(429, "rate_limited")
        raise ApiError(403, "refused")
    code_sha = expected.hex()
    with portal.db.transaction() as c:
        used = c.execute("SELECT d.id, d.token_sha256, d.revoked_at FROM pairings p JOIN devices d ON d.id=p.device_id "
                         "WHERE p.code_sha256=?", (code_sha,)).fetchone()
        if used is not None:
            if used["revoked_at"] is None and hmac.compare_digest(used["token_sha256"].encode(), body.token_sha256.encode()):
                return PairResponse(device_id=used["id"])
            raise ApiError(403, "refused")
        active = c.execute("SELECT COUNT(*) FROM devices WHERE revoked_at IS NULL").fetchone()[0]
        if active >= cfg.max_devices:
            raise ApiError(409, "max_devices")
        device_id = protocol.new_id()
        try:
            c.execute("INSERT INTO devices(id, name, token_sha256, created_at) VALUES (?,?,?,?)",
                      (device_id, body.device_name, body.token_sha256, int(now)))
        except sqlite3.IntegrityError:
            raise ApiError(409, "conflict") from None
        c.execute("INSERT INTO pairings(code_sha256, device_id, used_at) VALUES (?,?,?)", (code_sha, device_id, int(now)))
    return PairResponse(device_id=device_id)


@router.post("/pair")
async def pair(request: Request):
    portal = _portal(request)
    body = await _json_body(request, PairRequest)
    address = client_address(request.scope.get("client"), request.scope["headers"], portal.config)
    return _json(await run_in_threadpool(_pair, portal, body, address))


# ---------------------------------------------------------------- profile configuration
def _lost_message_results(c, portal: Portal, row, now: float) -> list[EventResult]:
    """Stream change: non-final results and every pending event become `lost` results (newest MAX_RESULTS kept)."""
    snapshot = ops._load_snapshot(c, portal.cipher, row["id"])
    lost = []
    for r in ops.load_results(c, portal.cipher, row["id"]):
        lost.append(r if r.status in protocol.FINAL_STATUSES else r.model_copy(update={"status": "lost", "message": ops.MSG_LOST}))
    for ev in ops.load_events(c, portal.cipher, row["id"]):
        lost.append(EventResult(event_id=ev.event_id, kind=ev.kind, status="lost", message=ops.MSG_LOST,
                                label=ops.event_label(ev, snapshot), at=_dt(now)))
    return sorted(lost, key=lambda r: r.at, reverse=True)[:protocol.MAX_RESULTS]


def _put_profile(portal: Portal, device_id: str, key: str, cfg: ProfileConfig) -> ProfileConfigResponse:
    now = _now(portal)
    digest = protocol.body_digest(cfg)
    handle_key = portal.cipher.mac("login", cfg.handle_sha256)
    try:
        with portal.db.transaction() as c:
            _active_device(c, device_id)
            row = c.execute("SELECT * FROM profiles WHERE profile_key=?", (key,)).fetchone()
            tombstone = c.execute("SELECT * FROM profile_tombstones WHERE profile_key=?", (key,)).fetchone()
            if any(record is not None and record["device_id"] != device_id for record in (row, tombstone)):
                raise ApiError(404, "not_found")
            taken = c.execute("SELECT device_id, profile_key FROM profiles WHERE handle_sha256=?", (cfg.handle_sha256,)).fetchone()
            if taken is not None and (row is None or taken["profile_key"] != key or taken["device_id"] != device_id):
                raise ApiError(409, "handle_taken")
            if row is None:
                if tombstone is not None and cfg.config_rev <= tombstone["config_rev"]:
                    raise ApiError(409, "config_rev", tombstone["config_rev"])
                if c.execute("SELECT 1 FROM retired_streams WHERE profile_key=? AND stream_id=?", (key, cfg.stream_id)).fetchone():
                    raise ApiError(409, "stream")
                if c.execute("SELECT COUNT(*) FROM profiles WHERE device_id=?", (device_id,)).fetchone()[0] >= protocol.MAX_PROFILES_PER_DEVICE:
                    raise ApiError(409, "profile_limit")
                if cfg.passcode is None:
                    raise ApiError(422, "invalid")
                c.execute("INSERT INTO profiles(device_id, profile_key, handle_sha256, enabled, held, config_rev, config_digest, "
                          "sessions_epoch, stream_id, passcode_salt, passcode_hash, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                          (device_id, key, cfg.handle_sha256, int(cfg.enabled), tombstone["held"] if tombstone is not None else 0,
                           cfg.config_rev, digest, cfg.sessions_epoch,
                           cfg.stream_id, bytes.fromhex(cfg.passcode.salt), bytes.fromhex(cfg.passcode.hash), int(now)))
                c.execute("DELETE FROM profile_tombstones WHERE profile_key=?", (key,))
                return ProfileConfigResponse(config_rev=cfg.config_rev)
            if cfg.config_rev < row["config_rev"] or (cfg.config_rev == row["config_rev"]
                                                      and not hmac.compare_digest(row["config_digest"].encode(), digest.encode())):
                raise ApiError(409, "config_rev", row["config_rev"])
            if cfg.config_rev == row["config_rev"]:
                return ProfileConfigResponse(config_rev=row["config_rev"])
            pid = row["id"]
            new_passcode = cfg.passcode is not None and (bytes.fromhex(cfg.passcode.salt) != bytes(row["passcode_salt"])
                                                         or bytes.fromhex(cfg.passcode.hash) != bytes(row["passcode_hash"]))
            credentials_changed = (new_passcode or cfg.sessions_epoch != row["sessions_epoch"]
                                   or cfg.handle_sha256 != row["handle_sha256"])
            if credentials_changed:
                delete_profile_sessions(c, pid)
                c.execute("UPDATE profiles SET revoked_through_seq=last_seq WHERE id=?", (pid,))
                if cfg.stream_id == row["stream_id"]:
                    results = ops.load_results(c, portal.cipher, pid)
                    if any(result.status in ("received", "awaiting_review", "queued") for result in results):
                        ops.store_results(c, portal.cipher, pid, [
                            result.model_copy(update={"status": "cancelled", "message": ops.MSG_REVOKED})
                            if result.status in ("received", "awaiting_review", "queued") else result for result in results])
                throttle.clear_login(c, handle_key)
                throttle.clear_login(c, portal.cipher.mac("login", row["handle_sha256"]))
            elif not cfg.enabled:
                delete_profile_sessions(c, pid)
            if cfg.stream_id != row["stream_id"]:
                if c.execute("SELECT 1 FROM retired_streams WHERE profile_key=? AND stream_id=?", (key, cfg.stream_id)).fetchone():
                    raise ApiError(409, "stream")
                results = _lost_message_results(c, portal, row, now)
                ops.store_results(c, portal.cipher, pid, results)
                c.execute("INSERT INTO retired_streams VALUES (?,?)", (key, row["stream_id"]))
                c.execute("DELETE FROM events WHERE profile_id=?", (pid,))
                c.execute("DELETE FROM snapshots WHERE profile_id=? AND purpose='snapshot'", (pid,))
                c.execute("DELETE FROM manifest WHERE profile_id=?", (pid,))
                c.execute("DELETE FROM documents WHERE profile_id=?", (pid,))
                c.execute("UPDATE profiles SET last_seq=0, acked_seq=0, revoked_through_seq=0, snapshot_rev=0, "
                          "snapshot_digest=NULL, snapshot_at=NULL WHERE id=?", (pid,))
            c.execute("UPDATE profiles SET enabled=?, handle_sha256=?, config_rev=?, config_digest=?, sessions_epoch=?, stream_id=? "
                      "WHERE id=?", (int(cfg.enabled), cfg.handle_sha256, cfg.config_rev, digest, cfg.sessions_epoch, cfg.stream_id, pid))
            if cfg.passcode is not None:
                c.execute("UPDATE profiles SET passcode_salt=?, passcode_hash=? WHERE id=?",
                          (bytes.fromhex(cfg.passcode.salt), bytes.fromhex(cfg.passcode.hash), pid))
            return ProfileConfigResponse(config_rev=cfg.config_rev)
    except sqlite3.IntegrityError:
        raise ApiError(409, "handle_taken") from None


@router.put("/profiles/{profile_key}")
async def put_profile(request: Request, profile_key: PROFILE_KEY):
    portal, device_id = await _authed(request)
    cfg = await _json_body(request, ProfileConfig)
    return _json(await run_in_threadpool(_put_profile, portal, device_id, profile_key, cfg))


# ---------------------------------------------------------------- inbox
def _inbox(portal: Portal, device_id: str, key: str, after: int) -> Inbox:
    with portal.db.transaction() as conn:
        row = _profile(conn, device_id, key)
        events = ops.load_events(conn, portal.cipher, row["id"], after, protocol.INBOX_PAGE)
        return Inbox(stream_id=row["stream_id"], last_seq=row["last_seq"], acked_seq=row["acked_seq"],
                     sessions_epoch=row["sessions_epoch"], revoked_through_seq=row["revoked_through_seq"],
                     held=bool(row["held"]), events=events)


@router.get("/profiles/{profile_key}/inbox")
async def get_inbox(request: Request, profile_key: PROFILE_KEY, after: Annotated[int, Query(ge=0, le=2 ** 53)] = 0):
    portal, device_id = await _authed(request)
    return _json(await run_in_threadpool(_inbox, portal, device_id, profile_key, after))


# ---------------------------------------------------------------- snapshot
def _missing_documents(c, profile_id: int) -> list[str]:
    rows = c.execute("SELECT m.doc_id FROM manifest m LEFT JOIN documents d ON d.profile_id=m.profile_id AND d.doc_id=m.doc_id "
                     "AND d.sha256=m.sha256 AND d.size=m.size WHERE m.profile_id=? AND d.doc_id IS NULL ORDER BY m.doc_id",
                     (profile_id,)).fetchall()
    return [r[0] for r in rows]


def _check_manifest(push: SnapshotPush) -> None:
    snap = push.snapshot
    ids = [d.doc_id for d in snap.documents]
    if len(set(ids)) != len(ids) or sum(d.size for d in snap.documents) > protocol.MAX_DOCUMENTS_BYTES:
        raise ApiError(422, "invalid")
    referenced = {a.doc_id for a in snap.awaiting if a.doc_id} | {s.doc_id for s in snap.sent if s.doc_id}
    if not referenced <= set(ids):
        raise ApiError(422, "invalid")


def _check_snapshot_privacy(snapshot) -> None:
    for field in snapshot.answer_schema.fields:
        value = snapshot.answers.get(field.path)
        if field.write_only and value is not None and (value.value is not None or value.legacy):
            raise ApiError(422, "invalid")
    if any(answer.masked and answer.answer for answer in snapshot.custom):
        raise ApiError(422, "invalid")


def _put_snapshot(portal: Portal, device_id: str, key: str, push: SnapshotPush) -> SnapshotPushResponse:
    _check_manifest(push)
    _check_snapshot_privacy(push.snapshot)
    now = _now(portal)
    digest = protocol.body_digest(push)
    with portal.db.transaction() as c:
        row = _profile(c, device_id, key)
        pid = row["id"]
        if push.stream_id != row["stream_id"]:
            raise ApiError(409, "stream")
        if push.rev == row["snapshot_rev"] and row["snapshot_digest"] and hmac.compare_digest(row["snapshot_digest"].encode(), digest.encode()):
            return SnapshotPushResponse(rev=push.rev, missing_documents=_missing_documents(c, pid))
        if push.rev <= row["snapshot_rev"]:
            raise ApiError(409, "rev", row["snapshot_rev"])
        if push.acked_seq < row["acked_seq"] or push.acked_seq > row["last_seq"]:
            raise ApiError(409, "ack", row["acked_seq"])
        stored_refs = {doc["doc_id"]: (doc["sha256"], doc["size"]) for doc in
                       c.execute("SELECT doc_id, sha256, size FROM manifest WHERE profile_id=?", (pid,))}
        if any(doc.doc_id in stored_refs and stored_refs[doc.doc_id] != (doc.sha256, doc.size)
               for doc in push.snapshot.documents):
            raise ApiError(422, "invalid")
        c.execute("DELETE FROM events WHERE profile_id=? AND seq<=?", (pid, push.acked_seq))
        sealed = portal.cipher.seal("snapshot", pid, "0", push.snapshot.model_dump_json().encode())
        c.execute("INSERT INTO snapshots(profile_id, purpose, key_id, nonce, ciphertext) VALUES (?, 'snapshot', ?, ?, ?) "
                  "ON CONFLICT(profile_id, purpose) DO UPDATE SET key_id=excluded.key_id, nonce=excluded.nonce, "
                  "ciphertext=excluded.ciphertext", (pid, sealed.key_id, sealed.nonce, sealed.ciphertext))
        # the server's own `lost` results (stream change) survive the Mac's next results; the rest is replaced
        new_ids = {r.event_id for r in push.results}
        keep = [r for r in ops.load_results(c, portal.cipher, pid) if r.status == "lost" and r.event_id not in new_ids]
        merged = sorted([*push.results, *keep], key=lambda r: r.at, reverse=True)[:protocol.MAX_RESULTS]
        ops.store_results(c, portal.cipher, pid, merged)
        c.execute("DELETE FROM manifest WHERE profile_id=?", (pid,))
        c.executemany("INSERT INTO manifest(profile_id, doc_id, sha256, size) VALUES (?,?,?,?)",
                      [(pid, d.doc_id, d.sha256, d.size) for d in push.snapshot.documents])
        c.execute("DELETE FROM documents WHERE profile_id=? AND NOT EXISTS (SELECT 1 FROM manifest m WHERE m.profile_id=documents.profile_id "
                  "AND m.doc_id=documents.doc_id AND m.sha256=documents.sha256 AND m.size=documents.size)", (pid,))
        c.execute("UPDATE profiles SET snapshot_rev=?, snapshot_digest=?, snapshot_at=?, acked_seq=? WHERE id=?",
                  (push.rev, digest, int(now), push.acked_seq, pid))
        return SnapshotPushResponse(rev=push.rev, missing_documents=_missing_documents(c, pid))


@router.put("/profiles/{profile_key}/snapshot")
async def put_snapshot(request: Request, profile_key: PROFILE_KEY):
    portal, device_id = await _authed(request)
    push = await _json_body(request, SnapshotPush)
    return _json(await run_in_threadpool(_put_snapshot, portal, device_id, profile_key, push))


# ---------------------------------------------------------------- documents
def _document_precheck(portal: Portal, device_id: str, key: str, doc_id: str):
    c = portal.db.conn
    row = _profile(c, device_id, key)
    m = c.execute("SELECT sha256, size FROM manifest WHERE profile_id=? AND doc_id=?", (row["id"], doc_id)).fetchone()
    if m is None:
        raise ApiError(404, "not_found")
    stored = c.execute("SELECT 1 FROM documents WHERE profile_id=? AND doc_id=? AND sha256=? AND size=?",
                       (row["id"], doc_id, m["sha256"], m["size"])).fetchone()
    return row["id"], m["sha256"], m["size"], stored is not None


def _store_document(portal: Portal, device_id: str, key: str, doc_id: str, sha: str, data: bytes) -> None:
    if not data.startswith(b"%PDF-") or hashlib.sha256(data).hexdigest() != sha:
        raise ApiError(422, "invalid")
    with portal.db.transaction() as c:
        row = _profile(c, device_id, key)
        pid = row["id"]
        m = c.execute("SELECT 1 FROM manifest WHERE profile_id=? AND doc_id=? AND sha256=? AND size=?", (pid, doc_id, sha, len(data))).fetchone()
        if m is None:
            raise ApiError(409, "manifest")
        used, n = c.execute("SELECT COALESCE(SUM(size),0), COUNT(*) FROM documents WHERE profile_id=? AND doc_id<>?", (pid, doc_id)).fetchone()
        if used + len(data) > protocol.MAX_DOCUMENTS_BYTES or n + 1 > protocol.MAX_DOCUMENTS:
            raise ApiError(413, "quota")
        total = c.execute("SELECT COALESCE(SUM(size),0) FROM documents WHERE NOT (profile_id=? AND doc_id=?)",
                          (pid, doc_id)).fetchone()[0]
        if total + len(data) > portal.config.max_total_bytes:
            raise ApiError(413, "quota")
        sealed: Sealed = portal.cipher.seal("document", pid, doc_id, data)
        c.execute("INSERT OR REPLACE INTO documents(profile_id, doc_id, sha256, size, key_id, nonce, ciphertext) VALUES (?,?,?,?,?,?,?)",
                  (pid, doc_id, sha, len(data), sealed.key_id, sealed.nonce, sealed.ciphertext))


@router.put("/profiles/{profile_key}/documents/{doc_id}")
async def put_document(request: Request, profile_key: PROFILE_KEY, doc_id: DOC_ID):
    portal, device_id = await _authed(request)
    pid, sha, size, stored = await run_in_threadpool(_document_precheck, portal, device_id, profile_key, doc_id)
    if request.headers.get("content-type", "").split(";")[0].strip().lower() != "application/pdf":
        raise ApiError(415, "content_type")
    declared = request.headers.get("content-length", "")
    if not declared.isdigit():
        raise ApiError(411, "length_required")
    if int(declared) != size:
        raise ApiError(422, "invalid")
    if stored:
        return JSONResponse({"ok": True})
    data = bytearray()
    try:
        async for chunk in request.stream():
            data += chunk
            if len(data) > size:
                raise ApiError(413, "too_large")
    except ClientDisconnect:
        raise ApiError(400, "aborted") from None
    if len(data) != size:
        raise ApiError(422, "invalid")
    await run_in_threadpool(_store_document, portal, device_id, profile_key, doc_id, sha, bytes(data))
    return JSONResponse({"ok": True})


# ---------------------------------------------------------------- delete, status
def _delete_profile(portal: Portal, device_id: str, key: str) -> None:
    with portal.db.transaction() as c:
        row = _profile(c, device_id, key)
        throttle.clear_login(c, portal.cipher.mac("login", row["handle_sha256"]))
        purge_profile_data(c, row)


@router.delete("/profiles/{profile_key}")
async def delete_profile(request: Request, profile_key: PROFILE_KEY):
    portal, device_id = await _authed(request)
    await run_in_threadpool(_delete_profile, portal, device_id, profile_key)
    return JSONResponse({"ok": True})


def _status(portal: Portal, device_id: str) -> ServerStatus:
    rows = portal.db.conn.execute(
        "SELECT p.*, (SELECT COUNT(*) FROM sessions s WHERE s.profile_id=p.id) AS n_sessions FROM profiles p "
        "WHERE p.device_id=? ORDER BY p.id", (device_id,)).fetchall()
    return ServerStatus(version=portal.config.version, profiles=[
        ProfileStatus(profile_key=r["profile_key"], enabled=bool(r["enabled"]), held=bool(r["held"]), config_rev=r["config_rev"],
                      snapshot_rev=r["snapshot_rev"], last_seq=r["last_seq"], acked_seq=r["acked_seq"], sessions=r["n_sessions"])
        for r in rows])


@router.get("/status")
async def get_status(request: Request):
    portal, device_id = await _authed(request)
    return _json(await run_in_threadpool(_status, portal, device_id))
