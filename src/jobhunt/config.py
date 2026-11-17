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
    france_travail_client_id: str | None = None
    france_travail_client_secret: str | None = None
    labonnealternance_api_key: str | None = None
    # Claude models. Default is Opus 5.5 everywhere; set JOBHUNT_SCORING_MODEL=claude-haiku-5-5
    # to make bulk scoring ~40x cheaper.
    planner_model: str = "claude-opus-5-5"
    scoring_model: str = "claude-opus-5-5"
    writer_model: str = "claude-opus-5-5"
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


@lru_cache
def get_settings() -> Settings:
    data_dir = Path(os.environ.get("JOBHUNT_DATA_DIR", Path(__file__).resolve().parents[2] / "data"))
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
        france_travail_client_id=env("FRANCE_TRAVAIL_CLIENT_ID"),
        france_travail_client_secret=env("FRANCE_TRAVAIL_CLIENT_SECRET"),
        labonnealternance_api_key=env("LABONNEALTERNANCE_API_KEY"),
        planner_model=env("JOBHUNT_PLANNER_MODEL", "claude-opus-5-5"),
        scoring_model=env("JOBHUNT_SCORING_MODEL", "claude-opus-5-5"),
        writer_model=env("JOBHUNT_WRITER_MODEL", "claude-opus-5-5"),
        headless=env("JOBHUNT_HEADLESS", "0") == "1",
    )
    for d in (s.browser_profiles_dir, s.cache_dir):
        d.mkdir(parents=True, exist_ok=True)
    return s
