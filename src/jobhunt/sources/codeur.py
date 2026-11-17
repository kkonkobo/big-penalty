"""Codeur.com: public list of client projects (fixed-price web/dev/design missions for freelancers).

Method: server-side rendered HTML, no login for reading the list and project pages:
    GET https://www.codeur.com/projects?q=<keywords>&page=N     (~30 cards per page, newest first)
    GET https://www.codeur.com/projects/<id>-<slug>             (full description + exact publication date)
Projects carry a *budget range* (e.g. "500 € à 1 000 €"), not a daily rate, so it goes in salary_text as a
project budget and salary_min/max_eur are left empty. Remote/location are not published.
Applying ("Faire une offre") requires a freelancer account on the platform.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import httpx
from selectolax.lexbor import LexborHTMLParser, LexborNode

from jobhunt.models import ContractType, JobPosting, SearchQuery
from jobhunt.sources._freelance_util import html_to_text, keyword_attempts, parse_relative_fr
from jobhunt.sources.base import ChallengeRequired, Source, SourceContext, SourceError

BASE = "https://www.codeur.com"
MONTHS = {m: i for i, m in enumerate(
    "janvier février mars avril mai juin juillet août septembre octobre novembre décembre".split(), 1)}


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", s.replace("\xa0", " ")).strip()


class CodeurSource(Source):
    name = "codeur"
    label = "Codeur.com"
    kind = "http"
    contract_types = frozenset({ContractType.FREELANCE})

    async def search(self, query: SearchQuery, ctx: SourceContext) -> list[JobPosting]:
        if query.contract_types and ContractType.FREELANCE not in query.contract_types:
            return []
        cutoff = datetime.now(timezone.utc) - timedelta(days=query.posted_within_days) if query.posted_within_days else None
        out: dict[str, JobPosting] = {}
        # The site's search is a strict AND on all words: if the full phrase finds nothing, retry on single words.
        attempts = keyword_attempts(query.keywords)
        try:
            # NB: with the default `Accept: text/html,...` header the site 301s to /projects and drops ?q=
            async with ctx.http(headers={"Accept": "*/*"}) as c:
                for kw in attempts:
                    if await self._collect(c, kw, query, cutoff, out):
                        break  # this phrasing matched something on the board (even if all too old): stop relaxing
        except httpx.HTTPError as e:
            raise SourceError(f"codeur.com error: {e}") from e
        return list(out.values())[: query.max_results]

    async def _collect(self, c, kw: str, query: SearchQuery, cutoff, out: dict) -> bool:
        found = False
        for page in range(1, 4):
            r = await c.get(f"{BASE}/projects", params={"q": kw, "page": page})
            if r.status_code in (403, 429, 503):
                raise ChallengeRequired(f"codeur.com returned {r.status_code}")
            if r.status_code >= 400:
                raise SourceError(f"codeur.com HTTP {r.status_code}")
            cards = self.parse_list(r.text)
            if not cards:
                return found
            found = True
            for p in cards:
                if cutoff and p.posted_at and p.posted_at < cutoff:
                    continue
                out.setdefault(p.source_id, p)
            if len(out) >= query.max_results:
                return found
            # out-of-range pages silently fall back to the unfiltered list: only follow a real "next" link
            if 'rel="next"' not in r.text:
                return found
        return found

    @staticmethod
    def parse_list(html: str, now: datetime | None = None) -> list[JobPosting]:
        tree = LexborHTMLParser(html)
        out: list[JobPosting] = []
        for a in tree.css("#projects-list > a[href^='/projects/']"):
            try:
                p = CodeurSource._parse_card(a, now)
            except Exception:
                continue
            if p:
                out.append(p)
        return out

    @staticmethod
    def _parse_card(a: LexborNode, now: datetime | None) -> JobPosting | None:
        href = a.attributes.get("href") or ""
        m = re.match(r"/projects/(\d+)-", href)
        if not m:
            return None
        h3 = a.css_first("h3")
        title = _clean(h3.text()) if h3 else _clean(a.attributes.get("aria-label") or "")
        when = None
        badges = []
        for span in a.css("span"):
            t = _clean(span.text())
            if t.lower().startswith("il y a") and when is None:
                when = parse_relative_fr(t, now)
            if span.attributes.get("title") == "Budget":
                badges.append(t)
        status = next((_clean(s.text()) for s in a.css("span.inline-flex") if _clean(s.text()) in ("Ouvert", "Fermé", "Terminé", "En cours", "Annulé")), None)
        if status and status != "Ouvert":
            return None
        budget = badges[0] if badges else None
        desc_node = a.css_first("div.line-clamp-3")
        tags = [_clean(s.text()) for s in a.css("div.max-h-6 span")]
        desc = (desc_node.text().strip() if desc_node else "") or None
        if desc:
            desc = re.sub(r"[ \t]+", " ", desc)
        if tags:
            desc = (desc or "") + "\n\nCatégories : " + ", ".join(t for t in tags if t)
        return JobPosting(
            source="codeur",
            source_id=m.group(1),
            url=urljoin(BASE, href),
            title=title,
            company=None,  # client identity is reserved to subscribed freelancers
            contract_type=ContractType.FREELANCE,
            salary_text=f"Budget projet : {budget}" if budget else None,
            description=desc,
            posted_at=when,
            apply_url=urljoin(BASE, href),
            apply_method="platform",
            raw={"status": status, "budget": budget},
        )

    async def fetch_details(self, posting: JobPosting, ctx: SourceContext) -> JobPosting:
        try:
            async with ctx.http(headers={"Accept": "*/*"}) as c:
                r = await c.get(posting.url)
            if r.status_code != 200:
                return posting
        except httpx.HTTPError:
            return posting
        return self.apply_detail(posting, r.text)

    @staticmethod
    def apply_detail(posting: JobPosting, html: str) -> JobPosting:
        tree = LexborHTMLParser(html)
        node = tree.css_first("div.project-description")
        upd: dict = {}
        if node:
            text = html_to_text(node.html)
            if text:
                extras = []
                text_all = tree.body.text() if tree.body else ""
                m = re.search(r"Estimation du délai\s*:\s*([^\n]+)", text_all)
                if m:
                    extras.append(f"Délai estimé : {_clean(m.group(1))}")
                upd["description"] = text + ("\n\n" + "\n".join(extras) if extras else "")
        body = tree.body.text() if tree.body else ""
        m = re.search(r"Publication\s*:\s*(\d{1,2})\s+(\w+)\s+(\d{4})(?:\s+à\s+(\d{1,2})h(\d{2}))?", body)
        if m and m.group(2).lower() in MONTHS:
            upd["posted_at"] = datetime(int(m.group(3)), MONTHS[m.group(2).lower()], int(m.group(1)),
                                        int(m.group(4) or 0), int(m.group(5) or 0), tzinfo=ZoneInfo("Europe/Paris"))
        return posting.model_copy(update=upd)
