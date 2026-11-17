"""Indeed France via python-jobspy (Indeed's mobile GraphQL API; no browser needed).

Aggregator: discovery only. jobspy exposes `job_url_direct` (the employer / ATS apply URL) when the
posting is hosted off Indeed; that URL is the best input for the origin resolver.
"""

from __future__ import annotations

from typing import Any, ClassVar, Literal

from jobhunt.models import JobPosting, SearchQuery
from jobhunt.sources._common import jobspy_scrape, row_to_posting
from jobhunt.sources.base import Source, SourceContext


def row_to_indeed_posting(row: dict[str, Any]) -> JobPosting | None:
    p = row_to_posting(row, source="indeed", id_prefix="in-")
    if p is None:
        return None
    direct = row.get("job_url_direct")
    if direct:
        p.apply_url = direct
        p.apply_method = "external"
    else:
        # jobspy drops viewJobUrl when it points back to indeed.com => Indeed's own apply flow.
        p.apply_method = "easy_apply"
    return p


class IndeedSource(Source):
    domains = ("indeed.com", "indeed.fr", "indeed.co.uk")
    name: ClassVar[str] = "indeed"
    label: ClassVar[str] = "Indeed"
    kind: ClassVar[Literal["api", "http", "browser"]] = "http"
    is_aggregator: ClassVar[bool] = True

    async def search(self, query: SearchQuery, ctx: SourceContext) -> list[JobPosting]:
        rows = await jobspy_scrape("indeed", query, country="france")
        out: list[JobPosting] = []
        for row in rows:
            try:
                p = row_to_indeed_posting(row)
            except Exception as e:  # one bad row must not sink the batch
                ctx.log.debug("indeed: skipping row: %s", e)
                continue
            if p:
                out.append(p)
        return out[: query.max_results]
