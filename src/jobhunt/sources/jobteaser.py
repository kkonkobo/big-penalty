"""JobTeaser (jobteaser.com/fr/job-offers): student / young-graduate jobs, internships, apprenticeships.

Method: the public search page is served by JobTeaser's own Algolia proxy at
https://search.jobteaser.com/1/indexes/*/queries (index `job_ads_search`; the site itself sends the
placeholder key "fakeprodapikey" with app id WS3ZUW0BOJ - the proxy injects the real credentials). It is
reachable with plain httpx (no Cloudflare challenge) and returns the full description. We restrict to
public offers (`private_offer:false`); offers private to a given school are only visible after a school login
(`jobhunt login jobteaser`), which this connector does not scrape (see README note in the final report).

The "Postuler" target of externally-handled offers is not exposed without a click behind the site's
redirect, so apply_url stays None for those (apply_method="external"); the origin resolver should find the
company's own posting.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from urllib.parse import urlencode
from typing import ClassVar

import httpx

from jobhunt.models import ContractType, JobPosting, SearchQuery
from jobhunt.sources._frboards_util import geocode_city, html_to_text

from jobhunt.sources.base import Source, SourceContext, SourceError

ENDPOINT = "https://search.jobteaser.com/1/indexes/*/queries"
APP_ID = "WS3ZUW0BOJ"
API_KEY = "fakeprodapikey"
INDEX = "job_ads_search"
SITE = "https://www.jobteaser.com"

CONTRACT_FILTERS: dict[ContractType, list[str]] = {
    ContractType.STAGE: ["internship", "curricular_internship", "extra_curricular_intership", "work_placement"],
    ContractType.ALTERNANCE: ["alternating", "apprenticeship"],
    ContractType.CDI: ["cdi"],
    ContractType.CDD: ["cdd"],
    ContractType.FREELANCE: ["freelance"],
    ContractType.VIE: ["vie"],
    ContractType.OTHER: ["graduate_program", "part_time", "master_thesis", "thesis", "civic_service"],
}
LABEL_TO_CONTRACT = {
    **{v: k for k, vs in CONTRACT_FILTERS.items() for v in vs},
    "mba_internship": ContractType.STAGE,
    "gopro_internship": ContractType.STAGE,
    "duales_studium": ContractType.ALTERNANCE,
    "training_contract": ContractType.ALTERNANCE,
    "volunteering": ContractType.OTHER,
    "voluntariat": ContractType.OTHER,
    "werkstudent": ContractType.OTHER,
}
REMOTE_MAP = {
    "remote_not_allowed": "none",
    "remote_partial_allowed": "partial",
    "remote_full_allowed": "full",
    "remote_only": "full",
}
EXPERIENCE = {
    "student": "étudiant",
    "young_graduate": "jeune diplômé",
    "experienced": "expérimenté",
}


class JobTeaserSource(Source):
    name = "jobteaser"
    label = "JobTeaser"
    kind = "http"
    requires_login = False
    login_url: ClassVar[str] = "https://www.jobteaser.com/fr/users/sign_in"
    contract_types = None

    async def search(self, query: SearchQuery, ctx: SourceContext) -> list[JobPosting]:
        facet_filters: list = []
        if query.contract_types:
            vals = sorted({v for c in query.contract_types for v in CONTRACT_FILTERS.get(c, [])})
            if not vals:
                return []  # e.g. only INTERIM requested: not carried by JobTeaser
            facet_filters.append([f"contract.type.original_label:{v}" for v in vals])
        if query.remote_only:
            facet_filters.append(["remote_type:remote_full_allowed", "remote_type:remote_only"])
        params: dict = {
            "hitsPerPage": min(max(query.max_results, 10), 100),
            "filters": "private_offer:false",
            "facetFilters": json.dumps(facet_filters),
            "attributesToHighlight": "[]",
            "attributesToSnippet": "[]",
            "analytics": "false",
        }
        if query.posted_within_days:
            params["numericFilters"] = json.dumps([f"activated_at>{int(time.time()) - query.posted_within_days * 86400}"])
        async with ctx.http(headers=self._headers()) as client:
            geo = await geocode_city(client, query.location, query.postal_code)
            if geo:
                params["aroundLatLng"] = f"{geo[0]},{geo[1]}"
                params["aroundRadius"] = (query.radius_km or 30) * 1000
            else:
                facet_filters.append(["locations.country:France"])
                params["facetFilters"] = json.dumps(facet_filters)
                if query.location and query.location.strip().lower() not in ("france", "remote", "télétravail"):
                    query = query.model_copy(update={"keywords": f"{query.keywords} {query.location}"})
            hits: list[dict] = []
            page = 0
            while len(hits) < query.max_results and page < 5:
                params["page"] = page
                body = {"requests": [{"indexName": INDEX, "query": query.keywords, "params": urlencode(params)}]}
                try:
                    r = await client.post(ENDPOINT, content=json.dumps(body))
                except httpx.HTTPError as e:
                    raise SourceError(f"jobteaser search failed: {e}") from e
                if r.status_code >= 400:
                    raise SourceError(f"jobteaser search HTTP {r.status_code}: {r.text[:200]}")
                res = (r.json().get("results") or [{}])[0]
                batch = res.get("hits") or []
                hits.extend(batch)
                page += 1
                if not batch or page >= res.get("nbPages", 0):
                    break
        out, seen = [], set()
        for h in hits[: query.max_results]:
            p = self._from_hit(h, ctx)
            if p and p.source_id not in seen:
                seen.add(p.source_id)
                out.append(p)
        return out

    @staticmethod
    def _headers() -> dict[str, str]:
        return {
            "x-algolia-application-id": APP_ID,
            "x-algolia-api-key": API_KEY,
            "Origin": SITE,
            "Referer": SITE + "/",
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        }

    def _from_hit(self, h: dict, ctx: SourceContext) -> JobPosting | None:
        try:
            uuid, slug, title = h.get("uuid") or h.get("objectID"), h.get("slug"), h.get("title")
            if not (uuid and slug and title):
                return None
            locs = h.get("locations") or []
            loc0 = locs[0] if locs else {}
            city = loc0.get("city")
            sub = loc0.get("sub_state")
            location = ", ".join(x for x in (f"{city} ({sub})" if city and sub else (city or sub),) if x) or None
            if len(locs) > 1:
                location = f"{location} +{len(locs) - 1}" if location else None
            label = ((h.get("contract") or {}).get("type") or {}).get("original_label") or ""
            company = h.get("company") or {}
            external = h.get("candidacy_type") == "EXTERNAL"
            posted = h.get("activated_at")
            return JobPosting(
                source=self.name,
                source_id=str(uuid),
                url=f"{SITE}/fr/job-offers/{slug}",
                title=title.strip(),
                company=company.get("name"),
                location=location,
                country=_iso_country(loc0.get("country")),
                contract_type=LABEL_TO_CONTRACT.get(label, ContractType.UNKNOWN if not label else ContractType.OTHER),
                remote=REMOTE_MAP.get(h.get("remote_type") or "", "unknown"),
                description=html_to_text(h.get("description")),
                posted_at=datetime.fromtimestamp(posted, tz=timezone.utc) if posted else None,
                apply_method="external" if external else "platform",
                apply_url=None if external else f"{SITE}/fr/job-offers/{slug}",
                experience_level=EXPERIENCE.get(h.get("work_experience_code") or ""),
                raw=h,
            )
        except Exception as e:
            ctx.log.debug("jobteaser skip hit: %s", e)
            return None


def _iso_country(name: str | None) -> str:
    return {"France": "FR"}.get(name or "France", (name or "FR")[:2].upper())
