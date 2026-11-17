"""Glassdoor France via python-jobspy (Glassdoor's GraphQL endpoint on glassdoor.fr).

Aggregator: discovery only. Glassdoor does not expose the employer apply URL through jobspy, so
`apply_url` stays None; the origin resolver has to find the company posting from title + company.
Salaries are Glassdoor's p10-p90 estimates, labelled "(estimation)".
"""

from __future__ import annotations

from typing import Any, ClassVar, Literal

from jobhunt.models import JobPosting, SearchQuery
from jobhunt.sources._common import jobspy_scrape, row_to_posting
from jobhunt.sources.base import Source, SourceContext, SourceError


def row_to_glassdoor_posting(row: dict[str, Any]) -> JobPosting | None:
    p = row_to_posting(row, source="glassdoor", id_prefix="gd-", estimated_salary=True)
    if p is None:
        return None
    p.apply_method = "unknown"
    return p


class GlassdoorSource(Source):
    domains = ("glassdoor.com", "glassdoor.fr")
    name: ClassVar[str] = "glassdoor"
    label: ClassVar[str] = "Glassdoor"
    kind: ClassVar[Literal["api", "http", "browser"]] = "http"
    is_aggregator: ClassVar[bool] = True
    # Works live today (jobspy), but the endpoint relies on a hardcoded CSRF token that Glassdoor
    # rotates; keep it opt-in until it proves stable.
    enabled_by_default: ClassVar[bool] = False

    async def search(self, query: SearchQuery, ctx: SourceContext) -> list[JobPosting]:
        # Glassdoor resolves `location` through its own autocomplete (cities, regions, "France").
        try:
            rows = await jobspy_scrape("glassdoor", query, country="france", fetch_description=True)
        except SourceError as e:
            raise SourceError(
                f"{e} (Glassdoor needs a location its autocomplete recognises: a French city, region or 'France')"
            ) from e
        out: list[JobPosting] = []
        for row in rows:
            try:
                p = row_to_glassdoor_posting(row)
            except Exception as e:
                ctx.log.debug("glassdoor: skipping row: %s", e)
                continue
            if p:
                out.append(p)
        return out[: query.max_results]
