"""The HTML pages of the portal (DESIGN §6): login, a-faire, reponses, accord, candidatures, documents and the two stylesheets.

`create_app` builds one `Portal` (`app.state.portal`); a route reads it with `portal_of(request)`. Every page except `/`,
`/connexion` and the static files needs a session (`auth.resolve_session`); the profile is ALWAYS the session's. Every POST
checks same-origin evidence and the session CSRF token, changes state through `ops`, then redirects (303) to a GET page that
shows the outcome through a one-shot signed flash cookie (fixed French sentences and field labels only, never typed values).

Blocking work (SQLite, scrypt) runs in the thread pool: GET routes are plain `def`, POST routes read the body then hand a
plain function to `run_in_threadpool`. Forms are parsed with `urllib.parse` (urlencoded only): no multipart dependency.
Imports are limited to the standard library, fastapi/starlette, jinja2, pydantic and `jobhunt.portal.protocol` (via ops/auth)."""

from __future__ import annotations

import base64
import hmac
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import parse_qsl
from zoneinfo import ZoneInfo

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from jinja2 import Environment, FileSystemLoader, select_autoescape
from starlette.concurrency import run_in_threadpool

from jobhunt.portal import protocol

from . import auth, ops
from .db import SESSION_ABSOLUTE

if TYPE_CHECKING:
    from .app import Portal

router = APIRouter()

HERE = Path(__file__).resolve().parent
STYLE_CSS = Path(__file__).resolve().parents[2] / "web" / "static" / "style.css"
PORTAL_CSS = HERE / "static" / "portal.css"
STATIC_CACHE = "public, max-age=3600"

FLASH_COOKIE = "__Secure-bp_flash"
FLASH_TTL = 60
FLASH_MAX = 600
FLASH_MESSAGE_MAX = 160

_HEX32 = re.compile(r"^[0-9a-f]{32}$")
_HEX16 = re.compile(r"^[0-9a-f]{16}$")
BACK_PAGES = ("a-faire", "accord")
MAX_GRANTS = 500

MSG_PICK_ONE = "Sélectionnez au moins une candidature."
MSG_ALREADY = "Ces demandes sont déjà en attente du Mac."
MSG_NOTHING_AWAITING = "Aucune candidature n'attend votre accord."
MSG_NO_ANSWER = "Aucune réponse à enregistrer."
MSG_NO_CHANGE = "Aucune modification à enregistrer."

STATUS_LABELS = {
    "applied": "Appliquée", "conflict": "Non appliquée (modifiée sur le Mac)", "rejected": "Refusée",
    "awaiting_review": "En attente de validation sur le Mac", "queued": "En file d'attente", "running": "En cours",
    "done": "Faite", "refused": "Refusée", "failed": "Échec", "cancelled": "Annulée", "interrupted": "Interrompue",
    "lost": "Perdue : à refaire",
}
STATUS_CLASSES = {"applied": "ok", "done": "ok", "conflict": "warn", "awaiting_review": "warn", "queued": "warn", "running": "warn",
                  "rejected": "bad", "refused": "bad", "failed": "bad", "interrupted": "bad", "lost": "bad", "cancelled": ""}
INFO_LABELS = {"captcha": "CAPTCHA à résoudre", "host": "Site à approuver", "dry_run": "Essai à blanc en pause",
               "sending": "Envoi en cours", "interrupted": "Envoi interrompu : à vérifier",
               "supervised": "Action humaine requise", "human": "Action humaine requise",
               "email": "Code par e-mail", "approved": "Accord enregistré, en attente du Mac"}
GRANT_VERBS = {"send": "Approbation de la candidature", "resume": "Reprise de la candidature", "sent": "Marquée comme envoyée",
               "not_sent": "Remise dans la file"}
EVENT_VERBS = {"answer": "Réponse enregistrée", "question_answer": "Réponse à une question", "custom_edit": "Réponse modifiée",
               "custom_delete": "Réponse retirée"}
MONTHS = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août", "septembre", "octobre", "novembre", "décembre"]
RESULTS_SHOWN = 20


# ---------------------------------------------------------------- templates and filters
try:
    _TZ = ZoneInfo("Europe/Paris")
except Exception:  # no tz database in the image: UTC
    _TZ = UTC


def _local(dt: datetime) -> datetime:
    return (dt if dt.tzinfo else dt.replace(tzinfo=UTC)).astimezone(_TZ)


def date_fr(dt: datetime | None) -> str:
    if dt is None:
        return ""
    d = _local(dt)
    return f"{d.day} {MONTHS[d.month - 1]} {d.year}"


def datetime_fr(dt: datetime | None) -> str:
    if dt is None:
        return ""
    d = _local(dt)
    return f"{d.day} {MONTHS[d.month - 1]} {d.year} à {d.hour:02d}:{d.minute:02d}"


def duration_fr(seconds: float) -> str:
    s = int(max(0, seconds))
    if s < 60:
        return "moins d'une minute"
    if s < 3600:
        n = s // 60
        return f"{n} minute{'s' if n > 1 else ''}"
    if s < 86400:
        n = s // 3600
        return f"{n} heure{'s' if n > 1 else ''}"
    n = s // 86400
    return f"{n} jour{'s' if n > 1 else ''}"


def age_days_fr(days: float | None) -> str:
    if days is None:
        return ""
    n = int(max(0, days))
    if n == 0:
        return "publiée aujourd'hui"
    return f"publiée il y a {n} jour{'s' if n > 1 else ''}"


def safe_url(url: str | None) -> bool:
    return bool(url) and (url.startswith("http://") or url.startswith("https://"))


ENV = Environment(loader=FileSystemLoader(str(HERE / "templates")), autoescape=select_autoescape(["html"], default=True),
                  trim_blocks=True, lstrip_blocks=True)
ENV.filters.update(date_fr=date_fr, datetime_fr=datetime_fr, age_days_fr=age_days_fr, safe_url=safe_url,
                   status_label=lambda s: STATUS_LABELS.get(s, s), status_class=lambda s: STATUS_CLASSES.get(s, ""))


# ---------------------------------------------------------------- plumbing
def portal_of(request: Request) -> Portal:
    return request.app.state.portal


def _forbidden(portal: Portal) -> Response:
    from . import app as appmod  # late: app.py imports this module
    return appmod.error_page(403, portal.config.base_path)


def _not_found(portal: Portal) -> Response:
    from . import app as appmod
    return appmod.error_page(404, portal.config.base_path)


def _session(portal: Portal, request: Request) -> auth.Session | None:
    return auth.resolve_session(portal, auth.cookie_values(request.scope["headers"], auth.SESSION_COOKIE), portal.clock())


def _single(request: Request, name: str) -> str | None:
    values = auth.cookie_values(request.scope["headers"], name)
    return values[0] if len(values) == 1 else None


class Form:
    def __init__(self, items: list[tuple[str, str]]) -> None:
        self.items = items

    def get(self, name: str) -> str:
        return next((v for k, v in self.items if k == name), "")

    def getall(self, name: str) -> list[str]:
        return [v for k, v in self.items if k == name]

    def has(self, name: str) -> bool:
        return any(k == name for k, _ in self.items)


async def _read_form(request: Request) -> Form:
    if not request.headers.get("content-type", "").lower().startswith("application/x-www-form-urlencoded"):
        return Form([])
    body = await request.body()
    try:
        return Form(parse_qsl(body.decode("utf-8", "replace"), keep_blank_values=True, max_num_fields=2000))
    except ValueError:
        return Form([])


# ---------------------------------------------------------------- flash cookie (signed, one shot, 60 s)
def _flash_mac(portal: Portal, session_id: str, body: str) -> str:
    # `Cipher.mac` only accepts its three fixed purposes: "login" with a "flash|" prefix (the nonce uses "nonce|") keeps the
    # domains apart, and the session id is part of the signed text so a flash is only valid in the session that got it.
    return portal.cipher.mac("login", f"flash|{session_id}|{body}")[:32]


def make_flash(portal: Portal, session_id: str, now: float, ok: list[str], err: list[str]) -> str | None:
    ok = [m[:FLASH_MESSAGE_MAX] for m in ok][:6]
    err = [m[:FLASH_MESSAGE_MAX] for m in err][:6]
    while ok or err:
        raw = json.dumps({"t": int(now), "o": ok, "e": err}, ensure_ascii=False, separators=(",", ":")).encode()
        body = base64.urlsafe_b64encode(raw).decode().rstrip("=")
        value = f"{body}.{_flash_mac(portal, session_id, body)}"
        if len(value) <= FLASH_MAX:
            return value
        (err if err else ok).pop()
    return None


def read_flash(portal: Portal, session_id: str, value: str | None, now: float) -> tuple[list[str], list[str]]:
    if not value:
        return [], []
    body, _, mac = value.rpartition(".")
    if not body or not hmac.compare_digest(_flash_mac(portal, session_id, body).encode(), mac.encode("utf-8", "replace")):
        return [], []
    try:
        data = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        if not isinstance(data["t"], int) or not 0 <= now - data["t"] <= FLASH_TTL:
            return [], []
        ok = [m for m in data["o"] if isinstance(m, str)][:6]
        err = [m for m in data["e"] if isinstance(m, str)][:6]
    except (ValueError, KeyError, TypeError):
        return [], []
    return ok, err


# ---------------------------------------------------------------- request context
@dataclass
class Ctx:
    request: Request
    portal: Portal
    session: auth.Session
    form: Form
    now: float
    _view: ops.PortalView | None = None

    @property
    def base(self) -> str:
        return self.portal.config.base_path

    @property
    def view(self) -> ops.PortalView:
        if self._view is None:
            self._view = ops.load_view(self.portal, self.session.profile_id, self.now)
        return self._view

    def redirect(self, path: str, ok: list[str] | None = None, err: list[str] | None = None) -> Response:
        response = RedirectResponse(f"{self.base}/{path}", status_code=303)
        value = make_flash(self.portal, self.session.id, self.now, ok or [], err or [])
        if value:
            response.set_cookie(**auth.cookie_kwargs(self.portal.config, FLASH_COOKIE, value, FLASH_TTL))
        return response


def _to_login(portal: Portal) -> Response:
    return RedirectResponse(f"{portal.config.base_path}/connexion", status_code=303)


def _get_ctx(request: Request) -> Ctx | Response:
    portal = portal_of(request)
    now = portal.clock()
    session = _session(portal, request)
    if session is None:
        return _to_login(portal)
    return Ctx(request, portal, session, Form([]), now)


async def _post(request: Request, handler: Callable[[Ctx], Response]) -> Response:
    portal = portal_of(request)
    if not auth.same_origin_ok("POST", request.scope["headers"], portal.config):
        return _forbidden(portal)
    form = await _read_form(request)

    def run() -> Response:
        now = portal.clock()
        session = _session(portal, request)
        if session is None:
            return _to_login(portal)
        if not auth.csrf_ok(portal, session.id, form.get("csrf")):
            return _forbidden(portal)
        return handler(Ctx(request, portal, session, form, now))

    return await run_in_threadpool(run)


def _layout(ctx: Ctx) -> dict:
    view, snap = ctx.view, ctx.view.snapshot
    name = (snap.display_name if snap else "") or ""
    if snap is None or view.synced_age_seconds is None:
        return {"name": name, "waiting": True, "counts": {}}
    open_q = [q for q in snap.questions if q.qid not in view.answered_questions]
    age = view.synced_age_seconds
    return {"name": name, "waiting": False, "synced_line": "maintenant" if age < 60 else f"il y a {duration_fr(age)}",
            "stale": duration_fr(age) if age > 3600 else None, "inbox_full": view.inbox_full, "paused": snap.paused,
            "review_mode": snap.review_mode,
            "counts": {"a_faire": len(open_q) + len(snap.email_pauses) + len(snap.unconfirmed), "accord": len(snap.awaiting),
                       "envoyees": max(snap.counts.sent_total, len(snap.sent))}}


def _render(ctx: Ctx, template: str, active: str, status: int = 200, *, docs: set[str] | None = None, **extra) -> Response:
    view, portal = ctx.view, ctx.portal
    layout = _layout(ctx)
    raw_flash = _single(ctx.request, FLASH_COOKIE)
    ok, err = read_flash(portal, ctx.session.id, raw_flash, ctx.now)
    if layout["waiting"]:
        template, extra = "attente.html", {}
    snap = view.snapshot
    if docs is None:
        docs = {d.doc_id for d in snap.documents} if snap else set()
    html = ENV.get_template(template).render(
        base=ctx.base, version=portal.config.version, csrf=auth.csrf_token(portal, ctx.session.id), layout=layout, active=active,
        flash={"ok": ok, "err": err}, snap=snap, view=view, docs=docs, **extra)
    response = HTMLResponse(html, status_code=status)
    if raw_flash is not None:
        response.headers.append("set-cookie", auth.clear_cookie_header(portal.config, FLASH_COOKIE))
    return response


def _count(n: int, one: str, many: str) -> str:
    return one if n == 1 else many.format(n=n)


# ---------------------------------------------------------------- grants
@dataclass(frozen=True)
class Entry:
    grant: str
    kind: str  # send | resume | sent | not_sent
    company: str
    title: str
    host: str
    job_url: str | None = None
    doc_id: str | None = None
    sibling: str | None = None


def grant_index(snap: protocol.Snapshot) -> dict[str, Entry]:
    out: dict[str, Entry] = {}
    for a in snap.awaiting:
        out[a.grant] = Entry(a.grant, "send", a.company, a.title, a.host, a.job_url, a.doc_id)
    for u in snap.unconfirmed:
        out[u.grant_sent] = Entry(u.grant_sent, "sent", u.company, u.title, u.host, sibling=u.grant_not_sent)
        out[u.grant_not_sent] = Entry(u.grant_not_sent, "not_sent", u.company, u.title, u.host, sibling=u.grant_sent)
    return out


def _back(form: Form) -> str:
    value = form.get("retour")
    return value if value in BACK_PAGES else "a-faire"


# ---------------------------------------------------------------- static files
def install(app: FastAPI, portal: Portal) -> None:
    """Serve exactly the two stylesheets (no directory, no other file) under `<base>/static/`."""
    base = portal.config.base_path

    def serve(path: Path):
        def handler() -> Response:
            return FileResponse(path, media_type="text/css", headers={"Cache-Control": STATIC_CACHE})
        return handler

    app.add_api_route(base + "/static/style.css", serve(STYLE_CSS), methods=["GET"], include_in_schema=False)
    app.add_api_route(base + "/static/portal.css", serve(PORTAL_CSS), methods=["GET"], include_in_schema=False)


# ---------------------------------------------------------------- entry, login, logout
@router.get("/", include_in_schema=False)
def home(request: Request) -> Response:
    portal = portal_of(request)
    if _session(portal, request) is not None:
        return RedirectResponse(f"{portal.config.base_path}/a-faire", status_code=303)
    return HTMLResponse(ENV.get_template("neutre.html").render(base=portal.config.base_path, version=portal.config.version, layout=None))


def _login_page(portal: Portal, status: int = 200, error: str | None = None) -> Response:
    nonce = auth.new_login_nonce(portal, portal.clock())
    html = ENV.get_template("login.html").render(base=portal.config.base_path, version=portal.config.version, layout=None,
                                                 error=error, nonce=nonce)
    response = HTMLResponse(html, status_code=status)
    response.set_cookie(**auth.cookie_kwargs(portal.config, auth.LOGIN_COOKIE, nonce, auth.LOGIN_NONCE_TTL))
    return response


@router.get("/connexion", include_in_schema=False)
def login_page(request: Request) -> Response:
    portal = portal_of(request)
    if _session(portal, request) is not None:
        return RedirectResponse(f"{portal.config.base_path}/a-faire", status_code=303)
    return _login_page(portal)


@router.post("/connexion", include_in_schema=False)
async def login_submit(request: Request) -> Response:
    portal = portal_of(request)
    config = portal.config
    if not auth.same_origin_ok("POST", request.scope["headers"], config):
        return _login_page(portal, 400, auth.GENERIC_LOGIN_ERROR)
    form = await _read_form(request)

    def run() -> Response:
        now = portal.clock()
        if not auth.consume_login_nonce(portal, _single(request, auth.LOGIN_COOKIE), form.get("nonce") or None, now):
            return _login_page(portal, 400, auth.GENERIC_LOGIN_ERROR)
        outcome = auth.login(portal, form.get("identifiant"), form.get("code"),
                             auth.client_address(request.scope.get("client"), request.scope["headers"], config),
                             _single(request, auth.DEVICE_COOKIE), now, _single(request, auth.SESSION_COOKIE))
        if outcome.kind != "ok":
            return _login_page(portal, 401 if outcome.kind == "refused" else 429, auth.GENERIC_LOGIN_ERROR)
        response = RedirectResponse(f"{config.base_path}/a-faire", status_code=303)
        response.set_cookie(**auth.cookie_kwargs(config, auth.SESSION_COOKIE, outcome.session_id, SESSION_ABSOLUTE))
        response.set_cookie(**auth.cookie_kwargs(config, auth.DEVICE_COOKIE, outcome.known_device_id, auth.DEVICE_COOKIE_MAX_AGE))
        response.headers.append("set-cookie", auth.clear_cookie_header(config, auth.LOGIN_COOKIE))
        return response

    return await run_in_threadpool(run)


@router.post("/deconnexion", include_in_schema=False)
async def logout(request: Request) -> Response:
    portal = portal_of(request)
    config = portal.config
    if not auth.same_origin_ok("POST", request.scope["headers"], config):
        return _forbidden(portal)
    form = await _read_form(request)

    def run() -> Response:
        session = _session(portal, request)
        if session is not None:
            if not auth.csrf_ok(portal, session.id, form.get("csrf")):
                return _forbidden(portal)
            auth.delete_session(portal, session.id)
        response = RedirectResponse(f"{config.base_path}/connexion", status_code=303)
        for name in (auth.SESSION_COOKIE, auth.DEVICE_COOKIE, FLASH_COOKIE):
            response.headers.append("set-cookie", auth.clear_cookie_header(config, name))
        return response

    return await run_in_threadpool(run)


# ---------------------------------------------------------------- a-faire
@router.get("/a-faire", include_in_schema=False)
def a_faire(request: Request) -> Response:
    ctx = _get_ctx(request)
    if isinstance(ctx, Response):
        return ctx
    view, snap = ctx.view, ctx.view.snapshot
    if snap is None:
        return _render(ctx, "a_faire.html", "a-faire")
    open_questions = [q for q in snap.questions if q.qid not in view.answered_questions]
    answered = [q for q in snap.questions if q.qid in view.answered_questions]
    groups = []
    for kind, label in INFO_LABELS.items():
        items = [i for i in snap.info if i.kind == kind]
        if items:
            groups.append((label, items))
    technical = [item for item in snap.info if item.kind == "technical"]
    groups.extend((category, [item for item in technical if item.category == category])
                  for category in dict.fromkeys(item.category for item in technical))
    index = grant_index(snap)
    pending_rows = []
    for p in view.pending:
        e = p.event
        if e.kind == "cancel":
            continue
        if e.kind == "intent":
            if view.intent_events.get(e.grant) != e.event_id:
                continue
            entry = index.get(e.grant)
            pending_rows.append({"verb": GRANT_VERBS.get(entry.kind if entry else "send", "Envoi"), "label": p.label,
                                 "cancel": e.event_id if e.event_id in view.cancellable_intents else None,
                                 "cancelling": e.event_id in view.cancelling_intents})
        else:
            pending_rows.append({"verb": EVENT_VERBS.get(e.kind, "Action"), "label": p.label, "cancel": None})
    live_ids = set(view.intent_events.values())
    results = [result for index, result in enumerate(view.results) if index < RESULTS_SHOWN or result.event_id in live_ids]
    nothing = not (snap.questions or snap.email_pauses or snap.unconfirmed or groups)
    return _render(ctx, "a_faire.html", "a-faire", open_questions=open_questions, answered_questions=answered, info_groups=groups,
                   pending_rows=pending_rows, results=results, nothing=nothing)


def _do_questions(c: Ctx) -> Response:
    snap = c.view.snapshot
    if snap is None:
        return c.redirect("a-faire", err=[ops.MSG_NO_SNAPSHOT])
    never_employed = c.form.get("never_employed") == "1"
    answers = [(q.qid, c.form.get(f"q_{q.qid}").strip() or ("Non" if never_employed and q.group == "employer" else ""))
               for q in snap.questions if q.qid not in c.view.answered_questions]
    answers = [(qid, text) for qid, text in answers if text]
    if not answers:
        return c.redirect("a-faire", ok=[MSG_NO_ANSWER])
    out = ops.answer_questions(c.portal, c.session, answers, c.now,
                               tokens={qid: c.form.get(f"v_{qid}") for qid, _ in answers})
    ok = [_count(out.stored, "1 réponse enregistrée, en attente du Mac.", "{n} réponses enregistrées, en attente du Mac.")] if out.stored else []
    return c.redirect("a-faire", ok=ok, err=out.messages())


@router.post("/a-faire/questions", include_in_schema=False)
async def post_questions(request: Request) -> Response:
    return await _post(request, _do_questions)


# ---------------------------------------------------------------- intents
def _bulk_awaiting(ctx: Ctx) -> tuple[list[str], int, set[str]]:
    docs = ops.available_document_ids(ctx.portal, ctx.session.profile_id)
    snap = ctx.view.snapshot
    if snap is None:
        return [], 0, docs
    docs.intersection_update(doc.doc_id for doc in snap.documents)
    free = [app for app in snap.awaiting if app.grant not in ctx.view.intent_events]
    grants = [app.grant for app in free if app.doc_id in docs]
    return grants, len(free) - len(grants), docs


def _do_confirm(c: Ctx) -> Response:
    snap = c.view.snapshot
    back = _back(c.form)
    if snap is None:
        return c.redirect(back, err=[ops.MSG_CHANGED])
    index = grant_index(snap)
    omitted, docs = 0, None
    if c.form.get("tout"):
        grants, omitted, docs = _bulk_awaiting(c)
        if not grants and omitted:
            return _render(c, "confirmer.html", back, entries=[], heading="Aucun CV disponible à approuver",
                           retour=back, omitted=omitted, docs=docs)
        if not grants:
            return c.redirect(back, err=[MSG_ALREADY if snap.awaiting else MSG_NOTHING_AWAITING])
    elif c.form.get("un"):
        grants = [c.form.get("un")]
    else:
        grants = c.form.getall("g")
    grants = list(dict.fromkeys(grants))[:MAX_GRANTS]
    if not grants:
        return c.redirect(back, err=[MSG_PICK_ONE])
    if any(not _HEX32.match(g) or g not in index for g in grants):
        return c.redirect(back, err=[ops.MSG_CHANGED])
    live = c.view.intent_events
    entries = [index[g] for g in grants if g not in live and index[g].sibling not in live]
    if not entries:
        return c.redirect(back, err=[MSG_ALREADY])
    kinds = {e.kind for e in entries}
    if kinds == {"send"}:
        heading = "Approuver cette candidature ?" if len(entries) == 1 else f"Approuver ces {len(entries)} candidatures ?"
    elif len(entries) == 1:
        heading = {"sent": "Marquer comme envoyée ?", "not_sent": "Marquer comme non envoyée ?"}[entries[0].kind]
    else:
        heading = f"Confirmer ces {len(entries)} demandes ?"
    return _render(c, "confirmer.html", back, entries=entries, heading=heading, retour=back, omitted=omitted, docs=docs)


@router.post("/confirmer", include_in_schema=False)
async def post_confirm(request: Request) -> Response:
    return await _post(request, _do_confirm)


def _do_intents(c: Ctx) -> Response:
    back = _back(c.form)
    grants = list(dict.fromkeys(g for g in c.form.getall("g") if _HEX32.match(g)))[:MAX_GRANTS]
    if not grants:
        return c.redirect(back, err=[ops.MSG_CHANGED])
    out = ops.request_intents(c.portal, c.session, grants, c.now, verified=c.form.get("verified") == "1")
    ok = []
    if out.stored:
        ok = [_count(out.stored, "Demande envoyée au Mac : elle sera traitée à sa prochaine synchronisation.",
                     "{n} demandes envoyées au Mac : elles seront traitées à sa prochaine synchronisation.")]
    return c.redirect(back, ok=ok, err=out.messages())


@router.post("/intentions", include_in_schema=False)
async def post_intents(request: Request) -> Response:
    return await _post(request, _do_intents)


def _do_cancel(c: Ctx) -> Response:
    event_id = c.form.get("event")
    if not _HEX32.match(event_id):
        return c.redirect("a-faire", err=["Trop tard ou déjà annulée : rechargez la page."])
    out = ops.cancel_intent(c.portal, c.session, event_id, c.now)
    return c.redirect("a-faire", ok=["Annulation demandée."] if out.stored else [], err=out.messages())


@router.post("/annuler", include_in_schema=False)
async def post_cancel(request: Request) -> Response:
    return await _post(request, _do_cancel)


# ---------------------------------------------------------------- reponses
def _answer_names(schema: protocol.AnswerSchema) -> dict[str, tuple[str, str, str]]:
    """path -> (field name, token name, clear-checkbox name). Derived from the schema only; posted names are never parsed."""
    out: dict[str, tuple[str, str, str]] = {}
    used: set[str] = set()
    for spec in schema.fields:
        stem = spec.path.replace(".", "__")
        while stem in used:
            stem += "_"
        used.add(stem)
        out[spec.path] = (f"ans_{stem}", f"tok_{stem}", f"clr_{stem}")
    return out


@router.get("/reponses", include_in_schema=False)
def reponses(request: Request) -> Response:
    ctx = _get_ctx(request)
    if isinstance(ctx, Response):
        return ctx
    view, snap = ctx.view, ctx.view.snapshot
    if snap is None:
        return _render(ctx, "reponses.html", "reponses")
    names = _answer_names(snap.answer_schema)
    by_section: dict[str, list[dict]] = {s: [] for s in snap.answer_schema.sections}
    for spec in snap.answer_schema.fields:
        fv = view.fields.get(spec.path)
        if fv is None:
            continue
        name, tok, clr = names[spec.path]
        by_section.setdefault(spec.section, []).append({"spec": spec, "fv": fv, "name": name, "tok": tok, "clr": clr})
    sections = [(title, rows) for title, rows in by_section.items() if rows]
    customs = [c for c in view.custom if not c.deleted]
    return _render(ctx, "reponses.html", "reponses", sections=sections, customs=customs)


def _do_answers(c: Ctx) -> Response:
    view, snap = c.view, c.view.snapshot
    if snap is None:
        return c.redirect("reponses", err=[ops.MSG_NO_SNAPSHOT])
    names = _answer_names(snap.answer_schema)
    edits = []
    for spec in snap.answer_schema.fields:
        name, tok, clr = names[spec.path]
        if spec.path not in view.fields or not c.form.has(name):
            continue
        value = c.form.get(name)
        clear = spec.write_only and c.form.has(clr) and not value.strip()
        edits.append(ops.AnswerEdit(spec.path, value, c.form.get(tok), clear))
    stored, err = 0, []
    if edits:
        out = ops.submit_answers(c.portal, c.session, edits, c.now)
        stored += out.stored
        err += out.messages()
    for cv in view.custom:
        if cv.deleted or not c.form.has(f"cus_{cv.custom_id}"):
            continue
        value = c.form.get(f"cus_{cv.custom_id}")
        if cv.masked and not value.strip():
            continue
        out = ops.edit_custom(c.portal, c.session, cv.custom_id, value, c.form.get(f"ctok_{cv.custom_id}"), c.now)
        stored += out.stored
        err += out.messages()
    err = list(dict.fromkeys(err))
    if stored:
        ok = [_count(stored, "1 réponse enregistrée, en attente du Mac.", "{n} réponses enregistrées, en attente du Mac.")]
    else:
        ok = [] if err else [MSG_NO_CHANGE]
    return c.redirect("reponses", ok=ok, err=err)


@router.post("/reponses", include_in_schema=False)
async def post_answers(request: Request) -> Response:
    return await _post(request, _do_answers)


def _do_remove_confirm(c: Ctx) -> Response:
    custom_id = c.form.get("retirer")
    cv = next((x for x in c.view.custom if x.custom_id == custom_id and not x.deleted), None) if _HEX16.match(custom_id) else None
    if cv is None:
        return c.redirect("reponses", err=[ops.MSG_STALE])
    return _render(c, "retirer.html", "reponses", question=cv.question, custom_id=cv.custom_id,
                   token=c.form.get(f"ctok_{cv.custom_id}") or cv.token)


@router.post("/reponses/retirer", include_in_schema=False)
async def post_remove_confirm(request: Request) -> Response:
    return await _post(request, _do_remove_confirm)


def _do_remove(c: Ctx) -> Response:
    custom_id = c.form.get("custom_id")
    if not _HEX16.match(custom_id):
        return c.redirect("reponses", err=[ops.MSG_STALE])
    out = ops.delete_custom(c.portal, c.session, custom_id, c.form.get("token"), c.now)
    return c.redirect("reponses", ok=["Réponse retirée, en attente du Mac."] if out.stored else [], err=out.messages())


@router.post("/reponses/retirer/confirmer", include_in_schema=False)
async def post_remove(request: Request) -> Response:
    return await _post(request, _do_remove)


# ---------------------------------------------------------------- accord, candidatures, documents
@router.get("/accord", include_in_schema=False)
def accord(request: Request) -> Response:
    ctx = _get_ctx(request)
    if isinstance(ctx, Response):
        return ctx
    snap, live = ctx.view.snapshot, ctx.view.intent_events
    free = sum(1 for a in snap.awaiting if a.grant not in live) if snap else 0
    bulk_grants, _, docs = _bulk_awaiting(ctx)
    return _render(ctx, "accord.html", "accord", free_count=free, bulk_count=len(bulk_grants), docs=docs)


def _epoch(item) -> float:
    return item.submitted_at.timestamp() if item.submitted_at else float("-inf")


@router.get("/candidatures", include_in_schema=False)
def candidatures(request: Request) -> Response:
    ctx = _get_ctx(request)
    if isinstance(ctx, Response):
        return ctx
    snap = ctx.view.snapshot
    items = sorted(snap.sent, key=_epoch, reverse=True) if snap else []
    return _render(ctx, "candidatures.html", "candidatures", items=items)


@router.get("/candidatures/{ref}", include_in_schema=False)
def candidature(request: Request, ref: str) -> Response:
    ctx = _get_ctx(request)
    if isinstance(ctx, Response):
        return ctx
    snap = ctx.view.snapshot
    item = next((s for s in snap.sent if s.ref == ref), None) if snap and _HEX32.match(ref) else None
    if item is None:
        return _not_found(ctx.portal)
    return _render(ctx, "candidature.html", "candidatures", s=item)


@router.get("/documents/{doc_id}", include_in_schema=False)
def document(request: Request, doc_id: str) -> Response:
    ctx = _get_ctx(request)
    if isinstance(ctx, Response):
        return ctx
    data = ops.open_document(ctx.portal, ctx.session, doc_id) if _HEX32.match(doc_id) else None
    if data is None:
        return _not_found(ctx.portal)
    return Response(data, media_type="application/pdf", headers={
        "Content-Disposition": f'attachment; filename="cv-{doc_id[:8]}.pdf"', "Content-Security-Policy": "sandbox",
        "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})
