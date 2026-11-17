"""APEC (apec.fr): cadres + jeunes diplômés (and some stages / alternances).

Method: APEC's search page calls an internal JSON service, `POST /cms/webservices/rechercheOffre`
(body: motsCles, lieux, typesContrat, sorts[DATE desc], pagination{range,startIndex}, ...) and offer pages call
`GET /cms/webservices/offre/public?numeroOffre=...`. Everything on apec.fr (including those XHR) sits behind
DataDome: plain HTTP gets 403, headless Chrome and a fresh headful profile get the interactive CAPTCHA.
So the connector drives the real browser (BrowserPool): it loads the human search URL, captures the page's
own rechercheOffre request/response (so the exact body schema is whatever the site currently sends), and
pages by replaying that body with a new `pagination.startIndex` from inside the page. The DataDome clearance
cookie lives in the persistent browser profile: run `jobhunt login apec` once, solve the CAPTCHA by hand,
and later runs go through. We never attempt to solve the CAPTCHA automatically.

NOTE: could not be verified end-to-end against the live site from the build machine (CAPTCHA wall); response
parsing is defensive (several field aliases) and covered by an offline fixture that mirrors the documented
payload shape.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from typing import Any, ClassVar
from urllib.parse import urlencode

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

SITE = "https://www.apec.fr"
SEARCH_URL = SITE + "/candidat/recherche-emploi.html/emploi"
DETAIL_URL = SITE + "/candidat/recherche-emploi.html/emploi/detail-offre/{num}"
SEARCH_API = "/cms/webservices/rechercheOffre"
DETAIL_API = "/cms/webservices/offre/public"

_REPLAY_JS = """async ([url, body]) => {
  const r = await fetch(url, {method: 'POST', headers: {'Content-Type': 'application/json', 'Accept': 'application/json'}, body});
  return {status: r.status, text: await r.text()};
}"""


def _first(d: dict, *keys: str) -> Any:
    for k in keys:
        v = d.get(k)
        if v not in (None, "", []):
            return v
    return None


class ApecSource(Source):
    name = "apec"
    label = "APEC"
    kind = "browser"
    requires_login = False
    # Opening this lets the user clear the DataDome CAPTCHA once; the clearance persists in the profile.
    login_url: ClassVar[str] = "https://www.apec.fr/"
    contract_types = None

    async def search(self, query: SearchQuery, ctx: SourceContext) -> list[JobPosting]:
        if ctx.browser is None:
            raise SourceError("apec needs the browser pool (DataDome)")
        params = {"motsCles": query.keywords}
        if query.location or query.postal_code:
            async with ctx.http() as client:
                geo = await geocode_city(client, query.location, query.postal_code)
            dept = (geo[2] if geo else None) or query.department or dept_from_postal(query.postal_code)
            if dept:
                params["lieux"] = dept
        elif query.department:
            params["lieux"] = query.department

        captured: dict[str, Any] = {}

        def on_request(req):
            if SEARCH_API in req.url and req.method == "POST" and "body" not in captured:
                captured["url"], captured["body"] = req.url, req.post_data

        out: list[JobPosting] = []
        seen: set[str] = set()
        async with ctx.browser.page(ctx.profile_id, "apec") as page:
            page.on("request", on_request)
            holder: list[dict] = []

            async def on_response(resp):
                if SEARCH_API in resp.url and not holder:
                    try:
                        holder.append(json.loads(await resp.text()))
                    except Exception:
                        pass

            page.on("response", lambda r: asyncio.ensure_future(on_response(r)))
            await safe_goto(ctx.browser, page, f"{SEARCH_URL}?{urlencode(params)}", wait_until="networkidle")
            for _ in range(20):
                if holder:
                    break
                await asyncio.sleep(0.5)
            if not holder:
                raise SourceError("apec: search page did not issue rechercheOffre (layout changed or blocked)")
            data = holder[0]
            self._collect(data, query, ctx, out, seen)
            body = json.loads(captured["body"]) if captured.get("body") else None
            total = data.get("totalCount") or data.get("total") or 0
            start = len(data.get("resultats") or [])
            while body and len(out) < query.max_results and start < total and start < 200:
                body.setdefault("pagination", {})["startIndex"] = start
                await asyncio.sleep(1.5)
                res = await page.evaluate(_REPLAY_JS, [captured["url"], json.dumps(body)])
                if res["status"] != 200:
                    break
                data = json.loads(res["text"])
                got = data.get("resultats") or []
                if not got:
                    break
                start += len(got)
                self._collect(data, query, ctx, out, seen)
        return out[: query.max_results]

    def _collect(self, data: dict, query: SearchQuery, ctx: SourceContext, out: list, seen: set) -> None:
        cutoff = None
        if query.posted_within_days:
            cutoff = datetime.now(timezone.utc) - timedelta(days=query.posted_within_days)
        for r in data.get("resultats") or []:
            p = self._from_result(r, ctx)
            if not p or p.source_id in seen:
                continue
            if cutoff and p.posted_at and p.posted_at < cutoff:
                continue
            if query.contract_types and p.contract_type not in (ContractType.UNKNOWN, *query.contract_types):
                continue
            seen.add(p.source_id)
            out.append(p)

    def _from_result(self, r: dict, ctx: SourceContext) -> JobPosting | None:
        try:
            num = _first(r, "numeroOffre", "id")
            title = _first(r, "intitule", "titre")
            if not (num and title):
                return None
            num = str(num)
            salary = _first(r, "salaireTexte", "salaire")
            salary = salary if isinstance(salary, str) and any(c.isdigit() for c in salary) else None
            lo, hi = parse_salary_text(salary)
            lieu = _first(r, "lieuTexte", "lieu")
            if isinstance(lieu, dict):
                lieu = lieu.get("libelle")
            contract_label = _first(r, "typeContrat", "typeContratLibelle", "contrat")
            contract = guess_contract(contract_label if isinstance(contract_label, str) else None)
            if contract in (ContractType.UNKNOWN, ContractType.OTHER):
                contract = guess_contract(title) if guess_contract(title) in (ContractType.STAGE, ContractType.ALTERNANCE) else contract
            text = _first(r, "texteOffre", "texteHtml", "descriptif")
            return JobPosting(
                source=self.name,
                source_id=num,
                url=DETAIL_URL.format(num=num),
                title=str(title).strip(),
                company=_first(r, "nomCommercial", "nomCommercialEtablissement", "entreprise"),
                location=lieu,
                department=_dept_from_lieu(lieu),
                contract_type=contract,
                salary_text=salary,
                salary_min_eur=lo,
                salary_max_eur=hi,
                description=html_to_text(text) if isinstance(text, str) else None,
                posted_at=parse_dt(_first(r, "datePublication", "dateMiseEnLigne", "dateDebutPublication")),
                apply_method="platform",
                apply_url=DETAIL_URL.format(num=num),
                raw=r,
            )
        except Exception as e:
            ctx.log.debug("apec skip result: %s", e)
            return None

    # ---------- details ----------
    async def fetch_details(self, posting: JobPosting, ctx: SourceContext) -> JobPosting:
        if ctx.browser is None:
            return posting
        got: list[dict] = []

        async with ctx.browser.page(ctx.profile_id, "apec") as page:
            async def on_response(resp):
                if DETAIL_API in resp.url and not got:
                    try:
                        got.append(json.loads(await resp.text()))
                    except Exception:
                        pass

            page.on("response", lambda r: asyncio.ensure_future(on_response(r)))
            await safe_goto(ctx.browser, page, posting.url, wait_until="networkidle")
            for _ in range(16):
                if got:
                    break
                await asyncio.sleep(0.5)
        return self.apply_detail(posting, got[0]) if got else posting

    def apply_detail(self, p: JobPosting, d: dict) -> JobPosting:
        text = _first(d, "texteHtml", "texteOffre", "descriptif")
        parts = [html_to_text(text) if isinstance(text, str) else None]
        for k in ("texteHtmlProfil", "texteHtmlEntreprise"):
            if isinstance(d.get(k), str):
                parts.append(html_to_text(d[k]))
        desc = "\n\n".join(x for x in parts if x)
        if desc:
            p.description = desc
        salary = _first(d, "salaireTexte")
        if isinstance(salary, str) and any(c.isdigit() for c in salary):
            p.salary_text = salary
            p.salary_min_eur, p.salary_max_eur = parse_salary_text(salary)
        lieu = _first(d, "lieuTexte")
        if isinstance(lieu, str):
            p.location = lieu
            p.department = _dept_from_lieu(lieu) or p.department
        if d.get("datePublication"):
            p.posted_at = parse_dt(d["datePublication"]) or p.posted_at
        remote = str(_first(d, "typeTeletravail", "teletravail") or "").lower()
        if "total" in remote or "100" in remote:
            p.remote = "full"
        elif "partiel" in remote or "occasionnel" in remote:
            p.remote = "partial"
        return p


def _dept_from_lieu(lieu: str | None) -> str | None:
    """'Toulouse - 31' / 'Paris 08 - 75' -> '31' / '75'."""
    import re

    if not lieu:
        return None
    m = re.search(r"-\s*(\d{2,3}|2[AB])\s*$", lieu.strip())
    return m.group(1) if m else None
