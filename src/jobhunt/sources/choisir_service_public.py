"""Choisir le service public (choisirleservicepublic.gouv.fr): public-sector jobs (État, territoriale, hospitalière).

Plain HTTP works. The public search is a WordPress site whose *path* URLs are server-rendered:
    https://choisirleservicepublic.gouv.fr/nos-offres/filtres/mot-cles/<kw>/localisation/<id>/page/<n>/
(`?s=` query-string searches only 404 for curl; `localisation` ids = region / département ids scraped from the
site's filter form, shipped in src/jobhunt/data/csp_locations.json; `domaine/<id>` also exists). 20 cards per page.
Detail pages carry a JSON-LD-ish block plus `ul.table` facts (nature de l'emploi, rémunération, catégorie,
télétravail) and a "Postuler sur le site employeur" link.

Keyword search is OR-like and relevance sorted; public employers rarely publish stages/alternances, so for
internship/apprenticeship-only queries cards are post-filtered on the title.

`place-emploi-public.gouv.fr` (PEP) was merged into this portal: its offers are listed here (reference numbers
"2026-NNNNNNN"), so no separate connector is needed.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote

import httpx
from bs4 import BeautifulSoup

from jobhunt.fetch import get_html
from jobhunt.models import ContractType, JobPosting, SearchQuery
from jobhunt.sources._fr_public import (
    clean, contract_from_title, detect_remote, find_region, geocode, html_to_text, norm, parse_fr_date, parse_salary,
)
from jobhunt.sources.base import Source, SourceContext, SourceError

log = logging.getLogger("jobhunt.sources.choisir_service_public")

BASE = "https://choisirleservicepublic.gouv.fr"
PAGE = 20
MAX_PAGES = 5
MAX_PAGES_INTERN = 2  # internship/apprenticeship-only: relevance puts keyword+stage matches first, then noise
_LOC_FILE = Path(__file__).resolve().parent.parent / "data" / "csp_locations.json"
_DEP_RE = re.compile(r"\((\d{2,3}|2[AB])\)")


@lru_cache
def _locations() -> dict:
    return json.loads(_LOC_FILE.read_text(encoding="utf-8"))


def location_id(region: str | None = None, department: str | None = None) -> str | None:
    """Site filter id for a region / département *name* (accents & hyphens ignored)."""
    loc = _locations()
    if department and (i := loc["departments"].get(norm(department))):
        return i
    if region and (i := loc["regions"].get(norm(region).replace("provence alpes cote d azur", "provence alpes cote d azur"))):
        return i
    return None


def _ref(url: str) -> str | None:
    m = re.search(r"reference-(\d{4}-\d+)", url)
    return m.group(1) if m else None


def parse_listing(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for card in soup.select(".fr-card"):
        a = card.select_one(".fr-card__title a[href*='/offre-emploi/']")
        if not a:
            continue
        item = {"url": a["href"], "title": clean(a.get_text()), "domain": None, "location": None, "versant": None, "employer": None, "date": None}
        tag = card.select_one(".fr-tags-group .fr-tag")
        item["domain"] = clean(tag.get_text()) if tag else None
        for li in card.select(".fr-card__desc li"):
            label = li.select_one(".sr-only")
            lab = clean(label.get_text()) if label else ""
            if label:
                label.extract()
            val = clean(li.get_text(" "))
            if lab.startswith("Localisation"):
                item["location"] = val
            elif lab.startswith("Fonction publique"):
                item["versant"] = val
            elif lab.startswith("Employeur"):
                item["employer"] = val
            elif val and "En ligne depuis" in val:
                item["date"] = val
        out.append(item)
    return out


def listing_to_posting(it: dict) -> JobPosting | None:
    ref = _ref(it["url"])
    if not ref or not it.get("title"):
        return None
    loc = it.get("location") or ""
    m = _DEP_RE.search(loc)
    dep = m.group(1) if m else None
    location = re.sub(r"\s+\(", " (", loc).strip() or None
    return JobPosting(
        source="choisir_service_public",
        source_id=ref,
        url=it["url"],
        title=it["title"],
        company=it.get("employer"),
        location=location,
        department=dep,
        contract_type=contract_from_title(it["title"], ContractType.UNKNOWN),
        posted_at=parse_fr_date(it.get("date")),
        apply_method="unknown",
        raw={"listing": it, "domain": it.get("domain"), "versant": it.get("versant")},
    )


def parse_detail(html: str, p: JobPosting) -> JobPosting:
    soup = BeautifulSoup(html, "html.parser")
    upd: dict = {}
    raw = dict(p.raw)
    # ---- facts table
    facts: dict[str, str] = {}
    for li in soup.select("ul.table li.table-cell"):
        k = li.find("strong")
        if not k:
            continue
        label = clean(k.get_text()) or ""
        k.extract()
        offer_results = [clean(x.get_text()) for x in li.select(".offer-result")]
        facts[label] = " | ".join(x for x in offer_results if x) if offer_results else (clean(li.get_text(" ")) or "")
    raw["facts"] = facts
    nature = next((v for k, v in facts.items() if k.lower().startswith("nature")), "")
    exp = next((v for k, v in facts.items() if k.lower().startswith("exp")), "")
    if exp and "non renseign" not in exp.lower():
        upd["experience_level"] = exp
    # ---- salary (contractuels range first, then fonctionnaires)
    sal_cell = soup.select_one("li[data-module=salary]")
    if sal_cell:
        results = [clean(x.get_text()) or "" for x in sal_cell.select(".offer-result")]
        for r in results:
            lo, hi = parse_salary(r)
            if lo:
                upd.update(salary_text=r, salary_min_eur=lo, salary_max_eur=hi)
                break
    tele = next((v for k, v in facts.items() if k.lower().startswith("télétravail") or k.lower().startswith("teletravail")), "")
    # ---- description: all .rte sections of the page up to the "recommended offers" strip
    parts: list[str] = []
    for rte in soup.select(".strate-two-columns .rte"):
        for btn in rte.select("button, .text-clamp--btn, [data-module*=clamp] button"):
            btn.decompose()
        for h2 in rte.find_all("h2"):
            title = clean(h2.get_text())
            body = []
            for sib in h2.find_next_siblings():
                if sib.name == "h2":
                    break
                body.append(sib.get_text("\n") if sib.name not in ("div", "ul", "p") else (html_to_text(str(sib)) or ""))
            text = "\n".join(b for b in body if b and b.strip())
            text = re.sub(r"\n?Afficher la suite\n?", "\n", text).strip()
            if text:
                parts.append(f"{title}\n{text}")
    desc = "\n\n".join(parts) or None
    if not desc:  # fallback: JSON-LD Description
        for s in soup.select('script[type="application/ld+json"]'):
            try:
                ld = json.loads(s.string or "", strict=False)
            except ValueError:
                continue
            if isinstance(ld, dict) and ld.get("Description"):
                desc = html_to_text(ld["Description"])
                break
    if desc:
        upd["description"] = desc
    # ---- remote
    if tele:
        t = norm(tele)
        remote = "partial" if t.startswith("oui") else "none" if t.startswith("non") and "renseign" not in t else detect_remote(tele)
        if remote == "unknown":
            remote = detect_remote(desc)
        upd["remote"] = remote
    else:
        upd["remote"] = detect_remote(desc)
    # ---- contract
    low = (desc or "").lower() + " " + nature.lower()
    ctype = p.contract_type
    if ctype == ContractType.UNKNOWN:
        if re.search(r"stagiaire|\bstage\b", p.title, re.I):
            ctype = ContractType.STAGE
        elif re.search(r"apprenti|alternan", p.title, re.I) or "contrat d'apprentissage" in low or "contrat d’apprentissage" in low:
            ctype = ContractType.ALTERNANCE
        elif "uniquement aux contractuels" in low or "contrat à durée déterminée" in low or re.search(r"\bcdd\b|contrat de projet", low):
            ctype = ContractType.CDD
        elif re.search(r"\bcdi\b|durée indéterminée", low) and "titulaires et" not in low:
            ctype = ContractType.CDI
        elif "réservé aux fonctionnaires" in low:
            ctype = ContractType.OTHER  # titulaire de la fonction publique
    upd["contract_type"] = ctype
    # ---- header facts: employer / location
    for li in soup.select("ul.fr-grid-row li.ic"):
        lab = li.select_one(".sr-only")
        if lab and lab.get_text(strip=True).startswith("Employeur"):
            lab.extract()
            emp = clean(li.get_text(" "))
            if emp:
                upd["company"] = emp
    # ---- JSON-LD location (département / région)
    for s in soup.select('script[type="application/ld+json"]'):
        try:
            ld = json.loads(s.string or "", strict=False)
        except ValueError:
            continue
        if isinstance(ld, dict) and ld.get("@type") == "JobPosting":
            addr = ((ld.get("jobLocation") or {}).get("address") or {})
            loc = clean(addr.get("addressLocality"))
            if loc and not p.location:
                upd["location"] = re.sub(r",\s*France$", "", loc)
                m = _DEP_RE.search(loc)
                if m:
                    upd["department"] = m.group(1)
            raw["region"] = addr.get("addressRegion")
            break
    # ---- apply: employer site link, else the portal's own "Postuler" button (needs a candidate account)
    link = next((x for x in soup.select("a[href]") if "postuler" in (x.get_text() or "").lower()), None)
    if link and link["href"].startswith("mailto:"):
        upd.update(apply_url=link["href"], apply_method="email")
    elif link and link["href"].startswith("http"):
        upd.update(apply_url=link["href"], apply_method="external")
    elif any("postuler" in (b.get_text() or "").lower() for b in soup.select("button")):
        upd.update(apply_url=p.url, apply_method="platform")
    # ---- reference / header date
    upd["raw"] = raw
    return p.model_copy(update=upd)


def _is_intern_only(cts: set[ContractType]) -> bool:
    return bool(cts) and cts <= {ContractType.STAGE, ContractType.ALTERNANCE}


class ChoisirServicePublicSource(Source):
    name = "choisir_service_public"
    label = "Choisir le service public"
    kind = "http"
    is_aggregator = False

    async def _loc_segment(self, query: SearchQuery, ctx: SourceContext) -> str | None:
        loc = (query.location or "").strip()
        async with ctx.http() as c:
            if query.postal_code or (loc and norm(loc) not in ("france", "toute la france", "remote")):
                if loc and not query.postal_code:
                    region = await find_region(c, loc)
                    if region and (i := location_id(region=region["nom"])):
                        return i
                place = await geocode(c, loc or None, query.postal_code)
                if place:
                    # context: "31, Haute-Garonne, Occitanie"
                    parts = [x.strip() for x in (place.get("region") and [place["region"]] or [])]
                    r = await c.get("https://geo.api.gouv.fr/departements/" + str(place.get("dept"))) if place.get("dept") else None
                    dep_name = r.json().get("nom") if r is not None and r.status_code == 200 else None
                    return (location_id(department=dep_name) if dep_name else None) or (location_id(region=parts[0]) if parts else None)
                ctx.log.warning("choisir_service_public: could not resolve location %r", loc)
            elif query.department:
                r = await c.get("https://geo.api.gouv.fr/departements/" + query.department)
                if r.status_code == 200:
                    return location_id(department=r.json().get("nom"))
        return None

    async def search(self, query: SearchQuery, ctx: SourceContext) -> list[JobPosting]:
        cts = set(query.contract_types)
        loc_id = await self._loc_segment(query, ctx)
        intern_only = _is_intern_only(cts)
        kws = [query.keywords.strip()]
        if intern_only:
            base_kw = re.sub(r"(?i)\b(stages?|stagiaires?|alternance|apprenti\w*)\b", " ", kws[0]).strip()
            kws = []
            if ContractType.STAGE in cts:
                kws.append(f"{base_kw} stage".strip())
            if ContractType.ALTERNANCE in cts:
                kws.append(f"{base_kw} apprenti alternance".strip())
        seen: dict[str, JobPosting] = {}
        limit = datetime.now(timezone.utc) - timedelta(days=query.posted_within_days) if query.posted_within_days else None
        for kw in kws:
            segs = [f"mot-cles/{quote(kw)}"] if kw else []
            if loc_id:
                segs.append(f"localisation/{loc_id}")
            base = f"{BASE}/nos-offres/filtres/" + "/".join(segs) + "/" if segs else f"{BASE}/nos-offres/"
            for page in range(1, (MAX_PAGES_INTERN if intern_only else MAX_PAGES) + 1):
                url = base if page == 1 else f"{base}page/{page}/"
                html = await get_html(url, ctx, site="choisir_service_public")
                items = parse_listing(html)
                if not items:
                    break
                for it in items:
                    p = listing_to_posting(it)
                    if not p or p.source_id in seen:
                        continue
                    if intern_only and p.contract_type not in cts:
                        continue
                    if limit and p.posted_at and p.posted_at < limit:
                        continue
                    seen[p.source_id] = p
                if len(seen) >= query.max_results or len(items) < PAGE:
                    break
                await asyncio.sleep(random.uniform(0.4, 0.9))
            if len(seen) >= query.max_results:
                break
        out = list(seen.values())
        return out[: query.max_results]

    async def fetch_details(self, posting: JobPosting, ctx: SourceContext) -> JobPosting:
        try:
            html = await get_html(posting.url, ctx, site="choisir_service_public")
        except SourceError as e:
            ctx.log.debug("choisir_service_public detail failed: %s", e)
            return posting
        return parse_detail(html, posting)
