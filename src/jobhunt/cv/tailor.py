"""Tailor the master CV to one job with ONE structured LLM call.

The model rewords and reframes REAL experience in the ad's vocabulary; it may never invent. Everything it returns is
expanded from master ids (titles, dates, schools are copied by code, not by the model) and fact-checked afterwards
(jobhunt.cv.check).
"""

from __future__ import annotations

import json
import logging

from pydantic import BaseModel, Field

from jobhunt.cv.master import Master
from jobhunt.cv.tailored import (TailoredBullet, TailoredCV, TailoredEducation, TailoredLanguage, TailoredProject,
                                 TailoredSkillGroup, build_headline)
from jobhunt.dedupe import MergedJob
from jobhunt.models import Profile

log = logging.getLogger("jobhunt.cv")

MAX_PROJECTS = 3
DESCRIPTION_CHARS = 6000


class DraftBullet(BaseModel):
    label: str = Field(description='Short bold prefix (2-5 words), in the ad\'s vocabulary when it fits, e.g. "Acquisition capteurs"')
    text: str = Field(description="Reworded text following the label (starts lowercase like the master's, no label, no trailing label colon)")
    source_ids: list[str] = Field(description="ids of the master bullets (same project) this bullet is derived from; at least one")


class DraftProject(BaseModel):
    id: str = Field(description="Master project id")
    bullets: list[DraftBullet]


class DraftSkillGroup(BaseModel):
    label: str = Field(description="Group label; may be renamed to the ad's vocabulary")
    items: list[str] = Field(description="Items copied EXACTLY from master skill items")


class TailorDraft(BaseModel):
    domain: str = Field(description='Field/function of the job in 2-5 words, no leading "en", e.g. "systèmes embarqués"')
    summary: str = Field(description="Rewritten italic introduction, French, at most 3 lines (~330 characters)")
    skill_groups: list[DraftSkillGroup]
    projects: list[DraftProject] = Field(description="The 2-3 most relevant master projects, most relevant first")
    education_ids: list[str] = Field(description="Master education ids to keep, in display order")
    language_ids: list[str] = Field(description="Master language ids to keep, in display order")
    matched_keywords: list[str] = Field(description="Terms of the ad that the CV genuinely supports")
    gaps: list[str] = Field(description="Ad keywords/tools added to the CV without master support (short, French): interview-preparation list")


TAILOR_SYSTEM = """You tailor a candidate's one-page CV (French) to a specific job ad so it gets through ATS keyword filters and
automated screening. You receive the candidate's MASTER CV (every item has an id) and the job ad.

POLICY (decided by the candidate's family): wording may go beyond the master and may be embellished. Be assertive:
- Maximise ATS keyword coverage: put the ad's must-have keywords, tools and skills verbatim into skill_groups, the summary
  and the bullets, even when the master only has adjacent experience or none.
- Reframe the projects in the ad's vocabulary with strong verbs (conçu, développé, piloté, mis en œuvre, validé) and a
  larger scope when it helps; stay plausible for a Bac+4 engineering student's academic projects.

HARD FACTS (a verifier blocks the CV if you change them; a recruiter can check them):
- never invent employers, companies worked for, jobs, internships, apprenticeships or any work experience; projects stay
  academic projects (never present them as work done for a company);
- never change degrees, diplomas, schools, year of study, grades, dates or durations; never add certifications;
- never state a language level (A1-C2, bilingue, courant...) other than the master's; never mention nationality or work permit.

Output rules:
1. skill_groups: master skill items (copied exactly) PLUS added ad keywords/tools, grouped under labels in the ad's
   vocabulary, most relevant first; drop irrelevant master items to keep the page short (max ~6 groups, ~8 items each).
2. projects: choose the 2-3 most relevant master projects, most relevant first. Only return the project id (titles/dates
   are copied by code). Each bullet rewrites one or more master bullets OF THE SAME PROJECT, source_ids = those bullet ids.
   2 bullets per project (3 for the best one). Each bullet text <= 230 characters.
3. education_ids / language_ids: ids to keep (usually all).
4. summary: at most 3 lines (~330 characters), packed with the ad's keywords, no availability dates or locations.
5. domain: the job's field using words from the job title or ad (e.g. "systèmes embarqués"); it is inserted in
   "Recherche d'alternance en <domain>".
6. matched_keywords: 5-12 ad terms the master already supports. gaps: ad keywords you ADDED without master support (they
   go into the candidate's interview-preparation list).
Write everything in French. Concrete, keyword-dense, no empty marketing fluff."""


def master_for_llm(master: Master) -> str:
    d = master.model_dump(exclude={"identity"})
    d["identity"] = {"default_title": master.identity.default_title}
    return json.dumps(d, ensure_ascii=False, indent=1)


def job_for_llm(job: MergedJob, profile: Profile | None) -> str:
    d = {
        "title": job.title, "company": job.company, "location": job.location, "contract": job.contract_type.value,
        "description": (job.description or "(pas de description)")[:DESCRIPTION_CHARS],
    }
    if profile:
        d["candidate_goal"] = {"headline": profile.headline, "interests": profile.interests}
    return json.dumps(d, ensure_ascii=False, indent=1)


def _uniq(ids: list[str]) -> list[str]:
    return list(dict.fromkeys(ids))


DOMAIN_STOP = {"de", "des", "du", "la", "le", "les", "et", "en", "d", "l", "a", "au", "aux", "pour", "sur", "un", "une", "dans"}


def sanitize_domain(domain: str, master: Master, job: MergedJob) -> tuple[str, str | None]:
    """Keep the domain only if every word comes from the job title/ad and no tool name is absent from the master.
    Returns (domain, note): ("", note) when it must be dropped."""
    from jobhunt.cv.check import TOKEN_RE, _stem, technical_terms

    domain = (domain or "").strip()
    if not domain:
        return "", None
    ad = {_stem(t) for t in TOKEN_RE.findall(f"{job.title} {job.description or ''}")}
    words = [w for w in TOKEN_RE.findall(domain) if _stem(w) not in DOMAIN_STOP]
    if any(_stem(w) not in ad for w in words):
        return "", "Domaine du titre retiré : mots absents de l'annonce"
    return domain, None


def build_tailored(draft: TailorDraft, master: Master, job: MergedJob) -> TailoredCV:
    """Expand a draft into a TailoredCV: titles/dates/schools come from the master, not from the model."""
    notes: list[str] = []
    mp = {p.id: p for p in master.projects}
    projects: list[TailoredProject] = []
    seen: set[str] = set()
    for dp in draft.projects:
        base = mp.get(dp.id)
        if base is None:
            notes.append(f"Projet inconnu ignoré : {dp.id}")
            continue
        if dp.id in seen:
            continue
        seen.add(dp.id)
        projects.append(TailoredProject(
            id=base.id, title=base.title, dates=base.dates, context=base.context,
            bullets=[TailoredBullet(label=b.label.strip().rstrip(":").strip(), text=b.text.strip(), source_ids=_uniq(b.source_ids))
                     for b in dp.bullets if b.text.strip()],
        ))
    if len(projects) > MAX_PROJECTS:
        notes.append(f"{len(projects) - MAX_PROJECTS} projet(s) au-delà de {MAX_PROJECTS} retiré(s)")
        projects = projects[:MAX_PROJECTS]
    me = {e.id: e for e in master.education}
    edu_ids = [i for i in _uniq(draft.education_ids) if i in me] or [e.id for e in master.education]
    ml = {l.id: l for l in master.languages}
    lang_ids = [i for i in _uniq(draft.language_ids) if i in ml] or [l.id for l in master.languages]
    domain, note = sanitize_domain(draft.domain, master, job)
    if note:
        notes.append(note)
    return TailoredCV(
        domain=domain,
        headline=build_headline(master.identity, job.contract_type, domain),
        summary=draft.summary.strip(),
        skill_groups=[TailoredSkillGroup(label=g.label.strip(), items=[i.strip() for i in g.items if i.strip()])
                      for g in draft.skill_groups if g.items],
        projects=projects,
        education=[TailoredEducation(**me[i].model_dump()) for i in edu_ids],
        languages=[TailoredLanguage(**ml[i].model_dump()) for i in lang_ids],
        matched_keywords=[k.strip() for k in draft.matched_keywords if k.strip()],
        gaps=[g.strip() for g in draft.gaps if g.strip()],
        adjustments=notes,
    )


async def tailor(master: Master, job: MergedJob, profile: Profile | None, llm=None) -> TailoredCV:
    """One LLM call -> TailoredCV. Raises LLMError when no provider answers (no rule-based fallback for a CV)."""
    from jobhunt.llm import get_llm

    own = llm is None
    llm = llm or get_llm()
    try:
        draft = await llm.structured(
            TailorDraft, system_text=TAILOR_SYSTEM + "\n\n=== MASTER CV ===\n" + master_for_llm(master),
            user_text="=== JOB AD ===\n" + job_for_llm(job, profile), max_tokens=6000)
    finally:
        if own:
            await llm.aclose()
    return build_tailored(draft, master, job)
