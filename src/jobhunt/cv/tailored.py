"""Data model of a tailored CV (what is stored, edited, checked and rendered) + contract-adapted headline."""

from __future__ import annotations

from pydantic import BaseModel, Field

from jobhunt.cv.master import Identity


class TailoredBullet(BaseModel):
    label: str = ""
    text: str
    source_ids: list[str] = Field(default_factory=list)  # master bullet ids this bullet is derived from


class TailoredProject(BaseModel):
    id: str  # master project id
    title: str
    dates: str = ""
    context: str = ""
    bullets: list[TailoredBullet] = Field(default_factory=list)


class TailoredSkillGroup(BaseModel):
    label: str
    items: list[str] = Field(default_factory=list)


class TailoredEducation(BaseModel):
    id: str
    title: str
    school: str = ""
    dates: str = ""
    details: str = ""


class TailoredLanguage(BaseModel):
    id: str
    name: str
    level: str = ""


class TailoredCV(BaseModel):
    domain: str = ""  # "systèmes embarqués": used to (re)build the headline
    headline: str
    summary: str
    skill_groups: list[TailoredSkillGroup] = Field(default_factory=list)
    projects: list[TailoredProject] = Field(default_factory=list)
    education: list[TailoredEducation] = Field(default_factory=list)
    languages: list[TailoredLanguage] = Field(default_factory=list)
    matched_keywords: list[str] = Field(default_factory=list)  # ad terms the master really supports
    gaps: list[str] = Field(default_factory=list)  # ad keywords added without master support (interview prep)
    adjustments: list[str] = Field(default_factory=list)  # what generation/page fitting changed (shown to the user)
    font_scale: float = 1.0  # page fitting


def build_headline(identity: Identity, contract: str, domain: str) -> str:
    """"<title> - Recherche d'alternance en <domain>" / "... de stage (<qualifier>) en ..." following the job's contract."""
    contract = getattr(contract, "value", contract)
    if contract == "alternance":
        wish = "Recherche d'alternance"
    elif contract == "stage":
        q = (identity.stage_qualifier or "").strip()
        wish = f"Recherche de stage ({q})" if q else "Recherche de stage"
    else:
        wish = {"cdi": "Recherche d'un CDI", "cdd": "Recherche d'un CDD", "vie": "Recherche d'un VIE",
                "freelance": "Recherche de missions", "interim": "Recherche de missions"}.get(contract, "Recherche d'un poste")
    domain = (domain or "").strip().rstrip(".")
    if domain:
        wish += f" en {domain}"
    title = (identity.default_title or "").strip()
    return f"{title} - {wish}" if title else wish
