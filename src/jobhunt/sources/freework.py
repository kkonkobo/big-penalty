"""Free-Work (free-work.com): IT freelance missions + permanent jobs.

Method: the public JSON API the site's own front-end calls (Hydra/JSON-LD, no auth, no bot wall):
    GET https://www.free-work.com/api/job_postings
        ?page=N&itemsPerPage=M&searchKeywords=<kw>&contracts=contractor|permanent&order=date
        &locationKeys=<key from /api/locations/autocomplete?search=...>&remoteMode=full
The list payload already carries the full description, so fetch_details is a no-op.
Daily rate (TJM) -> salary_text "310-440 €/jour"; yearly min/max = TJM * 218 working days (assumption).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from jobhunt.models import ContractType, JobPosting, SearchQuery
from jobhunt.sources._freelance_util import daily_text, html_to_text, keyword_attempts, parse_iso, yearly_from_daily
from jobhunt.sources.base import ChallengeRequired, Source, SourceContext, SourceError

BASE = "https://www.free-work.com"
PAGE_SIZE = 20

API_CONTRACT = {ContractType.FREELANCE: "contractor", ContractType.CDI: "permanent"}
FROM_API_CONTRACT = {
    "contractor": ContractType.FREELANCE,
    "permanent": ContractType.CDI,
    "fixed-term": ContractType.CDD,
    "temporary": ContractType.INTERIM,
    "internship": ContractType.STAGE,
    "apprenticeship": ContractType.ALTERNANCE,
}
DURATION_UNITS = {"day": "jour", "week": "semaine", "month": "mois", "year": "an"}


class FreeWorkSource(Source):
    name = "freework"
    label = "Free-Work"
    kind = "api"
    is_aggregator = False
    contract_types = frozenset({ContractType.FREELANCE, ContractType.CDI})

    async def search(self, query: SearchQuery, ctx: SourceContext) -> list[JobPosting]:
        wanted = [c for c in (query.contract_types or list(API_CONTRACT)) if c in API_CONTRACT]
        if not wanted:
            return []
        out: dict[str, JobPosting] = {}
        cutoff = None
        if query.posted_within_days:
            cutoff = datetime.now(timezone.utc) - timedelta(days=query.posted_within_days)
        try:
            async with ctx.http(headers={"Accept": "application/ld+json"}) as c:
                loc_key = await self._location_key(c, query)
                # keyword phrasing x location breadth: stop at the first combination the board answers for
                for kw in keyword_attempts(query.keywords):
                    found = False
                    for key in self._broaden(loc_key):
                        for ctype in wanted:
                            found |= await self._search_type(c, query, kw, ctype, key, cutoff, out)
                        if found:
                            break
                    if found:
                        break
        except httpx.HTTPError as e:
            raise SourceError(f"free-work API error: {e}") from e
        return list(out.values())[: query.max_results]

    @staticmethod
    def _broaden(key: str | None) -> list[str | None]:
        """'fr~ile-de-france~paris~' -> itself, then region level 'fr~ile-de-france~~' (the board's city filter is
        exact, so 'Paris' would miss the suburbs)."""
        if not key:
            return [None]
        parts = key.split("~")
        out = [key]
        if len(parts) == 4:
            for broader in (f"{parts[0]}~{parts[1]}~{parts[2]}~", f"{parts[0]}~{parts[1]}~~"):
                if broader != key and broader not in out and parts[1]:
                    out.append(broader)
        return out

    async def _search_type(self, c, query, kw, ctype, loc_key, cutoff, out) -> bool:
        """Returns True if the board had any posting for this phrasing/location (even if filtered out here)."""
        found = False
        page = 1
        while len(out) < query.max_results and page <= 5:
            params: dict[str, Any] = {
                "page": page,
                "itemsPerPage": PAGE_SIZE,
                "searchKeywords": kw,
                "contracts": API_CONTRACT[ctype],
                "order": "date",
            }
            if loc_key:
                params["locationKeys"] = loc_key
            if query.remote_only:
                params["remoteMode"] = "full"
            data = await self._get(c, "/api/job_postings", params)
            members = data.get("hydra:member") or []
            found = found or bool(members)
            for item in members:
                try:
                    p = self.parse_item(item, ctype)
                except Exception:  # one bad item must not kill the search
                    continue
                if cutoff and p.posted_at and p.posted_at < cutoff:
                    return found  # sorted by date: everything after is older
                out.setdefault(p.source_id, p)
                if len(out) >= query.max_results:
                    return found
            if len(members) < PAGE_SIZE:
                return found
            page += 1
        return found

    async def _get(self, c: httpx.AsyncClient, path: str, params: dict) -> dict:
        r = await c.get(BASE + path, params=params)
        if r.status_code in (403, 429, 503):
            raise ChallengeRequired(f"free-work returned {r.status_code}")
        if r.status_code >= 400:
            raise SourceError(f"free-work {path} -> HTTP {r.status_code}")
        return r.json()

    async def _location_key(self, c: httpx.AsyncClient, query: SearchQuery) -> str | None:
        loc = (query.location or "").strip()
        if not loc or loc.lower() in {"france", "remote", "télétravail", "teletravail"}:
            return None
        r = await c.get(f"{BASE}/api/locations/autocomplete", params={"search": loc})
        if r.status_code != 200:
            return None
        for cand in r.json():
            if cand.get("countryCode") == "FR" and cand.get("key"):
                return cand["key"]
        return None

    @staticmethod
    def parse_item(item: dict, queried: ContractType | None = None) -> JobPosting:
        contracts = [FROM_API_CONTRACT.get(c, ContractType.OTHER) for c in item.get("contracts") or []]
        ctype = queried if queried in contracts else (contracts[0] if contracts else ContractType.UNKNOWN)

        loc = item.get("location") or {}
        location = loc.get("shortLabel") or loc.get("label")
        job_slug = (item.get("job") or {}).get("slug")
        slug = item["slug"]
        url = f"{BASE}/fr/tech-it/job-mission/{job_slug}/{slug}" if job_slug else f"{BASE}/fr/tech-it/job-mission/{slug}"

        salary_text = None
        smin = smax = None
        if ctype == ContractType.FREELANCE:
            lo, hi = item.get("minDailySalary"), item.get("maxDailySalary")
            salary_text = daily_text(lo, hi) or (f"{item['dailySalary']} /jour" if item.get("dailySalary") else None)
            smin, smax = yearly_from_daily(lo), yearly_from_daily(hi)
        else:
            salary_text = item.get("annualSalary") or None
            smin, smax = item.get("minAnnualSalary"), item.get("maxAnnualSalary")

        parts = [html_to_text(item.get("description"))]
        if item.get("candidateProfile"):
            parts.append("Profil recherché :\n" + html_to_text(item["candidateProfile"]))
        if item.get("companyDescription"):
            parts.append("L'entreprise :\n" + html_to_text(item["companyDescription"]))
        if ctype == ContractType.FREELANCE and item.get("durationValue"):
            unit = DURATION_UNITS.get(item.get("durationPeriod") or "", item.get("durationPeriod") or "")
            unit = unit + "s" if unit == "jour" and item["durationValue"] > 1 else unit
            dur = f"Durée : {item['durationValue']} {unit}" + (" (renouvelable)" if item.get("renewable") else "")
            parts.insert(0, dur)
        skills = [s["name"] for s in item.get("skills") or [] if s.get("name")]
        if skills:
            parts.append("Compétences : " + ", ".join(skills))

        remote = {"full": "full", "partial": "partial", "none": "none"}.get(item.get("remoteMode") or "", "unknown")
        company = (item.get("company") or {}).get("name")

        return JobPosting(
            source="freework",
            source_id=str(item["id"]),
            url=url,
            title=item["title"].strip(),
            company=company.strip() if company else None,
            location=location,
            postal_code=(loc.get("postalCode") if loc.get("postalCode") and len(loc["postalCode"]) == 5 else None),
            department=(loc.get("postalCode") or "")[:2] or None,
            contract_type=ctype,
            remote=remote,
            salary_text=salary_text,
            salary_min_eur=smin,
            salary_max_eur=smax,
            description="\n\n".join(p for p in parts if p),
            posted_at=parse_iso(item.get("publishedAt") or item.get("createdAt")),
            apply_url=url,
            apply_method="platform",
            experience_level=item.get("experienceLevel"),
            raw={k: item.get(k) for k in ("id", "slug", "contracts", "remoteMode", "applicationType")},
        )
