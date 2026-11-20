"""plan -> search -> dedupe -> origin -> score. `run_search` is the single entry point (CLI + web)."""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import inspect
import json
import logging
import os
import time
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Callable

from jobhunt import db
from jobhunt.config import get_settings
from jobhunt.dedupe import MergedJob, canonical_url, merge_postings
from jobhunt.fetch import is_http_url
from jobhunt.llm import LLMClient, get_llm
from jobhunt.models import JobPosting, SearchQuery
from jobhunt.scoring import JobScore, invalidate_score, keyword_score, prefilter, scoring_fingerprint
from jobhunt.sources import registry
from jobhunt.sources.base import ChallengeRequired, LoginRequired, Source, SourceContext

log = logging.getLogger("jobhunt.pipeline")

QUERY_TIMEOUT_S = 420.0  # browser sources may wait for a human to solve a challenge
MAX_CONSECUTIVE_ERRORS = 3
MAX_CONSECUTIVE_BLOCKS = 2  # 429 / Cloudflare "Just a moment": the site is rate-limiting us, more queries only dig deeper
MAX_TOTAL_ERRORS = 6
SEARCH_BUDGET_S = 20 * 60.0  # sources stop starting new queries after this; what they found is already stored
ORIGIN_BUDGET_S = 15 * 60.0  # jobs left unresolved are picked up by the next run (`_origin_due`)
ORIGIN_CHUNK = 48
ORIGIN_CHUNKS_IN_FLIGHT = 2
SCORE_BATCH = 12
SCORE_CONCURRENCY = 4
STALE_RUN_AFTER = timedelta(hours=3)  # a "running" row without a live owner process older than this is dead
DETAIL_LIMIT = 60
DETAIL_CONCURRENCY = 4
DETAIL_TIMEOUT_S = 45.0
CANCEL_GRACE_S = 20.0  # a timed-out call gets this long to unwind; then it is abandoned, never awaited forever


async def _bounded(coro, timeout: float):
    """`asyncio.wait_for` that cannot hang: wait_for waits for the cancelled call to finish, so a call whose cleanup is
    stuck (a browser tab that never closes) blocked the whole run for hours. Raises TimeoutError after `timeout` (+ grace)."""
    task = asyncio.ensure_future(coro)
    try:
        done, _ = await asyncio.wait({task}, timeout=timeout)
    except asyncio.CancelledError:
        task.cancel()
        raise
    if done:
        return task.result()
    task.cancel()
    await asyncio.wait({task}, timeout=CANCEL_GRACE_S)
    if not task.done():
        log.warning("abandoning a call that did not stop %.0fs after its timeout", CANCEL_GRACE_S)
        task.add_done_callback(lambda t: t.cancelled() or t.exception())  # no "exception never retrieved" noise
    raise TimeoutError


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
    linkedin_detail_calls: int = 0  # LinkedIn detail calls (paid provider) of this run
    linkedin_links: int = 0  # employer apply links recovered by them
    sources_ok: list[str] = field(default_factory=list)  # sources that completed with no error/challenge/block
    stage: str = "pending"
    pid: int = field(default_factory=os.getpid)  # lets later runs tell a live run from one killed mid-way

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


FoundCb = Callable[[str, list[JobPosting]], Any]


def _is_block(error: BaseException) -> bool:
    """A rate limit or an anti-bot interstitial (not an ordinary failure of one query)."""
    text = str(error)
    return "429" in text or "Just a moment" in text or "Too Many Requests" in text or "challenge" in text.lower()


async def _run_source(name: str, src: Source, queries: list[SearchQuery], ctx: SourceContext,
                      result: RunResult, out: list[JobPosting], cb: ProgressCb | None,
                      on_found: FoundCb | None = None, deadline: float | None = None) -> None:
    """Run one source's queries one after the other. Every query's postings go to `on_found` (which stores them)
    as soon as the query returns, so an interrupted run keeps everything the finished queries found."""
    stat = result.sources[name]
    todo = [q for q in queries if applicable(src, q)]
    stat.queries_total = len(todo)
    if not todo:
        stat.status, stat.skipped_reason = "skipped", "aucune requête compatible (type de contrat)"
        return
    stat.status = "running"
    consecutive = blocks = 0
    for q in todo:
        if deadline is not None and time.monotonic() >= deadline:
            stat.errors.append(f"Budget de temps atteint ({stat.queries_done}/{stat.queries_total} requêtes)")
            break
        try:
            found = await _bounded(src.search(q, ctx), QUERY_TIMEOUT_S)
            consecutive = blocks = 0
            stat.postings += len(found)
            out.extend(found)
            if on_found is not None and found:
                try:
                    await on_found(name, found)
                except Exception as e:  # noqa: BLE001 - storing must never stop the search
                    log.exception("storing postings of %s failed", name)
                    result.errors.append(f"Enregistrement {name}: {type(e).__name__}: {e}")
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
            stat.errors.append(f"« {q.keywords} »: {type(e).__name__}: {e}"[:300])
            consecutive += 1
            blocks = blocks + 1 if _is_block(e) else 0
        stat.queries_done += 1
        await _emit(cb, result, source=name, event="query_done", done=stat.queries_done, total=stat.queries_total)
        if blocks >= MAX_CONSECUTIVE_BLOCKS:
            stat.errors.append(f"Abandon : site bloquant ({blocks} blocages consécutifs)")
            stat.status = "error"
            return
        if consecutive >= MAX_CONSECUTIVE_ERRORS or len(stat.errors) >= MAX_TOTAL_ERRORS:
            stat.errors.append(f"Abandon après {len(stat.errors)} erreurs")
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
                    enriched = await _bounded(source.fetch_details(posting.model_copy(deep=True), ctx), DETAIL_TIMEOUT_S)
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


async def _resolve_origins(jobs: list[MergedJob], ctx: SourceContext, result: RunResult, *,
                           persist: Callable[[], Any] | None = None, profile: Any = None,
                           deadline: float | None = None) -> list[MergedJob]:
    """Resolve the origin of the jobs that need it, a chunk at a time; `persist` stores `jobs` after each chunk (it may
    consolidate duplicates in place), so an interruption keeps every chunk done. Jobs the profile's prefilter already
    drops are not worth a ladder run. Past `deadline` the remaining jobs wait for the next run."""
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
    if profile is not None:
        todo = [j for j in todo if j.origin_url or prefilter(profile, j) is None]
        todo.sort(key=lambda j: -keyword_score(profile, j).fit)  # most promising first: best partial results
    chunks = [todo[i:i + ORIGIN_CHUNK] for i in range(0, len(todo), ORIGIN_CHUNK)]
    sem = asyncio.Semaphore(ORIGIN_CHUNKS_IN_FLIGHT)
    skipped = 0

    async def one(chunk: list[MergedJob]) -> None:
        nonlocal skipped
        async with sem:
            if deadline is not None and time.monotonic() >= deadline:
                skipped += len(chunk)
                return
            try:
                resolved = await resolve([j.to_posting() for j in chunk], ctx)
                for j, p in zip(chunk, resolved):
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
                return
            if persist is not None:
                persist()

    try:
        await asyncio.gather(*(one(c) for c in chunks))
    finally:
        if skipped:
            result.errors.append(f"Résolution de l'origine : budget de temps atteint, {skipped} offre(s) reportée(s)")
    return jobs


def reap_stale_runs() -> int:
    """Mark as failed the runs still "running" whose process is gone (killed, host restart): they must not look alive.
    A run without a recorded pid counts as dead after STALE_RUN_AFTER."""
    from jobhunt.apply.store import pid_alive  # noqa: PLC0415

    reaped = 0
    with db.connect() as c:
        rows = c.execute("SELECT id, started_at, stats FROM runs WHERE status='running'").fetchall()
    for r in rows:
        try:
            pid = (json.loads(r["stats"]) if r["stats"] else {}).get("pid")
        except (TypeError, ValueError):
            pid = None
        started = db._dt(r["started_at"])
        if pid:
            alive = pid == os.getpid() or pid_alive(int(pid))
        else:
            alive = started is not None and db.now() - started < STALE_RUN_AFTER
        if not alive:
            db.update_run(int(r["id"]), status="error", error="interrompu (processus arrêté avant la fin)", finished=True)
            reaped += 1
    return reaped


async def run_search(
    profile_id: int, *, sources: list[str] | None = None, max_queries: int | None = None,
    progress: ProgressCb | None = None, llm: LLMClient | None = None, headless: bool | None = None,
) -> RunResult:
    """`headless` overrides the browser setting for this run only (None = JOBHUNT_HEADLESS / settings)."""
    profile = db.get_profile(profile_id)
    if profile is None:
        raise ValueError(f"profil {profile_id} introuvable")
    llm = llm or get_llm()
    try:
        reap_stale_runs()
    except Exception:  # noqa: BLE001 - bookkeeping only
        log.exception("could not reap stale runs")
    result = RunResult(run_id=db.create_run(profile_id), profile_id=profile_id, llm_enabled=llm.enabled)
    db.update_run(result.run_id, stats=result.stats_dict())
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
        jobs = db.load_jobs(profile_id)
        n_before = len(jobs)
        store_lock = asyncio.Lock()

        async def store(name: str, found: list[JobPosting]) -> None:
            """Dedupe `found` against everything known and write it now (one source query at a time)."""
            async with store_lock:
                merged, _ = merge_postings(found, jobs)
                jobs[:] = merged
                db.save_jobs(profile_id, jobs)
                result.unique_jobs = len(jobs)
                result.new_jobs = max(0, len(jobs) - n_before)
                db.update_run(result.run_id, stats=result.stats_dict())

        deadline = time.monotonic() + SEARCH_BUDGET_S
        await asyncio.gather(*(
            _run_source(n, s, result.plan, ctx, result, postings, progress, store, deadline) for n, s in chosen.items()
        ))
        result.raw_postings = len(postings)
        result.sources_ok = sorted(n for n in chosen if _source_ok(result.sources[n]))
        db.update_run(result.run_id, stats=result.stats_dict())
        before = list(postings)
        postings = await _enrich_details(postings, chosen, ctx, result)
        enriched = [p for p, old in zip(postings, before) if p is not old]
        if enriched:
            await store("details", enriched)

        # 3. DEDUPE: done progressively by `store` (against this run and everything stored for the profile)
        result.stage = "dedupe"
        await _emit(progress, result, event="stage")
        result.unique_jobs = len(jobs)
        result.new_jobs = max(0, len(jobs) - n_before)

        # 4. ORIGIN (stored chunk by chunk)
        result.stage = "origin"
        await _emit(progress, result, event="stage")

        def persist() -> None:
            db.save_jobs(profile_id, jobs, consolidate_origins=True)
            result.unique_jobs = len(jobs)
            result.new_jobs = max(0, len(jobs) - n_before)

        await _resolve_origins(jobs, ctx, result, persist=persist, profile=profile,
                               deadline=time.monotonic() + ORIGIN_BUDGET_S)
        persist()

        # 5. SCORE (stored batch by batch)
        result.stage = "score"
        await _emit(progress, result, event="stage")
        await _score_jobs(profile, jobs, llm, result, progress)
        result.llm_stats = getattr(llm, "stats", {})
        result.errors.extend(e for e in llm.errors if e not in result.errors)
        db.save_scores(profile_id, jobs)
        if await _linkedin_second_pass(jobs, ctx, result):
            db.save_jobs(profile_id, jobs, consolidate_origins=True)

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
        db.save_scores(profile_id, jobs)
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
    if result.prefiltered:
        db.save_scores(profile.id, jobs)
    to_llm.sort(key=lambda j: -keyword_score(profile, j).fit)  # best candidates first: they are stored first
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
        db.save_scores(profile.id, batch)  # one batch lost on interruption, never the run
        done += 1
        result.llm_stats = getattr(llm, "stats", {})
        await _emit(cb, result, event="score_batch", done=done, total=len(batches))

    await asyncio.gather(*(one(b) for b in batches))


# --------------------------------------------------------------------------------------------
# Re-resolve the origin (company's own posting) of jobs that are already stored
# --------------------------------------------------------------------------------------------

@dataclass
class OriginRow:
    job_id: int | None
    company: str
    title: str
    score: float
    before: str  # "missing" | "aggregator" | "low"
    found: bool
    via: str = ""
    reason: str = ""  # origin.REASON_LABELS key when not found
    detail: str = ""  # host / best candidate host (conf): never a full URL, never personal data
    host: str = ""  # host of the new origin when found
    has_apply_url: bool = True
    trace: list[str] = field(default_factory=list)  # ladder trace (company/title/host only), see origin.Trace


@dataclass
class OriginReport:
    profile_id: int
    min_score: float
    total: int = 0  # stored jobs at or above min_score
    with_origin_before: int = 0
    with_origin_after: int = 0
    tried: int = 0
    found: int = 0
    cleared: int = 0  # stored aggregator links removed and not replaced
    refetched: int = 0  # sightings whose detail page was fetched again to look for an apply link
    apply_urls_found: int = 0
    li_calls: int = 0  # LinkedIn detail provider: calls / employer links / Easy Apply / closed / cost / failures
    li_links: int = 0
    li_easy: int = 0
    li_closed: int = 0
    li_cost: float = 0.0
    li_failures: int = 0
    dry_run: bool = False
    rows: list[OriginRow] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def by_via(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for r in self.rows:
            if r.found:
                out[r.via or "inconnu"] = out.get(r.via or "inconnu", 0) + 1
        return dict(sorted(out.items(), key=lambda kv: -kv[1]))

    def by_reason(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for r in self.rows:
            if not r.found:
                out[r.reason or "no_candidate"] = out.get(r.reason or "no_candidate", 0) + 1
        return dict(sorted(out.items(), key=lambda kv: -kv[1]))


def origin_status(job: MergedJob, force: bool = False) -> str | None:
    """Why a stored job must be resolved again: "missing" | "aggregator" | "low" (only with force) | None."""
    from jobhunt import origin as origin_mod

    if not job.origin_url:
        return "missing"
    if not is_http_url(job.origin_url) or origin_mod.is_aggregator(job.origin_url):
        return "aggregator"
    if force and (job.origin_confidence or 0.0) < origin_mod.MIN_CONFIDENCE:
        return "low"
    return None


def _best_apply_url(job: MergedJob) -> str | None:
    from jobhunt import origin as origin_mod

    urls = [u for u in [*(s.apply_url for s in job.sightings), job.apply_url] if u]
    return next((u for u in urls if not origin_mod.is_aggregator(u)), urls[0] if urls else None)


async def _refetch_apply_urls(jobs: list[MergedJob], ctx: SourceContext, report: OriginReport, skip_sources: tuple[str, ...] = ()) -> None:
    """Sightings stored without apply_url (LinkedIn guest pages, ...): fetch their detail page again, with the source's own
    pacing, to pick up the off-site apply link / company website. One source at a time, sources in parallel."""
    reg = registry()
    by_source: dict[str, list[tuple[MergedJob, Any]]] = {}
    for job in jobs:
        for sighting in job.sightings:
            if (not sighting.apply_url and sighting.source in reg and sighting.source not in skip_sources
                    and not reg[sighting.source].requires_credentials):
                by_source.setdefault(sighting.source, []).append((job, sighting))

    async def run(name: str, entries: list[tuple[MergedJob, Any]]) -> None:
        src = reg[name]()
        for offset, (job, sighting) in enumerate(entries):
            if offset:
                await asyncio.sleep(src.detail_delay_s)
            base = job.to_posting().model_copy(update={"source": sighting.source, "source_id": sighting.source_id,
                                                      "url": sighting.url, "apply_url": None, "origin_url": None})
            try:
                enriched = await _bounded(src.fetch_details(base.model_copy(deep=True), ctx), DETAIL_TIMEOUT_S)
            except Exception as error:  # noqa: BLE001
                report.errors.append(f"Détails {name}: {type(error).__name__}")
                continue
            report.refetched += 1
            if enriched.apply_url and is_http_url(enriched.apply_url):
                sighting.apply_url = enriched.apply_url
                job.apply_url = job.apply_url or enriched.apply_url
                report.apply_urls_found += 1
                job.changed = True
            if enriched.apply_method not in ("unknown", None) and not job.extra.get("apply_method"):
                job.extra["apply_method"] = enriched.apply_method
                job.changed = True
            if enriched.company_website and not job.extra.get("company_website"):
                job.extra["company_website"] = enriched.company_website
                job.changed = True
            if enriched.description and len(enriched.description) > len(job.description or ""):
                job.description = enriched.description
                job.changed = True

    await asyncio.gather(*(run(n, e) for n, e in by_source.items()))


async def reresolve_origins(
    profile_id: int, *, min_score: float = 50.0, limit: int | None = None, force: bool = False, dry_run: bool = False,
    refetch: bool = True, headless: bool | None = True, progress: Callable[[int, int], Any] | None = None,
    detail_runner: Any = None,
) -> OriginReport:
    """Run the origin resolver again on stored jobs >= min_score whose origin is missing, is a job-board link
    (cleared when it cannot be replaced) or, with `force`, is below the confidence threshold. The miss cache and the
    3-day back-off are ignored. `dry_run` computes the report without writing anything."""
    from jobhunt import origin as origin_mod

    if db.get_profile(profile_id) is None:
        raise ValueError(f"profil {profile_id} introuvable")
    report = OriginReport(profile_id=profile_id, min_score=min_score, dry_run=dry_run)
    jobs = db.load_jobs(profile_id)
    eligible = [j for j in jobs if (j.score or 0) >= min_score]
    report.total = len(eligible)
    report.with_origin_before = sum(1 for j in eligible if origin_status(j) is None)
    todo = sorted((j for j in eligible if origin_status(j, force)), key=lambda j: -(j.score or 0))
    if limit:
        todo = todo[:limit]
    report.tried = len(todo)
    if not todo:
        report.with_origin_after = report.with_origin_before
        return report
    before = {id(j): origin_status(j, force) or "missing" for j in todo}
    snapshots = [deepcopy(job) for job in todo] if not dry_run else []
    for job in todo:
        if before[id(job)] == "aggregator":
            job.origin_url, job.origin_ats, job.origin_confidence = None, None, None
            for key in ("origin_fingerprint", "origin_posted_at", "origin_date_checked", "origin_via"):
                job.extra.pop(key, None)
            job.recompute_posted_at()
            job.changed = True

    browser = None
    try:
        from jobhunt.browser import BrowserPool
        browser = BrowserPool(headless=headless)
    except Exception as e:  # noqa: BLE001
        log.info("browser unavailable: %s", e)
    ctx = SourceContext(profile_id=profile_id, browser=browser)
    done = 0

    def tick(_res) -> None:
        nonlocal done
        done += 1
        if progress is not None:
            progress(done, len(todo))

    try:
        if refetch:
            from jobhunt.sources import linkedin_detail as ld

            provider = detail_runner is not None or ld.enabled(get_settings().linkedin_detail_cmd)
            if provider:
                stats = await enrich_linkedin_jobs(todo, runner=detail_runner, retry_checked=force)
                report.li_calls, report.li_links, report.li_easy = stats.calls, stats.links, stats.easy
                report.li_closed, report.li_cost, report.li_failures = stats.closed, stats.cost, stats.failures
                report.apply_urls_found += stats.links
            await _refetch_apply_urls(todo, ctx, report, skip_sources=("linkedin",) if provider else ())
        postings = []
        for job in todo:
            posting = origin_posting(job)
            if not posting.apply_url or origin_mod.is_aggregator(posting.apply_url):
                posting = posting.model_copy(update={"apply_url": _best_apply_url(job)})
            posting = _with_provenance(job, posting)
            postings.append(posting)
        results = await origin_mod.resolve_origins_detailed(postings, ctx, use_cache=False, on_done=tick)
    finally:
        if browser is not None:
            try:
                await browser.close()
            except Exception:  # noqa: BLE001
                pass

    stamp = db.now().isoformat()
    for job, res, posting in zip(todo, results, postings):
        found = job.apply_origin(res.posting)
        job.recompute_posted_at()
        if found:
            job.extra["origin_via"] = res.via or "inconnu"
            job.extra["origin_date_checked"] = stamp
        elif not job.origin_url:
            job.extra.pop("origin_via", None)
        job.extra["origin_attempted_at"] = stamp
        job.extra["origin_fingerprint"] = origin_fingerprint(job)
        job.extra.pop("origin_tried", None)
        job.changed = True
        if not found and before[id(job)] == "aggregator":
            report.cleared += 1
        reason, detail = (res.reason, res.detail) if not found else ("", "")
        report.rows.append(OriginRow(
            job_id=job.id, company=job.company or "", title=job.title, score=float(job.score or 0),
            before=before[id(job)], found=found, via=res.via if found else "", reason=reason, detail=detail,
            host=origin_mod._host(job.origin_url or "") if found else "",
            has_apply_url=bool(posting.apply_url), trace=list(res.trace)))
    report.found = sum(1 for r in report.rows if r.found)
    report.with_origin_after = sum(1 for j in eligible if origin_status(j) is None)
    if not dry_run:
        db.save_origin_updates(list(zip(snapshots, todo)))
    return report


# --------------------------------------------------------------------------------------------
# LinkedIn detail provider (employer apply link of "apply on company website" jobs)
# --------------------------------------------------------------------------------------------

@dataclass
class LinkedInStats:
    calls: int = 0
    links: int = 0
    easy: int = 0
    closed: int = 0
    cost: float = 0.0
    failures: int = 0


def _with_provenance(job: MergedJob, posting: JobPosting) -> JobPosting:
    """Mark an apply URL that came from LinkedIn's "apply on company website" field (see origin.Resolver.direct)."""
    if job.extra.get("apply_source") == "linkedin_offsite" and posting.apply_url and \
            posting.apply_url == job.extra.get("linkedin_apply_url"):
        return posting.model_copy(update={"raw": {**(posting.raw or {}), "apply_source": "linkedin_offsite"}})
    return posting


def origin_posting(job: MergedJob) -> JobPosting:
    return _with_provenance(job, job.to_posting())


async def enrich_linkedin_jobs(jobs: list[MergedJob], *, runner: Any = None, max_calls: int | None = None,
                               retry_checked: bool = False) -> LinkedInStats:
    """For jobs whose LinkedIn sightings have no apply_url, ask the detail provider for the employer link. Stores the link
    on the sighting/job, `company_website`, `apply_method` (external/easy_apply), `linkedin_state` and, for a closed
    job, `closed_at_source` (never deletes it). Never raises."""
    from jobhunt.sources import linkedin_detail as ld

    targets: list[tuple[MergedJob, Any]] = []
    for job in jobs:
        if job.apply_url or any(s.apply_url for s in job.sightings):
            continue
        if not retry_checked and job.extra.get("linkedin_state"):
            continue
        sighting = next((s for s in job.sightings if s.source == "linkedin" and ld.ID_RE.match(str(s.source_id))), None)
        if sighting is not None:
            targets.append((job, sighting))
    if max_calls is not None:
        targets = targets[:max(0, max_calls)]
    stats = LinkedInStats()
    if not targets:
        return stats
    ld.reset_usage()
    details = await asyncio.gather(*(ld.fetch_job_detail(s.source_id, runner=runner) for _, s in targets))
    for (job, sighting), d in zip(targets, details):
        if d is None:
            continue
        job.extra["linkedin_state"] = d.state
        if d.state == "closed":
            job.extra["closed_at_source"] = True
            stats.closed += 1
        elif d.state == "listed":
            job.extra.pop("closed_at_source", None)
        if d.apply_url:
            sighting.apply_url = d.apply_url
            job.apply_url = job.apply_url or d.apply_url
            job.extra.update(apply_method="external", apply_source="linkedin_offsite", linkedin_apply_url=d.apply_url)
            stats.links += 1
        elif d.easy_apply:
            job.extra["apply_method"] = "easy_apply"
            stats.easy += 1
        if d.company_website and not job.extra.get("company_website"):
            job.extra["company_website"] = d.company_website
        job.changed = True
    stats.calls, stats.cost, stats.failures = ld.USAGE.calls, round(ld.USAGE.cost, 6), ld.USAGE.failures
    return stats


def _store_origin(job: MergedJob, p: JobPosting) -> None:
    job.apply_origin(p)
    job.recompute_posted_at()
    if job.origin_url:
        job.extra["origin_date_checked"] = db.now().isoformat()
    job.extra["origin_attempted_at"] = db.now().isoformat()
    job.extra["origin_fingerprint"] = origin_fingerprint(job)
    job.extra.pop("origin_tried", None)
    job.changed = True


async def _linkedin_second_pass(jobs: list[MergedJob], ctx: SourceContext, result: RunResult) -> bool:
    """Runs AFTER scoring (the search resolves origins before scoring, so the detail provider, which costs money, is only
    called for jobs that matter): jobs at/above `apply_prepare_min_score` that come only from LinkedIn sightings without
    an apply link get their detail fetched (cap `linkedin_detail_max_per_run`), then a small second origin pass runs for
    the jobs that gained a link. Returns True when something changed."""
    from jobhunt.sources import linkedin_detail as ld

    settings = get_settings()
    cap = settings.linkedin_detail_max_per_run
    if cap <= 0 or not ld.enabled(settings.linkedin_detail_cmd):
        return False
    cand = [j for j in jobs if (j.score or 0) >= settings.apply_prepare_min_score and not j.origin_url and j.sightings
            and all(s.source == "linkedin" for s in j.sightings)]
    cand.sort(key=lambda j: -(j.score or 0))
    try:
        stats = await enrich_linkedin_jobs(cand, max_calls=cap)
    except Exception as e:  # noqa: BLE001
        log.warning("linkedin detail pass failed: %s", e)
        result.errors.append(f"Détails LinkedIn: {type(e).__name__}: {e}")
        return False

    result.linkedin_detail_calls, result.linkedin_links = stats.calls, stats.links
    got = [j for j in cand if j.apply_url and j.extra.get("apply_source") == "linkedin_offsite" and not j.origin_url]
    try:
        if got:
            mod = importlib.import_module("jobhunt.origin")
            resolved = await mod.resolve_origins([origin_posting(j) for j in got], ctx)
            for j, p in zip(got, resolved):
                _store_origin(j, p)
    except Exception as e:  # noqa: BLE001
        log.warning("linkedin follow-up origin resolution failed: %s", e)
        result.errors.append(f"Origines LinkedIn: {type(e).__name__}: {e}")
    return stats.calls > 0
