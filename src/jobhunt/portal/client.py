"""HTTP client of the portal API (DESIGN §4). TLS only (plain http is accepted for literal 127.0.0.1, for tests), certificates
verified, no redirect followed, timeouts, response size cap, no environment proxy. Every response is validated with the
`protocol` models; anything else is a `PortalError` with a fixed French message (never the body, never the token)."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from urllib.parse import quote, urlsplit

import httpx
from pydantic import BaseModel, TypeAdapter, ValidationError

from jobhunt.portal import protocol

MAX_RESPONSE_BYTES = 4 * 1024 * 1024
TIMEOUT = httpx.Timeout(connect=10.0, read=30.0, write=30.0, pool=10.0)
LOCAL_HOSTS = ("127.0.0.1",)
_HEX32 = re.compile(r"^[0-9a-f]{32}$")

MESSAGES = {
    "unauthorized": "Le portail refuse cet appareil (jeton inconnu ou révoqué) : appairez-le de nouveau.",
    "not_found": "Le portail ne connaît pas cet élément.",
    "config_rev": "La configuration du portail est plus récente que celle de cet appareil.",
    "rev": "Le portail a déjà une version plus récente de l'instantané.",
    "ack": "Le portail refuse l'accusé de réception : resynchronisez le profil.",
    "stream": "Le portail utilise un autre flux : resynchronisez le profil.",
    "handle_taken": "Cet identifiant est déjà pris sur le portail.",
    "too_large": "Le portail a refusé ou renvoyé des données trop volumineuses.",
    "invalid": "Le portail a refusé la demande (données invalides).",
    "throttled": "Le portail demande de patienter (trop de requêtes).",
    "bad_response": "Réponse inattendue du portail.",
    "redirect": "Le portail a répondu par une redirection : refusée.",
    "bad_url": "Adresse du portail invalide : https:// est obligatoire.",
    "no_token": "Cet appareil n'est pas appairé.",
    "offline": "Portail injoignable.",
    "http_error": "Le portail a répondu par une erreur.",
}


class PortalError(Exception):
    def __init__(self, code: str, status: int = 0, current: int | None = None):
        self.code, self.status, self.current = code, status, current
        super().__init__(MESSAGES.get(code, MESSAGES["http_error"]))


class PortalOffline(PortalError):
    def __init__(self):
        super().__init__("offline", 0)


@dataclass
class InvalidEvent:
    """An event with a valid envelope but a body the protocol refuses: journaled as `rejected`, the stream goes on."""
    seq: int
    event_id: str
    kind: str
    at: object


@dataclass
class InboxPage:
    stream_id: str
    last_seq: int
    acked_seq: int
    held: bool
    events: list
    sessions_epoch: int
    revoked_through_seq: int


class _Envelope(BaseModel):
    seq: int
    event_id: protocol.Hex32
    at: protocol.Timestamp
    kind: str


_EVENT = TypeAdapter(protocol.Event)


def check_base_url(url: str) -> str:
    try:
        u = urlsplit(url.strip())
        host = u.hostname
        port = u.port  # noqa: F841 - validates the port
    except ValueError:
        raise PortalError("bad_url") from None
    ok = (u.scheme == "https" and host) or (u.scheme == "http" and host in LOCAL_HOSTS)
    if not ok or u.username or u.password or u.query or u.fragment:
        raise PortalError("bad_url")
    return url.strip().rstrip("/")


def origin_of(url: str) -> str:
    u = urlsplit(check_base_url(url))
    return f"{u.scheme}://{u.netloc.lower()}"


class PortalClient:
    def __init__(self, base_url: str, token: str | None = None, *, transport: httpx.BaseTransport | None = None):
        self.base_url = check_base_url(base_url)
        self._token = token
        self._http = httpx.Client(verify=True, follow_redirects=False, timeout=TIMEOUT, trust_env=False, transport=transport)

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "PortalClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ------------------------------------------------------------ plumbing
    def _request(self, method: str, path: str, *, body: bytes | None = None, content_type: str = "application/json",
                 limit: int = protocol.MAX_JSON_BODY, auth: bool = True) -> bytes:
        if body is not None and len(body) > limit:
            raise PortalError("too_large", 413)
        headers = {"Accept": "application/json", "Accept-Encoding": "identity"}
        if auth:
            if not self._token:
                raise PortalError("no_token")
            headers["Authorization"] = f"Bearer {self._token}"
        if body is not None:
            headers["Content-Type"] = content_type
        url = f"{self.base_url}{protocol.API_PREFIX}{path}"
        try:
            with self._http.stream(method, url, content=body, headers=headers) as r:
                declared = r.headers.get("content-length")
                if declared and declared.isdigit() and int(declared) > MAX_RESPONSE_BYTES:
                    raise PortalError("too_large", r.status_code)
                chunks, size = [], 0
                for chunk in r.iter_bytes():
                    size += len(chunk)
                    if size > MAX_RESPONSE_BYTES:
                        raise PortalError("too_large", r.status_code)
                    chunks.append(chunk)
                status, data = r.status_code, b"".join(chunks)
        except PortalError:
            raise
        except httpx.TransportError:
            raise PortalOffline() from None
        except (httpx.HTTPError, ValueError):
            raise PortalError("bad_response") from None
        if 200 <= status < 300:
            return data
        raise self._error(status, data)

    @staticmethod
    def _error(status: int, data: bytes) -> PortalError:
        if 300 <= status < 400:
            return PortalError("redirect", status)
        try:
            err = protocol.ErrorBody.model_validate_json(data)
        except (ValidationError, ValueError):
            err = None
        known = {401: "unauthorized", 404: "not_found", 413: "too_large", 422: "invalid", 429: "throttled"}
        if status == 409:
            code = err.error if err and err.error in ("config_rev", "rev", "ack", "stream", "handle_taken") else "http_error"
            return PortalError(code, status, err.current if err else None)
        return PortalError(known.get(status, "http_error"), status)

    @staticmethod
    def _model(model: type, data: bytes):
        try:
            return model.model_validate_json(data)
        except (ValidationError, ValueError):
            raise PortalError("bad_response") from None

    @staticmethod
    def _key(profile_key: str) -> str:
        if not _HEX32.match(profile_key or ""):
            raise PortalError("invalid", 0)
        return profile_key

    def _json(self, model: BaseModel) -> bytes:
        return model.model_dump_json().encode("utf-8")

    # ------------------------------------------------------------ API
    def pair(self, pairing_code: str, device_name: str, token_sha256: str) -> protocol.PairResponse:
        req = protocol.PairRequest(pairing_code=pairing_code, device_name=device_name, token_sha256=token_sha256)
        return self._model(protocol.PairResponse, self._request("POST", "/pair", body=self._json(req), auth=False))

    def put_profile(self, profile_key: str, config: protocol.ProfileConfig) -> protocol.ProfileConfigResponse:
        data = self._request("PUT", f"/profiles/{self._key(profile_key)}", body=self._json(config))
        return self._model(protocol.ProfileConfigResponse, data)

    def get_inbox(self, profile_key: str, after: int) -> InboxPage:
        data = self._request("GET", f"/profiles/{self._key(profile_key)}/inbox?after={int(after)}")
        try:
            raw = json.loads(data)
            raw_events = raw["events"]
            head = protocol.Inbox.model_validate({**raw, "events": []})
            if not isinstance(raw_events, list) or len(raw_events) > protocol.INBOX_PAGE:
                raise ValueError
        except (ValueError, KeyError, TypeError, ValidationError):
            raise PortalError("bad_response") from None
        events: list = []
        for e in raw_events:
            try:
                events.append(_EVENT.validate_python(e))
            except ValidationError:
                try:
                    env = _Envelope.model_validate(e)
                except ValidationError:
                    raise PortalError("bad_response") from None
                events.append(InvalidEvent(env.seq, env.event_id, env.kind, env.at))
        return InboxPage(head.stream_id, head.last_seq, head.acked_seq, head.held, events,
                         head.sessions_epoch, head.revoked_through_seq)

    def put_snapshot(self, profile_key: str, push: protocol.SnapshotPush) -> protocol.SnapshotPushResponse:
        data = self._request("PUT", f"/profiles/{self._key(profile_key)}/snapshot", body=self._json(push),
                             limit=protocol.MAX_SNAPSHOT_BODY)
        response = self._model(protocol.SnapshotPushResponse, data)
        if response.rev != push.rev:
            raise PortalError("bad_response")
        return response

    def put_document(self, profile_key: str, doc_id: str, pdf: bytes) -> None:
        if not _HEX32.match(doc_id or ""):
            raise PortalError("invalid", 0)
        self._request("PUT", f"/profiles/{self._key(profile_key)}/documents/{quote(doc_id)}", body=pdf, content_type="application/pdf",
                      limit=protocol.MAX_DOCUMENT_BYTES)

    def delete_profile(self, profile_key: str) -> None:
        self._request("DELETE", f"/profiles/{self._key(profile_key)}")

    def status(self) -> protocol.ServerStatus:
        return self._model(protocol.ServerStatus, self._request("GET", "/status"))
