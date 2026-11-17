from __future__ import annotations

import json
from pathlib import Path

import pytest

from jobhunt.models import ContractType, SearchQuery
from jobhunt.sources import registry
from jobhunt.sources.base import SourceContext, SourceError
from jobhunt.sources.glassdoor import GlassdoorSource, row_to_glassdoor_posting

FX = Path(__file__).parent / "fixtures"


def test_registered_opt_in():
    cls = registry()["glassdoor"]
    assert cls is GlassdoorSource and cls.is_aggregator and cls.enabled_by_default is False


def test_rows_map():
    rows = json.loads((FX / "glassdoor_rows.json").read_text())
    ps = [row_to_glassdoor_posting(r) for r in rows]
    assert all(ps)
    p = ps[0]
    assert p.source == "glassdoor" and p.url.startswith("https://www.glassdoor.fr/job-listing/")
    assert p.location == "Toulouse" and p.apply_method == "unknown" and p.apply_url is None
    assert p.salary_min_eur == 29000 and p.salary_max_eur == 32000 and "(estimation)" in p.salary_text
    assert ps[2].contract_type == ContractType.STAGE  # from the "STAGE -" title although board says fulltime


@pytest.mark.asyncio
async def test_location_not_found_is_source_error(monkeypatch):
    import jobhunt.sources.glassdoor as g

    async def boom(*a, **k):
        raise SourceError("glassdoor: location 'Zzz' not found")

    monkeypatch.setattr(g, "jobspy_scrape", boom)
    with pytest.raises(SourceError, match="autocomplete"):
        await GlassdoorSource().search(SearchQuery(keywords="x", location="Zzz"), SourceContext(profile_id=0))
