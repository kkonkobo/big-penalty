"""Meteojob (meteojob.com, Cleverconnect group).

Method: the site's own JSON API `GET /api/joboffers/search` (what the Angular search page calls), reachable
with plain httpx. Parameters: what, where, sorting=DATE|SCORING, page, limit (<=50), facetContract (repeatable:
CDI, CDD, INTERIM, TRAINING, APPRENTICE, INDEPEDENT, VIE...), facetSince (1/3/7/30 days). Results carry the full
description, salary, telework, and `url.redirect` (company/ATS or partner URL when the offer is not handled on
Meteojob; "mailto:" when applying by e-mail).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx

from jobhunt.models import ContractType, JobPosting, SearchQuery
from jobhunt.sources._frboards_util import html_to_text, parse_dt
from jobhunt.sources.base import Source, SourceContext, SourceError

SITE = "https://www.meteojob.com"
API = SITE + "/api/joboffers/search"

CONTRACT_PARAM = {
    ContractType.CDI: ["CDI"],
    ContractType.CDD: ["CDD"],
    ContractType.INTERIM: ["INTERIM"],
    ContractType.STAGE: ["TRAINING"],
    ContractType.ALTERNANCE: ["APPRENTICE"],
    ContractType.FREELANCE: ["INDEPEDENT"],  # sic (site spelling)
    ContractType.VIE: ["VIE"],
}
PARAM_TO_CONTRACT = {
    "CDI": ContractType.CDI, "CDIC": ContractType.CDI, "CDI_INTERIMAIRE": ContractType.CDI, "INTERMIT": ContractType.CDI,
    "CDD": ContractType.CDD, "SEASONAL": ContractType.CDD, "CIDD": ContractType.CDD, "EXTRA": ContractType.CDD,
    "INTERIM": ContractType.INTERIM, "TRAINING": ContractType.STAGE, "APPRENTICE": ContractType.ALTERNANCE,
    "INDEPEDENT": ContractType.FREELANCE, "VIE": ContractType.VIE,
}
REMOTE = {"NONE": "none", "OCCASIONAL": "partial", "FULL": "full"}
_PERIOD = {"ANNUM": 1, "YEAR": 1, "MONTH": 12}


class MeteojobSource(Source):
    name = "meteojob"
    label = "Meteojob"
    kind = "http"
    contract_types = None

    async def search(self, query: SearchQuery, ctx: SourceContext) -> list[JobPosting]:
        params: list[tuple[str, str]] = [
            ("what", query.keywords),
            ("sorting", "DATE"),
            ("limit", str(min(max(query.max_results, 10), 50))),
            ("expandLocations", "true"),
        ]
        if query.location and query.location.strip().lower() not in ("france", "remote"):
            params.append(("where", query.location))
        elif query.postal_code:
            params.append(("where", query.postal_code))
        wanted = [v for c in query.contract_types for v in CONTRACT_PARAM.get(c, [])]
        if query.contract_types and not wanted:
            return []
        params += [("facetContract", v) for v in dict.fromkeys(wanted)]
        if query.posted_within_days:
            d = query.posted_within_days
            params.append(("facetSince", str(1 if d <= 1 else 3 if d <= 3 else 7 if d <= 7 else 30)))
        cutoff = datetime.now(timezone.utc) - timedelta(days=query.posted_within_days) if query.posted_within_days else None

        out: list[JobPosting] = []
        seen: set[str] = set()
        async with ctx.http(headers={"Accept": "application/json", "Referer": SITE + "/jobs"}) as client:
            for page in range(1, 5):
                try:
                    r = await client.get(API, params=[*params, ("page", str(page))])
                except httpx.HTTPError as e:
                    raise SourceError(f"meteojob search failed: {e}") from e
                if r.status_code >= 400:
                    raise SourceError(f"meteojob HTTP {r.status_code}: {r.text[:200]}")
                data = r.json()
                batch = data.get("content") or []
                for o in batch:
                    p = self._from_offer(o, ctx)
                    if not p or p.source_id in seen:
                        continue
                    if cutoff and p.posted_at and p.posted_at < cutoff:
                        continue
                    if query.remote_only and p.remote != "full":
                        continue
                    seen.add(p.source_id)
                    out.append(p)
                if len(out) >= query.max_results or not batch or page * len(batch) >= data.get("total", 0):
                    break
        return out[: query.max_results]

    def _from_offer(self, o: dict, ctx: SourceContext) -> JobPosting | None:
        try:
            oid = str(o["id"])
            loc = (o.get("locations") or [{}])[0]
            lbl = o.get("labels") or {}
            contract = ContractType.UNKNOWN
            for c in o.get("contractTypes") or []:
                contract = PARAM_TO_CONTRACT.get(c, ContractType.OTHER)
                break
            sal = o.get("salary") or {}
            lo = hi = None
            factor = _PERIOD.get(sal.get("period") or "ANNUM")
            if sal.get("from") and factor and contract not in (ContractType.STAGE, ContractType.ALTERNANCE):
                lo = int(sal["from"] * factor)
                hi = int((sal.get("to") or sal["from"]) * factor) if sal.get("to") else None
            salary_text = (lbl.get("salary") or {}).get("value")
            if salary_text and "non précisé" in salary_text.lower():
                salary_text = None
            salary_text = salary_text.replace("\xa0", " ") if salary_text else None
            desc = "\n\n".join(x for x in (html_to_text(o.get("description")), html_to_text(o.get("profileDescription"))) if x)
            redirect = (o.get("url") or {}).get("redirect") or ""
            if redirect.startswith("mailto:"):
                apply_method, apply_url = "email", None
            elif redirect.startswith("http"):
                apply_method, apply_url = "external", redirect
            else:
                apply_method, apply_url = "platform", None
            url = SITE + ((o.get("url") or {}).get("jobOffer") or f"/jobs/{oid}")
            if apply_method == "platform":
                apply_url = url
            return JobPosting(
                source=self.name,
                source_id=oid,
                url=url,
                title=o["title"].strip(),
                company=None if o.get("anonymous") else (o.get("company") or {}).get("name"),
                location=loc.get("name") or o.get("locality"),
                postal_code=loc.get("admin3Code") if str(loc.get("admin3Code", "")).isdigit() else None,
                department=loc.get("admin2Code"),
                country=(loc.get("countryCode") or "FR"),
                contract_type=contract,
                remote=REMOTE.get(((lbl.get("telework") or {}).get("meteojobId")) or "", "unknown"),
                salary_text=salary_text,
                salary_min_eur=lo,
                salary_max_eur=hi,
                description=desc or None,
                posted_at=parse_dt(o.get("publicationDate")),
                apply_url=apply_url,
                apply_method=apply_method,
                experience_level=", ".join(x["value"] for x in lbl.get("experienceLevelList") or []) or None,
                raw=o,
            )
        except Exception as e:
            ctx.log.debug("meteojob skip offer: %s", e)
            return None
