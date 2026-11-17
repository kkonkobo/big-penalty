"""Collective.work: public job board (freelance missions + permanent jobs, many scraped from other boards).

Method: the public GraphQL endpoint used by the site's own search page (no auth):
    POST https://api.collective.work/graphql   operation PublicPages_SearchJobs
    variables.data: {title|query, contractType: All|Freelance|Permanent, locations: ["Paris"], workPreferences: ["REMOTE"],
                     sort: "Latest", from: <offset, 30/page>, externalJobs: true, ...}
`title` matches job titles (precise); `query` is full text (noisy) so we use `title`.
Daily rate: free text in `budgetBrief` ("400 à 450 €/j", "450-530TJM", "410") for freelance missions.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from jobhunt.models import ContractType, JobPosting, SearchQuery
from jobhunt.sources._freelance_util import daily_text, html_to_text, keyword_attempts, parse_daily_rate, parse_iso, yearly_from_daily
from jobhunt.sources.base import ChallengeRequired, Source, SourceContext, SourceError

API = "https://api.collective.work/graphql"
SITE = "https://www.collective.work"
PAGE_SIZE = 30

QUERY = """query PublicPages_SearchJobs($data: PublicPages_SearchJobsInputType!) {
  results: PublicPages_SearchJobs(data: $data) {
    projects {
      id slug name sumUp description budgetBrief workPreferences expirationDate isPermanentContract contractTypes
      job { applicationType applicationTypeValue }
      language source scrappedUrl publishedAt minSalary maxSalary salaryCurrency salaryFrequency
      company { name }
      location { fullNameEnglish fullNameFrench }
    }
    pagination { from total label }
  }
}"""

BASE_VARS: dict[str, Any] = {
    "query": "", "from": 0, "sort": "Latest",
    "dailyRates": {"from": 0, "to": None}, "salaryEur": {"frequency": "Yearly", "from": 0, "to": None},
    "companySize": {"from": 0, "to": None}, "industries": [], "fundingStages": [], "startup": False,
    "investors": [], "exclusive": False, "locations": [], "locationsV3": [], "skills": [], "workPreferences": [],
    "hasDailyRate": False, "companies": [], "fromTopRecruiter": False, "contractType": "All", "contractTypes": [],
    "offerLanguages": [], "recruiterOrganizationId": None, "externalJobs": True, "isDefaultSearch": False,
}
CONTRACT_MAP = {"FREELANCE": ContractType.FREELANCE, "CONTRACT": ContractType.CDD, "FULL_TIME": ContractType.CDI,
                "PART_TIME": ContractType.CDI, "INTERNSHIP": ContractType.STAGE, "APPRENTICESHIP": ContractType.ALTERNANCE}
REMOTE_MAP = {"REMOTE": "full", "HYBRID": "partial", "ON_SITE": "none"}


class CollectiveSource(Source):
    domains = ("collective.work",)
    name = "collective"
    label = "Collective.work"
    kind = "api"
    is_aggregator = True  # part of the listings are scraped from other boards (scrappedUrl)
    contract_types = frozenset({ContractType.FREELANCE, ContractType.CDI})

    async def search(self, query: SearchQuery, ctx: SourceContext) -> list[JobPosting]:
        wanted = set(query.contract_types) & self.contract_types if query.contract_types else set(self.contract_types)
        if not wanted:
            return []
        if wanted == {ContractType.FREELANCE}:
            api_types = ["Freelance"]
        elif wanted == {ContractType.CDI}:
            api_types = ["Permanent"]
        else:
            api_types = ["All"]
        cutoff = datetime.now(timezone.utc) - timedelta(days=query.posted_within_days) if query.posted_within_days else None
        out: dict[str, JobPosting] = {}
        try:
            async with ctx.http(headers={"Origin": SITE, "Referer": SITE + "/", "Accept": "application/json"}) as c:
                for kw in keyword_attempts(query.keywords):  # `title` is a strict AND on all words
                    found = False
                    for api_type in api_types:
                        found |= await self._paginate(c, query, kw, api_type, wanted, cutoff, out)
                    if found:
                        break
        except httpx.HTTPError as e:
            raise SourceError(f"collective.work API error: {e}") from e
        return list(out.values())[: query.max_results]

    async def _paginate(self, c, query, kw, api_type, wanted, cutoff, out) -> bool:
        found = False
        offset = 0
        while len(out) < query.max_results and offset < 5 * PAGE_SIZE:
            data = {**BASE_VARS, "title": kw, "contractType": api_type, "from": offset}
            loc = (query.location or "").strip()
            if loc and loc.lower() not in {"france", "remote"}:
                data["locations"] = [loc]
            if query.remote_only:
                data["workPreferences"] = ["REMOTE"]
            r = await c.post(API, json={"query": QUERY, "variables": {"data": data}})
            if r.status_code in (403, 429, 503) or "Just a moment" in r.text[:500]:
                raise ChallengeRequired(f"collective.work blocked the request ({r.status_code})")
            try:
                payload = r.json()
            except ValueError as e:
                raise SourceError("collective.work returned non-JSON") from e
            if payload.get("errors"):
                raise SourceError(f"collective.work GraphQL error: {payload['errors'][0].get('message')}")
            projects = payload["data"]["results"]["projects"] or []
            for item in projects:
                try:
                    p = self.parse_item(item)
                except Exception:
                    continue
                if p.contract_type not in wanted and (query.contract_types or p.contract_type != ContractType.UNKNOWN):
                    continue  # the API's contractType filter also lets through postings with no contract info
                found = True  # a real match of the wanted contract type exists for this phrasing
                if cutoff and p.posted_at and p.posted_at < cutoff:
                    return found  # sorted by "Latest"
                out.setdefault(p.source_id, p)
                if len(out) >= query.max_results:
                    return found
            if len(projects) < PAGE_SIZE:
                return found
            offset += PAGE_SIZE
        return found

    @staticmethod
    def parse_item(item: dict, lang: str = "fr") -> JobPosting:
        types = [CONTRACT_MAP.get(t, ContractType.OTHER) for t in item.get("contractTypes") or []]
        if not types and item.get("isPermanentContract"):
            types = [ContractType.CDI]
        ctype = ContractType.FREELANCE if ContractType.FREELANCE in types else (types[0] if types else ContractType.UNKNOWN)
        loc = item.get("location") or {}
        location = loc.get("fullNameFrench") or loc.get("fullNameEnglish")
        prefs = item.get("workPreferences") or []
        remote = "unknown"
        for key in ("REMOTE", "HYBRID", "ON_SITE"):
            if key in prefs:
                remote = REMOTE_MAP[key]
                break

        salary_text = smin = smax = None
        brief = (item.get("budgetBrief") or "").strip() or None
        if ctype == ContractType.FREELANCE and brief:
            lo, hi = parse_daily_rate(brief)
            salary_text = (daily_text(lo, hi) or brief) if lo else brief
            smin, smax = yearly_from_daily(lo), yearly_from_daily(hi)
        elif item.get("minSalary") or item.get("maxSalary"):
            smin, smax = item.get("minSalary"), item.get("maxSalary")
            salary_text = brief or f"{smin or ''}-{smax or ''} {item.get('salaryCurrency') or '€'}/{item.get('salaryFrequency') or 'an'}"
        elif brief:
            salary_text = brief

        desc = html_to_text(item.get("description")) or (item.get("sumUp") or "")
        url = f"{SITE}/jobs/{lang}/{item['slug']}"
        apply_url = item.get("scrappedUrl") or url
        return JobPosting(
            source="collective",
            source_id=str(item["id"]),
            url=url,
            title=(item["name"] or "").strip(),
            company=((item.get("company") or {}).get("name") or None),
            location=location,
            contract_type=ctype,
            remote=remote,
            salary_text=salary_text,
            salary_min_eur=smin,
            salary_max_eur=smax,
            description=desc or None,
            posted_at=parse_iso(item.get("publishedAt")),
            apply_url=apply_url,
            apply_method="external" if item.get("scrappedUrl") else "platform",
            raw={k: item.get(k) for k in ("id", "slug", "source", "contractTypes", "workPreferences", "budgetBrief")},
        )
