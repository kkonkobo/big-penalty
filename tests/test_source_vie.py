from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import respx

from jobhunt.models import ContractType, SearchQuery
from jobhunt.sources import registry
from jobhunt.sources.base import SourceContext
from jobhunt.sources.vie import API, HOME, VieSource, extract_api_key, offer_to_posting

FX = Path(__file__).parent / "fixtures"
DATA = json.loads((FX / "vie_search.json").read_text())


def test_registered_and_key_extraction():
    assert registry()["vie"] is VieSource
    html = 'x API_KEY:"abc\\u002Fdef+g=" y API_KEY:"AIzaSyXXXX" z'
    assert extract_api_key(html) == "abc/def+g="
    assert extract_api_key("nothing") is None


def test_mapping():
    p = offer_to_posting(DATA["result"][0])
    assert p.contract_type == ContractType.VIE and p.source_id == "248276" and p.country == "CA"
    assert p.url == "https://mon-vie-via.businessfrance.fr/offres/248276"
    assert p.company == "CS GROUP - FRANCE" and "Canada" in p.location
    assert p.salary_min_eur == round(2649.28 * 12) and "indemnité" in p.salary_text
    assert p.posted_at.date().isoformat() == "2026-11-16" and p.apply_url.startswith("https://smrtr.io/")
    assert "Durée : 24 mois" in p.description


@respx.mock
def test_search_flow():
    respx.get(HOME).mock(return_value=httpx.Response(200, text='<script>API_KEY:"KEY123"</script>'))
    route = respx.post(API).mock(return_value=httpx.Response(200, json=DATA))
    out = asyncio.run(VieSource().search(SearchQuery(keywords="embarqué", max_results=5), SourceContext(profile_id=0)))
    assert len(out) == 2
    req = route.calls.last.request
    assert req.headers["x-api-key"] == "KEY123" and json.loads(req.content)["query"] == "embarqué"
    assert asyncio.run(VieSource().search(SearchQuery(keywords="x", contract_types=[ContractType.CDI]), SourceContext(profile_id=0))) == []
