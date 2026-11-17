"""La Bonne Alternance (labonnealternance.apprentissage.beta.gouv.fr): apprenticeship / professionalisation
offers + "recruteurs LBA" (companies likely to hire apprentices -> spontaneous applications).

API (checked 2026-11 against https://api.apprentissage.beta.gouv.fr/api/swagger.json):
    GET https://api.apprentissage.beta.gouv.fr/api/job/v1/search
        ?latitude=&longitude=&radius=(0-200, default 30)&romes=H1206,M1805&target_diploma_level=3..7&departements=31
    Header: Authorization: Bearer <API key>      (free key, https://api.apprentissage.beta.gouv.fr/compte/profil)
    Rate limit: 60 calls/min. Returns {"jobs": [...], "recruiters": [...], "warnings": [...]}.
There is NO free-text keyword parameter: the search is by ROME code(s) + geography, so keywords are used
client-side to rank the (often broad) ROME result set.
The older key-less endpoint labonnealternance.apprentissage.beta.gouv.fr/api/v1/jobs?... now answers 404
and /api/v3/jobs/search answers 401: a token is required (settings.labonnealternance_api_key,
env LABONNEALTERNANCE_API_KEY).
Location -> lat/lon through the free api-adresse.data.gouv.fr geocoder.
"""

from __future__ import annotations

import logging
import re
from typing import Any

import httpx

from jobhunt.models import ContractType, JobPosting, SearchQuery
from jobhunt.sources._fr_public import (
    clean, department_of, detect_remote, find_region, geocode, html_to_text, norm, parse_fr_date,
)
from jobhunt.sources._rome import lookup_rome
from jobhunt.sources.base import Source, SourceContext, SourceError

log = logging.getLogger("jobhunt.sources.labonnealternance")

API_URL = "https://api.apprentissage.beta.gouv.fr/api/job/v1/search"
SPONTANEOUS_PREFIX = "[Candidature spontanée] "
_REMOTE = {"onsite": "none", "remote": "full", "hybrid": "partial"}


def _address_parts(address: str | None) -> tuple[str | None, str | None, str | None]:
    """'20 AVENUE DE SEGUR 75007 PARIS' -> ('Paris (75)', '75007', '75')."""
    if not address:
        return None, None, None
    m = re.search(r"\b(\d{5})\s+([^\d,]+?)\s*$", address.strip())
    if not m:
        return clean(address), None, None
    pc, city = m.group(1), m.group(2).strip().title()
    dep = department_of(pc)
    return f"{city} ({dep})" if dep else city, pc, dep


def _workplace_common(wp: dict[str, Any]) -> dict[str, Any]:
    loc, pc, dep = _address_parts((wp.get("location") or {}).get("address"))
    return {
        "company": clean(wp.get("name") or wp.get("brand") or wp.get("legal_name")),
        "location": loc, "postal_code": pc, "department": dep,
        "company_website": wp.get("website") or None,
    }


def job_to_posting(j: dict[str, Any]) -> JobPosting | None:
    offer = j.get("offer") or {}
    ident = j.get("identifier") or {}
    title = clean(offer.get("title"))
    if not title or offer.get("status") not in (None, "Active"):
        return None
    pid = ident.get("id") or ident.get("partner_job_id")
    if not pid:
        return None
    apply = j.get("apply") or {}
    contract = j.get("contract") or {}
    wp = j.get("workplace") or {}
    parts = [html_to_text(offer.get("description"))]
    for label, key in (("Compétences attendues", "desired_skills"), ("À acquérir pendant l'alternance", "to_be_acquired_skills"), ("Conditions d'accès", "access_conditions")):
        vals = [clean(x) for x in offer.get(key) or [] if clean(x)]
        if vals:
            parts.append(f"{label} :\n" + "\n".join(f"- {v}" for v in vals))
    emp = html_to_text(wp.get("description"))
    if emp:
        parts.append("Employeur : " + emp)
    meta = []
    if contract.get("duration"):
        meta.append(f"Durée du contrat : {contract['duration']} mois")
    if contract.get("start"):
        meta.append(f"Début : {contract['start'][:10]}" + (" (dès que possible)" if contract.get("start_type") == "des_que_possible" else ""))
    if contract.get("type"):
        meta.append("Type : " + " / ".join(contract["type"]))
    if meta:
        parts.append("\n".join(meta))
    desc = "\n\n".join(x for x in parts if x) or None
    target = offer.get("target_diploma") or {}
    partner = ident.get("partner_label")
    return JobPosting(
        source="labonnealternance",
        source_id=str(pid),
        url=apply.get("url") or f"https://labonnealternance.apprentissage.beta.gouv.fr/emploi/{pid}",
        title=title,
        contract_type=ContractType.ALTERNANCE,
        remote=_REMOTE.get(contract.get("remote") or "", detect_remote(desc)),
        description=desc,
        posted_at=parse_fr_date((offer.get("publication") or {}).get("creation")),
        apply_url=apply.get("url"),
        apply_method="platform" if apply.get("recipient_id") else "external",
        experience_level=clean(target.get("label")),
        raw={"flag": "offer", "partner_label": partner, "rome_codes": offer.get("rome_codes"), "siret": wp.get("siret"),
             "phone": apply.get("phone"), "naf": ((wp.get("domain") or {}).get("naf") or {}).get("label"),
             "opco": (wp.get("domain") or {}).get("opco"), "recipient_id": apply.get("recipient_id")},
        **_workplace_common(wp),
    )


def recruiter_to_posting(r: dict[str, Any]) -> JobPosting | None:
    """Company likely to hire apprentices (no published offer): flagged as spontaneous application."""
    ident = r.get("identifier") or {}
    wp = r.get("workplace") or {}
    rid = ident.get("id")
    common = _workplace_common(wp)
    if not rid or not common["company"]:
        return None
    apply = r.get("apply") or {}
    naf = ((wp.get("domain") or {}).get("naf") or {}).get("label")
    lines = [
        "Candidature spontanée : cette entreprise n'a pas publié d'offre mais recrute régulièrement des alternants "
        "(source : La Bonne Alternance, \"recruteurs LBA\")."
    ]
    if naf:
        lines.append(f"Secteur : {naf}")
    if wp.get("size"):
        lines.append(f"Effectif : {wp['size']} salariés")
    if (wp.get("location") or {}).get("address"):
        lines.append(f"Adresse : {wp['location']['address']}")
    if apply.get("phone"):
        lines.append(f"Téléphone : {apply['phone']}")
    emp = html_to_text(wp.get("description"))
    if emp:
        lines.append(emp)
    url = apply.get("url") or f"https://labonnealternance.apprentissage.beta.gouv.fr/recherche-apprentissage?type=lba&itemId={rid}"
    return JobPosting(
        source="labonnealternance",
        source_id=f"recruteur:{rid}",
        url=url,
        title=SPONTANEOUS_PREFIX + common["company"] + (f" ({naf})" if naf else ""),
        contract_type=ContractType.ALTERNANCE,
        description="\n".join(lines),
        apply_url=url,
        apply_method="platform",  # LBA relays the spontaneous application (contact form); no direct e-mail exposed
        raw={"flag": "recruiter_lba", "spontaneous": True, "siret": wp.get("siret"), "naf": naf, "phone": apply.get("phone"),
             "opco": (wp.get("domain") or {}).get("opco"), "recipient_id": apply.get("recipient_id")},
        **common,
    )


def _tokens(s: str) -> set[str]:
    return {t[:6] for t in norm(s).split() if len(t) >= 3 and t not in {"les", "des", "pour", "dans", "avec", "une", "stage", "alternance"}}


def keyword_score(p: JobPosting, kw: str) -> float:
    q = _tokens(kw)
    if not q:
        return 0.0
    title, body = _tokens(p.title), _tokens((p.description or "")[:3000])
    return sum(2.0 if t in title else 1.0 if t in body else 0.0 for t in q) / (2.0 * len(q))


class LaBonneAlternanceSource(Source):
    domains = ("labonnealternance.apprentissage.beta.gouv.fr",)
    name = "labonnealternance"
    label = "La Bonne Alternance"
    kind = "api"
    is_aggregator = True  # relays partner offers (France Travail, Hellowork...); prefer the origin when it is resolvable
    requires_credentials = ("labonnealternance_api_key",)
    contract_types = frozenset({ContractType.ALTERNANCE})

    async def search(self, query: SearchQuery, ctx: SourceContext) -> list[JobPosting]:
        key = ctx.settings.labonnealternance_api_key
        if not key:
            raise SourceError("LABONNEALTERNANCE_API_KEY not set (free key: https://api.apprentissage.beta.gouv.fr/compte/profil)")
        if query.contract_types and ContractType.ALTERNANCE not in query.contract_types:
            return []
        romes = [r for r in (query.rome_codes or lookup_rome(query.keywords)) if re.fullmatch(r"[A-Z]\d{4}", r)][:10]
        async with ctx.http() as c:
            params: dict[str, Any] = {}
            if romes:
                params["romes"] = ",".join(romes)
            loc = (query.location or "").strip()
            if query.postal_code or (loc and norm(loc) not in ("france", "toute la france", "remote")):
                region = await find_region(c, loc) if (loc and not query.postal_code) else None
                if region:  # no region parameter: use departments of the region via geo API
                    deps = await self._region_departments(c, region["code"])
                    if deps:
                        params["departements"] = deps
                else:
                    place = await geocode(c, loc or None, query.postal_code)
                    if place:
                        params.update(latitude=place["lat"], longitude=place["lon"], radius=min(query.radius_km or 30, 200))
                    else:
                        ctx.log.warning("labonnealternance: could not geocode %r; searching all France", loc)
            elif query.department:
                params["departements"] = query.department
            r = await c.get(API_URL, params=params, headers={"Authorization": f"Bearer {key}", "Accept": "application/json"})
        if r.status_code in (401, 403):
            raise SourceError(f"La Bonne Alternance API key rejected (HTTP {r.status_code}): {r.text[:150]}")
        if r.status_code == 429:
            raise SourceError("La Bonne Alternance rate limit (60/min) hit")
        if r.status_code >= 400:
            raise SourceError(f"La Bonne Alternance HTTP {r.status_code}: {r.text[:150]}")
        try:
            data = r.json()
        except ValueError as e:
            raise SourceError(f"La Bonne Alternance: invalid JSON ({e})") from e
        for w in data.get("warnings") or []:
            ctx.log.info("labonnealternance warning: %s %s", w.get("code"), w.get("message"))
        return self.rank(query, data)

    @staticmethod
    async def _region_departments(c: httpx.AsyncClient, code: str) -> list[str]:
        try:
            r = await c.get(f"https://geo.api.gouv.fr/regions/{code}/departements")
            r.raise_for_status()
            return [d["code"] for d in r.json()]
        except (httpx.HTTPError, ValueError, KeyError):
            return []

    @staticmethod
    def rank(query: SearchQuery, data: dict[str, Any]) -> list[JobPosting]:
        """API payload -> postings: offers (keyword-ranked) first, then a few spontaneous-application companies."""
        jobs = [p for j in data.get("jobs") or [] if (p := job_to_posting(j))]
        recs = [p for r in data.get("recruiters") or [] if (p := recruiter_to_posting(r))]
        if query.remote_only:
            jobs = [p for p in jobs if p.remote in ("full", "partial")]
            recs = []
        scored = sorted(((keyword_score(p, query.keywords), i, p) for i, p in enumerate(jobs)), key=lambda x: (-x[0], x[1]))
        # at most ~40% spontaneous-application companies, but they fill any room the real offers leave
        n_jobs = min(len(jobs), query.max_results - min(len(recs), max(2, query.max_results * 2 // 5)))
        n_rec = min(len(recs), query.max_results - n_jobs)
        recs_scored = sorted(
            ((keyword_score(p, query.keywords) + 0.5 * bool(p.raw.get("naf") and _tokens(query.keywords) & _tokens(p.raw["naf"])), i, p)
             for i, p in enumerate(recs)),
            key=lambda x: (-x[0], x[1]),
        )
        return [p for _, _, p in scored[:n_jobs]] + [p for _, _, p in recs_scored[:n_rec]]
