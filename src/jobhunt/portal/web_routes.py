"""The "Portail" section of the LOCAL web app (bound to 127.0.0.1): state, actions, review queue and journal of one profile.

Same wiring as `web/apply_routes.py`: `install(app, templates)` is called by web/app.py; helpers of web/app.py are imported
lazily (`_web()`). Every `manage.*` call blocks on the network or the journal: it runs in the thread pool, never in the event
loop. A credential (new code) is rendered straight in the response of the POST that created it, with `Cache-Control:
no-store`: it is never stored, redirected, flashed, put in a URL or logged. Pairing is NOT offered here: the pairing code
must not transit through a browser form (`big-penalty portal pair` in the terminal)."""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from starlette.concurrency import run_in_threadpool

from jobhunt.portal import manage, protocol, secrets, settings, sync
from jobhunt.portal.client import PortalError
from jobhunt.portal.cli import EVENT_KIND_LABELS, STATE_LABELS, change_text, report_line
from jobhunt.portal.manage import ManageError

log = logging.getLogger("jobhunt.web.portal")
router = APIRouter()

STATE_CLASS = {"received": "src", "awaiting_review": "warn", "queued": "src", "running": "src", "applied": "ok", "done": "ok",
               "conflict": "warn", "rejected": "bad", "refused": "bad", "failed": "bad", "cancelled": "warn", "interrupted": "warn",
               "lost": "warn"}
_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
EXPECTED = (ManageError, PortalError, secrets.PortalSecretError)

# Background "Synchroniser maintenant": in memory, ONE task at a time (like apply_routes.SENDING).
SYNCING: dict[str, Any] = {"running": False, "profile_id": None, "reports": [], "error": None, "finished_at": None}


# ------------------------------------------------------------ helpers
def _web():
    from jobhunt.web import app as webapp

    return webapp


def install(app, templates) -> None:
    """Wiring helper for web/app.py: `portal_web_routes.install(app, templates)`."""
    if not any(getattr(r, "path", None) == "/profiles/{pid}/portal" for r in app.routes):
        app.include_router(router)


def _pid_or_404(raw: str) -> int:
    from jobhunt.web.apply_routes import safe_id

    pid = safe_id(raw)
    if pid is None:
        raise HTTPException(404, "Profil introuvable")
    _web()._profile_or_404(pid)
    return pid


def _back(pid: int) -> str:
    return f"/profiles/{pid}/portal"


def _context(pid: int) -> dict[str, Any]:
    """Everything the page shows (blocking: journal reads). Values to be shown only; no secret is in here."""
    st = manage.status()
    ps = next((p for p in st.profiles if p.profile_id == pid), None)
    review, journal_rows = [], []
    if st.paired and ps is not None:
        review = [{"id": ev.event_id, "kind": EVENT_KIND_LABELS.get(ev.kind, ev.kind), "label": ev.label, "change": change_text(ev)}
                  for ev in manage.review_list(pid)]
        journal_rows = [{"at": ev.updated_at or ev.at, "kind": EVENT_KIND_LABELS.get(ev.kind, ev.kind), "label": ev.label,
                         "change": change_text(ev), "state": STATE_LABELS.get(ev.state, ev.state),
                         "state_class": STATE_CLASS.get(ev.state, "src"), "message": ev.message}
                        for ev in manage.journal_tail(pid, 50)]
    mine = [r for r in SYNCING["reports"] if r.profile_id in (pid, None)] if SYNCING["profile_id"] == pid else []
    return {"st": st, "ps": ps, "handle_shown": protocol.grouped(ps.handle, 5) if ps and ps.handle else "", "review": review, "journal_rows": journal_rows,
            "send_every": settings.get_portal_settings().send_every_min, "syncing": bool(SYNCING["running"]),
            "sync_lines": [report_line(r) for r in mine], "sync_error": SYNCING["error"] if SYNCING["profile_id"] == pid else None,
            "sync_at": SYNCING["finished_at"] if SYNCING["profile_id"] == pid else None, "creds": None}


async def _page(request: Request, pid: int, *, notice: str | None = None, error: str | None = None, creds=None) -> HTMLResponse:
    web = _web()
    profile = web._profile_or_404(pid)
    try:
        ctx = await run_in_threadpool(_context, pid)
    except EXPECTED as e:
        ctx = {"st": manage.PortalStatus(paired=False), "ps": None, "review": [], "journal_rows": [], "creds": None,
               "handle_shown": "", "send_every": settings.get_portal_settings().send_every_min, "syncing": False, "sync_lines": [], "sync_error": None,
               "sync_at": None}
        error = error or str(e)
    ctx["creds"] = creds
    resp = web.render(request, "portal_admin.html", profile=profile, notice=notice, error=error, **ctx)
    if creds is not None:
        resp.headers["Cache-Control"] = "no-store"
    return resp


async def _act(pid: int, fn, *args, notice: str, **kw):
    """Run one manage action in the thread pool; French errors are flashed, success redirects back (POST, redirect, GET)."""
    web = _web()
    try:
        await run_in_threadpool(lambda: fn(*args, **kw))
    except EXPECTED as e:
        return web._flash(_back(pid), error=str(e))
    return web._flash(_back(pid), notice=notice)


def _checked(form, name: str) -> bool:
    return str(form.get(name) or "").lower() in ("1", "on", "true", "yes")


# ------------------------------------------------------------ page
@router.get("/profiles/{pid}/portal", response_class=HTMLResponse)
async def portal_page(request: Request, pid: str, f: str | None = None):
    p = _pid_or_404(pid)
    notice, error = _web()._take_flash(f)
    return await _page(request, p, notice=notice, error=error)


# ------------------------------------------------------------ credentials: rendered in the response, never stored
@router.post("/profiles/{pid}/portal/enable", response_class=HTMLResponse)
async def portal_enable(request: Request, pid: str):
    p = _pid_or_404(pid)
    form = await request.form()
    review = _checked(form, "review")
    try:
        creds = await run_in_threadpool(lambda: manage.enable(p, None, review))
    except EXPECTED as e:
        return _web()._flash(_back(p), error=str(e))
    return await _page(request, p, notice="Portail activé. Notez le code maintenant : il ne sera plus affiché.", creds=creds)


@router.post("/profiles/{pid}/portal/passcode", response_class=HTMLResponse)
async def portal_passcode(request: Request, pid: str):
    p = _pid_or_404(pid)
    try:
        creds = await run_in_threadpool(lambda: manage.rotate_passcode(p, None))
    except EXPECTED as e:
        return _web()._flash(_back(p), error=str(e))
    return await _page(request, p, notice="Nouveau code créé, les appareils connectés sont déconnectés. Notez-le maintenant.", creds=creds)


# ------------------------------------------------------------ actions
@router.post("/profiles/{pid}/portal/revoke")
async def portal_revoke(pid: str):
    p = _pid_or_404(pid)
    return await _act(p, manage.revoke, p, notice="Appareils déconnectés. Le code reste valable.")


@router.post("/profiles/{pid}/portal/mode")
async def portal_mode(request: Request, pid: str):
    p = _pid_or_404(pid)
    mode = str((await request.form()).get("mode") or "")
    if mode not in ("direct", "review"):
        return _web()._flash(_back(p), error="Mode inconnu.")
    return await _act(p, manage.set_mode, p, mode, notice="Mode validation : vous validez chaque action." if mode == "review"
                      else "Mode direct : les réponses et validations de la personne s'appliquent sans vous.")


@router.post("/profiles/{pid}/portal/disable")
async def portal_disable(request: Request, pid: str):
    p = _pid_or_404(pid)
    purge = _checked(await request.form(), "purge")
    web = _web()
    try:
        res = await run_in_threadpool(lambda: manage.disable(p, purge))
    except EXPECTED as e:
        return web._flash(_back(p), error=str(e))
    if not res.server_updated:
        return web._flash(_back(p), error=f"Désactivé sur ce Mac, mais le serveur n'a pas été mis à jour ({res.error}). "
                          "L'accès reste ouvert côté serveur : réactivez puis désactivez le profil quand il est joignable.")
    return web._flash(_back(p), notice="Portail désactivé" + (", données effacées du serveur." if purge else "."))


@router.post("/profiles/{pid}/portal/pause")
async def portal_pause(pid: str):
    p = _pid_or_404(pid)
    return await _act(p, manage.pause, notice="Portail en pause sur ce Mac : rien n'est appliqué ni envoyé.")


@router.post("/profiles/{pid}/portal/resume")
async def portal_resume(pid: str):
    p = _pid_or_404(pid)
    return await _act(p, manage.resume, notice="Portail repris.")


@router.post("/profiles/{pid}/portal/resync")
async def portal_resync(pid: str):
    p = _pid_or_404(pid)
    return await _act(p, manage.resync, p, notice="Nouveau flux créé : les actions du portail en attente ont été abandonnées.")


# ------------------------------------------------------------ review queue
@router.post("/profiles/{pid}/portal/review/apply-all")
async def portal_apply_all(pid: str):
    p = _pid_or_404(pid)
    return await _review(p, None, True)


@router.post("/profiles/{pid}/portal/review/reject-all")
async def portal_reject_all(pid: str):
    p = _pid_or_404(pid)
    return await _review(p, None, False)


@router.post("/profiles/{pid}/portal/review/{event_id}/{decision}")
async def portal_review_one(pid: str, event_id: str, decision: str):
    p = _pid_or_404(pid)
    if decision not in ("apply", "reject") or not _ID.fullmatch(event_id):
        raise HTTPException(404, "Action introuvable")
    return await _review(p, [event_id], decision == "apply")


async def _review(pid: int, ids: list[str] | None, approve: bool):
    web = _web()
    try:
        if approve:
            rep = await run_in_threadpool(lambda: manage.review_apply(pid, ids))
            if rep.skipped_reason:
                return web._flash(_back(pid), error=f"Rien n'a été appliqué ({rep.skipped_reason}).")
            done = sum(rep.states.values())
            return web._flash(_back(pid), notice=f"{done} action(s) traitée(s).")
        n = await run_in_threadpool(lambda: manage.review_reject(pid, ids))
    except EXPECTED as e:
        return web._flash(_back(pid), error=str(e))
    return web._flash(_back(pid), notice=f"{n} action(s) refusée(s).")


# ------------------------------------------------------------ sync now (background)
async def _run_sync(pid: int) -> None:
    try:
        SYNCING["reports"] = await sync.async_sync_all([pid])
    except Exception as e:  # noqa: BLE001 - the type only: nothing secret in a message
        log.exception("portal sync failed: %s", type(e).__name__)
        SYNCING["error"] = f"Erreur inattendue ({type(e).__name__})."
    finally:
        SYNCING.update(running=False, finished_at=datetime.now(UTC))


@router.post("/profiles/{pid}/portal/sync")
async def portal_sync(pid: str):
    p = _pid_or_404(pid)
    web = _web()
    if SYNCING["running"]:
        return web._flash(_back(p), error="Une synchronisation est déjà en cours : attendez sa fin.")
    SYNCING.update(running=True, profile_id=p, reports=[], error=None, finished_at=None)
    SYNCING["task"] = asyncio.create_task(_run_sync(p))
    return web._flash(_back(p), notice="Synchronisation lancée. Les envois approuvés depuis le portail ouvrent des fenêtres de navigateur.")
