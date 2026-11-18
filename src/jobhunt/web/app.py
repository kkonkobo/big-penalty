"""FastAPI + Jinja2 + HTMX UI (French labels)."""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
import secrets
from urllib.parse import quote, urlparse

from fastapi import FastAPI, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from jobhunt import db, pipeline
from jobhunt import schedule as sched
from jobhunt.config import get_settings
from jobhunt.cv import service as cvs
from jobhunt.cv import store as cvstore
from jobhunt.cv.master import (MasterError, confirm_master, discard_pending, dump_master, load_master, load_pending, load_report,
                               master_path, parse_master, pending_path, save_master, save_pending)
from jobhunt.llm import LLMError, get_llm
from jobhunt.freshness import EXPIRE_AFTER_DAYS, effective_posted_at, is_repost, rank_score
from jobhunt.models import ContractType, Profile, SearchQuery
from jobhunt.scoring import FLAG_LABELS
from jobhunt.sources import registry

log = logging.getLogger("jobhunt.web")
HERE = Path(__file__).parent
templates = Jinja2Templates(directory=str(HERE / "templates"))
app = FastAPI(title="Big Penalty")
app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")

CONTRACT_LABELS = {
    "cdi": "CDI", "cdd": "CDD", "interim": "Intérim", "alternance": "Alternance", "stage": "Stage",
    "freelance": "Freelance", "vie": "VIE", "other": "Autre", "unknown": "Non précisé",
}
FORM_CONTRACTS = ["stage", "alternance", "cdi", "cdd", "interim", "freelance", "vie"]
STATUS_LABELS = {
    "new": "Nouveau", "shortlisted": "Présélectionné", "approved": "Approuvé", "rejected": "Rejeté", "applied": "Postulé",
}
APPLICATION_LABELS = {"draft": "Brouillon", "approved": "Approuvée", "submitted": "Envoyée", "failed": "Échec", "withdrawn": "Retirée"}
ACTIONS = {"approve": "approved", "reject": "rejected", "shortlist": "shortlisted", "reset": "new", "applied": "applied"}

# profile_id -> {"task", "running", "result", "error"}; in-memory, one run per profile at a time.
RUNS: dict[int, dict[str, Any]] = {}


def ago(dt: datetime | None) -> str:
    if not dt:
        return "date inconnue"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    s = (datetime.now(UTC) - dt).total_seconds()
    if s < 3600:
        return "il y a moins d'1 h" if s < 60 else f"il y a {int(s // 60)} min"
    if s < 86400:
        return f"il y a {int(s // 3600)} h"
    d = int(s // 86400)
    if d < 31:
        return f"il y a {d} j"
    return f"il y a {d // 30} mois"


def host(url: str | None) -> str:
    return (urlparse(url or "").hostname or "").removeprefix("www.")


templates.env.filters["ago"] = ago
templates.env.filters["host"] = host


def iso(value: str | datetime | None) -> datetime | None:
    if not value or isinstance(value, datetime):
        return value or None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


templates.env.filters["iso"] = iso


def http_url(url: str | None) -> str:
    """The URL only if its scheme is http or https (job data comes from scrapers: no javascript:/data: links)."""
    u = (url or "").strip()
    return u if urlparse(u).scheme in ("http", "https") and urlparse(u).netloc else ""


templates.env.filters["http_url"] = http_url
DATE_SOURCE_LABELS = {"origin": "site de l'entreprise", "source": "date de la source", "first_seen": "première détection", "none": "inconnue"}
templates.env.globals.update(effective_posted_at=effective_posted_at, is_repost=is_repost, rank_score=rank_score,
                             DATE_SOURCE_LABELS=DATE_SOURCE_LABELS)
templates.env.globals.update(
    CONTRACT_LABELS=CONTRACT_LABELS, STATUS_LABELS=STATUS_LABELS, FLAG_LABELS=FLAG_LABELS, FORM_CONTRACTS=FORM_CONTRACTS,
    APPLICATION_LABELS=APPLICATION_LABELS,
)


def source_labels() -> dict[str, str]:
    return {n: c.label for n, c in registry().items()}


def _split(s: str) -> list[str]:
    return [x.strip() for x in re.split(r"[\n,;]+", s or "") if x.strip()]


def _profile_from_form(form, pid: int | None) -> Profile:
    exp = (form.get("experience_years") or "").strip().replace(",", ".")
    radius = (form.get("radius_km") or "").strip()
    cts = [ContractType(c) for c in form.getlist("contract_types") if c in ContractType._value2member_map_]
    return Profile(
        id=pid, name=(form.get("name") or "").strip() or "Sans nom", headline=(form.get("headline") or "").strip(),
        interests=_split(form.get("interests", "")), education=(form.get("education") or "").strip() or None,
        experience_years=float(exp) if exp else None, skills=_split(form.get("skills", "")),
        cv_text=(form.get("cv_text") or "").strip() or None, locations=_split(form.get("locations", "")),
        radius_km=int(radius) if radius.isdigit() else None, contract_types=cts,
        languages=_split(form.get("languages", "")) or ["fr"], notes=(form.get("notes") or "").strip() or None,
        email=(form.get("email") or "").strip() or None, phone=(form.get("phone") or "").strip() or None,
    )


def _profile_or_404(pid: int) -> Profile:
    p = db.get_profile(pid)
    if p is None:
        raise HTTPException(404, "Profil introuvable")
    return p


def render(request: Request, name: str, **ctx: Any) -> HTMLResponse:
    ctx.setdefault("nav_profiles", db.list_profiles())
    return templates.TemplateResponse(request, name, ctx)


# ------------------------------------------------------------ profiles
@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    """Daily view: what is new since the last search, what to triage, which CVs wait for approval."""
    profiles = db.list_profiles()
    boards = []
    for p in profiles:
        new_jobs = db.query_jobs(p.id, new_only=True)
        to_triage = db.query_jobs(p.id, status="new")
        run = db.latest_run(p.id)
        src = ((run or {}).get("stats") or {}).get("sources", {})
        boards.append({
            "profile": p, "new_count": len(new_jobs), "new_high": sum(1 for j in new_jobs if (j.score or 0) >= 70),
            "to_triage": len(to_triage), "approved": len(db.query_jobs(p.id, status="approved")),
            "picks": (new_jobs or to_triage)[:6], "picks_are_new": bool(new_jobs),
            "drafts": [a for a in cvstore.list_applications(p.id) if a.status == "draft"],
            "run": run, "failing": [source_labels().get(n, n) for n, s in src.items() if s.get("status") == "error"],
        })
    try:
        st = sched.status(tail=1, **SCHEDULE_KW)
    except sched.ScheduleError:
        st = None
    return render(request, "today.html", boards=boards, st=st, nav_profiles=profiles,
                  today=datetime.now().astimezone())


@app.get("/profiles/new", response_class=HTMLResponse)
def profile_new(request: Request):
    return render(request, "profile_form.html", profile=None)


@app.post("/profiles")
async def profile_create(request: Request):
    p = db.save_profile(_profile_from_form(await request.form(), None))
    return RedirectResponse(f"/profiles/{p.id}", status_code=303)


@app.get("/profiles/{pid}/edit", response_class=HTMLResponse)
def profile_edit(request: Request, pid: int):
    return render(request, "profile_form.html", profile=_profile_or_404(pid))


@app.post("/profiles/{pid}")
async def profile_update(request: Request, pid: int):
    _profile_or_404(pid)
    db.save_profile(_profile_from_form(await request.form(), pid))
    return RedirectResponse(f"/profiles/{pid}", status_code=303)


@app.post("/profiles/{pid}/delete")
def profile_delete(pid: int):
    db.delete_profile(pid)
    return RedirectResponse("/", status_code=303)


@app.get("/profiles/{pid}", response_class=HTMLResponse)
def profile_page(request: Request, pid: int):
    p = _profile_or_404(pid)
    return render(request, "profile.html", profile=p, counts=db.job_counts(pid), **_progress_ctx(pid))


# ------------------------------------------------------------ search runs
def _progress_ctx(pid: int) -> dict[str, Any]:
    st = RUNS.get(pid)
    if st:
        res = st.get("result")
        return {
            "running": st["running"], "error": st.get("error"), "plan": res.plan if res else [],
            "sources": res.sources if res else {}, "result": res, "stage": res.stage if res else "plan",
            "live": True, "labels": source_labels(),
        }
    run = db.latest_run(pid)
    if run:
        stats = run["stats"]
        return {
            "running": False, "error": run.get("error"), "plan": [SearchQuery(**q) for q in run["plan"]],
            "sources": {k: _AttrDict(v) for k, v in stats.get("sources", {}).items()}, "result": _AttrDict(stats) if stats else None,
            "stage": run["status"], "live": False, "labels": source_labels(), "run": run,
        }
    return {"running": False, "error": None, "plan": [], "sources": {}, "result": None, "stage": None, "live": False, "labels": {}}


class _AttrDict(dict):
    __getattr__ = dict.get


async def _run_in_background(pid: int, max_queries: int | None) -> None:
    st = RUNS[pid]

    def cb(ev: dict[str, Any]) -> None:
        st["result"] = ev.get("result", st.get("result"))

    try:
        res = await pipeline.run_search(pid, max_queries=max_queries, progress=cb)
        st["result"] = res
        if res.stage == "error":
            st["error"] = "; ".join(res.errors[-1:])
    except Exception as e:  # noqa: BLE001
        log.exception("background search failed")
        st["error"] = f"{type(e).__name__}: {e}"
    finally:
        st["running"] = False


@app.post("/profiles/{pid}/search", response_class=HTMLResponse)
async def search_start(request: Request, pid: int, max_queries: str = Form("")):
    _profile_or_404(pid)
    st = RUNS.get(pid)
    if not (st and st["running"]):
        RUNS[pid] = st = {"running": True, "result": None, "error": None}
        mq = int(max_queries) if max_queries.strip().isdigit() else None
        st["task"] = asyncio.create_task(_run_in_background(pid, mq))
    return render(request, "_progress.html", profile=db.get_profile(pid), **_progress_ctx(pid))


@app.get("/profiles/{pid}/progress", response_class=HTMLResponse)
def search_progress(request: Request, pid: int):
    return render(request, "_progress.html", profile=_profile_or_404(pid), **_progress_ctx(pid))


# ------------------------------------------------------------ shortlist
def _filters(request: Request) -> dict[str, Any]:
    q = request.query_params

    def num(k: str) -> float | None:
        try:
            return float(q[k]) if q.get(k) else None
        except ValueError:
            return None

    return {
        "min_score": num("min_score"), "contract_type": q.get("contract_type") or None, "source": q.get("source") or None,
        "max_age_days": int(num("max_age_days")) if num("max_age_days") else None, "status": q.get("status") or None,
        "sort": q.get("sort") if q.get("sort") in ("rank", "score", "freshness") else "rank",
        "show_filtered": q.get("show_filtered") == "1", "new_only": q.get("new") == "1",
    }


@app.get("/profiles/{pid}/shortlist", response_class=HTMLResponse)
def shortlist(request: Request, pid: int):
    p = _profile_or_404(pid)
    f = _filters(request)
    jobs = db.query_jobs(pid, **f)
    tab_counts: dict[str, int] = {}
    for j in db.query_jobs(pid):
        tab_counts[j.status] = tab_counts.get(j.status, 0) + 1
    return render(request, "shortlist.html", profile=p, jobs=jobs, f=f, labels=source_labels(), counts=db.job_counts(pid),
                  tab_counts=tab_counts, tab_total=sum(tab_counts.values()),
                  applications=cvstore.applications_by_job(pid),
                  overview=db.shortlist_overview(pid), expire_days=EXPIRE_AFTER_DAYS,
                  sources_available=sorted({s for j in db.load_jobs(pid, with_sightings=True) for s in j.sources}))


@app.post("/profiles/{pid}/jobs/action")
async def job_action(request: Request, pid: int):
    _profile_or_404(pid)
    form = await request.form()
    action = form.get("action", "")
    if action not in ACTIONS:
        raise HTTPException(400, "Action inconnue")
    single = str(form.get("job_id") or "")
    if single.isdigit() and request.headers.get("HX-Request"):
        # One-row triage from the list: update that job and send back only its row, no page reload.
        db.set_status([int(single)], ACTIONS[action], pid)
        job = db.get_job(int(single))
        if job is None or job.profile_id != pid:
            raise HTTPException(404, "Offre introuvable")
        app_ = cvstore.find_application(pid, job.id)
        return render(request, "_job_row.html", j=job, profile=_profile_or_404(pid), labels=source_labels(),
                      applications={job.id: app_} if app_ and app_.status != "withdrawn" else {})
    ids = [int(i) for i in form.getlist("job_ids") if str(i).isdigit()]
    db.set_status(ids, ACTIONS[action], pid)
    if request.headers.get("HX-Request"):
        return Response(status_code=204, headers={"HX-Refresh": "true"})
    return RedirectResponse(request.headers.get("referer") or f"/profiles/{pid}/shortlist", status_code=303)


# ------------------------------------------------------------ sources
@app.get("/sources", response_class=HTMLResponse)
def sources_page(request: Request):
    settings = get_settings()
    run = db.latest_run(None)
    stats = (run or {}).get("stats", {}).get("sources", {})
    rows = []
    for name, cls in sorted(registry().items()):
        inst = cls()
        missing = [a for a in cls.requires_credentials if not getattr(settings, a, None)]
        rows.append({
            "name": name, "label": cls.label, "kind": cls.kind, "aggregator": cls.is_aggregator,
            "requires_login": cls.requires_login, "missing": missing, "configured": inst.is_configured(settings),
            "enabled": cls.enabled_by_default, "stat": stats.get(name), "login_url": getattr(cls, "login_url", None),
        })
    return render(request, "sources.html", rows=rows, run=run, profiles=db.list_profiles(),
                  llm_enabled=bool(settings.anthropic_api_key), settings=settings)


# ------------------------------------------------------------ planification
# Module-level indirection: tests set SCHEDULE_KW (runner, home, repo, uid, check_platform) to avoid real launchctl.
SCHEDULE_KW: dict[str, Any] = {}


def _schedule_redirect(notice: str | None = None, error: str | None = None) -> RedirectResponse:
    q = f"?notice={quote(notice)}" if notice else f"?error={quote(error or '')}"
    return RedirectResponse("/schedule" + q, status_code=303)


def _schedule_action(fn, ok: str) -> RedirectResponse:
    try:
        fn()
    except sched.ScheduleError as e:
        return _schedule_redirect(error=str(e))
    return _schedule_redirect(notice=ok)


@app.get("/schedule", response_class=HTMLResponse)
def schedule_page(request: Request, notice: str | None = None, error: str | None = None):
    try:
        st, err = sched.status(tail=10, **SCHEDULE_KW), error
    except sched.ScheduleError as e:
        st, err = None, error or str(e)
    return render(request, "schedule.html", st=st, notice=notice, error=err, default_time=sched.DEFAULT_TIME)


@app.post("/schedule/install")
def schedule_install():
    return _schedule_action(lambda: sched.install(sched.DEFAULT_TIME, **SCHEDULE_KW),
                            f"Planification activée ({sched.DEFAULT_TIME}).")


@app.post("/schedule/add")
def schedule_add(time: str = Form(...)):
    return _schedule_action(lambda: sched.add_time(time, **SCHEDULE_KW), f"Heure {time} ajoutée.")


@app.post("/schedule/remove")
def schedule_remove(time: str = Form(...)):
    return _schedule_action(lambda: sched.remove_time(time, **SCHEDULE_KW), f"Heure {time} retirée.")


@app.post("/schedule/uninstall")
def schedule_uninstall():
    return _schedule_action(lambda: sched.uninstall(**SCHEDULE_KW), "Planification désactivée.")


@app.post("/schedule/run")
def schedule_run():
    return _schedule_action(lambda: sched.run_now(**SCHEDULE_KW), "Recherche lancée, résultats dans ~25 min.")


# ------------------------------------------------------------ CV sur mesure (candidatures)
# job_id -> {"running", "error", "app_id", "task"}; in-memory, one generation per job at a time.
GENERATING: dict[int, dict[str, Any]] = {}
# One-shot messages stored server side: nothing from a model or a CV ever travels in a URL.
FLASH: dict[str, tuple[str, str]] = {}
SAFE_ERROR = "Erreur interne : voir les journaux du serveur"


def _flash(path: str, notice: str | None = None, error: str | None = None) -> RedirectResponse:
    if not (notice or error):
        return RedirectResponse(path, status_code=303)
    token = secrets.token_urlsafe(6)
    FLASH[token] = ("notice", notice) if notice else ("error", error or "")
    while len(FLASH) > 100:
        FLASH.pop(next(iter(FLASH)))
    return RedirectResponse(f"{path}?f={token}", status_code=303)


def _take_flash(token: str | None) -> tuple[str | None, str | None]:
    kind, msg = FLASH.pop(token or "", (None, None))
    return (msg, None) if kind == "notice" else (None, msg)


def _safe_message(e: Exception) -> str:
    """User-facing text of an exception: our own French errors only; anything else is logged, not shown."""
    if isinstance(e, (cvs.CVError, LLMError)):
        return str(e)
    log.exception("unexpected error: %s", type(e).__name__)
    return SAFE_ERROR


async def _generate(job_id: int, profile_id: int) -> None:
    st = GENERATING[job_id]
    try:
        st["app_id"] = (await cvs.prepare_application(profile_id, job_id)).id
    except Exception as e:  # noqa: BLE001
        st["error"] = _safe_message(e)
    finally:
        st["running"] = False


def _job_or_404(job_id: int):
    job = db.get_job(job_id)
    if job is None:
        raise HTTPException(404, "Offre introuvable")
    return job


def _application_or_404(app_id: int):
    a = cvstore.get_application(app_id)
    if a is None:
        raise HTTPException(404, "Candidature introuvable")
    return a


@app.post("/jobs/{job_id}/application")
async def application_start(job_id: int, regenerate: str = Form("")):
    job = _job_or_404(job_id)
    existing = cvstore.find_application(job.profile_id, job_id)
    st = GENERATING.get(job_id)
    if existing and not regenerate and not (st and st["running"]):
        return RedirectResponse(f"/applications/{existing.id}", status_code=303)
    if not (st and st["running"]):
        # Nothing is withdrawn or replaced here: the new revision only appears when it is complete.
        GENERATING[job_id] = st = {"running": True, "error": None, "app_id": None}
        st["task"] = asyncio.create_task(_generate(job_id, job.profile_id))
    return RedirectResponse(f"/jobs/{job_id}/application/wait", status_code=303)


@app.get("/jobs/{job_id}/application/wait", response_class=HTMLResponse)
def application_wait(request: Request, job_id: int):
    job = _job_or_404(job_id)
    st = GENERATING.get(job_id)
    if st and st.get("app_id") and not st["running"]:
        return RedirectResponse(f"/applications/{st['app_id']}", status_code=303)
    if st is None:
        existing = cvstore.find_application(job.profile_id, job_id)
        return RedirectResponse(f"/applications/{existing.id}" if existing else f"/profiles/{job.profile_id}/shortlist", status_code=303)
    return render(request, "application_wait.html", job=job, running=st["running"], error=st.get("error"))


@app.get("/applications", response_class=HTMLResponse)
def applications_page(request: Request, f: str | None = None):
    profiles = {p.id: p for p in db.list_profiles()}
    notice, error = _take_flash(f)
    return render(request, "applications.html", applications=cvstore.list_applications(), profiles=profiles,
                  notice=notice, error=error)


@app.get("/applications/{app_id}", response_class=HTMLResponse)
def application_page(request: Request, app_id: int, f: str | None = None):
    a = _application_or_404(app_id)
    job = db.get_job(a.job_id)
    notice, error = _take_flash(f)
    try:
        master, _ = cvs._load(a.profile_id)
    except cvs.CVError as e:
        master, error = None, error or str(e)
    eligible, reasons = cvs.is_eligible(a)
    return render(request, "application.html", a=a, job=job, master=master, labels=source_labels(), notice=notice, error=error,
                  checked={n.lower() for g in a.tailored.skill_groups for n in g.items}, eligible=eligible,
                  not_eligible_reasons=reasons, revisions=cvstore.list_revisions(a.id), fit=a.fit_notes)


@app.get("/applications/{app_id}/preview", response_class=HTMLResponse)
def application_preview(app_id: int):
    a = _application_or_404(app_id)
    try:
        return HTMLResponse(cvs.preview_html(a))
    except cvs.CVError as e:
        raise HTTPException(404, str(e)) from e


@app.get("/applications/{app_id}/pdf")
def application_pdf(app_id: int):
    a = _application_or_404(app_id)
    pdf = cvs.safe_pdf(a)  # resolved, .pdf, inside settings.applications_dir
    if pdf is None:
        raise HTTPException(404, "PDF introuvable : mettre à jour le CV pour le régénérer")
    return FileResponse(pdf, media_type="application/pdf", filename=pdf.name)


@app.post("/applications/{app_id}/update")
async def application_update(request: Request, app_id: int):
    _application_or_404(app_id)
    form = await request.form()
    bullet_texts, bullet_labels, group_labels = {}, {}, {}
    for key, value in form.multi_items():
        m = re.fullmatch(r"(b|l)_(\d+)_(\d+)", key)
        if m:
            (bullet_texts if m.group(1) == "b" else bullet_labels)[(int(m.group(2)), int(m.group(3)))] = str(value)
        m = re.fullmatch(r"gl_(\d+)", key)
        if m:
            group_labels[int(m.group(1))] = str(value)
    rev = str(form.get("rev", ""))
    if not rev.isdigit():
        return _flash(f"/applications/{app_id}", error="Version du CV manquante : rouvrir la page")
    try:
        await cvs.update_application(
            app_id, rev=int(rev), headline=str(form.get("headline", "")), summary=str(form.get("summary", "")),
            bullet_texts=bullet_texts, bullet_labels=bullet_labels, group_labels=group_labels,
            skills=set(map(str, form.getlist("skill"))) if form.get("skills_present") else None, extra_skills=set(map(str, form.getlist("skillx"))))
    except Exception as e:  # noqa: BLE001
        return _flash(f"/applications/{app_id}", error=_safe_message(e))
    return _flash(f"/applications/{app_id}", notice="CV mis à jour (nouvelle version).")


@app.post("/applications/{app_id}/approve")
def application_approve(app_id: int, rev: str = Form(""), ignore: str = Form("")):
    _application_or_404(app_id)
    if not rev.isdigit():
        return _flash(f"/applications/{app_id}", error="Version du CV manquante : rouvrir la page")
    try:
        cvs.approve(app_id, int(rev), ignore_errors=ignore in ("1", "on"))
    except cvs.CVError as e:
        return _flash(f"/applications/{app_id}", error=str(e))
    return _flash(f"/applications/{app_id}", notice="CV approuvé.")


@app.post("/applications/{app_id}/withdraw")
def application_withdraw(app_id: int):
    _application_or_404(app_id)
    try:
        cvs.withdraw(app_id)
    except cvs.CVError as e:
        return _flash(f"/applications/{app_id}", error=str(e))
    return _flash("/applications", notice="Candidature retirée.")


@app.post("/applications/{app_id}/restore")
def application_restore(app_id: int):
    _application_or_404(app_id)
    try:
        cvs.restore(app_id)
    except cvs.CVError as e:
        return _flash(f"/applications/{app_id}", error=str(e))
    return _flash(f"/applications/{app_id}", notice="Candidature restaurée (brouillon).")


# ------------------------------------------------------------ profil maître (CV)
def _master_page(request: Request, pid: int, *, text: str | None = None, notice=None, error=None, status: int = 200):
    p = _profile_or_404(pid)
    master = pending = None
    try:
        master = load_master(pid)
    except MasterError as e:
        error = error or str(e)
    try:
        pending = load_pending(pid)
    except MasterError as e:
        error = error or str(e)
    if text is None:
        shown = pending or master
        text = dump_master(shown) if shown else ""
    report = load_report(pid) if pending else {}
    disputed = {d["field"] for d in report.get("disputed", [])}
    return templates.TemplateResponse(request, "master_cv.html", dict(
        profile=p, master=master, pending=pending, report=report, disputed=disputed, text=text, notice=notice, error=error,
        path=master_path(pid), confirmed=bool(master and master.is_confirmed)), status_code=status)


@app.get("/profiles/{pid}/cv", response_class=HTMLResponse)
def master_cv_page(request: Request, pid: int, f: str | None = None):
    notice, error = _take_flash(f)
    return _master_page(request, pid, notice=notice, error=error)


@app.get("/profiles/{pid}/cv/preview", response_class=HTMLResponse)
def master_cv_preview(pid: int):
    _profile_or_404(pid)
    try:
        return HTMLResponse(cvs.master_preview_html(pid))
    except cvs.CVError as e:
        raise HTTPException(404, str(e)) from e


@app.post("/profiles/{pid}/cv", response_class=HTMLResponse)
def master_cv_save(request: Request, pid: int, text: str = Form("")):
    """Saves the pending import when there is one, else the active master (which becomes unconfirmed)."""
    _profile_or_404(pid)
    try:
        master = parse_master(text)
        if pending_path(pid).is_file():
            save_pending(pid, master)
        else:
            save_master(pid, master)
    except MasterError as e:
        return _master_page(request, pid, text=text, error=str(e), status=422)
    return _flash(f"/profiles/{pid}/cv", notice="Enregistré : à confirmer pour pouvoir générer des CV." )


@app.post("/profiles/{pid}/cv/confirm")
def master_cv_confirm(pid: int):
    _profile_or_404(pid)
    try:
        confirm_master(pid)
    except MasterError as e:
        return _flash(f"/profiles/{pid}/cv", error=str(e))
    return _flash(f"/profiles/{pid}/cv", notice="CV maître confirmé.")


@app.post("/profiles/{pid}/cv/discard")
def master_cv_discard(pid: int):
    _profile_or_404(pid)
    discard_pending(pid)
    return _flash(f"/profiles/{pid}/cv", notice="Import abandonné.")


@app.post("/profiles/{pid}/cv/import")
async def master_cv_import(pid: int, file: UploadFile):
    import tempfile

    from jobhunt.cv.importer import import_cv

    profile = _profile_or_404(pid)
    name = (file.filename or "cv").lower()
    suffix = ".pdf" if name.endswith(".pdf") else ".txt"
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / f"upload{suffix}"
        src.write_bytes(await file.read())
        llm = get_llm()
        try:
            res = await import_cv(pid, profile, llm, pdf=src if suffix == ".pdf" else None,
                                  text_file=None if suffix == ".pdf" else src)
        except (LLMError, ValueError) as e:
            return _flash(f"/profiles/{pid}/cv", error=str(e) if isinstance(e, LLMError) else "CV illisible ou vide")
        finally:
            await llm.aclose()
    n = len(res.unverified) + len(res.disputed)
    return _flash(f"/profiles/{pid}/cv", notice="CV importé, en attente de confirmation."
                  + (f" {n} élément(s) contesté(s) à vérifier." if n else ""))
