"""The ASGI application: `create_app(config)`; everything lives under `config.base_path` (DESIGN §6, §8).

Outermost layer (`PortalASGI`, pure ASGI, wraps FastAPI's own middleware stack so even its error responses are covered):
Host allow-list, request body limits on the declared length AND on the stream (413), security headers on every response,
request log (method, route template, status, duration: nothing else), a once-an-hour purge, and a catch-all that turns any
escaped exception into a generic error response without echoing it."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import Headers
from starlette.exceptions import HTTPException as StarletteHTTPException

from jobhunt.portal import protocol

from . import api, db as dbmod, pages
from .auth import ScryptGate
from .config import Config
from .crypto import Cipher
from .db import Database

log = logging.getLogger("bigpenalty.portal")

MAX_FORM_BODY = 64 * 1024
BODY_DEADLINE_SECONDS = 30
SECURITY_HEADERS = {
    "content-security-policy": "default-src 'none'; style-src 'self'; img-src 'self'; form-action 'self'; base-uri 'none'; "
                               "frame-ancestors 'none'",
    "x-frame-options": "DENY",
    "strict-transport-security": "max-age=31536000",
    "referrer-policy": "same-origin",  # no-referrer makes browsers send `Origin: null` on our own form posts
    "x-content-type-options": "nosniff",
    "permissions-policy": "accelerometer=(), camera=(), geolocation=(), gyroscope=(), magnetometer=(), microphone=(), payment=(), usb=()",
    "cross-origin-opener-policy": "same-origin",
    "cross-origin-resource-policy": "same-origin",
    "x-robots-tag": "noindex, nofollow",
}
_PAGE_TEXT = {
    400: "Requête invalide.", 403: "Accès refusé.", 404: "Page introuvable.", 405: "Méthode non autorisée.",
    408: "Délai de réception dépassé.", 413: "Requête trop volumineuse.", 415: "Type de contenu non accepté.", 421: "Adresse non reconnue.",
    422: "Requête invalide.", 429: "Trop de requêtes. Réessayez plus tard.",
}


@dataclass
class Portal:
    """Everything a request needs; `app.state.portal`."""
    config: Config
    db: Database
    cipher: Cipher
    gate: ScryptGate = field(default_factory=ScryptGate)
    clock: Callable[[], float] = time.time


def error_page(status: int, base_path: str = "") -> HTMLResponse:
    """Small generic French page: no template, no input echoed."""
    text = _PAGE_TEXT.get(status, "Une erreur est survenue. Réessayez plus tard.")
    home = f'<p><a href="{base_path}/">Retour à l\'accueil</a></p>' if base_path else ""
    return HTMLResponse(f'<!doctype html><html lang="fr"><head><meta charset="utf-8"><title>{text}</title></head>'
                        f"<body><h1>{text}</h1>{home}</body></html>", status_code=status)


def _limit_for(method: str, path: str, base: str) -> int:
    api_prefix = base + protocol.API_PREFIX + "/"
    if not path.startswith(api_prefix):
        return MAX_FORM_BODY
    parts = path[len(api_prefix):].split("/")
    if method == "PUT" and len(parts) == 3 and parts[0] == "profiles" and parts[2] == "snapshot":
        return protocol.MAX_SNAPSHOT_BODY
    if method == "PUT" and len(parts) == 4 and parts[0] == "profiles" and parts[2] == "documents":
        return protocol.MAX_DOCUMENT_BYTES
    return protocol.MAX_JSON_BODY


class PortalASGI:
    def __init__(self, app, portal: Portal) -> None:
        self.app, self.portal = app, portal
        self.base = portal.config.base_path
        self.upload_slots = asyncio.Semaphore(2)

    def _generic(self, status: int, path: str):
        if path.startswith(self.base + protocol.API_PREFIX + "/"):
            code = {408: "timeout", 413: "too_large", 421: "bad_host", 400: "invalid", 500: "server"}.get(status, "error")
            return api.error_response(status, code)
        return error_page(status, self.base)

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "lifespan":
            await self.app(scope, receive, send)
            return
        if scope["type"] != "http":
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
            return
        path, method = scope["path"], scope["method"]
        started = blocked = False
        status = 0
        seen = 0
        t0 = time.monotonic()
        body_deadline = asyncio.get_running_loop().time() + BODY_DEADLINE_SECONDS
        static = path.startswith(self.base + "/static/")

        async def raw_send(message) -> None:
            nonlocal started, status
            if message["type"] == "http.response.start":
                started, status = True, message["status"]
                present = {k.lower() for k, _ in message.get("headers", [])}
                extra = [(k.encode(), v.encode()) for k, v in SECURITY_HEADERS.items() if k.encode() not in present]
                if not static and b"cache-control" not in present:
                    extra.append((b"cache-control", b"no-store"))
                message = {**message, "headers": [*message.get("headers", []), *extra]}
            await send(message)

        async def inner_send(message) -> None:
            if not blocked:
                await raw_send(message)

        async def respond(response: Response) -> None:
            await response(scope, receive, raw_send)

        limit = _limit_for(method, path, self.base)

        async def inner_receive():
            nonlocal blocked, seen
            if blocked:
                return {"type": "http.disconnect"}
            try:
                async with asyncio.timeout_at(body_deadline):
                    message = await receive()
            except TimeoutError:
                blocked = True
                if not started:
                    await respond(self._generic(408, path))
                return {"type": "http.disconnect"}
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > limit:
                    if not started:
                        await respond(self._generic(413, path))
                    blocked = True
                    return {"type": "http.disconnect"}
            return message

        try:
            headers = Headers(scope=scope)
            host = headers.get("host", "").lower()
            if host not in self.portal.config.allowed_hosts and (method, path) != ("GET", self.base + "/healthz"):
                await respond(PlainTextResponse("Misdirected Request", status_code=421))
                return
            declared = headers.get("content-length")
            if declared is not None:
                if not declared.isdigit():
                    await respond(self._generic(400, path))
                    return
                if int(declared) > limit:
                    await respond(self._generic(413, path))
                    return
            if self.portal.clock() - self.portal.db.last_purge >= dbmod.PURGE_EVERY:
                try:
                    await run_in_threadpool(dbmod.purge_if_due, self.portal.db, self.portal.clock())
                except Exception as exc:  # a purge problem never fails a request
                    log.error("purge failed: %s", type(exc).__name__)
            try:
                if limit in (protocol.MAX_SNAPSHOT_BODY, protocol.MAX_DOCUMENT_BYTES):
                    try:
                        async with asyncio.timeout_at(body_deadline):
                            await self.upload_slots.acquire()
                    except TimeoutError:
                        await respond(self._generic(408, path))
                        return
                    try:
                        await self.app(scope, inner_receive, inner_send)
                    finally:
                        self.upload_slots.release()
                else:
                    await self.app(scope, inner_receive, inner_send)
            except Exception as exc:
                log.error("unhandled %s", type(exc).__name__)  # the type only: never the message, never the input
                if not started and not blocked:
                    await respond(self._generic(500, path))
        finally:
            route = scope.get("route")
            template = getattr(route, "path", None) or "(no route)"
            log.info("%s %s %s %dms", method, template, status, int((time.monotonic() - t0) * 1000))


class PortalApp(FastAPI):
    portal: Portal

    def build_middleware_stack(self):
        return PortalASGI(super().build_middleware_stack(), self.state.portal)


def create_app(config: Config, *, clock: Callable[[], float] | None = None, db: Database | None = None) -> FastAPI:
    portal = Portal(config=config, db=db or Database(config.db_path), cipher=Cipher(config.data_key, config.data_key_old))
    if clock is not None:
        portal.clock = clock
    dbmod.purge_expired(portal.db, portal.clock())
    if config.hold_all_on_start:
        with portal.db.transaction() as conn:
            conn.execute("UPDATE profiles SET held=1")
            conn.execute("DELETE FROM sessions")
            conn.execute("DELETE FROM known_devices")
    app = PortalApp(docs_url=None, redoc_url=None, openapi_url=None, redirect_slashes=False)
    app.state.portal = portal
    base = config.base_path

    @app.exception_handler(api.ApiError)
    async def _api_error(request: Request, exc: api.ApiError):
        return api.error_response(exc.status, exc.code, exc.current)

    @app.exception_handler(RequestValidationError)
    async def _invalid(request: Request, exc: RequestValidationError):
        if request.url.path.startswith(base + protocol.API_PREFIX + "/"):
            return api.error_response(422, "invalid")
        return error_page(422, base)

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException):
        if request.url.path.startswith(base + protocol.API_PREFIX + "/"):
            code = {404: "not_found", 405: "method_not_allowed", 401: "unauthorized"}.get(exc.status_code, "error")
            return api.error_response(exc.status_code, code)
        response = error_page(exc.status_code, base)
        for k, v in (exc.headers or {}).items():
            response.headers[k] = v
        return response

    @app.exception_handler(Exception)
    async def _server(request: Request, exc: Exception):
        if request.url.path.startswith(base + protocol.API_PREFIX + "/"):
            return api.error_response(500, "server")
        return error_page(500, base)

    @app.get(base, include_in_schema=False)
    async def _base_redirect():
        return RedirectResponse(base + "/", status_code=308)

    @app.get(base + "/healthz", include_in_schema=False)
    async def _healthz():
        return JSONResponse({"ok": True})

    @app.get(base + "/version.json", include_in_schema=False)
    async def _version():
        return JSONResponse({"sha": config.version})

    app.include_router(api.router, prefix=base)
    pages.install(app, portal)
    app.include_router(pages.router, prefix=base)
    return app
