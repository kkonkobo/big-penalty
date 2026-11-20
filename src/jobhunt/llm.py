"""Official SDK providers, ordered failover, and deterministic planning/scoring fallbacks."""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import hashlib
import json
import logging
import re
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Any, Literal, Protocol, TypeVar

import anthropic
import openai
from pydantic import BaseModel, Field
from tenacity import AsyncRetrying, RetryCallState, RetryError, retry_if_exception, stop_after_attempt

from jobhunt.config import Settings, get_settings
from jobhunt.dedupe import MergedJob, strip_accents
from jobhunt.freshness import MAX_JOB_AGE_DAYS
from jobhunt.models import ContractType, Profile, SearchQuery
from jobhunt.scoring import JobScore, keyword_score

log = logging.getLogger("jobhunt.llm")
T = TypeVar("T", bound=BaseModel)

BETAS = ["server-side-fallback-2026-07-01"]
REMOTE_WORDS = {"remote", "teletravail", "télétravail", "full remote", "a distance", "à distance"}


class LLMError(Exception):
    """`code` is a short machine code (never model output or document text) safe to show in the UI."""

    def __init__(self, message: str = "", *, code: str = "error"):
        super().__init__(message)
        self.code = code


class LLMRefusal(LLMError):
    pass


# ----------------------------------------------------------------- usage accounting
# One JSON line per API call in <data_dir>/logs/llm-usage.jsonl: when, which call site, which model, how many tokens, and an
# estimated cost. Counts and hashes only: never prompt or output text. `jobhunt llm-usage` sums it up.
USAGE_FILE = "llm-usage.jsonl"
# USD per million tokens: input, output, cache read, 5-minute cache write (1.25x input). Estimates only (list prices
# 2026-11); the billing console is the source of truth. Unknown models are logged without a cost.
PRICES: dict[str, tuple[float, float, float, float]] = {
    "claude-opus-5-5": (4.0, 20.0, 0.20, 5.0),
    "claude-opus-5": (5.0, 25.0, 0.50, 6.25),
    "claude-sonnet-5-5": (2.0, 10.0, 0.10, 2.5),
    "claude-sonnet-5": (2.0, 10.0, 0.20, 2.5),
    "claude-haiku-5-5": (0.10, 0.50, 0.01, 0.125),
}
# Put this between two parts of a system prompt to give the first part its own prompt-cache breakpoint: a prefix shared by
# every request (the rules) stays readable from the cache when the second part (per-run data) changes. Never sent as text.
CACHE_BREAK = "\x00cache-breakpoint\x00"
_SITE: contextvars.ContextVar[str] = contextvars.ContextVar("jobhunt_llm_site", default="")


@contextlib.contextmanager
def llm_site(name: str) -> Iterator[None]:
    """Label the LLM calls made inside the block (e.g. "apply.planner") in the usage log."""
    token = _SITE.set(name)
    try:
        yield
    finally:
        _SITE.reset(token)


def current_site(default: str = "") -> str:
    return _SITE.get() or default


def estimate_cost(model: str, input_tokens: int, output_tokens: int, cache_read: int, cache_write: int) -> float | None:
    price = PRICES.get(model)
    if price is None:
        return None
    p_in, p_out, p_read, p_write = price
    return round((input_tokens * p_in + output_tokens * p_out + cache_read * p_read + cache_write * p_write) / 1e6, 6)


def prompt_sha(model: str, system_text: str, user_text: str) -> str:
    """Short fingerprint of a request: identical prompts share it (measures how often a plan cache would hit)."""
    return hashlib.sha256("\x00".join((model, system_text, user_text)).encode("utf-8")).hexdigest()[:16]


def record_usage(settings: Settings, *, provider: str, model: str, site: str, input_tokens: int, output_tokens: int,
                 cache_read: int, cache_write: int, sha: str = "", latency_s: float | None = None) -> dict[str, Any]:
    """Log one call (INFO on jobhunt.llm.usage) and append it to the usage file. Never raises: accounting must not break a run.
    `input_tokens` is the UNCACHED input (Anthropic semantics; OpenAI cached tokens are subtracted by the caller)."""
    row = {"ts": datetime.now(UTC).isoformat(timespec="seconds"), "provider": provider, "model": model, "site": site or "other",
           "input": input_tokens, "output": output_tokens, "cache_read": cache_read, "cache_write": cache_write,
           "cost_usd": estimate_cost(model, input_tokens, output_tokens, cache_read, cache_write), "sha": sha}
    if latency_s is not None:
        row["latency_s"] = round(latency_s, 3)
    log.info("llm usage site=%s model=%s in=%d out=%d cache_read=%d cache_write=%d cost_usd=%s", row["site"], model,
             input_tokens, output_tokens, cache_read, cache_write, row["cost_usd"])
    if getattr(settings, "llm_usage_log", True):
        try:
            path = Path(settings.data_dir) / "logs" / USAGE_FILE
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        except OSError as exc:
            log.warning("llm usage log not written: %s", type(exc).__name__)
    return row


def read_usage(path: Path, since: datetime | None = None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.exists():
        return rows
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if since is not None:
            try:
                if datetime.fromisoformat(row.get("ts", "")) < since:
                    continue
            except ValueError:
                continue
        rows.append(row)
    return rows


def summarize_usage(rows: list[dict[str, Any]], key: tuple[str, ...] = ("site", "model")) -> list[dict[str, Any]]:
    """Totals per (site, model), most expensive first. `repeats` = calls whose exact prompt was already sent before."""
    groups: dict[tuple, dict[str, Any]] = {}
    seen: set[str] = set()
    for row in rows:
        k = tuple(row.get(name, "") for name in key)
        g = groups.setdefault(k, {**dict(zip(key, k)), "calls": 0, "input": 0, "output": 0, "cache_read": 0,
                                   "cache_write": 0, "cost_usd": 0.0, "repeats": 0})
        g["calls"] += 1
        for name in ("input", "output", "cache_read", "cache_write"):
            g[name] += int(row.get(name) or 0)
        g["cost_usd"] += float(row.get("cost_usd") or 0)
        sha = row.get("sha")
        if sha:
            g["repeats"] += sha in seen
            seen.add(sha)
    return sorted(groups.values(), key=lambda g: (-g["cost_usd"], -g["calls"]))


# ----------------------------------------------------------------- schemas
class PlannedQuery(BaseModel):
    keywords: str = Field(description="Search keywords as typed in a job board search box")
    language: Literal["fr", "en"] = "fr"
    contract_types: list[ContractType] = Field(default_factory=list, description="Empty = use the profile's contracts")
    rome_codes: list[str] = Field(default_factory=list, description="France Travail ROME codes like H1208, M1805")


class Plan(BaseModel):
    queries: list[PlannedQuery]


class LLMJobScore(BaseModel):
    ref: int
    fit: int = Field(description="0-100 fit with the profile")
    matches_function: bool
    rationale: str = Field(description="1-2 sentences in French")
    flags: list[Literal["wrong_contract", "too_senior", "location_mismatch", "language", "other"]] = Field(default_factory=list)


class ScoreBatch(BaseModel):
    scores: list[LLMJobScore]


# ----------------------------------------------------------------- prompts
PROFILE_SYSTEM = """You are the planning and ranking engine of a personal job-search tool for the French job market.
The user is looking for work on behalf of a family member. The profile describes a FUNCTION or MAJOR, not a job title:
you must reason about which real job titles, keywords and ROME codes (France Travail) correspond to it.
Write all user-facing text (rationales) in French. Be concrete and brief.

=== PROFILE ===
{profile}
"""

PLAN_TASK = """Task: expand the profile into at most {n} diverse job-board searches ordered from most to least relevant.
- Mix French and English keywords (language field), adjacent roles, typical titles AND tools/technologies (e.g. for embedded:
  "ingénieur systèmes embarqués", "développeur firmware", "stage électronique embarquée", "automatisme", "robotique", "C embarqué", "STM32").
- Short keywords (1-4 words); no boolean operators, no location in keywords.
- Prefix with the contract wording when it helps ("stage ...", "alternance ...") and set contract_types accordingly
  (values: cdi, cdd, interim, alternance, stage, freelance, vie). Leave empty to use the profile's contracts.
- Add 0-3 relevant ROME codes on the queries where they fit (e.g. H1208, H1206, M1805).
- No duplicates; no job titles clearly above the profile's seniority."""

SCORE_TASK = """Task: score each job below against the profile.
For each job return ref (as given), fit 0-100, matches_function (does the job fall within the function/major the profile describes,
even if the title differs), a rationale of 1-2 short sentences IN FRENCH, and flags among:
wrong_contract (contract type not wanted), too_senior (requires clearly more professional EXPERIENCE than the profile has,
e.g. 5+ years, senior/lead role), location_mismatch (not near the wanted locations and not remote),
language (the job is clearly conducted in a language the profile lacks), other.
Requirements in job ads are written by HR and are a WISH LIST, not a filter. The ONLY hard blockers are the contract type
(wrong_contract) and the location (location_mismatch): penalise those strongly. Everything else (tools, skills, languages
beyond the basics, education level) is a wish list: weigh function fit first; missing nice-to-have items cost little.
Education level: compare the level the job asks for (Bac+N, "4e année", "M1", "M2", "stage de fin d'études"/PFE, "césure",
"assistant ingénieur") with the profile's CURRENT level from education/headline/notes. A gap (e.g. a 6-month PFE reserved
for Bac+5 when the profile is Bac+4, or a Bac+2 technician internship for a Bac+4 profile) lowers the score MODERATELY,
roughly 10-20 points depending on how explicit and strict the requirement is, and must be mentioned in the rationale. It must
NOT cap the score and must NOT by itself set too_senior. A "language" flag is a soft penalty unless the job is clearly
conducted in a language the profile lacks.
Scale: 85+ = excellent match, 60-84 = good, 35-59 = partial, <35 = poor. Never invent facts: if the description is
missing, say so and score cautiously. Return exactly one entry per job."""


def profile_text(p: Profile) -> str:
    d = p.model_dump(exclude={"id", "email", "phone"}, exclude_none=True)
    d["contract_types"] = [c.value for c in p.contract_types]
    if d.get("cv_text"):
        d["cv_text"] = d["cv_text"][:6000]
    return json.dumps({k: v for k, v in d.items() if v not in ([], "")}, ensure_ascii=False, indent=1)


def system_blocks(system_text: str) -> list[dict[str, Any]]:
    """Anthropic system blocks: one cache breakpoint at the end of each CACHE_BREAK-separated part (the API allows 4)."""
    parts = [part for part in system_text.split(CACHE_BREAK) if part][-4:] or [""]
    return [{"type": "text", "text": part, "cache_control": {"type": "ephemeral"}} for part in parts]


def _int(value: Any) -> int:
    return value if isinstance(value, int) else 0


def _retryable(exc: BaseException) -> bool:
    if isinstance(exc, (anthropic.RateLimitError, anthropic.APIConnectionError)):
        return True
    return isinstance(exc, anthropic.APIStatusError) and (exc.status_code >= 500 or exc.status_code in (408, 409, 429))


# ----------------------------------------------------------------- planner helpers (pure)
def _is_remote(loc: str) -> bool:
    return strip_accents(loc.lower()).strip() in {strip_accents(w) for w in REMOTE_WORDS}


def build_queries(profile: Profile, planned: list[PlannedQuery], max_queries: int) -> list[SearchQuery]:
    """Turn planned keyword variants into concrete, deduplicated, capped SearchQuery objects."""
    locs = [l for l in profile.locations if not _is_remote(l)]
    wants_remote = any(_is_remote(l) for l in profile.locations)
    pcts = list(dict.fromkeys(profile.contract_types))

    def contracts(pq: PlannedQuery) -> list[ContractType]:
        if pq.contract_types and pcts:
            inter = [c for c in pq.contract_types if c in pcts]
            return inter or pcts
        return list(pq.contract_types or pcts)

    def mk(pq: PlannedQuery, loc: str | None, remote: bool = False) -> SearchQuery:
        return SearchQuery(
            keywords=pq.keywords.strip(), rome_codes=list(dict.fromkeys(pq.rome_codes)), location=loc,
            radius_km=profile.radius_km, contract_types=contracts(pq), remote_only=remote,
            language=pq.language, posted_within_days=MAX_JOB_AGE_DAYS,
        )

    seen_kw: set[str] = set()
    uniq: list[PlannedQuery] = []
    for pq in planned:
        k = strip_accents(pq.keywords.lower()).strip()
        if k and k not in seen_kw:
            seen_kw.add(k)
            uniq.append(pq)

    n_remote = min(2, len(uniq), max_queries // 6) if wants_remote else 0
    n_extra = min(len(locs) - 1, 4) * 3 if len(locs) > 1 else 0
    n_extra = min(n_extra, max_queries // 3)
    n_primary = max(1, max_queries - n_extra - n_remote)
    out: list[SearchQuery] = [mk(pq, locs[0] if locs else None) for pq in uniq[:n_primary]]
    extras = [mk(pq, loc) for loc in locs[1:] for pq in uniq[:3]]
    out += extras[:n_extra]
    if wants_remote:
        out += [mk(pq, None, True) for pq in uniq[:n_remote]]
    seen: set[tuple] = set()
    final = []
    for q in out:
        key = (strip_accents(q.keywords.lower()), q.location, q.remote_only, tuple(q.contract_types))
        if key not in seen:
            seen.add(key)
            final.append(q)
    return final[:max_queries]


SYNONYMS: dict[str, list[str]] = {
    "embarque": ["systèmes embarqués", "firmware", "électronique embarquée", "C embarqué", "STM32", "microcontrôleur", "embedded software"],
    "mecatronique": ["mécatronique", "automatisme", "robotique", "systèmes embarqués", "mechatronics"],
    "robotique": ["robotique", "robotics engineer", "automatisme", "ROS"],
    "electronique": ["électronique", "conception électronique", "hardware engineer"],
    "automatisme": ["automatisme", "automaticien", "PLC", "supervision industrielle"],
    "bas niveau": ["C embarqué", "firmware", "développeur C/C++", "systèmes embarqués"],
    "informatique": ["développeur logiciel", "ingénieur logiciel", "software engineer"],
    "developpeur": ["développeur", "software engineer", "ingénieur développement"],
    "data": ["data analyst", "data scientist", "data engineer", "analyse de données"],
    "ia": ["intelligence artificielle", "machine learning", "data scientist"],
    "cyber": ["cybersécurité", "analyste SOC", "pentester", "security engineer"],
    "reseau": ["ingénieur réseau", "administrateur systèmes et réseaux", "network engineer"],
    "marketing": ["marketing digital", "chargé de marketing", "community manager", "growth marketing"],
    "commerce": ["commercial", "business developer", "ingénieur d'affaires", "sales"],
    "finance": ["analyste financier", "contrôleur de gestion", "finance d'entreprise", "audit"],
    "comptab": ["comptable", "assistant comptable", "gestionnaire de paie"],
    "rh": ["ressources humaines", "chargé de recrutement", "assistant RH", "talent acquisition"],
    "mecanique": ["ingénieur mécanique", "conception mécanique", "bureau d'études", "CAO SolidWorks"],
    "logistique": ["logistique", "supply chain", "gestionnaire d'approvisionnement"],
    "design": ["designer", "UX designer", "UI designer", "graphiste"],
    "infirm": ["infirmier", "infirmière", "soins infirmiers"],
}
ROME_HINTS: dict[str, list[str]] = {
    "embarque": ["H1206", "M1805"], "mecatronique": ["H1206", "H1208"], "robotique": ["H1206", "H1208"],
    "automatisme": ["H1208"], "informatique": ["M1805"], "developpeur": ["M1805"], "data": ["M1805", "M1403"],
    "electronique": ["H1206"], "mecanique": ["H1206"],
}
CONTRACT_PREFIX = {ContractType.STAGE: "stage", ContractType.ALTERNANCE: "alternance"}


def rule_based_plan(profile: Profile, max_queries: int) -> list[PlannedQuery]:
    base: list[str] = []
    text = strip_accents(" ".join([profile.headline, *profile.interests, *profile.skills]).lower())
    for i in profile.interests:
        base.append(i.strip())
    for key, syns in SYNONYMS.items():
        if re.search(r"\b" + re.escape(key), text):
            base += syns
    if not base:  # last resort: the headline minus the filler
        base.append(re.sub(r"\b(étudiant|etudiant|[0-9]+(e|ème|eme)? année|en|de|d')\b", " ", profile.headline, flags=re.I).strip() or profile.headline)
    romes = list(dict.fromkeys(r for k, rs in ROME_HINTS.items() if re.search(r"\b" + k, text) for r in rs))[:3]
    out: list[PlannedQuery] = []
    cts = profile.contract_types
    prefixes = [CONTRACT_PREFIX[c] for c in cts if c in CONTRACT_PREFIX]
    for kw in dict.fromkeys(base):
        out.append(PlannedQuery(keywords=kw, rome_codes=romes))
    for pre in prefixes:  # "stage X" variants for the first keywords
        for kw in list(dict.fromkeys(base))[:6]:
            out.append(PlannedQuery(keywords=f"{pre} {kw}", rome_codes=romes,
                                    contract_types=[c for c, p in CONTRACT_PREFIX.items() if p == pre]))
    # interleave plain and prefixed so the cap keeps a mix
    plain = [q for q in out if q.keywords.split(" ")[0] not in prefixes]
    pref = [q for q in out if q not in plain]
    mixed: list[PlannedQuery] = []
    for i in range(max(len(plain), len(pref))):
        if i < len(plain):
            mixed.append(plain[i])
        if i < len(pref):
            mixed.append(pref[i])
    return mixed


# ----------------------------------------------------------------- client
class AnthropicProvider:
    name = "anthropic"

    def __init__(self, settings: Settings | None = None, client: Any | None = None, *, retry_base_s: float = 2.0,
                 max_attempts: int = 5):
        self.settings = settings or get_settings()
        if client is None and self.settings.anthropic_api_key:
            client = anthropic.AsyncAnthropic(api_key=self.settings.anthropic_api_key, max_retries=0, timeout=180)
        self.client = client
        self.retry_base_s = retry_base_s
        self.max_attempts = max_attempts
        self.errors: list[str] = []
        self.usage = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "calls": 0}

    @property
    def enabled(self) -> bool:
        return self.client is not None

    # ---- low level
    def _wait(self, rs: RetryCallState) -> float:
        exc = rs.outcome.exception() if rs.outcome else None
        if self.retry_base_s <= 0:
            return 0
        if isinstance(exc, anthropic.APIStatusError):
            ra = exc.response.headers.get("retry-after") if getattr(exc, "response", None) is not None else None
            try:
                if ra:
                    return min(float(ra), 60)
            except ValueError:
                pass
        return min(self.retry_base_s * 2 ** (rs.attempt_number - 1), 60)

    async def _parse(self, model_cls: type[T], *, model: str, system_text: str, user_text: str, effort: str,
                     max_tokens: int) -> T:
        assert self.client is not None
        started = perf_counter()
        try:
            async for attempt in AsyncRetrying(
                stop=stop_after_attempt(self.max_attempts), wait=self._wait,
                retry=retry_if_exception(_retryable), reraise=True,
            ):
                with attempt:
                    resp = await self.client.beta.messages.parse(
                        model=model, max_tokens=max_tokens, betas=BETAS, fallbacks="default",
                        output_format=model_cls,
                        system=system_blocks(system_text),
                        messages=[{"role": "user", "content": user_text}],
                        output_config={"effort": effort},
                    )
        except anthropic.RateLimitError as e:
            raise LLMError(f"Limite de débit Anthropic atteinte après {self.max_attempts} essais", code="rate_limit") from e
        except anthropic.APIConnectionError as e:
            raise LLMError("Connexion à l'API Anthropic impossible", code="connection") from e
        except anthropic.APIStatusError as e:
            raise LLMError(f"Erreur API Anthropic {e.status_code}", code=f"http_{e.status_code}") from e
        except (ValueError, anthropic.APIResponseValidationError) as e:  # never embed e: it may quote the model output
            raise LLMError("Réponse structurée Anthropic invalide", code="invalid_response") from e
        u = getattr(resp, "usage", None)
        if u is not None:
            tokens = {name: _int(getattr(u, attr, 0)) for name, attr in (
                ("input", "input_tokens"), ("output", "output_tokens"), ("cache_read", "cache_read_input_tokens"),
                ("cache_write", "cache_creation_input_tokens"))}
            self.usage["calls"] += 1
            for name, value in tokens.items():
                self.usage[name] += value
            served = getattr(resp, "model", None)
            record_usage(self.settings, provider=self.name, model=served if isinstance(served, str) and served else model,
                         site=current_site(model_cls.__name__), input_tokens=tokens["input"], output_tokens=tokens["output"],
                         cache_read=tokens["cache_read"], cache_write=tokens["cache_write"],
                         sha=prompt_sha(model, system_text, user_text), latency_s=perf_counter() - started)
        if resp.stop_reason == "refusal":
            raise LLMRefusal("Le modèle a refusé de répondre")
        if resp.stop_reason == "max_tokens":
            raise LLMError("Réponse Anthropic tronquée (max_tokens)")
        out = resp.parsed_output
        if out is None:
            raise LLMError(f"Réponse non structurée (stop_reason={resp.stop_reason})")
        return out

    # ---- planner
    async def plan(self, profile: Profile, max_queries: int = 25) -> Plan:
        if not self.enabled:
            raise LLMError(f"{self.name}: clé absente")
        with llm_site("search.plan"):
            return await self._parse(
                Plan, model=self.settings.planner_model,
                system_text=PROFILE_SYSTEM.format(profile=profile_text(profile)),
                user_text=PLAN_TASK.format(n=max_queries), effort=self.settings.planner_effort, max_tokens=4000,
            )

    # ---- generic structured call (CV import / tailoring)
    async def structured(self, model_cls: type[T], *, system_text: str, user_text: str, max_tokens: int = 8000) -> T:
        if not self.enabled:
            raise LLMError(f"{self.name}: clé absente")
        model, effort = self.model_for(current_site())
        return await self._parse(model_cls, model=model, system_text=system_text, user_text=user_text, effort=effort,
                                 max_tokens=max_tokens)

    def model_for(self, site: str) -> tuple[str, str]:
        """(model, effort) of a structured call: the apply-form planner has its own pair, every other call uses the CV pair."""
        if site.startswith("apply.planner"):
            return self.settings.apply_planner_model, self.settings.apply_planner_effort
        return self.settings.cv_model, self.settings.cv_effort

    # ---- scorer
    async def score(self, profile: Profile, jobs: list[MergedJob]) -> ScoreBatch:
        if not self.enabled:
            raise LLMError(f"{self.name}: clé absente")
        items = [
            {
                "ref": i, "title": j.title, "company": j.company, "location": j.location,
                "contract": j.contract_type.value, "remote": j.remote, "salary": j.salary_text,
                "posted": j.posted_at.date().isoformat() if j.posted_at else None,
                "description": (j.description or "")[:1800],
            }
            for i, j in enumerate(jobs)
        ]
        with llm_site("search.score"):
            return await self._parse(
                ScoreBatch, model=self.settings.scoring_model,
                system_text=PROFILE_SYSTEM.format(profile=profile_text(profile)) + "\n" + SCORE_TASK,
                user_text="Jobs:\n" + json.dumps(items, ensure_ascii=False), effort=self.settings.scoring_effort,
                max_tokens=700 * len(jobs) + 500,
            )


class LLMProvider(Protocol):
    name: str
    enabled: bool
    usage: dict[str, int]

    async def plan(self, profile: Profile, max_queries: int = 25) -> Plan: ...

    async def score(self, profile: Profile, jobs: list[MergedJob]) -> ScoreBatch: ...


def _openai_retryable(exc: BaseException) -> bool:
    return isinstance(exc, openai.APIConnectionError) or (
        isinstance(exc, openai.APIStatusError) and (exc.status_code >= 500 or exc.status_code in (408, 409, 429))
    )


class OpenAIProvider(AnthropicProvider):
    name = "openai"

    def __init__(self, settings: Settings | None = None, client: Any | None = None, **kwargs):
        settings = settings or get_settings()
        if client is None and settings.openai_api_key:
            client = openai.AsyncOpenAI(api_key=settings.openai_api_key, max_retries=0, timeout=180)
        provider_settings = settings.model_copy(update={
            "anthropic_api_key": None,
            "planner_model": settings.openai_planner_model, "scoring_model": settings.openai_scoring_model,
            "planner_effort": settings.openai_planner_effort, "scoring_effort": settings.openai_scoring_effort,
            "cv_model": settings.openai_planner_model, "cv_effort": settings.openai_planner_effort,
            "apply_planner_model": settings.openai_planner_model, "apply_planner_effort": settings.openai_planner_effort,
        })
        super().__init__(provider_settings, client, **kwargs)

    def _wait(self, rs: RetryCallState) -> float:
        exc = rs.outcome.exception() if rs.outcome else None
        if self.retry_base_s > 0 and isinstance(exc, openai.APIStatusError):
            try:
                return max(0, min(float(exc.response.headers["retry-after"]), 60))
            except (KeyError, ValueError):
                pass
        return super()._wait(rs)

    async def _parse(self, model_cls: type[T], *, model: str, system_text: str, user_text: str, effort: str,
                     max_tokens: int) -> T:
        if self.client is None:
            raise LLMError("OpenAI: clé absente")
        options = {}
        if effort and model.startswith(("gpt-5", "gpt-6", "o1", "o3", "o4")):
            options["reasoning"] = {"effort": effort}
        started = perf_counter()
        try:
            async for attempt in AsyncRetrying(
                stop=stop_after_attempt(self.max_attempts), wait=self._wait,
                retry=retry_if_exception(_openai_retryable), reraise=True,
            ):
                with attempt:
                    response = await self.client.responses.parse(
                        model=model, instructions=system_text.replace(CACHE_BREAK, ""), input=user_text, text_format=model_cls,
                        max_output_tokens=max_tokens, store=False, **options,
                    )
        except openai.APIStatusError as exc:
            raise LLMError(f"Erreur API OpenAI {exc.status_code}", code=f"http_{exc.status_code}") from exc
        except openai.APIConnectionError as exc:
            raise LLMError("Connexion à l'API OpenAI impossible (délai ou réseau)", code="connection") from exc
        except (ValueError, openai.APIResponseValidationError) as exc:
            raise LLMError("Réponse structurée OpenAI invalide", code="invalid_response") from exc
        usage = response.usage
        if usage:
            self.usage["calls"] += 1
            self.usage["input"] += usage.input_tokens
            self.usage["output"] += usage.output_tokens
            self.usage["cache_read"] += getattr(usage.input_tokens_details, "cached_tokens", 0) or 0
            self.usage["cache_write"] += getattr(usage.input_tokens_details, "cache_write_tokens", 0) or 0
            details = getattr(usage.input_tokens_details, "cache_write_tokens_details", None)
            if details is not None:
                self.usage["cache_write_12h"] = self.usage.get("cache_write_12h", 0) + (getattr(details, "ttl_12h_tokens", 0) or 0)
            cached = _int(getattr(usage.input_tokens_details, "cached_tokens", 0))
            written = _int(getattr(usage.input_tokens_details, "cache_write_tokens", 0))
            record_usage(self.settings, provider=self.name, model=model, site=current_site(model_cls.__name__),
                         input_tokens=max(0, _int(usage.input_tokens) - cached), output_tokens=_int(usage.output_tokens),
                         cache_read=cached, cache_write=written, sha=prompt_sha(model, system_text, user_text),
                         latency_s=perf_counter() - started)
        for item in response.output or []:
            for content in getattr(item, "content", []) or []:
                if content.type == "refusal":
                    raise LLMRefusal("OpenAI a refusé de répondre")
        if response.status != "completed":
            raise LLMError(f"Réponse OpenAI non terminée ({response.status})")
        if not isinstance(response.output_parsed, model_cls):
            raise LLMError("Réponse structurée OpenAI absente")
        return response.output_parsed


class FailoverLLM:
    def __init__(self, settings: Settings | None = None, client: Any | None = None, *,
                 providers: list[LLMProvider] | None = None, retry_base_s: float = 2.0, max_attempts: int = 5):
        self.settings = settings or get_settings()
        factories = {"anthropic": AnthropicProvider, "openai": OpenAIProvider}
        if providers is not None:
            self.providers = providers
        elif client is not None:
            self.providers = [AnthropicProvider(self.settings, client, retry_base_s=retry_base_s,
                                                max_attempts=max_attempts)]
        else:
            self.providers = [factories[name](self.settings, retry_base_s=retry_base_s, max_attempts=max_attempts)
                              for name in self.settings.llm_providers]
        self.errors: list[str] = []
        self.calls: list[dict[str, Any]] = []

    @property
    def enabled(self) -> bool:
        return any(provider.enabled for provider in self.providers)

    @property
    def usage(self) -> dict[str, int]:
        return {key: sum(provider.usage.get(key, 0) for provider in self.providers)
                for key in ("input", "output", "cache_read", "cache_write", "calls")}

    @property
    def stats(self) -> dict[str, Any]:
        return {"calls": list(self.calls), "usage": {provider.name: dict(provider.usage) for provider in self.providers}}

    def _error(self, provider: str, exc: Exception) -> str:
        message = str(exc) if isinstance(exc, LLMError) else type(exc).__name__
        for secret in (self.settings.anthropic_api_key, self.settings.openai_api_key):
            if secret:
                message = message.replace(secret, "[redacted]")
        return f"{provider}: {message}"

    async def _call(self, task: str, profile: Profile, argument: Any):
        for provider in self.providers:
            if not provider.enabled:
                continue
            started = perf_counter()
            try:
                output = await getattr(provider, task)(profile, argument)
                if task == "plan" and not any(query.keywords.strip() for query in output.queries):
                    raise LLMError("Plan LLM vide")
                if task == "score":
                    refs = [score.ref for score in output.scores]
                    if len(refs) != len(set(refs)) or any(ref < 0 or ref >= len(argument) for ref in refs):
                        raise LLMError("Références de scores invalides")
                    if len(refs) != len(argument) and len(self.providers) > 1:
                        raise LLMError("Scores manquants")
                self.calls.append({"task": task, "provider": provider.name, "status": "ok",
                                   "latency_s": round(perf_counter() - started, 4)})
                return provider.name, output
            except (LLMError, anthropic.APIError, openai.APIError, RetryError, TimeoutError) as exc:
                self.errors.append(self._error(provider.name, exc))
                self.calls.append({"task": task, "provider": provider.name, "status": "error",
                                   "latency_s": round(perf_counter() - started, 4)})
        self.calls.append({"task": task, "provider": "rules", "status": "ok", "latency_s": 0})
        return "rules", None

    async def plan(self, profile: Profile, max_queries: int = 25) -> list[SearchQuery]:
        _, output = await self._call("plan", profile, max_queries)
        planned = output.queries if output else rule_based_plan(profile, max_queries)
        return build_queries(profile, planned, max_queries)

    async def score(self, profile: Profile, jobs: list[MergedJob]) -> list[JobScore]:
        if not jobs:
            return []
        provider, output = await self._call("score", profile, jobs)
        by_ref = {score.ref: score for score in output.scores} if output else {}
        if output is not None and len(by_ref) < len(jobs):
            self.errors.append("Scoring: scores manquants, mots-clés utilisés pour les offres sans score")
            self.calls.append({"task": "score", "provider": "rules", "status": "ok", "latency_s": 0,
                               "jobs": len(jobs) - len(by_ref)})
        return [JobScore(fit=by_ref[index].fit, matches_function=by_ref[index].matches_function,
                         rationale=by_ref[index].rationale, flags=by_ref[index].flags,
                         origin="llm", scored_by=provider).clamp()
                if index in by_ref else keyword_score(profile, job) for index, job in enumerate(jobs)]

    def _cv_providers(self, prefer: str | None) -> list[LLMProvider]:
        """Providers allowed to receive CV data, in call order. `prefer` moves one provider first (verifier: a different
        model than the tailor). With JOBHUNT_CV_ALLOW_FAILOVER=0 only the first enabled provider (configured order) is used
        and `prefer` is ignored: the CV never reaches a provider the user did not rank first."""
        usable = [p for p in self.providers if p.enabled and hasattr(p, "structured")]
        if not self.settings.cv_allow_failover:
            return usable[:1]
        if prefer:
            usable.sort(key=lambda p: 0 if p.name == prefer else 1)  # stable: keeps the configured order otherwise
        return usable

    @staticmethod
    def _code(exc: BaseException) -> str:
        if isinstance(exc, LLMError):
            return exc.code
        if isinstance(exc, anthropic.RateLimitError | openai.RateLimitError):
            return "rate_limit"
        if isinstance(exc, anthropic.APIConnectionError | openai.APIConnectionError | TimeoutError):
            return "connection"
        if isinstance(exc, anthropic.APIStatusError | openai.APIStatusError):
            return f"http_{exc.status_code}"
        return "error"

    async def structured_ex(self, model_cls: type[T], *, system_text: str, user_text: str, max_tokens: int = 8000,
                            prefer: str | None = None) -> tuple[dict[str, str], T]:
        """One structured call with provider failover and ONE overall deadline (settings.llm_deadline_s, retries and
        failover included). Returns ({"provider", "model"}, output). No rule-based fallback: raises LLMError with a short
        sanitized message (provider names + codes, never model output or document text)."""
        providers = self._cv_providers(prefer)
        tried: list[str] = []
        try:
            async with asyncio.timeout(self.settings.llm_deadline_s):
                for provider in providers:
                    started = perf_counter()
                    try:
                        output = await provider.structured(model_cls, system_text=system_text, user_text=user_text,
                                                           max_tokens=max_tokens)
                    except (LLMError, anthropic.APIError, openai.APIError, RetryError, TimeoutError) as exc:
                        code = self._code(exc.last_attempt.exception() if isinstance(exc, RetryError) and exc.last_attempt else exc)
                        tried.append(f"{provider.name}: {code}")
                        self.errors.append(f"{provider.name}: {code}")
                        self.calls.append({"task": "structured", "provider": provider.name, "status": "error",
                                           "latency_s": round(perf_counter() - started, 4)})
                        continue
                    self.calls.append({"task": "structured", "provider": provider.name, "status": "ok",
                                       "latency_s": round(perf_counter() - started, 4)})
                    model_for = getattr(provider, "model_for", None)
                    model = model_for(current_site())[0] if model_for else getattr(
                        getattr(provider, "settings", None), "cv_model", "") or ""
                    return {"provider": provider.name, "model": model}, output
        except TimeoutError:
            self.errors.append("deadline")
            raise LLMError(f"Délai dépassé ({self.settings.llm_deadline_s:.0f} s) pour l'appel IA", code="deadline") from None
        detail = "; ".join(tried) or "aucun fournisseur IA configuré"
        raise LLMError(f"Appel IA impossible ({detail})", code="unavailable")

    async def structured(self, model_cls: type[T], *, system_text: str, user_text: str, max_tokens: int = 8000) -> T:
        """`structured_ex` without the provider info."""
        _, output = await self.structured_ex(model_cls, system_text=system_text, user_text=user_text, max_tokens=max_tokens)
        return output

    async def check(self) -> list[dict[str, Any]]:
        results = []
        for provider in self.providers:
            started = perf_counter()
            status = "missing_key"
            if provider.enabled:
                try:
                    await provider.plan(Profile(name="Diagnostic", headline="Développeur Python"), 1)
                    status = "ok"
                except (LLMError, anthropic.APIError, openai.APIError, RetryError, TimeoutError) as exc:
                    status = self._error(provider.name, exc)
            results.append({"provider": provider.name, "status": status,
                            "latency_s": round(perf_counter() - started, 3)})
        return results

    async def aclose(self) -> None:
        for provider in self.providers:
            client = getattr(provider, "client", None)
            if client is not None and hasattr(client, "close"):
                await client.close()


LLMClient = FailoverLLM


def get_llm() -> LLMClient:
    """Fresh client per run (the error list is per run)."""
    return FailoverLLM()
