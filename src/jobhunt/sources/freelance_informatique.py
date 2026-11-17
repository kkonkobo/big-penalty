"""Freelance-Informatique (freelance-informatique.fr): public IT freelance mission list.

Method: server-side rendered; the list page embeds a schema.org JSON-LD `ItemList` of `JobPosting`s:
    GET https://www.freelance-informatique.fr/offres-freelance?keywords=<kw>&page=N    (36 per page, newest first)
    GET https://www.freelance-informatique.fr/mission-<slug>-<ref>                      (duration, start date, remote)
No login needed to read. The daily rate ("Tarif Journalier Moyen") is hidden behind a click/registration,
so salary_text stays None. Client name is not published (hiringOrganization is the board itself).
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

import httpx
import json
from selectolax.lexbor import LexborHTMLParser

from jobhunt.models import ContractType, JobPosting, SearchQuery
from jobhunt.sources._freelance_util import html_to_text, keyword_attempts, parse_iso
from jobhunt.sources.base import ChallengeRequired, Source, SourceContext, SourceError

BASE = "https://www.freelance-informatique.fr"


class FreelanceInformatiqueSource(Source):
    name = "freelance_informatique"
    label = "Freelance-Informatique"
    kind = "http"
    contract_types = frozenset({ContractType.FREELANCE})

    async def search(self, query: SearchQuery, ctx: SourceContext) -> list[JobPosting]:
        if query.contract_types and ContractType.FREELANCE not in query.contract_types:
            return []
        cutoff = datetime.now(timezone.utc) - timedelta(days=query.posted_within_days) if query.posted_within_days else None
        out: dict[str, JobPosting] = {}
        try:
            async with ctx.http() as c:
                for kw in keyword_attempts(query.keywords):
                    if await self._collect(c, kw, query, cutoff, out):
                        break  # this phrasing matched something on the board (even if all too old): stop relaxing
        except httpx.HTTPError as e:
            raise SourceError(f"freelance-informatique error: {e}") from e
        return list(out.values())[: query.max_results]

    async def _collect(self, c, kw: str, query: SearchQuery, cutoff, out: dict) -> bool:
        found = False
        for page in range(1, 4):
            params: dict = {"keywords": kw}
            if page > 1:
                params["page"] = page
            r = await c.get(f"{BASE}/offres-freelance", params=params)
            if r.status_code in (403, 429, 503):
                raise ChallengeRequired(f"freelance-informatique returned {r.status_code}")
            if r.status_code >= 400:
                raise SourceError(f"freelance-informatique HTTP {r.status_code}")
            items = self.parse_list(r.text)
            if not items:
                return found
            found = True
            for p in items:
                if cutoff and p.posted_at and p.posted_at < cutoff:
                    continue
                if query.location and not self._loc_ok(p, query):
                    continue
                out.setdefault(p.source_id, p)
            if len(out) >= query.max_results or len(items) < 30:
                return found
        return found

    @staticmethod
    def _loc_ok(p: JobPosting, query: SearchQuery) -> bool:
        """The site has no location filter in the URL; keep unknown/remote ones, drop clear mismatches only for departments."""
        if not query.department or not p.department:
            return True
        return p.department == query.department

    @staticmethod
    def parse_list(html: str) -> list[JobPosting]:
        out: list[JobPosting] = []
        for m in re.finditer(r'<script type="application/ld\+json"[^>]*>(.*?)</script>', html, re.S):
            try:
                data = json.loads(m.group(1))
            except ValueError:
                continue
            if data.get("@type") != "ItemList":
                continue
            for el in data.get("itemListElement", []):
                try:
                    out.append(FreelanceInformatiqueSource.parse_item(el.get("item") or {}))
                except Exception:
                    continue
        return out

    @staticmethod
    def parse_item(it: dict) -> JobPosting:
        addr = ((it.get("jobLocation") or {}).get("address")) or {}
        city = (addr.get("addressLocality") or "").strip()
        pc = (addr.get("postalCode") or "").strip()  # dept ("31") or full code ("75001")
        dept = pc[:2] or None
        location = ", ".join(x for x in (city.title() if city.isupper() else city, addr.get("addressRegion")) if x) or None
        if city and pc:
            location = f"{city.title() if city.isupper() else city} ({dept})"
        ref = (it.get("identifier") or {}).get("value") or it["url"].rsplit("-", 1)[-1]
        title = html_to_text(it["title"]) or it["title"]
        text = html_to_text(it.get("description"))
        return JobPosting(
            source="freelance_informatique",
            source_id=str(ref),
            url=it["url"],
            title=title,
            company=None,
            location=location,
            postal_code=pc if len(pc) == 5 else None,
            department=dept,
            contract_type=ContractType.FREELANCE,
            description=text or None,
            posted_at=parse_iso(it.get("datePosted")),
            apply_url=it["url"],
            apply_method="platform",
            raw={"industry": it.get("industry"), "validThrough": it.get("validThrough")},
        )

    async def fetch_details(self, posting: JobPosting, ctx: SourceContext) -> JobPosting:
        try:
            async with ctx.http() as c:
                r = await c.get(posting.url)
            if r.status_code != 200:
                return posting
        except httpx.HTTPError:
            return posting
        return self.apply_detail(posting, r.text)

    @staticmethod
    def apply_detail(posting: JobPosting, html: str) -> JobPosting:
        """Pull the 'Date de début / Localisation / Durée' block of the mission page into the description."""
        tree = LexborHTMLParser(html)
        for n in tree.css("script,style,nav,footer,header"):
            n.decompose()
        lines = [ln.strip() for ln in (tree.body.text(separator="\n") if tree.body else "").splitlines() if ln.strip()]
        facts: dict[str, str] = {}
        for label in ("Date de début", "Localisation", "Durée", "Profil"):
            if label in lines:
                i = lines.index(label)
                if i + 1 < len(lines):
                    facts[label] = lines[i + 1]
        if not facts:
            return posting
        remote = posting.remote
        loc = facts.get("Localisation", "")
        if re.search(r"t[ée]l[ée]travail|remote", loc, re.I):
            remote = "full"
        extra = []
        if facts.get("Durée"):
            extra.append(f"Durée : {facts['Durée']}")
        if facts.get("Date de début"):
            extra.append(f"Démarrage : {facts['Date de début']}")
        if loc:
            extra.append(f"Localisation : {loc}")
        desc = "\n".join(extra) + ("\n\n" + posting.description if posting.description else "")
        return posting.model_copy(update={"description": desc, "remote": remote})
