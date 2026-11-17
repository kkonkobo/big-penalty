from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest
import respx

from jobhunt.models import ContractType, SearchQuery
from jobhunt.sources import registry
from jobhunt.sources.base import SourceContext, SourceError
from jobhunt.sources.labonnealternance import (
    API_URL, SPONTANEOUS_PREFIX, LaBonneAlternanceSource, job_to_posting, recruiter_to_posting,
)

FX = Path(__file__).parent / "fixtures"
DATA = json.loads((FX / "labonnealternance_search.json").read_text())


def test_registered():
    cls = registry()["labonnealternance"]
    assert cls is LaBonneAlternanceSource
    assert cls.requires_credentials == ("labonnealternance_api_key",)
    assert cls.contract_types == frozenset({ContractType.ALTERNANCE})


def test_job_mapping():
    p = job_to_posting(DATA["jobs"][0])
    assert p.source_id == "6687165396d52b5e01b40954" and p.contract_type == ContractType.ALTERNANCE
    assert p.company == "Robotix Toulouse" and p.location == "Toulouse (31)" and p.postal_code == "31400" and p.department == "31"
    assert p.remote == "partial" and p.apply_method == "platform" and p.company_website == "https://robotix.example"
    assert p.posted_at.date().isoformat() == "2026-11-05" and "firmware" in p.description and "Durée du contrat : 24 mois" in p.description
    assert p.experience_level.startswith("Licence")
    other = job_to_posting(DATA["jobs"][1])
    assert other.source_id == "9988" and other.remote == "none" and other.apply_method == "external"
    assert job_to_posting(DATA["jobs"][2]) is None  # Filled


def test_recruiter_is_flagged_spontaneous():
    p = recruiter_to_posting(DATA["recruiters"][0])
    assert p.title.startswith(SPONTANEOUS_PREFIX) and "AEROSYS" in p.title
    assert p.raw["spontaneous"] is True and p.source_id == "recruteur:rec-1"
    assert p.apply_method == "platform" and p.location == "Blagnac (31)"
    assert "Candidature spontanée" in p.description


def test_rank_prefers_keyword_matches_and_mixes_recruiters():
    q = SearchQuery(keywords="systèmes embarqués", max_results=10)
    out = LaBonneAlternanceSource.rank(q, DATA)
    assert [p.source_id for p in out] == ["6687165396d52b5e01b40954", "9988", "recruteur:rec-1"]
    out = LaBonneAlternanceSource.rank(SearchQuery(keywords="x", max_results=2), DATA)
    assert len(out) == 2 and out[-1].raw.get("spontaneous")


def _ctx(key):
    ctx = SourceContext(profile_id=0)
    ctx.settings = ctx.settings.model_copy(update={"labonnealternance_api_key": key})
    return ctx


@respx.mock
def test_search_http_flow(monkeypatch):
    ctx = _ctx("k3y")
    geo = respx.get("https://api-adresse.data.gouv.fr/search/").mock(return_value=httpx.Response(200, json={"features": [
        {"geometry": {"coordinates": [1.4338, 43.6040]}, "properties": {"label": "Toulouse", "citycode": "31555", "postcode": "31000", "depcode": "31", "context": "31, Haute-Garonne, Occitanie"}}]}))
    respx.get("https://geo.api.gouv.fr/regions").mock(return_value=httpx.Response(200, json=[]))
    api = respx.get(API_URL).mock(return_value=httpx.Response(200, json=DATA))
    q = SearchQuery(keywords="systèmes embarqués", location="Toulouse", contract_types=[ContractType.ALTERNANCE], max_results=5)
    out = asyncio.run(LaBonneAlternanceSource().search(q, ctx))
    assert len(out) == 3 and geo.called
    req = api.calls.last.request
    assert req.headers["authorization"] == "Bearer k3y"
    assert req.url.params["latitude"] == "43.604" and req.url.params["radius"] == "30"
    assert req.url.params["romes"].startswith("M1892")


@respx.mock
def test_auth_errors_and_wrong_contract():
    ctx = _ctx("bad")
    respx.get("https://geo.api.gouv.fr/regions").mock(return_value=httpx.Response(200, json=[]))
    respx.get(API_URL).mock(return_value=httpx.Response(401, json={"message": "invalid"}))
    with pytest.raises(SourceError):
        asyncio.run(LaBonneAlternanceSource().search(SearchQuery(keywords="x", rome_codes=["M1805"]), ctx))
    assert asyncio.run(LaBonneAlternanceSource().search(SearchQuery(keywords="x", contract_types=[ContractType.CDI]), ctx)) == []
    with pytest.raises(SourceError):
        asyncio.run(LaBonneAlternanceSource().search(SearchQuery(keywords="x"), _ctx(None)))
