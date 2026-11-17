"""Cadremploi (cadremploi.fr): jobs for cadres.

Method: the site's own JSON API `POST /nouveau/api/emploi/offers` (what the search page calls). It sits
behind Cloudflare (plain HTTP gets 403, headless Chrome gets "Attention Required"), so it is called from
inside a real, headful Chrome page (BrowserPool) after the search page has loaded and cleared Cloudflare;
the persistent profile keeps the clearance cookies. Details come from the detail page's SSR payload
(`__NUXT_DATA__`): full description, ATS redirect URL (`postuler.url`), company site, publication date.

Contract ids (`contracts`): 1 CDI, 2 CDD, 3 intérim, 4 contractuel, 5 statutaire, 7 freelance, 8 stage,
9 franchise, 13 apprentissage/alternance.
"""

from __future__ import annotations

import asyncio
import json
import re
import unicodedata
from datetime import datetime, timedelta, timezone
from typing import Any

from jobhunt.models import ContractType, JobPosting, SearchQuery
from jobhunt.sources._frboards_util import (
    dept_from_postal,
    geocode_city,
    guess_contract,
    html_to_text,
    parse_dt,
    parse_salary_text,
    safe_goto,
)
from jobhunt.sources.base import Source, SourceContext, SourceError

SITE = "https://www.cadremploi.fr"
CONTRACT_IDS: dict[ContractType, list[int]] = {
    ContractType.CDI: [1],
    ContractType.CDD: [2],
    ContractType.INTERIM: [3],
    ContractType.FREELANCE: [7],
    ContractType.STAGE: [8],
    ContractType.ALTERNANCE: [13],
}
ID_TO_CONTRACT = {1: ContractType.CDI, 2: ContractType.CDD, 3: ContractType.INTERIM, 7: ContractType.FREELANCE,
                  8: ContractType.STAGE, 13: ContractType.ALTERNANCE, 4: ContractType.OTHER, 5: ContractType.OTHER, 9: ContractType.OTHER}
PAGE_SIZE = 30

_FETCH_JS = """async (body) => {
  const r = await fetch('/nouveau/api/emploi/offers', {method: 'POST',
      headers: {'Content-Type': 'application/json', 'Accept': 'application/json'}, body: JSON.stringify(body)});
  return {status: r.status, text: await r.text()};
}"""


def _slug(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")


def parse_relative_date(text: str | None, now: datetime | None = None) -> datetime | None:
    """'Publiée il y a 8 jours' / 'il y a 3 heures' / 'il y a 1 mois' / "aujourd'hui" -> datetime (approx)."""
    if not text:
        return None
    now = now or datetime.now(timezone.utc)
    t = text.lower()
    if "aujourd" in t or "à l'instant" in t:
        return now
    if "hier" in t:
        return now - timedelta(days=1)
    m = re.search(r"il y a\s+(\d+)\s*(minute|heure|jour|semaine|mois|an)", t)
    if not m:
        return None
    n, unit = int(m.group(1)), m.group(2)
    delta = {"minute": timedelta(minutes=n), "heure": timedelta(hours=n), "jour": timedelta(days=n),
             "semaine": timedelta(weeks=n), "mois": timedelta(days=30 * n), "an": timedelta(days=365 * n)}[unit]
    return now - delta


class CadremploiSource(Source):
    name = "cadremploi"
    label = "Cadremploi"
    kind = "browser"
    contract_types = None

    # ---------- search ----------
    async def search(self, query: SearchQuery, ctx: SourceContext) -> list[JobPosting]:
        if ctx.browser is None:
            raise SourceError("cadremploi needs the browser pool (Cloudflare)")
        body: dict[str, Any] = {"page": 1, "query": query.keywords, "withRanking": True, "isSEO": True}
        rel = "/emploi/liste_offres"
        if query.contract_types:
            ids = sorted({i for c in query.contract_types for i in CONTRACT_IDS.get(c, [])})
            if not ids:
                return []
            body["contracts"] = ids
        async with ctx.http() as client:
            geo = await geocode_city(client, query.location, query.postal_code)
        if geo:
            lat, lng, _, city = geo
            pc = query.postal_code or await self._postal(ctx, query.location)
            if pc and city:
                body["cities"] = [f"{_slug(city)}-{pc}"]
            body.update(latitude=lat, longitude=lng, radius=query.radius_km or 20)
        body["listeRelativeUrl"] = rel

        out: list[JobPosting] = []
        seen: set[str] = set()
        async with ctx.browser.page(ctx.profile_id, "cadremploi") as page:
            await safe_goto(ctx.browser, page, SITE + "/emploi/liste_offres?" + _qs(query, geo), wait_until="networkidle")
            for page_no in range(1, 6):
                body["page"] = page_no
                res = await page.evaluate(_FETCH_JS, body)
                if res["status"] != 200:
                    raise SourceError(f"cadremploi offers API HTTP {res['status']}")
                data = json.loads(res["text"])
                batch = data.get("jobPostings") or []
                for jp in batch:
                    p = self._from_posting(jp, ctx)
                    if p and p.source_id not in seen and self._fresh(p, query):
                        seen.add(p.source_id)
                        out.append(p)
                if len(out) >= query.max_results or len(batch) < PAGE_SIZE or not batch:
                    break
                await asyncio.sleep(1.5)
        return out[: query.max_results]

    @staticmethod
    async def _postal(ctx: SourceContext, location: str | None) -> str | None:
        if not location:
            return None
        async with ctx.http() as client:
            try:
                r = await client.get("https://api-adresse.data.gouv.fr/search/",
                                     params={"q": location, "type": "municipality", "limit": 1})
                feats = r.json().get("features") or []
                if not feats:
                    return None
                pr = feats[0]["properties"]
                pc = (pr.get("postcode") or "").split(";")[0]
                # Paris / Lyon / Marseille: Cadremploi uses the "<dept>000" pseudo postcode
                if (pr.get("city") or "") in ("Paris", "Lyon", "Marseille"):
                    pc = {"Paris": "75000", "Lyon": "69000", "Marseille": "13000"}[pr["city"]]
                return pc or None
            except Exception:
                return None

    @staticmethod
    def _fresh(p: JobPosting, q: SearchQuery) -> bool:
        if not (q.posted_within_days and p.posted_at):
            return True
        return p.posted_at >= datetime.now(timezone.utc) - timedelta(days=q.posted_within_days + 1)

    # ---------- mapping ----------
    def _from_posting(self, jp: dict, ctx: SourceContext) -> JobPosting | None:
        try:
            oid = str(jp["id"])
            salary = (jp.get("salary") or "").strip() or None
            lo, hi = parse_salary_text(salary)
            company = (jp.get("company") or {}).get("name")
            apply_type = (jp.get("apply") or {}).get("type")
            return JobPosting(
                source=self.name,
                source_id=oid,
                url=f"{SITE}/emploi/detail_offre?offreId={oid}",
                title=jp["title"].strip(),
                company=company.strip() if company else None,
                location=jp.get("location") or None,
                contract_type=guess_contract(jp.get("contract")),
                salary_text=salary,
                salary_min_eur=lo,
                salary_max_eur=hi,
                description=html_to_text(jp.get("snippet")),
                posted_at=parse_relative_date(jp.get("date")),
                apply_method={"formulaire": "platform", "redirection": "external"}.get(apply_type or "", "unknown"),
                raw=jp,
            )
        except Exception as e:
            ctx.log.debug("cadremploi skip posting: %s", e)
            return None

    # ---------- details ----------
    async def fetch_details(self, posting: JobPosting, ctx: SourceContext) -> JobPosting:
        if ctx.browser is None:
            return posting
        try:
            async with ctx.browser.page(ctx.profile_id, "cadremploi") as page:
                await safe_goto(ctx.browser, page, posting.url, wait_until="networkidle")
                html = await page.content()
        except SourceError:
            raise
        except Exception as e:
            ctx.log.debug("cadremploi detail failed: %s", e)
            return posting
        offer = extract_offer(html)
        return self.apply_offer(posting, offer) if offer else posting

    def apply_offer(self, p: JobPosting, o: dict) -> JobPosting:
        poste = o.get("poste") or {}
        desc_parts = [html_to_text(poste.get("description")), html_to_text((o.get("profil") or {}).get("description"))]
        desc = "\n\n".join(x for x in desc_parts if x)
        if desc:
            p.description = desc
        loc = poste.get("localisation") or {}
        if loc.get("ville"):
            dep = (loc.get("departement") or {}).get("code")
            p.location = f"{loc['ville']} ({dep})" if dep else loc["ville"]
        if loc.get("codePostal"):
            p.postal_code = loc["codePostal"]
            p.department = dept_from_postal(loc["codePostal"]) or (loc.get("departement") or {}).get("code")
        tc = ((o.get("contrat") or {}).get("typeContrat") or {})
        if tc.get("code") in ID_TO_CONTRACT or tc.get("label"):
            ct = ID_TO_CONTRACT.get(tc.get("code")) or guess_contract(tc.get("label"))
            p.contract_type = ct
        rem = o.get("remuneration") or {}
        if not rem.get("masquer") and (rem.get("salaireMin") or rem.get("salaireMax")):
            lo, hi = rem.get("salaireMin"), rem.get("salaireMax")
            p.salary_min_eur, p.salary_max_eur = lo, hi or lo
            p.salary_text = p.salary_text or f"{lo}-{hi} € brut annuel"
        pub = parse_dt(o.get("datePublication"))
        if pub:
            p.posted_at = pub
        ent = (o.get("entreprise") or {})
        p.company = (ent.get("raisonSociale") or p.company or "").strip() or p.company
        p.company_website = (o.get("pageEntreprise") or {}).get("siteWebUrl") or p.company_website
        post = o.get("postuler") or {}
        if post.get("canal") == "redirect" and post.get("url"):
            p.apply_url, p.apply_method = post["url"], "external"
        elif post.get("postulerType") == "SITE_RECRUTEUR" and post.get("url"):
            p.apply_url, p.apply_method = post["url"], "external"
        elif post.get("canal") == "email" or post.get("contactEmail"):
            p.apply_method = "email"
        else:
            p.apply_url, p.apply_method = p.url, "platform"
        return p


def _qs(query: SearchQuery, geo) -> str:
    from urllib.parse import urlencode

    return urlencode({"query": query.keywords})


def extract_offer(html: str) -> dict | None:
    """Pull the `offer` object out of a detail page's Nuxt payload."""
    m = re.search(r'id="__NUXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    if not m:
        return None
    arr = json.loads(m.group(1))
    memo: dict[int, Any] = {}

    def res(i: int):
        if i in memo:
            return memo[i]
        v = arr[i]
        if isinstance(v, list):
            if v and isinstance(v[0], str) and v[0] in ("Reactive", "ShallowReactive", "Ref", "ShallowRef", "EmptyRef", "EmptyShallowRef"):
                memo[i] = res(v[1])
                return memo[i]
            if v and isinstance(v[0], str) and v[0] in ("Set", "Map", "Date", "null", "undefined", "BigInt", "RegExp", "Island", "NuxtError"):
                memo[i] = v
                return v
            out: list = []
            memo[i] = out
            out.extend(res(x) if isinstance(x, int) else x for x in v)
            return out
        if isinstance(v, dict):
            d: dict = {}
            memo[i] = d
            for k, x in v.items():
                d[k] = res(x) if isinstance(x, int) else x
            return d
        memo[i] = v
        return v

    root = res(0)
    data = root.get("data") if isinstance(root, dict) else None
    for k, v in (data or {}).items():
        if k.startswith("offer-detail-") and isinstance(v, dict) and isinstance(v.get("offer"), dict):
            return v["offer"]
    return None
