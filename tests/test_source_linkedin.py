from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from jobhunt.models import ContractType, SearchQuery
from jobhunt.sources import registry
from jobhunt.sources.linkedin import (
    LinkedInSource,
    apply_detail,
    build_search_params,
    card_to_posting,
    external_url_from_linkedin_redirect,
    parse_browser_cards,
    parse_browser_detail,
    parse_detail,
    parse_search_cards,
)

FX = Path(__file__).parent / "fixtures"


def test_registered_and_flags():
    cls = registry()["linkedin"]
    assert cls is LinkedInSource
    assert cls.is_aggregator and cls.login_url.startswith("https://www.linkedin.com")


def test_search_cards():
    cards = parse_search_cards((FX / "linkedin_search.html").read_text())
    assert len(cards) == 3
    c = cards[0]
    assert c["id"].isdigit() and c["url"] == f"https://www.linkedin.com/jobs/view/{c['id']}"
    assert c["title"] and c["company"] and c["location"] and c["date"]
    p = card_to_posting(c)
    assert p.source == "linkedin" and p.source_id == c["id"]
    assert p.posted_at is not None and p.apply_method == "unknown"


def test_detail_offsite_is_external():
    d = parse_detail((FX / "linkedin_detail_offsite.html").read_text())
    assert d["apply_kind"] == "external"
    assert d["description"] and "\n" in d["description"] or len(d["description"]) > 100
    assert d["criteria"]


def test_detail_onsite_is_easy_apply():
    d = parse_detail((FX / "linkedin_detail_easy.html").read_text())
    assert d["apply_kind"] == "easy_apply"


def test_apply_detail_merges():
    card = parse_search_cards((FX / "linkedin_search.html").read_text())[0]
    p = card_to_posting(card)
    p = apply_detail(p, parse_detail((FX / "linkedin_detail_offsite.html").read_text()))
    assert p.apply_method == "external" and p.description


def test_stage_label_maps_to_contract():
    html = """<section><div class="show-more-less-html__markup"><p>Votre mission</p></div>
    <ul class="description__job-criteria-list"><li class="description__job-criteria-item">
    <h3 class="description__job-criteria-subheader">Type d’emploi</h3>
    <span class="description__job-criteria-text description__job-criteria-text--criteria">Stage / Alternance</span></li></ul></section>"""
    d = parse_detail(html)
    assert d["board_types"] == ["internship"]
    card = parse_search_cards((FX / "linkedin_search.html").read_text())[0]
    p = apply_detail(card_to_posting({**card, "title": "Ingénieur logiciel"}), d)
    assert p.contract_type == ContractType.STAGE
    p2 = apply_detail(card_to_posting({**card, "title": "Ingénieur logiciel"}), {**d, "description": "Poste en alternance (contrat d'apprentissage)"})
    assert p2.contract_type == ContractType.ALTERNANCE


def test_search_params():
    q = SearchQuery(keywords="x", location="Toulouse", contract_types=[ContractType.STAGE, ContractType.ALTERNANCE], posted_within_days=7)
    p = build_search_params(q, 10)
    assert p["location"] == "Toulouse, France" and p["f_TPR"] == "r604800" and p["f_JT"] == "I" and p["start"] == 10
    q = SearchQuery(keywords="x", location="France", contract_types=[ContractType.CDI, ContractType.CDD])
    assert build_search_params(q)["f_JT"] == "F,C,T"
    q = SearchQuery(keywords="x", contract_types=[ContractType.VIE])
    assert "f_JT" not in build_search_params(q)


def test_external_redirect_unwrap():
    u = "https://www.linkedin.com/safety/go/?url=https%3A%2F%2Fjobs.example.com%2Fa%3Fid%3D1&trk=x"
    assert external_url_from_linkedin_redirect(u) == "https://jobs.example.com/a?id=1"
    assert external_url_from_linkedin_redirect("https://jobs.example.com/x") == "https://jobs.example.com/x"


def test_browser_dom_helpers():
    html = """<ul><li data-occludable-job-id="123"><a class="job-card-list__title" href="/jobs/view/123/"><strong>Stage embarqué</strong></a>
    <div class="artdeco-entity-lockup__subtitle">ACME</div><div class="artdeco-entity-lockup__caption">Toulouse</div>
    <span>Candidature simplifiée</span></li></ul>"""
    (p,) = parse_browser_cards(html)
    assert (p.source_id, p.title, p.company, p.apply_method) == ("123", "Stage embarqué", "ACME", "easy_apply")
    d = parse_browser_detail('<div id="job-details"><p>Desc</p></div><a href="https://www.linkedin.com/jobs/view/externalApply/1?url=https%3A%2F%2Fx.fr%2Fj">Postuler</a>')
    assert d["apply_kind"] == "external" and d["external_url"] == "https://x.fr/j" and d["description"] == "Desc"


@pytest.mark.parametrize("session", [True, False, None])
async def test_external_apply_missing_url_falls_through_to_existing_browser(session):
    from jobhunt.models import JobPosting
    from jobhunt.sources.base import SourceContext

    posting = JobPosting(source="linkedin", source_id="1", url="https://linkedin.com/jobs/view/1",
                         title="Dev", description="Existing description", apply_method="external")
    source = LinkedInSource()
    source._guest_detail = AsyncMock(return_value=posting)
    source._browser_detail = AsyncMock(return_value=posting.model_copy(update={"apply_url": "https://acme.fr/jobs/1"}))
    browser = SimpleNamespace(has_session=lambda *args: session) if session is not None else None
    result = await source.fetch_details(posting, SourceContext(profile_id=1, browser=browser))
    assert bool(result.apply_url) == bool(session)
    assert source._browser_detail.await_count == int(bool(session))
