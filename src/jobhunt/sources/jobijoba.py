"""Jobijoba (jobijoba.com): job *aggregator* (discovery only; apply on the origin).

Method: server-rendered HTML of https://www.jobijoba.com/fr/query/ (plain httpx, no bot wall at time of
writing). Query params: what, where, where_type=city, perimeter (km), period=24_hours|7_days|15_days|30_days.
One page = up to ~30 offers (extra pages are loaded by JS on the site; the `page` param is ignored by the
server), so max_results is effectively capped at ~30 per query. The contract filter is JS-only, so contracts
are filtered client-side (offers without an explicit contract are kept).
Each offer's "Postuler" target is a Jobijoba redirect (/fr/redirect/offer/...) to the source board/company;
we expose it as apply_url (apply_method="external") for the origin resolver.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import httpx
from bs4 import BeautifulSoup

from jobhunt.models import ContractType, JobPosting, SearchQuery
from jobhunt.sources._frboards_util import guess_contract, parse_salary_text
from jobhunt.sources.base import Source, SourceContext, SourceError

SITE = "https://www.jobijoba.com"
_MONTHS = {m: i for i, m in enumerate(
    ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août", "septembre", "octobre", "novembre", "décembre"], 1)}


def parse_pub_date(text: str | None, now: datetime | None = None) -> datetime | None:
    """'Il y a 14 h' / 'Il y a 3 jours' / '23 octobre' / 'Hier' -> datetime (approx; year inferred)."""
    if not text:
        return None
    now = now or datetime.now(timezone.utc)
    t = text.strip().lower()
    m = re.search(r"il y a\s+(\d+)\s*(min|h|heure|j|jour|sem)", t)
    if m:
        n, u = int(m.group(1)), m.group(2)
        return now - (timedelta(minutes=n) if u == "min" else timedelta(hours=n) if u in ("h", "heure") else
                      timedelta(days=n) if u in ("j", "jour") else timedelta(weeks=n))
    if "hier" in t:
        return now - timedelta(days=1)
    if "aujourd" in t:
        return now
    m = re.fullmatch(r"(\d{1,2})(?:er)?\s+([a-zéû]+)(?:\s+(\d{4}))?", t)
    if m and m.group(2) in _MONTHS:
        year = int(m.group(3)) if m.group(3) else now.year
        try:
            d = datetime(year, _MONTHS[m.group(2)], int(m.group(1)), tzinfo=timezone.utc)
        except ValueError:
            return None
        return d - timedelta(days=365) if d > now + timedelta(days=1) else d
    return None


class JobijobaSource(Source):
    domains = ("jobijoba.com", "jobijoba.net")
    name = "jobijoba"
    label = "Jobijoba"
    kind = "http"
    is_aggregator = True
    contract_types = None

    async def search(self, query: SearchQuery, ctx: SourceContext) -> list[JobPosting]:
        params = {"what": query.keywords}
        if query.location and query.location.strip().lower() not in ("france", "remote"):
            params.update(where=query.location, where_type="city", perimeter=str(query.radius_km or 20))
        d = query.posted_within_days
        if d:
            params["period"] = "24_hours" if d <= 1 else "7_days" if d <= 7 else "15_days" if d <= 15 else "30_days"
        async with ctx.http(headers={"Referer": SITE + "/fr/"}) as client:
            try:
                r = await client.get(f"{SITE}/fr/query/", params=params)
            except httpx.HTTPError as e:
                raise SourceError(f"jobijoba request failed: {e}") from e
        if r.status_code >= 400:
            raise SourceError(f"jobijoba HTTP {r.status_code}")
        out = self.parse_results(r.text, ctx)
        if query.contract_types:
            out = [p for p in out if p.contract_type in (ContractType.UNKNOWN, *query.contract_types)]
        if query.remote_only:
            out = [p for p in out if p.remote == "full"]
        return out[: query.max_results]

    def parse_results(self, html: str, ctx: SourceContext) -> list[JobPosting]:
        soup = BeautifulSoup(html, "html.parser")
        out: list[JobPosting] = []
        seen: set[str] = set()
        for card in soup.select("div.offer[data-id]"):
            try:
                p = self._from_card(card)
            except Exception as e:  # one bad card must not kill the page
                ctx.log.debug("jobijoba skip card: %s", e)
                continue
            if p and p.source_id not in seen:
                seen.add(p.source_id)
                out.append(p)
        return out

    def _from_card(self, card) -> JobPosting | None:
        link = card.select_one("a.offer-link")
        title_el = card.select_one(".offer-header-title")
        if not (link and title_el):
            return None
        oid = card["data-id"].removeprefix("ad_")
        title = re.sub(r"\s+", " ", title_el.get_text(" ", strip=True))
        feats: dict[str, str] = {}
        salary = None
        for f in card.select(".offer-features .feature"):
            text = re.sub(r"\s+", " ", f.get_text(" ", strip=True))
            icon = next((c for c in (i.get("class") or [] for i in f.select("span[class^=icon-]")) for c in c if c.startswith("icon-")), "")
            if "€" in text:
                salary = text
            elif icon:
                feats[icon] = text
        location = re.sub(r"\s*\(à \d+ km[^)]*\)", "", feats.get("icon-map-marker", "")) or None
        contract = guess_contract(feats.get("icon-register")) if feats.get("icon-register") else ContractType.UNKNOWN
        if contract in (ContractType.UNKNOWN, ContractType.OTHER):
            m = re.search(r"\((CDI|CDD|Stage|Alternance|Intérim|Interim)\)\s*$", title, re.I)
            if m:
                contract = guess_contract(m.group(1))
        remote_txt = (feats.get("icon-home") or "").lower()
        remote = ("none" if "pas de télétravail" in remote_txt else "full" if "complet" in remote_txt else
                  "partial" if ("partiel" in remote_txt or "occasionnel" in remote_txt) else "unknown")
        detail = card.select_one('a[href*="/fr/annonce/"]')
        pub = card.select_one(".publication_date")
        desc = card.select_one(".description")
        lo, hi = parse_salary_text(salary)
        redirect = link.get("href")
        return JobPosting(
            source=self.name,
            source_id=oid,
            url=detail["href"] if detail else redirect,
            title=title,
            company=feats.get("icon-apartment"),
            location=location,
            contract_type=contract,
            remote=remote,
            salary_text=salary,
            salary_min_eur=lo if contract not in (ContractType.STAGE, ContractType.ALTERNANCE) else None,
            salary_max_eur=hi if contract not in (ContractType.STAGE, ContractType.ALTERNANCE) else None,
            description=re.sub(r"\s+", " ", desc.get_text(" ", strip=True)) if desc else None,  # search snippet only
            posted_at=parse_pub_date(pub.get_text(strip=True) if pub else None),
            apply_url=redirect,
            apply_method="external",
            raw={"redirect": redirect, "features": feats},
        )
