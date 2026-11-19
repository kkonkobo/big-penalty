"""Web UI of milestone 2b: the sending queue ("Envois"), run details and the buttons that start dry runs / submissions.

The router is included by web/app.py (`install(app, templates)` does it and registers the template globals). Helpers of
web/app.py are imported lazily (see `_web()`), because app.py imports this module.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse

from jobhunt import db
from jobhunt.apply import accounts, facts, missing_answers
from jobhunt.apply import service as apply_service
from jobhunt.apply import store as apply_store
from jobhunt.apply.errors import ApplyError
from jobhunt.apply.service import QueueItem
from jobhunt.config import get_settings
from jobhunt.cv import service as cvs
from jobhunt.cv import store as cvstore

log = logging.getLogger("jobhunt.web.apply")
router = APIRouter()

BUCKET_LABELS = {"to_validate": "À valider", "paused": "En pause", "sending": "En cours", "sent": "Envoyée", "failed": "Échec",
                 "blocked": "Bloquée"}
BUCKET_CLASS = {"to_validate": "src", "paused": "warn", "sending": "src", "sent": "ok", "failed": "bad", "blocked": "bad"}
RUN_STATUS_LABELS = {"running": "En cours", "ready": "Prêt (rien envoyé)", "submitted": "Envoyée", "needs_input": "Question en attente",
                     "needs_email": "E-mail à confirmer", "needs_human": "CAPTCHA à résoudre", "blocked": "Bloquée", "failed": "Échec"}
RUN_STATUS_CLASS = {"running": "src", "ready": "ok", "submitted": "ok", "needs_input": "warn", "needs_email": "warn",
                    "needs_human": "warn", "blocked": "bad", "failed": "bad"}
MODE_LABELS = {"dry_run": "Essai à blanc", "submit": "Envoi"}
TRIGGER_LABELS = {"auto": "Automatique", "user": "Manuel"}
PAUSE_STATUSES = ("needs_input", "needs_email", "needs_human")
PAGE_KIND_LABELS = {"job_page": "Page de l'offre", "form": "Formulaire de candidature", "account_login": "Connexion à un compte",
                    "account_signup": "Création de compte", "email_verification": "Vérification par e-mail", "captcha": "CAPTCHA",
                    "confirmation": "Confirmation", "closed": "Offre fermée", "error": "Erreur", "other": "Autre page"}

# Background state (in memory, like GENERATING in web/app.py).
SENDING: dict[str, Any] = {"running": False, "total": 0, "done": 0, "error": None, "ids": []}
APPLYING: dict[int, dict[str, Any]] = {}  # app_id -> {"running", "error", "task"}: single runs (dry run / resume)


# ------------------------------------------------------------ helpers
def _web():
    """Lazy import of web/app.py (circular otherwise); also makes sure the template globals exist."""
    from jobhunt.web import app as webapp

    _register_globals(webapp.templates)
    return webapp


def _register_globals(templates) -> None:
    templates.env.globals.update(APPLY_BUCKET_LABELS=BUCKET_LABELS, APPLY_BUCKET_CLASS=BUCKET_CLASS,
                                 APPLY_RUN_LABELS=RUN_STATUS_LABELS, APPLY_RUN_CLASS=RUN_STATUS_CLASS, APPLY_MODE_LABELS=MODE_LABELS)


def install(app, templates) -> None:
    """Wiring helper for web/app.py: `apply_routes.install(app, templates)`."""
    _register_globals(templates)
    if not any(getattr(r, "path", None) == "/apply" for r in app.routes):
        app.include_router(router)


def safe_id(v: Any) -> int | None:
    """A database id from user input: ASCII digits only, at most 12 of them (no Unicode digits, no sqlite overflow), > 0."""
    if isinstance(v, str) and v.isascii() and v.isdigit() and len(v) <= 12 and int(v) > 0:
        return int(v)
    return None


def _id_or_404(v: str) -> int:
    i = safe_id(v)
    if i is None:
        raise HTTPException(404, "Introuvable")
    return i


def _norm(text: str) -> str:
    return " ".join((text or "").split())


def _msg(e: Exception) -> str:
    """User-facing text of an exception: our own French errors only."""
    if isinstance(e, (ApplyError, cvs.CVError)):
        return str(e)
    return _web()._safe_message(e)


SENT_ROWS_MAX = 200
PAUSE_LABELS = {"input": "question", "email": "e-mail", "human": "CAPTCHA", "host": "site non approuvé"}


def _breakdown(groups: dict[str, list[QueueItem]]) -> list[str]:
    """What blocks the queue, by cause, for the header: paused by reason, to check (unconfirmed send), technical failures,
    CVs to fix, other blocked applications."""
    out: list[str] = []
    paused: dict[str, int] = {}
    for it in groups["paused"]:
        reason = PAUSE_LABELS.get(it.pause_reason, it.pause_reason)
        if it.pause_reason == "human" and it.last_run is not None:
            reason = apply_service.pause_reason_label(it.last_run)
        paused[reason] = paused.get(reason, 0) + 1
    if paused:
        out.append("en pause : " + ", ".join(f"{n} {k}" for k, n in sorted(paused.items(), key=lambda kv: (-kv[1], kv[0]))))
    check = sum(1 for it in groups["failed"] if it.app.status == "failed")
    if check:
        out.append(f"{check} envoi{'s' if check > 1 else ''} à vérifier")
    if len(groups["failed"]) - check:
        out.append(f"{len(groups['failed']) - check} échec{'s' if len(groups['failed']) - check > 1 else ''} technique{'s' if len(groups['failed']) - check > 1 else ''}")
    fix = sum(1 for it in groups["blocked"] if any("erreur(s) bloquante(s)" in p for p in it.problems))
    if fix:
        out.append(f"{fix} CV à corriger")
    if len(groups["blocked"]) - fix:
        out.append(f"{len(groups['blocked']) - fix} bloquée{'s' if len(groups['blocked']) - fix > 1 else ''}")
    return out


def _sent_host(it: QueueItem) -> str:
    """Host the application was sent to: from the recorded destination (`sent_origin_url`), else the job's link."""
    recorded = (it.app.sent_origin_url or "").strip()
    if recorded:
        host = recorded.split("://", 1)[-1].split("/", 1)[0].split("?", 1)[0].split("@")[-1].split(":")[0]
        if host:
            return host.lower()
    return it.origin_host


def queue_item(app_id: int) -> QueueItem | None:
    """The queue entry of one application (None when unknown, withdrawn or when the queue cannot be built)."""
    a = cvstore.get_application(app_id)
    if a is None:
        return None
    try:
        return next((i for i in apply_service.queue(a.profile_id) if i.app.id == app_id), None)
    except Exception:  # noqa: BLE001 - the application page must still render
        log.exception("apply queue failed")
        return None


def _any_running() -> bool:
    return bool(SENDING["running"] or any(s["running"] for s in APPLYING.values()))


def _status() -> dict[str, Any]:
    return {"running": _any_running(), "batch": bool(SENDING["running"]), "total": SENDING["total"], "done": SENDING["done"],
            "single": sum(1 for s in APPLYING.values() if s["running"])}


def _take_errors() -> list[str]:
    """Errors of finished background work, shown once."""
    out: list[str] = []
    if not SENDING["running"] and SENDING["error"]:
        out.append(SENDING["error"])
        SENDING["error"] = None
    for app_id in [k for k, s in APPLYING.items() if not s["running"]]:
        err = APPLYING.pop(app_id)["error"]
        if err:
            out.append(err)
    return out


async def _batch(ids: list[int], revs: dict[int, int], origins: dict[int, str]) -> None:
    def progress(_i: int, _run) -> None:
        SENDING["done"] += 1

    try:
        await apply_service.send_selected(ids, on_progress=progress, expected_revs=revs, expected_origins=origins)
    except Exception as e:  # noqa: BLE001
        SENDING["error"] = _msg(e)
    finally:
        SENDING["running"] = False


async def _single(app_id: int, mode: str, headless: bool | None, bound: dict[str, Any] | None = None) -> None:
    """One background run. `bound` (resume / answers / trust): what the user was shown, re-checked HERE, when the task really
    starts: the paused run (`run_id`, `mode`) must still be the application's last one, and `rev` (and `origin`, a digest of the
    job link) must still be current (service.run refuses otherwise with a blocked run)."""
    st = APPLYING[app_id]
    try:
        kw: dict[str, Any] = {}
        if bound is not None:
            last = apply_store.last_run(app_id)
            if last is None or last.id != bound["run_id"] or last.mode != mode or not (
                    last.status in PAUSE_STATUSES or apply_service.untrusted_host_of(last)):
                raise ApplyError("Page périmée : l'état de cette candidature a changé, recharger la page")
            kw = {"expected_rev": bound["rev"], "expected_origin": bound.get("origin")}
        await apply_service.run(app_id, mode=mode, trigger="user", headless=headless, **kw)  # type: ignore[arg-type]
    except Exception as e:  # noqa: BLE001
        st["error"] = _msg(e)
    finally:
        st["running"] = False


def _launch(app_id: int, mode: str, headless: bool | None = None, bound: dict[str, Any] | None = None) -> str | None:
    """Starts one background run; returns an error message when it cannot start."""
    if SENDING["running"] and app_id in SENDING["ids"]:
        return "Cette candidature fait partie d'un envoi en cours"
    st = APPLYING.get(app_id)
    if st and st["running"]:
        return "Un essai est déjà en cours pour cette candidature"
    APPLYING[app_id] = st = {"running": True, "error": None}
    st["task"] = asyncio.create_task(_single(app_id, mode, headless, bound))
    return None


def _resume(app_id: int, flash_ok: str, last, origin: str | None = None):
    """Resume the paused run `last` (validated by the caller against the page the user saw): its id, mode and revision are
    handed to the background task, never looked up again at launch."""
    web = _web()
    headless = False if last.status == "needs_human" else None
    err = _launch(app_id, last.mode, headless, {"run_id": last.id, "rev": last.rev, "origin": origin})
    return web._flash("/apply", error=err) if err else web._flash("/apply", notice=flash_ok)


def _launch_chain(rows: list[tuple[Any, str]]) -> tuple[int, list[str]]:
    """Resume several paused runs ONE AFTER THE OTHER in a single background task (sends share one lock). `rows` = (paused run,
    origin digest the user saw); each is bound to its run id / mode / revision / digest like `_resume` and re-checked when its
    turn comes. Returns (launched, reasons for the applications that were skipped)."""
    jobs, skipped = [], []
    for last, origin in rows:
        aid = last.application_id
        if SENDING["running"] and aid in SENDING["ids"]:
            skipped.append("Cette candidature fait partie d'un envoi en cours")
        elif (APPLYING.get(aid) or {}).get("running"):
            skipped.append("Un essai est déjà en cours pour cette candidature")
        else:
            APPLYING[aid] = {"running": True, "error": None}
            jobs.append((aid, last, origin))

    async def chain() -> None:
        for aid, last, origin in jobs:
            await _single(aid, last.mode, False if last.status == "needs_human" else None,
                          {"run_id": last.id, "rev": last.rev, "origin": origin})

    if jobs:
        task = asyncio.create_task(chain())
        for aid, _, _ in jobs:
            APPLYING[aid]["task"] = task
    return len(jobs), skipped


def _origin_field(form, name: str = "origin") -> str:
    """The destination digest (sha256 hex of the job link) a form carries: REQUIRED, "" when absent or malformed."""
    v = str(form.get(name) or "").strip().lower()
    return v if re.fullmatch(r"[0-9a-f]{64}", v) else ""


# ------------------------------------------------------------ queue
@router.get("/apply", response_class=HTMLResponse)
def apply_page(request: Request, profile: str | None = None, f: str | None = None):
    web = _web()
    pid = safe_id(profile)
    notice, error = web._take_flash(f)
    errors = _take_errors()
    if error:
        errors.insert(0, error)
    try:
        items = apply_service.queue(pid)
    except Exception as e:  # noqa: BLE001
        items = []
        errors.append(_msg(e))
    groups: dict[str, list[QueueItem]] = {b: [] for b in BUCKET_LABELS}
    for it in items:
        groups[it.bucket].append(it)
    breakdown = _breakdown(groups)
    # "Envoyées": newest first, at most SENT_ROWS_MAX rows (the page stays light after a big pass); counts cover them all.
    from jobhunt.freshness import start_of_today_paris  # noqa: PLC0415

    epoch = datetime.min.replace(tzinfo=UTC)
    all_sent = sorted(groups["sent"], key=lambda i: i.app.submitted_at or epoch, reverse=True)
    day = start_of_today_paris()
    sent_total, sent_today = len(all_sent), sum(1 for i in all_sent if i.app.submitted_at and i.app.submitted_at >= day)
    groups["sent"] = all_sent[:SENT_ROWS_MAX]
    sent_hosts = {i.app.id: _sent_host(i) for i in groups["sent"]}
    inferred_counts: dict[int, int] = {}
    for it in groups["sent"]:
        try:
            inferred_counts[it.app.id] = len(apply_service.inferred_answers(it.app.id))
        except Exception:  # noqa: BLE001 - a count is optional
            log.exception("inferred answers failed")
    return web.render(request, "apply_queue.html", groups=groups, count=len(items), profile_id=pid, notice=notice,
                      inferred_counts=inferred_counts, errors=errors, settings=get_settings(), status=_status(), profiles=db.list_profiles(),
                      sent_total=sent_total, sent_today=sent_today, breakdown=breakdown, sent_hosts=sent_hosts, sent_rows_max=SENT_ROWS_MAX)


@router.get("/apply/status", response_class=HTMLResponse)
def apply_status(request: Request):
    web = _web()
    st = _status()
    if not st["running"] and request.headers.get("HX-Request"):
        return Response(status_code=200, headers={"HX-Refresh": "true"})
    return HTMLResponse(str(web.templates.get_template("_apply_card.html").module.status_box(st)))


@router.post("/apply/send")
async def apply_send(request: Request):
    web = _web()
    form = await request.form()
    ids: list[int] = []
    for v in form.getlist("app_id"):
        i = safe_id(v)
        if i is not None and i not in ids:
            ids.append(i)
    if not ids:
        return web._flash("/apply", error="Aucune candidature sélectionnée")
    # The revision of the CV the user was shown, per application: a CV edited since then is never approved nor sent.
    revs = {i: r for i in ids if (r := safe_id(str(form.get(f"rev_{i}") or ""))) is not None}
    if len(revs) != len(ids):
        return web._flash("/apply", error="Page périmée : recharger la page avant d'envoyer")
    if any(cvstore.get_application(i) is None for i in ids):
        return web._flash("/apply", error="Candidature introuvable")
    # ... and the destination (sha256 of the job link) it was shown with.
    origins = {i: o for i in ids if re.fullmatch(r"[0-9a-f]{64}", o := str(form.get(f"origin_{i}") or "").strip().lower())}
    if len(origins) != len(ids):
        return web._flash("/apply", error="Page périmée : recharger la page avant d'envoyer")
    if SENDING["running"]:
        return web._flash("/apply", error="Un envoi est déjà en cours : attendre sa fin")
    if any(APPLYING.get(i, {}).get("running") for i in ids):
        return web._flash("/apply", error="Un essai est déjà en cours pour l'une de ces candidatures")
    SENDING.update(running=True, total=len(ids), done=0, error=None, ids=ids)
    SENDING["task"] = asyncio.create_task(_batch(ids, revs, origins))
    n = len(ids)
    return web._flash("/apply", notice=f"Envoi lancé pour {n} candidature{'s' if n > 1 else ''}")


@router.post("/applications/{app_id}/apply/dry-run")
async def apply_dry_run(app_id: str):
    web = _web()
    aid = _id_or_404(app_id)
    web._application_or_404(aid)
    err = _launch(aid, "dry_run")
    if err:
        return web._flash("/apply", error=err)
    return web._flash("/apply", notice="Essai à blanc lancé : rien ne sera envoyé")


@router.post("/applications/{app_id}/apply/resume")
async def apply_resume(request: Request, app_id: str):
    web = _web()
    aid = _id_or_404(app_id)
    web._application_or_404(aid)
    form = await request.form()
    app = cvstore.get_application(aid)
    last = apply_store.last_run(aid)
    origin = _origin_field(form)
    if (last is None or app is None or safe_id(str(form.get("run_id") or "")) != last.id or str(form.get("mode") or "") != last.mode
            or last.status not in PAUSE_STATUSES or safe_id(str(form.get("rev") or "")) != last.rev or last.rev != app.current_rev
            or origin == ""):
        return web._flash("/apply", error="Page périmée : l'état de cette candidature a changé, recharger la page")
    return _resume(aid, "Reprise lancée", last, origin)


@router.post("/applications/{app_id}/apply/answers")
async def apply_answers(request: Request, app_id: str):
    web = _web()
    aid = _id_or_404(app_id)
    app = web._application_or_404(aid)
    form = await request.form()
    last = apply_store.last_run(aid)
    stale = "Page périmée : les questions ont changé, recharger la page"
    origin = _origin_field(form)
    if (origin == "" or last is None or safe_id(str(form.get("run_id") or "")) != last.id or last.status != "needs_input"
            or last.rev != app.current_rev or last.result is None):
        return web._flash("/apply", error=stale)
    asked = {_norm(q.label) for q in last.result.questions}
    pairs: list[tuple[str, str]] = []
    for key in form.keys():
        m = re.fullmatch(r"q_([0-9]{1,4})", key)
        if not m:
            continue
        label, answer = str(form.get(key) or "").strip(), str(form.get(f"a_{m.group(1)}") or "").strip()
        if label and _norm(label) not in asked:
            return web._flash("/apply", error=stale)
        if label and answer:
            pairs.append((label, answer))
    if not pairs:
        return web._flash("/apply", error="Aucune réponse saisie")
    try:
        apply_service.answer_questions(aid, pairs)
    except Exception as e:  # noqa: BLE001
        return web._flash("/apply", error=_msg(e))
    return _resume(aid, "Réponses enregistrées, reprise lancée", last, origin)


@router.post("/applications/{app_id}/apply/resolve")
async def apply_resolve(request: Request, app_id: str):
    """The user checked the company's site after an unconfirmed attempt: `sent` = "1" (it was sent) / "0" (it was not).
    `run_id` binds the decision to the attempt the user was shown (it must still be the OUTSTANDING attempt)."""
    web = _web()
    aid = _id_or_404(app_id)
    web._application_or_404(aid)
    form = await request.form()
    sent = str(form.get("sent") or "")
    if sent not in ("0", "1"):
        return web._flash("/apply", error="Réponse invalide")
    run_id = safe_id(str(form.get("run_id") or ""))
    if run_id is None:
        return web._flash("/apply", error="Page périmée : recharger la page")
    try:
        apply_service.resolve_unconfirmed(aid, sent=sent == "1", run_id=run_id)
    except (ApplyError, cvs.CVError) as e:
        return web._flash("/apply", error=str(e))
    return web._flash("/apply", notice="Candidature marquée comme envoyée" if sent == "1"
                      else "Candidature remise en brouillon : elle pourra être approuvée et envoyée à nouveau")


@router.post("/applications/{app_id}/apply/trust")
async def apply_trust(request: Request, app_id: str):
    """"Faire confiance à <host> et reprendre": the host must be the one the application's LAST run refused to use."""
    web = _web()
    aid = _id_or_404(app_id)
    app = web._application_or_404(aid)
    form = await request.form()
    last = apply_store.last_run(aid)
    asked = apply_service.untrusted_host_of(last)
    host = accounts.hostname(str(form.get("host") or ""))
    origin = _origin_field(form)
    if (origin == "" or last is None or safe_id(str(form.get("run_id") or "")) != last.id or last.rev != app.current_rev or not asked
            or host != accounts.hostname(asked)):
        return web._flash("/apply", error="Page périmée : l'état de cette candidature a changé, recharger la page")
    try:
        trusted = facts.add_trusted_host(app.profile_id, host)
    except ApplyError as e:
        return web._flash("/apply", error=str(e))
    return _resume(aid, f"{trusted} ajouté aux sites de confiance, reprise lancée", last, origin)


# ------------------------------------------------------------ run details
def _run_or_404(run_id: int):
    run = apply_store.get_run(run_id)
    if run is None:
        raise HTTPException(404, "Exécution introuvable")
    return run


@router.get("/apply/runs/{run_id}", response_class=HTMLResponse)
def apply_run_page(request: Request, run_id: str):
    web = _web()
    run = _run_or_404(_id_or_404(run_id))
    a = cvstore.get_application(run.application_id)
    last = apply_store.last_run(run.application_id)
    can_send = bool(a and run.mode == "dry_run" and run.status == "ready" and a.status in ("draft", "approved")
                    and last is not None and last.id == run.id)
    return web.render(request, "apply_run.html", run=run, a=a, steps=run.result.steps if run.result else [],
                      kinds=PAGE_KIND_LABELS, triggers=TRIGGER_LABELS, can_send=can_send,
                      origin_sha=apply_service.origin_digest(db.get_job(a.job_id).origin_url if a and db.get_job(a.job_id) else None))


@router.get("/apply/runs/{run_id}/shots/{index}")
def apply_shot(run_id: str, index: str):
    run = _run_or_404(_id_or_404(run_id))
    index = int(index) if index.isascii() and index.isdigit() and len(index) <= 6 else -1
    steps = run.result.steps if run.result else []
    if index < 0 or index >= len(steps) or not steps[index].screenshot or not run.out_dir:
        raise HTTPException(404, "Capture introuvable")
    try:
        root = get_settings().applications_dir.resolve()
        out = Path(run.out_dir).resolve()
        shot = Path(steps[index].screenshot).resolve()  # symlinks resolved: an escaping link ends up outside out_dir
    except (OSError, RuntimeError, ValueError):
        raise HTTPException(404, "Capture introuvable") from None
    if not (out.is_relative_to(root) and shot.is_relative_to(out) and shot.suffix.lower() == ".png" and shot.is_file()):
        raise HTTPException(404, "Capture introuvable")
    return FileResponse(shot, media_type="image/png")


# ------------------------------------------------------------ standing answers ("Réponses aux formulaires")
def _field_name(path: str) -> str:
    return "ans_" + path.replace(".", "__")


def _displayed(f, stored: str | None) -> str:
    """What the form shows for a stored value (a date widget shows ISO, or nothing for a legacy free text)."""
    if f.widget == "date":
        return facts.from_french_date(stored) or ""
    return stored or ""


def _answers_context(pid: int, answers: dict | None = None) -> dict[str, Any]:
    if answers is None:
        answers = facts.load_answers(pid)
    sections = []
    for name in facts.ANSWER_SECTIONS:
        rows = []
        for f in (x for x in facts.ANSWER_FIELDS if x.section == name):
            stored = facts.get_answer(answers, f.path)
            shown = _displayed(f, stored)
            rows.append({"f": f, "name": _field_name(f.path), "value": shown, "stored": stored or "",
                         "legacy": bool(stored and f.widget == "date" and not shown),
                         "extra_option": bool(f.widget == "select" and stored and stored not in f.options)})
        sections.append({"title": name, "rows": rows})
    return {"sections": sections, "country_suggestion": facts.COUNTRY_SUGGESTION, "custom": facts.custom_answers(pid),
            "hosts": facts.trusted_hosts(pid)}


def _pending_answer_prompts(items: list[QueueItem], grouped_keys: set[str], answered_labels: set[str]) -> list[dict[str, Any]]:
    prompts = {}
    affected: dict[str, set[int]] = {}
    for item in items:
        for question in item.questions:
            key = facts.norm_question(question.label)
            if not key or key in answered_labels or missing_answers.fact_key(question) in grouped_keys:
                continue
            affected.setdefault(key, set()).add(item.app.id)
            prompts.setdefault(key, {"app": item.app, "q": question, "i": len(prompts)})
    for key, prompt in prompts.items():
        prompt["count"] = len(affected[key])
    return sorted(prompts.values(), key=lambda prompt: -prompt["count"])


@router.get("/profiles/{pid}/answers", response_class=HTMLResponse)
def answers_page(request: Request, pid: str, f: str | None = None):
    web = _web()
    profile_id = _id_or_404(pid)
    profile = web._profile_or_404(profile_id)
    notice, error = web._take_flash(f)
    errors = [error] if error else []
    recorded_keys = None
    try:
        answers = facts.load_answers(profile_id)
        ctx = _answers_context(profile_id, answers)
        recorded_keys = {key for key in missing_answers.ALIASES if facts.get_answer(answers, key)}
    except ApplyError as e:  # unreadable answers.yaml
        ctx = {"sections": [], "country_suggestion": "", "custom": [], "hosts": []}
        errors.append(str(e))
    groups, pending, resumable = [], [], []
    try:
        resumable = list(missing_answers.input_pauses(profile_id, apply_service.queue(profile_id)))
        if recorded_keys is not None:
            answered = {facts.norm_question(entry["question"]) for entry in ctx["custom"] if entry["answer"].strip()}
            groups = missing_answers.project(profile_id, resumable, recorded_fact_keys=recorded_keys, answered_labels=answered)
            pending = _pending_answer_prompts(resumable, {group.fact_key for group in groups},
                                              {facts.norm_question(entry["question"]) for entry in ctx["custom"]})
    except Exception as e:  # noqa: BLE001 - the form itself must still render
        log.exception("pending questions failed")
        groups, pending, resumable = [], [], []
        errors.append(_msg(e))
    return web.render(request, "apply_answers.html", profile=profile, pending=pending, resumable=resumable, notice=notice,
                      errors=errors, mode_labels=MODE_LABELS, missing_groups=groups, answer_fields=facts.ANSWER_BY_PATH, **ctx)


STALE_ANSWER = "cette réponse a changé entre-temps"


@router.post("/profiles/{pid}/answers")
async def answers_save(request: Request, pid: str):
    """Saves what the user CHANGED on the page (posted value != `prev_<path>` / `caprev_<id>`, the value the page displayed). A
    value that changed in the meantime (stored value != what the page displayed) is never overwritten: it is reported."""
    web = _web()
    profile_id = _id_or_404(pid)
    web._profile_or_404(profile_id)
    back = f"/profiles/{profile_id}/answers"
    form = await request.form()
    stale: list[str] = []
    try:
        current = facts.load_answers(profile_id)
        changes: dict[str, str] = {}
        for f in facts.ANSWER_FIELDS:
            name = _field_name(f.path)
            if name not in form:
                continue
            posted, now_ = str(form.get(name) or "").strip(), _displayed(f, facts.get_answer(current, f.path))
            shown = form.get("prev_" + name[4:])  # the value the page displayed
            if shown is None:
                if posted != now_:  # no record of what was displayed: never a blind overwrite
                    stale.append(f"« {f.label} » : {STALE_ANSWER}")
            elif posted != str(shown).strip():  # the user changed it
                if str(shown).strip() != now_:
                    stale.append(f"« {f.label} » : {STALE_ANSWER}")
                else:
                    changes[f.path] = str(form.get(name) or "")
        asked = {facts.norm_question(q.label): q.label for _, _, q in apply_service.pending_questions(profile_id)}
        have = {facts.norm_question(c["question"]) for c in facts.custom_answers(profile_id)}
        pairs: list[tuple[str, str]] = []
        for key in form.keys():
            m = re.fullmatch(r"pqa_([0-9]{1,4})", key)
            answer = str(form.get(key) or "").strip()
            label = str(form.get(f"pql_{m.group(1)}") or "") if m else ""
            if m and answer:
                norm = facts.norm_question(label)
                if norm not in asked:
                    if norm in have:  # answered in another tab since: never overwritten
                        stale.append(f"« {label} » : {STALE_ANSWER}")
                        continue
                    return web._flash(back, error="Page périmée : les questions ont changé, recharger la page")
                pairs.append((asked[norm], answer))
        edits: list[tuple[str, str, str, str]] = []
        for key in form.keys():
            m = re.fullmatch(r"ca_([0-9a-f]{16})", key)
            if not m:
                continue
            new = " ".join(str(form.get(key) or "").split())
            prev = form.get("caprev_" + m.group(1))
            label = next((c["question"] for c in facts.custom_answers(profile_id) if c["id"] == m.group(1)), "Réponse enregistrée")
            if prev is None:
                if new:
                    stale.append(f"« {label} » : {STALE_ANSWER}")
            elif new and new != str(prev):
                edits.append((m.group(1), new, str(prev), label))
        if changes:
            facts.save_answers(profile_id, changes)
        if pairs:
            facts.save_custom_answers(profile_id, pairs)
        edited = 0
        for cid, new, prev, label in edits:
            if facts.update_custom_answer(profile_id, cid, new, prev):
                edited += 1
            else:
                stale.append(f"« {label} » : {STALE_ANSWER}")
    except (ApplyError, cvs.CVError) as e:
        return web._flash(back, error=str(e))
    saved = len(changes) + len(pairs) + edited
    if stale:
        return web._flash(back, error=("Enregistré : " + str(saved) + ". " if saved else "") + "Non écrasé : " + " ; ".join(stale))
    msg = "Réponses enregistrées" if saved else "Aucun changement"
    return web._flash(back, notice=msg)


@router.post("/profiles/{pid}/answers/resume")
async def answers_resume(request: Request, pid: str):
    """Relaunch ONLY the applications of the rows the user was shown (`app_id` + `run_<id>`, `rev_<id>`, `mode_<id>`,
    `origin_<id>`), one after the other. A row whose paused run is no longer the application's last run, whose mode, revision
    or destination digest changed is skipped and reported; the digest is checked again when the task starts."""
    web = _web()
    profile_id = _id_or_404(pid)
    web._profile_or_404(profile_id)
    back = f"/profiles/{profile_id}/answers"
    form = await request.form()
    ids = list(dict.fromkeys(i for v in form.getlist("app_id") if (i := safe_id(v)) is not None))
    if not ids:
        return web._flash(back, error="Aucune candidature à reprendre : recharger la page")
    rows: list[tuple[Any, str]] = []
    skipped: list[str] = []
    for aid in ids:
        app = cvstore.get_application(aid)
        if app is None or app.profile_id != profile_id:
            skipped.append(f"candidature {aid} introuvable")
            continue
        label = app.company or f"candidature {aid}"
        last, job = apply_store.last_run(aid), db.get_job(app.job_id)
        origin = _origin_field(form, f"origin_{aid}")
        if (last is None or job is None or not origin or safe_id(str(form.get(f"run_{aid}") or "")) != last.id
                or str(form.get(f"mode_{aid}") or "") != last.mode or safe_id(str(form.get(f"rev_{aid}") or "")) != last.rev
                or last.rev != app.current_rev or last.status != "needs_input"):
            skipped.append(f"{label} : l'état a changé depuis l'affichage")
        elif origin != apply_service.origin_digest(job.origin_url):
            skipped.append(f"{label} : le lien de l'offre a changé depuis l'affichage")
        else:
            rows.append((last, origin))
    launched, busy = _launch_chain(rows) if rows else (0, [])
    skipped += busy
    if not launched:
        return web._flash(back, error="Aucune reprise lancée : " + " ; ".join(skipped) if skipped else "Aucune candidature à reprendre")
    note = f"Reprise lancée pour {launched} candidature{'s' if launched > 1 else ''}"
    if skipped:
        note += f" ; ignorée{'s' if len(skipped) > 1 else ''} : " + " ; ".join(skipped)
    return web._flash(back, notice=note)


@router.post("/profiles/{pid}/answers/custom/{custom_id}/delete")
async def answers_custom_delete(request: Request, pid: str, custom_id: str):
    """Removes one custom answer, addressed by its stable id; `caprev_<id>` = the answer the page displayed (stale: refused)."""
    web = _web()
    profile_id = _id_or_404(pid)
    web._profile_or_404(profile_id)
    if not re.fullmatch(r"[0-9a-f]{16}", custom_id):
        raise HTTPException(404, "Introuvable")
    form = await request.form()
    back = f"/profiles/{profile_id}/answers"
    prev = form.get(f"caprev_{custom_id}")
    if prev is None:
        return web._flash(back, error="Page périmée : recharger la page")
    try:
        done = facts.delete_custom_answer(profile_id, custom_id, str(prev))
    except ApplyError as e:
        return web._flash(back, error=str(e))
    return web._flash(back, notice="Réponse retirée") if done else web._flash(back, error=f"Non retirée : {STALE_ANSWER}")


@router.post("/profiles/{pid}/answers/hosts/remove")
async def answers_host_remove(request: Request, pid: str):
    web = _web()
    profile_id = _id_or_404(pid)
    web._profile_or_404(profile_id)
    form = await request.form()
    back = f"/profiles/{profile_id}/answers"
    try:
        removed = facts.remove_trusted_host(profile_id, str(form.get("host") or ""))
    except ApplyError as e:
        return web._flash(back, error=str(e))
    return web._flash(back, notice="Site retiré des sites de confiance") if removed else web._flash(back, error="Site introuvable")
