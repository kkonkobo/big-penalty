"""Rule-based parts of scoring: cheap pre-filter and a keyword-overlap fallback scorer (no LLM needed)."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Literal

from pydantic import BaseModel, Field

from jobhunt.dedupe import MergedJob, normalize_city, strip_accents
from jobhunt.models import ContractType, Profile

Flag = Literal["wrong_contract", "too_senior", "location_mismatch", "language", "other"]
FLAG_LABELS = {
    "wrong_contract": "Type de contrat différent",
    "too_senior": "Trop senior",
    "location_mismatch": "Localisation éloignée",
    "language": "Langue",
    "other": "Autre réserve",
}

SENIOR_TITLE = re.compile(
    r"\b(senior|sénior|confirm[ée]e?|lead|principal|directeur|directrice|head of|staff|architecte? (?:logiciel|solution))\b", re.I
)
YEARS_EXP = re.compile(r"(\d{1,2})\s*(?:\+|ans?\s*(?:et plus|minimum|min)?)?\s*(?:ans?)?\s*(?:d['’]\s*)?exp[ée]rience", re.I)
YEARS_EXP_EN = re.compile(r"(\d{1,2})\+?\s*years?(?:['’]| of)?\s*(?:of\s*)?(?:relevant\s*)?experience", re.I)
JUNIOR_HINT = re.compile(r"[ée]tudiant|student|alternan|stagiaire|apprenti|junior|jeune dipl", re.I)
STOP = set(
    "les des une pour dans avec sur par aux est son ses sont the and for with des du de la le en et ou a au un d l "
    "vous nous votre notre poste mission profil entreprise equipe experience travail".split()
)
REMOTE_WORDS = {"remote", "teletravail", "full remote", "distance"}


def profile_scoring_inputs(profile: Profile) -> dict:
    return profile.model_dump(mode="json", exclude={"id", "name", "email", "phone"})


def scoring_fingerprint(profile: Profile, job: MergedJob) -> str:
    inputs = {"version": 2, "profile": profile_scoring_inputs(profile), "job": {
        key: getattr(job, key) for key in (
            "title", "company", "contract_type", "location", "description", "remote", "salary_text",
            "salary_min_eur", "salary_max_eur", "posted_at", "origin_url", "origin_ats", "origin_confidence",
        )
    }}
    return hashlib.sha256(json.dumps(inputs, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def invalidate_score(profile: Profile, job: MergedJob) -> None:
    if job.extra.get("scoring_fingerprint") != scoring_fingerprint(profile, job):
        job.score, job.score_json, job.changed = None, {}, True


def is_junior(profile: Profile) -> bool:
    if profile.experience_years is not None and profile.experience_years < 2:
        return True
    cts = set(profile.contract_types)
    if cts and cts <= {ContractType.STAGE, ContractType.ALTERNANCE}:
        return True
    return bool(JUNIOR_HINT.search(profile.headline or "") or JUNIOR_HINT.search(profile.education or ""))


def min_years_required(text: str) -> int | None:
    found = [int(m.group(1)) for rx in (YEARS_EXP, YEARS_EXP_EN) for m in rx.finditer(text)]
    return min(found) if found else None


def prefilter(profile: Profile, job: MergedJob) -> str | None:
    """Return a French reason to drop the job without paying for an LLM call, or None to keep it."""
    cts = set(profile.contract_types)
    if cts and job.contract_type not in (ContractType.UNKNOWN, ContractType.OTHER) and job.contract_type not in cts:
        return f"Contrat « {job.contract_type.value} » hors des contrats recherchés"
    if is_junior(profile):
        if SENIOR_TITLE.search(job.title or ""):
            return "Intitulé senior pour un profil junior"
        if job.contract_type not in (ContractType.STAGE, ContractType.ALTERNANCE):
            yrs = min_years_required(job.description or "")
            if yrs is not None and yrs >= 4:
                return f"{yrs} ans d'expérience demandés"
    return None


def _tokens(s: str) -> set[str]:
    s = strip_accents(s.lower())
    return {t for t in re.findall(r"[a-z0-9+#]{3,}", s) if t not in STOP}


class JobScore(BaseModel):
    fit: int = 0  # 0-100
    matches_function: bool = False
    rationale: str = ""  # French, short
    flags: list[Flag] = Field(default_factory=list)
    origin: Literal["llm", "rules", "prefilter"] = "rules"
    scored_by: str = "rules"
    prefiltered: bool = False

    def clamp(self) -> "JobScore":
        self.fit = max(0, min(100, int(self.fit)))
        return self


def profile_terms(profile: Profile) -> set[str]:
    return _tokens(" ".join([profile.headline, *profile.interests, *profile.skills]))


def _location_ok(profile: Profile, job: MergedJob) -> bool:
    locs = [normalize_city(x) for x in profile.locations if x.lower().strip() not in REMOTE_WORDS]
    if not locs or not job.location or job.remote in ("full", "partial"):
        return True
    jc = normalize_city(job.location)
    jl = strip_accents(job.location.lower())
    return any(l and (l in jl or jc in l or l == jc) for l in locs) or jc in {"", "france"}


def keyword_score(profile: Profile, job: MergedJob) -> JobScore:
    terms = profile_terms(profile)
    title_t = _tokens(job.title or "")
    body_t = _tokens((job.description or "")[:4000])
    hits_title = terms & title_t
    hits_body = terms & body_t
    title_ratio = min(1.0, len(hits_title) / min(max(len(terms), 1), 3))
    body_ratio = min(1.0, len(hits_body) / min(max(len(terms), 1), 8))
    fit = round(10 + 50 * title_ratio + 40 * body_ratio)
    flags: list[Flag] = []
    cts = set(profile.contract_types)
    if cts and job.contract_type not in (ContractType.UNKNOWN, ContractType.OTHER) and job.contract_type not in cts:
        flags.append("wrong_contract")
        fit = min(fit, 30)
    if is_junior(profile) and SENIOR_TITLE.search(job.title or ""):
        flags.append("too_senior")
        fit = min(fit, 25)
    if not _location_ok(profile, job):
        flags.append("location_mismatch")
        fit = max(0, fit - 15)
    shown = ", ".join(sorted(hits_title | hits_body)[:6]) or "aucun"
    return JobScore(
        fit=fit,
        matches_function=title_ratio >= 0.34 or body_ratio >= 0.5,
        rationale=f"Estimation par mots-clés (sans IA) ; termes en commun : {shown}.",
        flags=flags,
        origin="rules",
    ).clamp()
