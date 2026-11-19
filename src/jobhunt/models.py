"""Shared data contracts. Every source connector returns `JobPosting`s built from a `SearchQuery`."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field

from jobhunt.freshness import MAX_JOB_AGE_DAYS


class ContractType(StrEnum):
    CDI = "cdi"
    CDD = "cdd"
    INTERIM = "interim"
    ALTERNANCE = "alternance"  # apprentissage + contrat de professionnalisation
    STAGE = "stage"
    FREELANCE = "freelance"  # incl. portage salarial missions
    VIE = "vie"  # volontariat international en entreprise
    OTHER = "other"
    UNKNOWN = "unknown"


Remote = Literal["full", "partial", "none", "unknown"]


class SearchQuery(BaseModel):
    """One concrete search to run on one board. The planner fans a profile out into many of these."""

    keywords: str  # e.g. "systèmes embarqués" or "stage mécatronique"
    # France Travail ROME 4.0 codes for this function (e.g. "H1208" études techniques industrielles).
    # Official sources (France Travail, La Bonne Alternance) search best by ROME; others ignore it.
    rome_codes: list[str] = Field(default_factory=list)
    location: str | None = None  # free text: "Paris", "Lyon", "Île-de-France", "France"
    postal_code: str | None = None
    department: str | None = None  # "75", "69", "31"...
    radius_km: int | None = None
    contract_types: list[ContractType] = Field(default_factory=list)  # empty = any
    remote_only: bool = False
    posted_within_days: int | None = MAX_JOB_AGE_DAYS
    max_results: int = 50
    language: Literal["fr", "en"] = "fr"


class JobPosting(BaseModel):
    """Normalized posting. Fill what the board gives you; leave the rest None."""

    source: str  # Source.name, e.g. "wttj"
    source_id: str  # stable id on that board
    url: str  # canonical posting URL on the board
    title: str
    company: str | None = None
    location: str | None = None  # human readable, e.g. "Toulouse (31)"
    postal_code: str | None = None
    department: str | None = None
    country: str = "FR"
    contract_type: ContractType = ContractType.UNKNOWN
    remote: Remote = "unknown"
    salary_text: str | None = None  # as displayed, e.g. "45-55 K€ brut annuel"
    salary_min_eur: int | None = None  # yearly gross EUR when derivable
    salary_max_eur: int | None = None
    description: str | None = None  # plain text (strip HTML)
    posted_at: datetime | None = None
    apply_url: str | None = None  # where the "Postuler" button goes, if different from url
    apply_method: Literal["platform", "easy_apply", "external", "email", "unknown"] = "unknown"
    company_website: str | None = None  # company homepage if the board exposes it
    # Origin = the company's own hosted posting (careers site / ATS: Greenhouse, Lever, Workday,
    # SmartRecruiters, Teamtailor, DigitalRecruiters, Welcome Kit...). Aggregators (LinkedIn, Indeed,
    # Glassdoor...) are discovery only; we always apply on the origin. Filled by jobhunt.origin.
    origin_url: str | None = None
    origin_ats: str | None = None  # "greenhouse", "lever", "workday", "careers_site", ...
    origin_confidence: float | None = None  # 0..1
    origin_resolution: dict[str, Any] | None = None
    origin_posted_at: datetime | None = None  # posting date shown on the origin ATS/careers page, when found
    experience_level: str | None = None
    raw: dict[str, Any] = Field(default_factory=dict, exclude=True)  # source payload for debugging


class Profile(BaseModel):
    """A family member's search profile."""

    id: int | None = None
    name: str
    headline: str  # free text function/major: "Étudiant 2e année ingénieur mécatronique"
    interests: list[str] = Field(default_factory=list)  # ["systèmes embarqués", "programmation bas niveau"]
    education: str | None = None
    experience_years: float | None = None
    skills: list[str] = Field(default_factory=list)
    cv_text: str | None = None
    locations: list[str] = Field(default_factory=list)  # ["Île-de-France", "Lyon", "remote"]
    radius_km: int | None = 50
    contract_types: list[ContractType] = Field(default_factory=list)
    languages: list[str] = Field(default_factory=lambda: ["fr"])
    notes: str | None = None  # anything else for the LLM: "pas de déplacements", "dispo à partir de juin"
    email: str | None = None
    phone: str | None = None
