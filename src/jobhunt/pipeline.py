"""plan -> search -> dedupe -> origin -> score. `run_search` is the single entry point (CLI + web)."""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import inspect
import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Callable

from jobhunt import db
from jobhunt.config import get_settings
from jobhunt.dedupe import MergedJob, canonical_url, consolidate, merge_postings
from jobhunt.fetch import is_http_url
from jobhunt.llm import LLMClient, get_llm
from jobhunt.models import JobPosting, SearchQuery
from jobhunt.scoring import JobScore, invalidate_score, keyword_score, prefilter, scoring_fingerprint
from jobhunt.sources import registry
from jobhunt.sources.base import ChallengeRequired, LoginRequired, Source, SourceContext

log = logging.getLogger("jobhunt.pipeline")

QUERY_TIMEOUT_S = 420.0  # browser sources may wait for a human to solve a challenge
MAX_CONSECUTIVE_ERRORS = 3
SCORE_BATCH = 8
SCORE_CONCURRENCY = 3
DETAIL_LIMIT = 60
DETAIL_CONCURRENCY = 4
DETAIL_TIMEOUT_S = 45.0


@dataclass
class SourceStat:
    label: str = ""
    status: str = "pending"  # pending | running | done | error | skipped
    queries_total: int = 0
    queries_done: int = 0
    postings: int = 0
    errors: list[str] = field(default_factory=list)
    skipped_reason: str | None = None
    needs_login: bool = False


@dataclass
class RunResult:
    run_id: int
    profile_id: int
    plan: list[SearchQuery] = field(default_factory=list)
    sources: dict[str, SourceStat] = field(default_factory=dict)
    raw_postings: int = 0
    unique_jobs: int = 0
    new_jobs: int = 0
    scored: int = 0
    prefiltered: int = 0
    errors: list[str] = field(default_factory=list)
    llm_enabled: bool = False
    llm_stats: dict[str, Any] = field(default_factory=dict)
    sources_ok: list[str] = field(default_factory=list)  # sources that completed with no error/challenge/block
    stage: str = "pending"

    def stats_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("plan")
        return d


ProgressCb = Callable[[dict[str, Any]], Any]


async def _emit(cb: ProgressCb | None, result: RunResult, **ev: Any) -> None:
    if cb is None:
        return
    try:
        r = cb({"run_id": result.run_id, "stage": result.stage, "result": result, **ev})
        if inspect.isawaitable(r):
            await r
    except Exception:  # progress reporting must never break a run
        log.exception("progress callback failed")


def select_sources(names: list[str] | None, result: RunResult) -> dict[str, Source]:
    """Instantiate enabled+configured sources (or the explicitly requested ones); record skips."""
    settings = get_settings()
    reg = registry()
    chosen: dict[str, Source] = {}
    for name in (names or list(reg)):
        cls = reg.get(name)
        if cls is None:
            result.sources[name] = SourceStat(label=name, status="skipped", skipped_reason="source inconnue")
            continue
        stat = SourceStat(label=cls.label)
        result.sources[name] = stat
        if names is None and not cls.enabled_by_default:
            stat.status, stat.skipped_reason = "skipped", "désactivée par défaut"
            continue
        inst = cls()
        if not inst.is_configured(settings):
            missing = ", ".join(a for a in cls.requires_credentials if not getattr(settings, a, None))
            stat.status, stat.skipped_reason = "skipped", f"identifiants manquants ({missing})"
            continue
        chosen[name] = inst
    return chosen


def applicable(source: Source, q: SearchQuery) -> bool:
    ct = source.contract_types
    return ct is None or not q.contract_types or bool(ct & set(q.contract_types))


async def _run_source(name: str, src: Source, queries: list[SearchQuery], ctx: SourceContext,
                      result: RunResult, out: list[JobPosting], cb: ProgressCb | None) -> None:
    stat = result.sources[name]
    todo = [q for q in queries if applicable(src, q)]
    stat.queries_total = len(todo)
    if not todo:
        stat.status, stat.skipped_reason = "skipped", "aucune requête compatible (type de contrat)"
        return
    stat.status = "running"
    consecutive = 0
    for q in todo:
        try:
            found = await asyncio.wait_for(src.search(q, ctx), QUERY_TIMEOUT_S)
            consecutive = 0
            stat.postings += len(found)
            out.extend(found)
        except (LoginRequired, ChallengeRequired) as e:
            stat.errors.append(f"{type(e).__name__}: {e}")
            stat.needs_login = isinstance(e, LoginRequired)
            stat.status = "error"
            await _emit(cb, result, source=name, event="source_error", error=str(e))
            return
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            stat.errors.append(f"« {q.keywords} »: délai dépassé")
            consecutive += 1
        except Exception as e:  # noqa: BLE001 - one bad query must not kill the run
            log.warning("source %s failed on %r: %s", name, q.keywords, e)
            stat.errors.append(f"« {q.keywords} »: {type(e).__name__}: {e}")
            consecutive += 1
        stat.queries_done += 1
        await _emit(cb, result, source=name, event="query_done", done=stat.queries_done, total=stat.queries_total)
        if consecutive >= MAX_CONSECUTIVE_ERRORS:
            stat.errors.append(f"Abandon après {MAX_CONSECUTIVE_ERRORS} erreurs consécutives")
            stat.status = "error"
            return
    stat.status = "error" if stat.errors and stat.postings == 0 else "done"


def _source_ok(stat: SourceStat) -> bool:
    """Fully checked: finished, no error at all (0 postings without error is fine; a challenge, login wall,
    timeout, exception or abandonment is not)."""
    return stat.status == "done" and not stat.errors and not stat.needs_login


async def _enrich_details(postings: list[JobPosting], chosen: dict[str, Source], ctx: SourceContext,
                          result: RunResult) -> list[JobPosting]:
    groups: dict[str, list[tuple[JobPosting, list[int]]]] = {}
    seen: dict[tuple[str, str, str], tuple[JobPosting, list[int]]] = {}
    selected = 0
    for index, posting in enumerate(postings):
        if posting.source not in chosen:
            continue
        keys = [(posting.source, "id", posting.source_id)]
        if url := canonical_url(posting.url):
            keys.append((posting.source, "url", url))
        group = next((seen[key] for key in keys if key in seen), None)
        if group is not None:
            group[1].append(index)
        elif (not posting.description or not posting.apply_url) and selected < DETAIL_LIMIT:
            group = (posting, [index])
            groups.setdefault(posting.source, []).append(group)
            selected += 1
        if group is not None:
            for key in keys:
                seen[key] = group
    semaphore = asyncio.Semaphore(DETAIL_CONCURRENCY)

    async def enrich_source(name, entries):
        source = chosen[name]
        for offset, (posting, indices) in enumerate(entries):
            if offset:
                await asyncio.sleep(source.detail_delay_s)
            try:
                async with semaphore:
                    enriched = await asyncio.wait_for(source.fetch_details(posting.model_copy(deep=True), ctx), DETAIL_TIMEOUT_S)
                updates = {key: getattr(enriched, key) for key in JobPosting.model_fields
                           if key not in {"source", "source_id", "url", "raw"}
                           and getattr(enriched, key) not in (None, "", "unknown")}
                for index in indices:
                    postings[index] = postings[index].model_copy(update=updates)
            except Exception as error:
                result.errors.append(f"Détails {name}/{posting.source_id}: {type(error).__name__}: {error}")

    await asyncio.gather(*(enrich_source(name, entries) for name, entries in groups.items()))
    return postings


def origin_fingerprint(job: MergedJob) -> str:
    inputs = [job.company, job.extra.get("company_website"), job.title, job.location, job.remote,
              sorted(job.identity_urls()), job.apply_url,
              sorted((sighting.url, sighting.apply_url or "") for sighting in job.sightings)]
    return hashlib.sha256(json.dumps(inputs, ensure_ascii=False).encode()).hexdigest()


def _origin_due(job: MergedJob) -> bool:
    if job.origin_url:  # already resolved: re-check once to backfill the origin posting date
        return "origin_date_checked" not in job.extra
    if job.extra.get("origin_fingerprint") != origin_fingerprint(job):
        return True
    try:
        attempted = datetime.fromisoformat(job.extra["origin_attempted_at"])
        attempted = attempted.replace(tzinfo=attempted.tzinfo or UTC)
    except (KeyError, TypeError, ValueError):
        return True
    return db.now() - attempted >= timedelta(days=3)


async def _resolve_origins(jobs: list[MergedJob], ctx: SourceContext, result: RunResult) -> list[MergedJob]:
    try:
        mod = importlib.import_module("jobhunt.origin")
        resolve = getattr(mod, "resolve_origins", None)
    except ImportError:
        return jobs
    except Exception as e:  # noqa: BLE001
        result.errors.append(f"Module origin illisible: {e}")
        return jobs
    if resolve is None:
        return jobs
    for job in jobs:
        if job.origin_url and (not is_http_url(job.origin_url) or
                               (mod.is_aggregator(job.origin_url) if hasattr(mod, "is_aggregator") else False)):
            job.origin_url, job.origin_ats, job.origin_confidence = None, None, None
            job.extra.pop("origin_fingerprint", None)
            job.extra.pop("origin_posted_at", None)
            job.extra.pop("origin_date_checked", None)
            job.recompute_posted_at()
            job.changed = True
    todo = [j for j in jobs if _origin_due(j)]
    if not todo:
        return jobs
    try:
        resolved = await resolve([j.to_posting() for j in todo], ctx)
        for j, p in zip(todo, resolved):
            j.apply_origin(p)
            j.recompute_posted_at()  # repairs rows where an old run folded the ATS date into posted_at
            if j.origin_url:
                j.extra["origin_date_checked"] = db.now().isoformat()
            j.extra["origin_attempted_at"] = db.now().isoformat()
            j.extra["origin_fingerprint"] = origin_fingerprint(j)
            j.extra.pop("origin_tried", None)
            j.changed = True
    except Exception as e:  # noqa: BLE001
        log.warning("origin resolution failed: %s", e)
        result.errors.append(f"Résolution de l'origine: {type(e).__name__}: {e}")
        return jobs
    return jobs


async def run_search(
    profile_id: int, *, sources: list[str] | None = None, max_queries: int | None = None,
    progress: ProgressCb | None = None, llm: LLMClient | None = None, headless: bool | None = None,
) -> RunResult:
    """`headless` overrides the browser setting for this run only (None = JOBHUNT_HEADLESS / settings)."""
    profile = db.get_profile(profile_id)
    if profile is None:
        raise ValueError(f"profil {profile_id} introuvable")
    llm = llm or get_llm()
    result = RunResult(run_id=db.create_run(profile_id), profile_id=profile_id, llm_enabled=llm.enabled)
    browser = None
    try:
        # 1. PLAN
        result.stage = "plan"
        await _emit(progress, result, event="stage")
        result.plan = await llm.plan(profile, max_queries or 25)
        result.llm_stats = getattr(llm, "stats", {})
        result.errors.extend(llm.errors)
        db.update_run(result.run_id, plan=[q.model_dump() for q in result.plan])

        # 2. SEARCH
        result.stage = "search"
        chosen = select_sources(sources, result)
        db.update_run(result.run_id, stats=result.stats_dict())
        await _emit(progress, result, event="stage")
        try:
            from jobhunt.browser import BrowserPool
            browser = BrowserPool(headless=headless)  # cheap: no browser is launched until a source asks for a context
        except Exception as e:  # noqa: BLE001
            log.info("browser unavailable: %s", e)
        ctx = SourceContext(profile_id=profile_id, browser=browser)
        postings: list[JobPosting] = []
        await asyncio.gather(*(
            _run_source(n, s, result.plan, ctx, result, postings, progress) for n, s in chosen.items()
        ))
        result.raw_postings = len(postings)
        result.sources_ok = sorted(n for n in chosen if _source_ok(result.sources[n]))
        db.update_run(result.run_id, stats=result.stats_dict())
        postings = await _enrich_details(postings, chosen, ctx, result)

        # 3. DEDUPE (against this run and everything stored for the profile)
        result.stage = "dedupe"
        await _emit(progress, result, event="stage")
        existing = db.load_jobs(profile_id)
        n_before = len(existing)
        jobs, _ = merge_postings(postings, existing)

        # 4. ORIGIN
        result.stage = "origin"
        await _emit(progress, result, event="stage")
        jobs = await _resolve_origins(jobs, ctx, result)
        jobs, removed = consolidate(jobs)
        db.save_jobs(profile_id, jobs, removed=removed)
        result.unique_jobs = len(jobs)
        result.new_jobs = max(0, len(jobs) - n_before)

        # 5. SCORE
        result.stage = "score"
        await _emit(progress, result, event="stage")
        await _score_jobs(profile, jobs, llm, result, progress)
        result.llm_stats = getattr(llm, "stats", {})
        result.errors.extend(e for e in llm.errors if e not in result.errors)
        db.save_jobs(profile_id, jobs)

        result.stage = "done"
        db.update_run(result.run_id, stats=result.stats_dict(), status="done", finished=True)
        await _emit(progress, result, event="done")
    except asyncio.CancelledError:
        db.update_run(result.run_id, status="cancelled", finished=True)
        raise
    except Exception as e:  # noqa: BLE001
        log.exception("run failed")
        result.llm_stats = getattr(llm, "stats", {})
        result.stage = "error"
        result.errors.append(f"{type(e).__name__}: {e}")
        db.update_run(result.run_id, stats=result.stats_dict(), status="error", error=str(e), finished=True)
        await _emit(progress, result, event="error", error=str(e))
    finally:
        if hasattr(llm, "aclose"):
            await llm.aclose()
        if browser is not None:
            try:
                await browser.close()
            except Exception:  # noqa: BLE001
                pass
    return result


async def rescore(profile_id: int, *, llm: LLMClient | None = None, progress: ProgressCb | None = None) -> RunResult:
    """Re-score stored jobs whose scoring inputs changed (profile edit, prompt version), without searching again."""
    profile = db.get_profile(profile_id)
    if profile is None:
        raise ValueError(f"profil {profile_id} introuvable")
    llm = llm or get_llm()
    result = RunResult(run_id=0, profile_id=profile_id, llm_enabled=llm.enabled)
    jobs = db.load_jobs(profile_id)
    result.unique_jobs = len(jobs)
    try:
        await _score_jobs(profile, jobs, llm, result, progress)
        result.llm_stats = getattr(llm, "stats", {})
        result.errors.extend(e for e in llm.errors if e not in result.errors)
        db.save_jobs(profile_id, jobs)
    finally:
        if hasattr(llm, "aclose"):
            await llm.aclose()
    return result


async def _score_jobs(profile, jobs: list[MergedJob], llm: LLMClient, result: RunResult, cb) -> None:
    for job in jobs:
        invalidate_score(profile, job)
    todo = [j for j in jobs if j.score is None]
    to_llm: list[MergedJob] = []
    for j in todo:
        reason = prefilter(profile, j)
        if reason:
            j.score = 0
            j.score_json = JobScore(fit=0, rationale=reason, origin="prefilter", scored_by="prefilter", prefiltered=True).model_dump()
            j.changed = True
            j.extra["scoring_fingerprint"] = scoring_fingerprint(profile, j)
            result.prefiltered += 1
        else:
            to_llm.append(j)
    batches = [to_llm[i:i + SCORE_BATCH] for i in range(0, len(to_llm), SCORE_BATCH)]
    sem = asyncio.Semaphore(SCORE_CONCURRENCY)
    done = 0

    async def one(batch: list[MergedJob]) -> None:
        nonlocal done
        async with sem:
            try:
                scores = await llm.score(profile, batch)
            except Exception as e:  # noqa: BLE001 - never lose a batch to an unexpected LLM-layer error
                result.errors.append(f"Scoring: {type(e).__name__}: {e} (mots-clés utilisés)")
                scores = [keyword_score(profile, j) for j in batch]
        for j, s in zip(batch, scores):
            j.score, j.score_json, j.changed = s.fit, s.model_dump(), True
            j.extra["scoring_fingerprint"] = scoring_fingerprint(profile, j)
            result.scored += 1
        done += 1
        result.llm_stats = getattr(llm, "stats", {})
        await _emit(cb, result, event="score_batch", done=done, total=len(batches))

    await asyncio.gather(*(one(b) for b in batches))
