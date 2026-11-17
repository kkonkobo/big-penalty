"""HelloWork (hellowork.com/fr-fr) via python-jobspy's HelloWork scraper (HTML search + job-page JSON-LD).

Not flagged as an aggregator: many offers are applied for directly on HelloWork. Redirected offers
are not distinguishable from the search card, so `apply_url` is left None and apply_method is
"platform"; `company_website` comes from the job page's JSON-LD (hiringOrganization.sameAs).
"""

from __future__ import annotations

from typing import Any, ClassVar, Literal

from jobhunt.models import JobPosting, SearchQuery
from jobhunt.sources._common import jobspy_scrape, row_to_posting
from jobhunt.sources.base import Source, SourceContext


def row_to_hellowork_posting(row: dict[str, Any]) -> JobPosting | None:
    p = row_to_posting(row, source="hellowork", id_prefix="hw-")
    if p is None:
        return None
    p.company_website = row.get("company_url_direct") or row.get("company_url") or None
    p.apply_method = "platform"
    return p


class HelloWorkSource(Source):
    name: ClassVar[str] = "hellowork"
    label: ClassVar[str] = "HelloWork"
    kind: ClassVar[Literal["api", "http", "browser"]] = "http"
    is_aggregator: ClassVar[bool] = False

    async def search(self, query: SearchQuery, ctx: SourceContext) -> list[JobPosting]:
        # fetch_description = one extra GET per job (JSON-LD: description, date, company site).
        rows = await jobspy_scrape("hellowork", query, country="france", fetch_description=True)
        out: list[JobPosting] = []
        for row in rows:
            try:
                p = row_to_hellowork_posting(row)
            except Exception as e:
                ctx.log.debug("hellowork: skipping row: %s", e)
                continue
            if p:
                out.append(p)
        return out[: query.max_results]
