"""FastAPI + Jinja2 + HTMX UI (French labels)."""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from fastapi import FastAPI, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from jobhunt import db, pipeline
from jobhunt.config import get_settings
from jobhunt.freshness import effective_posted_at, is_repost, rank_score
from jobhunt.models import ContractType, Profile, SearchQuery
from jobhunt.scoring import FLAG_LABELS
from jobhunt.sources import registry

log = logging.getLogger("jobhunt.web")
HERE = Path(__file__).parent
templates = Jinja2Templates(directory=str(HERE / "templates"))
app = FastAPI(title="jobhunt")
app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")

CONTRACT_LABELS = {
    "cdi": "CDI", "cdd": "CDD", "interim": "Intérim", "alternance": "Alternance", "stage": "Stage",
    "freelance": "Freelance", "vie": "VIE", "other": "Autre", "unknown": "Non précisé",
}
FORM_CONTRACTS = ["stage", "alternance", "cdi", "cdd", "interim", "freelance", "vie"]
STATUS_LABELS = {
    "new": "Nouveau", "shortlisted": "Présélectionné", "approved": "Approuvé", "rejected": "Rejeté", "applied": "Postulé",
}
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
DATE_SOURCE_LABELS = {"origin": "site de l'entreprise", "source": "date de la source", "first_seen": "première détection", "none": "inconnue"}
templates.env.globals.update(effective_posted_at=effective_posted_at, is_repost=is_repost, rank_score=rank_score,
                             DATE_SOURCE_LABELS=DATE_SOURCE_LABELS)
templates.env.globals.update(
    CONTRACT_LABELS=CONTRACT_LABELS, STATUS_LABELS=STATUS_LABELS, FLAG_LABELS=FLAG_LABELS, FORM_CONTRACTS=FORM_CONTRACTS,
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
    return templates.TemplateResponse(request, name, ctx)


# ------------------------------------------------------------ profiles
@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    profiles = db.list_profiles()
    counts = {p.id: db.job_counts(p.id) for p in profiles}
    return render(request, "profiles.html", profiles=profiles, counts=counts)


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
        "show_filtered": q.get("show_filtered") == "1",
    }


@app.get("/profiles/{pid}/shortlist", response_class=HTMLResponse)
def shortlist(request: Request, pid: int):
    p = _profile_or_404(pid)
    f = _filters(request)
    jobs = db.query_jobs(pid, **f)
    return render(request, "shortlist.html", profile=p, jobs=jobs, f=f, labels=source_labels(), counts=db.job_counts(pid),
                  sources_available=sorted({s for j in db.load_jobs(pid, with_sightings=True) for s in j.sources}))


@app.post("/profiles/{pid}/jobs/action")
async def job_action(request: Request, pid: int):
    _profile_or_404(pid)
    form = await request.form()
    action = form.get("action", "")
    if action not in ACTIONS:
        raise HTTPException(400, "Action inconnue")
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
