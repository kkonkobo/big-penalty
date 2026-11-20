"""Runtime settings. Everything on disk lives under JOBHUNT_DATA_DIR (on the SSD)."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel, Field, field_validator


class Settings(BaseModel):
    data_dir: Path
    anthropic_api_key: str | None = Field(default=None, repr=False, exclude=True)
    openai_api_key: str | None = Field(default=None, repr=False, exclude=True)
    llm_providers: list[str] = Field(default_factory=lambda: ["anthropic", "openai"])
    openai_planner_model: str = "gpt-6-luna"
    openai_scoring_model: str = "gpt-6.1-sol"
    planner_effort: str = "medium"
    scoring_effort: str = "medium"
    openai_planner_effort: str = "low"
    openai_scoring_effort: str = "low"
    firecrawl_api_key: str | None = None
    # External command returning a LinkedIn job as JSON (JOBHUNT_LINKEDIN_DETAIL_CMD): "auto" = treg when installed,
    # "off" = disabled, else a template with {id} (split with shlex, never run through a shell).
    linkedin_detail_cmd: str = "auto"
    linkedin_detail_max_per_run: int = 150  # JOBHUNT_LINKEDIN_DETAIL_MAX_PER_RUN: detail calls of one search run
    france_travail_client_id: str | None = None
    france_travail_client_secret: str | None = None
    labonnealternance_api_key: str | None = None
    # Claude models. Default is Opus 5.5 everywhere; set JOBHUNT_SCORING_MODEL=claude-haiku-5-5
    # to make bulk scoring ~40x cheaper.
    planner_model: str = "claude-opus-5-5"
    scoring_model: str = "claude-opus-5-5"
    writer_model: str = "claude-opus-5-5"
    cv_model: str = "claude-opus-5-5"  # CV import + tailoring (JOBHUNT_CV_MODEL, defaults to the planner model)
    cv_effort: str = "medium"
    # CV data (master CV, job ad, tailored text) may be sent to the next provider when the first one fails.
    # JOBHUNT_CV_ALLOW_FAILOVER=0 keeps it with the FIRST enabled provider only (the verifier then uses it too).
    cv_allow_failover: bool = True
    # Overall deadline of ONE structured() call (all retries + provider failover), seconds (JOBHUNT_LLM_DEADLINE_S).
    llm_deadline_s: float = 180.0
    # One JSON line per LLM call (tokens, model, call site, estimated cost) in <data_dir>/logs/llm-usage.jsonl
    # (JOBHUNT_LLM_USAGE_LOG=0 disables it). Counts only, never prompt text: `jobhunt llm-usage` sums it up.
    llm_usage_log: bool = True
    # Milestone 2b: apply on the company's own ATS. apply_auto is the master switch of AUTOMATIC sending (off by default).
    apply_auto: bool = False  # JOBHUNT_APPLY_AUTO
    apply_daily: bool = False  # JOBHUNT_APPLY_DAILY: the daily run prepares CVs (and sends, if apply_auto) after the search
    apply_auto_min_score: float = 60  # JOBHUNT_APPLY_AUTO_MIN_SCORE
    apply_daily_cap: int = 40
    apply_autonomous: bool = True
    # JOBHUNT_APPLY_MAX_AGE_DAYS: oldest job (effective posting date) an application may still be sent to. The search and
    # the shortlist keep their own 14-day freshness window; this one only governs sending.
    apply_max_age_days: int = 14
    apply_prepare_min_score: float = 60  # JOBHUNT_APPLY_PREPARE_MIN_SCORE: the daily run prepares a CV for jobs at/above it
    apply_prepare_daily_cap: int = 15  # JOBHUNT_APPLY_PREPARE_DAILY_CAP
    apply_max_steps: int = 12  # JOBHUNT_APPLY_MAX_STEPS: pages per run
    apply_cv_retries: int = 2  # JOBHUNT_APPLY_CV_RETRIES: automatic CV regenerations per application when the revision has blocking errors
    apply_captcha_wait_s: float = 180  # JOBHUNT_APPLY_CAPTCHA_WAIT_S: a VISIBLE browser keeps the page open this long for a CAPTCHA
    headless: bool = False  # headful by default: lets a human solve CAPTCHAs and looks like a real user
    min_delay_s: float = 2.0  # per-domain minimum delay between requests
    max_delay_s: float = 6.0

    @field_validator("llm_providers", mode="before")
    @classmethod
    def validate_providers(cls, value):
        names = value.split(",") if isinstance(value, str) else value
        names = list(dict.fromkeys(name.strip().lower() for name in names if name.strip()))
        if any(name not in {"anthropic", "openai"} for name in names):
            raise ValueError("JOBHUNT_LLM_PROVIDERS accepts only anthropic,openai")
        return names

    @property
    def db_path(self) -> Path:
        return self.data_dir / "jobhunt.sqlite3"

    @property
    def browser_profiles_dir(self) -> Path:
        return self.data_dir / "browser-profiles"

    @property
    def cache_dir(self) -> Path:
        return self.data_dir / "cache"

    @property
    def profiles_dir(self) -> Path:
        return self.data_dir / "profiles"

    @property
    def applications_dir(self) -> Path:
        return self.data_dir / "applications"


def configured_data_dir() -> Path:
    return Path(os.environ.get("JOBHUNT_DATA_DIR", Path(__file__).resolve().parents[2] / "data"))


@lru_cache
def get_settings() -> Settings:
    data_dir = configured_data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    env = os.environ.get
    s = Settings(
        data_dir=data_dir,
        anthropic_api_key=env("ANTHROPIC_API_KEY"),
        openai_api_key=env("OPENAI_API_KEY"),
        llm_providers=env("JOBHUNT_LLM_PROVIDERS", "anthropic,openai"),
        openai_planner_model=env("JOBHUNT_OPENAI_PLANNER_MODEL", "gpt-6-luna"),
        openai_scoring_model=env("JOBHUNT_OPENAI_SCORING_MODEL", "gpt-6.1-sol"),
        planner_effort=env("JOBHUNT_PLANNER_EFFORT", "medium"),
        scoring_effort=env("JOBHUNT_SCORING_EFFORT", "medium"),
        openai_planner_effort=env("JOBHUNT_OPENAI_PLANNER_EFFORT", "low"),
        openai_scoring_effort=env("JOBHUNT_OPENAI_SCORING_EFFORT", "low"),
        firecrawl_api_key=env("FIRECRAWL_API_KEY"),
        linkedin_detail_cmd=(env("JOBHUNT_LINKEDIN_DETAIL_CMD") or "auto").strip(),
        linkedin_detail_max_per_run=int(env("JOBHUNT_LINKEDIN_DETAIL_MAX_PER_RUN") or 150),
        france_travail_client_id=env("FRANCE_TRAVAIL_CLIENT_ID"),
        france_travail_client_secret=env("FRANCE_TRAVAIL_CLIENT_SECRET"),
        labonnealternance_api_key=env("LABONNEALTERNANCE_API_KEY"),
        planner_model=env("JOBHUNT_PLANNER_MODEL", "claude-opus-5-5"),
        scoring_model=env("JOBHUNT_SCORING_MODEL", "claude-opus-5-5"),
        writer_model=env("JOBHUNT_WRITER_MODEL", "claude-opus-5-5"),
        cv_model=env("JOBHUNT_CV_MODEL") or env("JOBHUNT_PLANNER_MODEL", "claude-opus-5-5"),
        cv_effort=env("JOBHUNT_CV_EFFORT", "medium"),
        cv_allow_failover=env("JOBHUNT_CV_ALLOW_FAILOVER", "1").strip().lower() not in ("0", "false", "no", "off"),
        llm_deadline_s=float(env("JOBHUNT_LLM_DEADLINE_S") or 180),
        llm_usage_log=env("JOBHUNT_LLM_USAGE_LOG", "1").strip().lower() not in ("0", "false", "no", "off"),
        apply_auto=env("JOBHUNT_APPLY_AUTO", "0").strip().lower() in ("1", "true", "yes", "on"),
        apply_daily=env("JOBHUNT_APPLY_DAILY", "0").strip().lower() in ("1", "true", "yes", "on"),
        apply_auto_min_score=float(env("JOBHUNT_APPLY_AUTO_MIN_SCORE") or 60),
        apply_daily_cap=int(env("JOBHUNT_APPLY_DAILY_CAP") or 40),
        apply_autonomous=env("JOBHUNT_APPLY_AUTONOMOUS", "1").strip().lower() not in ("0", "false", "no", "off"),
        apply_max_age_days=int(env("JOBHUNT_APPLY_MAX_AGE_DAYS") or 14),
        apply_prepare_min_score=float(env("JOBHUNT_APPLY_PREPARE_MIN_SCORE") or 60),
        apply_prepare_daily_cap=int(env("JOBHUNT_APPLY_PREPARE_DAILY_CAP") or 15),
        apply_max_steps=int(env("JOBHUNT_APPLY_MAX_STEPS") or 12),
        apply_captcha_wait_s=float(env("JOBHUNT_APPLY_CAPTCHA_WAIT_S") or 180),
        apply_cv_retries=int(env("JOBHUNT_APPLY_CV_RETRIES") or 2),
        headless=env("JOBHUNT_HEADLESS", "0") == "1",
    )
    for d in (s.browser_profiles_dir, s.cache_dir):
        d.mkdir(parents=True, exist_ok=True)
    return s
