"""Welcome to the Jungle (welcometothejungle.com/fr/jobs).

Method: the site's own Algolia search (app CSEKHVMS53, index wk_cms_jobs_production, public search-only
key embedded in the front-end; Algolia enforces a Referer allow-list, so we send the site's Referer),
then the public JSON detail API (api.welcometothejungle.com/api/v1/organizations/<org>/jobs/<slug>)
which gives the full description and, for ATS-redirect jobs, the company's own apply URL.
If the baked-in key is ever rotated we rediscover it by loading the jobs page in the real browser and
reading the x-algolia-* headers of the page's own search call.
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timedelta, timezone
from typing import ClassVar
from urllib.parse import quote

import httpx

from jobhunt.models import ContractType, JobPosting, SearchQuery
from jobhunt.sources._frboards_util import dept_from_postal, geocode_city, html_to_text, parse_dt
from jobhunt.sources.base import Source, SourceContext, SourceError

APP_ID = "CSEKHVMS53"
SEARCH_KEY = "4bd8f6215d0cc52b26430765769e65a0"
INDEX = "wk_cms_jobs_production"
INDEX_BY_DATE = "wk_cms_jobs_production_published_at_desc"
SITE = "https://www.welcometothejungle.com"
DETAIL_API = "https://api.welcometothejungle.com/api/v1/organizations/{org}/jobs/{slug}"

CONTRACT_TO_FACET = {
    ContractType.CDI: ["full_time"],
    ContractType.CDD: ["temporary"],
    ContractType.INTERIM: ["temporary"],
    ContractType.STAGE: ["internship"],
    ContractType.ALTERNANCE: ["apprenticeship"],
    ContractType.FREELANCE: ["freelance"],
    ContractType.VIE: ["vie"],
    ContractType.OTHER: ["other", "part_time", "graduate_program", "volunteer"],
}
FACET_TO_CONTRACT = {
    "FULL_TIME": ContractType.CDI,
    "TEMPORARY": ContractType.CDD,
    "INTERNSHIP": ContractType.STAGE,
    "APPRENTICESHIP": ContractType.ALTERNANCE,
    "FREELANCE": ContractType.FREELANCE,
    "VIE": ContractType.VIE,
    "PART_TIME": ContractType.OTHER,
    "GRADUATE_PROGRAM": ContractType.OTHER,
    "VOLUNTEER": ContractType.OTHER,
    "OTHER": ContractType.OTHER,
}
REMOTE_MAP = {"fulltime": "full", "partial": "partial", "punctual": "partial", "no": "none"}
REGION_STATES = {  # region name -> value of offices.state
    "île-de-france": "Ile-de-France", "ile-de-france": "Ile-de-France", "idf": "Ile-de-France",
    "occitanie": "Occitanie", "bretagne": "Bretagne", "normandie": "Normandie",
    "nouvelle-aquitaine": "Nouvelle-Aquitaine", "auvergne-rhône-alpes": "Auvergne-Rhône-Alpes",
    "provence-alpes-côte d'azur": "Provence-Alpes-Côte d'Azur", "paca": "Provence-Alpes-Côte d'Azur",
    "hauts-de-france": "Hauts-de-France", "grand est": "Grand Est", "pays de la loire": "Pays de la Loire",
    "bourgogne-franche-comté": "Bourgogne-Franche-Comté", "centre-val de loire": "Centre-Val de Loire",
}
_SALARY_FACTOR = {"yearly": 1, "monthly": 12, "daily": 218, "hourly": 1607}


class WTTJSource(Source):
    name = "wttj"
    label = "Welcome to the Jungle"
    kind = "http"
    contract_types = None
    # Set by the live-discovery fallback when the baked-in key stops working.
    _key: ClassVar[tuple[str, str] | None] = None

    # ---------- Algolia ----------
    def _algolia_headers(self) -> dict[str, str]:
        app, key = self._key or (APP_ID, SEARCH_KEY)
        return {
            "x-algolia-application-id": app,
            "x-algolia-api-key": key,
            "Referer": SITE + "/",
            "Origin": SITE,
            "Content-Type": "application/json",
        }

    async def _discover_key(self, ctx: SourceContext) -> bool:
        """Open the jobs page in the real browser and read the Algolia credentials from its own request."""
        if ctx.browser is None:
            return False
        found: dict[str, str] = {}
        try:
            async with ctx.browser.page(ctx.profile_id, "wttj") as page:
                def on_req(req):
                    h = req.headers
                    if "algolia" in req.url and "x-algolia-api-key" in h:
                        found["app"], found["key"] = h["x-algolia-application-id"], h["x-algolia-api-key"]

                page.on("request", on_req)
                await ctx.browser.goto(page, f"{SITE}/fr/jobs?query=stage", wait_until="networkidle")
                await asyncio.sleep(2)
        except SourceError:
            raise
        except Exception as e:
            ctx.log.info("wttj key discovery failed: %s", e)
        if found:
            type(self)._key = (found["app"], found["key"])
            return True
        return False

    async def _query(self, client: httpx.AsyncClient, ctx: SourceContext, index: str, body: dict) -> dict:
        app = (self._key or (APP_ID, SEARCH_KEY))[0]
        url = f"https://{app.lower()}-dsn.algolia.net/1/indexes/{index}/query"
        for attempt in range(2):
            r = await client.post(url, content=json.dumps(body), headers=self._algolia_headers())
            if r.status_code in (401, 403) and attempt == 0 and await self._discover_key(ctx):
                url = f"https://{self._key[0].lower()}-dsn.algolia.net/1/indexes/{index}/query"
                continue
            break
        if r.status_code == 404 and index != INDEX:
            raise FileNotFoundError(index)
        if r.status_code >= 400:
            raise SourceError(f"wttj algolia HTTP {r.status_code}: {r.text[:200]}")
        return r.json()

    # ---------- search ----------
    async def search(self, query: SearchQuery, ctx: SourceContext) -> list[JobPosting]:
        facet_filters: list[list[str]] = [["offices.country_code:FR"]]
        if query.contract_types:
            vals = sorted({v for c in query.contract_types for v in CONTRACT_TO_FACET.get(c, [])})
            if vals:
                facet_filters.append([f"contract_type:{v}" for v in vals])
        if query.remote_only:
            facet_filters.append(["remote:fulltime"])
        body: dict = {
            "query": query.keywords,
            "facetFilters": facet_filters,
            "attributesToHighlight": [],
            "attributesToSnippet": [],
            "analytics": False,
            "clickAnalytics": False,
        }
        text_location = None
        async with ctx.http(headers={"Accept": "application/json"}) as client:
            loc = (query.location or "").strip()
            if loc.lower() in REGION_STATES:
                facet_filters.append([f"offices.state:{REGION_STATES[loc.lower()]}"])
            elif query.department and not loc:
                text_location = None
            else:
                geo = await geocode_city(client, loc or None, query.postal_code)
                if geo:
                    lat, lng, _, _ = geo
                    body["aroundLatLng"] = f"{lat},{lng}"
                    body["aroundRadius"] = (query.radius_km or 30) * 1000
                elif loc and loc.lower() not in ("france", "remote"):
                    text_location = loc
            if text_location:
                body["query"] = f"{query.keywords} {text_location}".strip()

            cutoff = None
            if query.posted_within_days:
                cutoff = datetime.now(timezone.utc) - timedelta(days=query.posted_within_days)

            hits: list[dict] = []
            index = INDEX_BY_DATE
            want = query.max_results
            per_page = min(max(want, 20), 100)
            for page_no in range(6):
                body.update(page=page_no, hitsPerPage=per_page)
                try:
                    data = await self._query(client, ctx, index, body)
                except FileNotFoundError:
                    index = INDEX
                    data = await self._query(client, ctx, index, body)
                except httpx.HTTPError as e:
                    raise SourceError(f"wttj search failed: {e}") from e
                batch = data.get("hits") or []
                for h in batch:
                    pub = parse_dt(h.get("published_at"))
                    if cutoff and pub and pub < cutoff:
                        continue
                    hits.append(h)
                if len(hits) >= want or page_no + 1 >= data.get("nbPages", 0) or not batch:
                    break
            hits = hits[:want]

            postings, seen = [], set()
            for h in hits:  # one posting can be indexed once per office: dedupe on URL
                p = self._from_hit(h, ctx)
                if p and p.url not in seen:
                    seen.add(p.url)
                    postings.append(p)
            # Enrich with the detail API (full description + ATS redirect URL). Few, paced requests.
            sem = asyncio.Semaphore(3)

            async def enrich(p: JobPosting) -> JobPosting:
                async with sem:
                    await asyncio.sleep(0.15)
                    try:
                        return await self._details(p, client)
                    except Exception as e:  # keep the summary-only posting
                        ctx.log.debug("wttj detail failed for %s: %s", p.url, e)
                        return p

            return list(await asyncio.gather(*(enrich(p) for p in postings)))

    # ---------- mapping ----------
    def _from_hit(self, h: dict, ctx: SourceContext) -> JobPosting | None:
        try:
            org = h.get("organization") or {}
            org_slug, slug = org.get("slug"), h.get("slug")
            if not (org_slug and slug and h.get("name")):
                return None
            office = h.get("office") or (h.get("offices") or [{}])[0] or {}
            p = JobPosting(
                source=self.name,
                source_id=str(h.get("reference") or h.get("objectID")),
                url=f"{SITE}/fr/companies/{org_slug}/jobs/{slug}",
                title=h["name"].strip(),
                company=org.get("name"),
                location=self._loc_text(office),
                department=None,
                country=(office.get("country_code") or "FR").upper(),
                contract_type=FACET_TO_CONTRACT.get((h.get("contract_type") or "").upper(), ContractType.UNKNOWN),
                remote=REMOTE_MAP.get(h.get("remote") or "", "unknown"),
                description=html_to_text(h.get("profile")),
                posted_at=parse_dt(h.get("published_at")),
                apply_method="unknown",
                experience_level=self._exp(h.get("experience_level_minimum")),
                raw=h,
            )
            self._apply_salary(p, h.get("salary_minimum"), h.get("salary_maximum"), h.get("salary_period"), h.get("salary_currency"))
            return p
        except Exception as e:
            ctx.log.debug("wttj skip hit: %s", e)
            return None

    @staticmethod
    def _loc_text(office: dict) -> str | None:
        city = office.get("city")
        district = office.get("district")
        if city and district and district != city:
            return f"{city} ({district})"
        return city or district

    @staticmethod
    def _exp(months) -> str | None:
        if months is None:
            return None
        return f"{months} ans min." if months >= 12 else ("débutant" if months == 0 else f"{months} mois min.")

    @staticmethod
    def _apply_salary(p: JobPosting, lo, hi, period, currency) -> None:
        if not (lo or hi) or (currency and currency != "EUR"):
            return
        period = period or "yearly"
        shown = {"yearly": "an", "monthly": "mois", "daily": "jour", "hourly": "heure"}.get(period, period)
        a, b = lo or hi, hi or lo
        p.salary_text = f"{int(a):,} - {int(b):,} € brut / {shown}".replace(",", " ") if a != b else f"{int(a):,} € brut / {shown}".replace(",", " ")
        if p.contract_type in (ContractType.STAGE, ContractType.ALTERNANCE):
            return  # gratification / alternance pay: not a yearly salary
        f = _SALARY_FACTOR.get(period)
        if f and period in ("yearly", "monthly"):
            p.salary_min_eur, p.salary_max_eur = int(a * f), int(b * f)

    async def fetch_details(self, posting: JobPosting, ctx: SourceContext) -> JobPosting:
        async with ctx.http(headers={"Accept": "application/json"}) as client:
            try:
                return await self._details(posting, client)
            except (httpx.HTTPError, ValueError, KeyError) as e:
                ctx.log.debug("wttj detail failed: %s", e)
                return posting

    async def _details(self, p: JobPosting, client: httpx.AsyncClient) -> JobPosting:
        m = re.search(r"/companies/([^/]+)/jobs/([^/?#]+)", p.url)
        if not m:
            return p
        r = await client.get(DETAIL_API.format(org=quote(m.group(1)), slug=quote(m.group(2))))
        r.raise_for_status()
        return self.apply_detail(p, r.json().get("job") or {})

    def apply_detail(self, p: JobPosting, j: dict) -> JobPosting:
        parts = [html_to_text(j.get(k)) for k in ("description", "profile", "recruitment_process")]
        if j.get("key_missions"):
            parts.insert(0, "\n".join(f"- {x}" for x in j["key_missions"] if isinstance(x, str)))
        desc = "\n\n".join(x for x in parts if x)
        if desc:
            p.description = desc
        ext = j.get("apply_url")
        if j.get("ats") == "external" and ext:
            p.apply_url, p.apply_method = ext, "external"
        else:
            p.apply_url, p.apply_method = p.url, "platform"
        org = j.get("organization") or {}
        p.company_website = org.get("media_website_url") or p.company_website
        off = j.get("office") or {}
        if off.get("zip_code"):
            p.postal_code = off["zip_code"]
            p.department = dept_from_postal(off["zip_code"])
        if j.get("salary_min") or j.get("salary_max"):
            self._apply_salary(p, j.get("salary_min"), j.get("salary_max"), j.get("salary_period"), j.get("salary_currency"))
        if j.get("published_at"):
            p.posted_at = parse_dt(j["published_at"]) or p.posted_at
        return p
