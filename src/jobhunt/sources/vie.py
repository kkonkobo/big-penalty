"""V.I.E / V.I.A (Business France, Civiweb): international volunteer missions, 6-24 months abroad.

Civiweb's Nuxt front-end calls a JSON API that wants an `x-api-key` header. That key is the site's *public*
front-end configuration (it is embedded in the HTML of every page as `API_KEY:"..."`), so it is read from the
home page at runtime rather than hard-coded; we send the same request the website's search box sends:
    POST https://api-mon-vie-via.businessfrance.fr/api/Offers/search
    {"limit":N,"skip":0,"query":"embarqué","activitySectorId":[],"missionsTypesIds":[],"missionsDurations":[],
     "geographicZones":[],"countriesIds":[],"studiesLevelId":[],"companiesSizes":[],"specializationsIds":[],
     "entreprisesIds":[0],"missionStartDate":null}
Plain HTTP works (no bot wall). Location of the query is ignored (missions are abroad): keywords only.
"""

from __future__ import annotations

import logging
import re
from typing import Any

import httpx

from jobhunt.models import ContractType, JobPosting, SearchQuery
from jobhunt.sources._fr_public import clean, detect_remote, html_to_text, parse_fr_date
from jobhunt.sources.base import Source, SourceContext, SourceError

log = logging.getLogger("jobhunt.sources.vie")

HOME = "https://mon-vie-via.businessfrance.fr/"
API = "https://api-mon-vie-via.businessfrance.fr/api/Offers/search"
OFFER_URL = "https://mon-vie-via.businessfrance.fr/offres/{id}"
PAGE = 50


def extract_api_key(html: str) -> str | None:
    for m in re.findall(r'API_KEY:"([^"]+)"', html):
        if not m.startswith("AIza"):  # the other one is a Google Maps key
            return m.replace("\\u002F", "/")
    return None


def offer_to_posting(o: dict[str, Any]) -> JobPosting | None:
    oid, title = o.get("id"), clean(o.get("missionTitle"))
    if not oid or not title:
        return None
    city, country = clean(o.get("cityName")), clean(o.get("countryName"))
    location = ", ".join(x.title() if x.isupper() else x for x in (city, country) if x) or None
    parts = [html_to_text(o.get("missionDescription"))]
    if o.get("missionProfile"):
        parts.append("Profil :\n" + (html_to_text(o["missionProfile"]) or ""))
    if o.get("organizationPresentation"):
        parts.append("Entreprise :\n" + (html_to_text(o["organizationPresentation"]) or ""))
    meta = []
    if o.get("missionDuration"):
        meta.append(f"Durée : {o['missionDuration']} mois")
    if o.get("missionStartDate"):
        meta.append(f"Début : {o['missionStartDate'][:10]}")
    if o.get("missionType"):
        meta.append(f"Type : {o['missionType']}")
    if meta:
        parts.append(" | ".join(meta))
    desc = "\n\n".join(x for x in parts if x) or None
    indem = o.get("indemnite")
    sal_text = sal = None
    if isinstance(indem, (int, float)) and indem > 0:
        sal_text = f"{indem:.0f} € / mois (indemnité mensuelle V.I.E, hors avantages)"
        sal = int(round(indem * 12))
    site = o.get("organizationUrlImage")
    contact_url = o.get("contactURL")
    return JobPosting(
        source="vie",
        source_id=str(oid),
        url=OFFER_URL.format(id=oid),
        title=title,
        company=clean(o.get("organizationName")),
        location=location,
        country=(o.get("countryId") or "FR").upper()[:2],
        contract_type=ContractType.VIE,
        remote="partial" if o.get("teleworkingAvailable") else detect_remote(desc),
        salary_text=sal_text,
        salary_min_eur=sal,
        salary_max_eur=sal,
        description=desc,
        posted_at=parse_fr_date(o.get("startBroadcastDate") or o.get("creationDate")),
        apply_url=contact_url if contact_url and contact_url.startswith("http") else OFFER_URL.format(id=oid),
        apply_method="external" if contact_url and contact_url.startswith("http") else "platform",  # Civiweb candidate account
        company_website=site if isinstance(site, str) and site.startswith("http") else None,
        experience_level=", ".join(s.get("specializationLabel", "") for s in (o.get("specializations") or [])[:3]) or None,
        raw={"reference": o.get("reference"), "mission_type": o.get("missionType"), "duration_months": o.get("missionDuration"),
             "end_date": o.get("missionEndDate"), "contact": clean(o.get("contactName"))},
    )


class VieSource(Source):
    name = "vie"
    label = "V.I.E (Business France)"
    kind = "http"
    contract_types = frozenset({ContractType.VIE})

    def __init__(self) -> None:
        self._key: str | None = None

    async def _api_key(self, c: httpx.AsyncClient) -> str:
        if not self._key:
            r = await c.get(HOME)
            self._key = extract_api_key(r.text) if r.status_code == 200 else None
            if not self._key:
                raise SourceError(f"V.I.E: could not read the public API key from {HOME} (HTTP {r.status_code})")
        return self._key

    async def search(self, query: SearchQuery, ctx: SourceContext) -> list[JobPosting]:
        if query.contract_types and ContractType.VIE not in query.contract_types:
            return []
        body = {
            "limit": min(PAGE, query.max_results), "skip": 0, "query": query.keywords, "activitySectorId": [],
            "missionsTypesIds": [], "missionsDurations": [], "geographicZones": [], "countriesIds": [], "studiesLevelId": [],
            "companiesSizes": [], "specializationsIds": [], "entreprisesIds": [0], "missionStartDate": None,
        }
        out: list[JobPosting] = []
        async with ctx.http(headers={"Origin": "https://mon-vie-via.businessfrance.fr", "Referer": HOME}) as c:
            key = await self._api_key(c)
            while len(out) < query.max_results:
                r = await c.post(API, json=body, headers={"x-api-key": key})
                if r.status_code in (401, 403):
                    self._key = None
                    raise SourceError(f"V.I.E API rejected the key (HTTP {r.status_code})")
                if r.status_code != 200:
                    raise SourceError(f"V.I.E API HTTP {r.status_code}")
                data = r.json()
                rows = data.get("result") or []
                out += [p for o in rows if (p := offer_to_posting(o))]
                if len(rows) < body["limit"] or len(out) >= int(data.get("count") or 0):
                    break
                body["skip"] += body["limit"]
        return out[: query.max_results]
