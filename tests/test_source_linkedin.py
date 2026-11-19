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


# ------------------------------------------------------------------ external apply URL (guest page variants)

from jobhunt.sources.linkedin import extract_external_apply_url  # noqa: E402

_DETAIL_BODY = """<section class="top-card-layout"><h2 class="top-card-layout__title">Stage ingénieur systèmes</h2>
<a class="topcard__org-name-link" href="https://fr.linkedin.com/company/orbitex">Orbitex Aerospace</a>
{apply}</section>
<div class="show-more-less-html__markup"><p>Mission de stage chez Orbitex.</p></div>"""


@pytest.mark.parametrize("apply,expected", [
    # 1. <code id="applyUrl"> comment block with a quoted externalApply redirect
    ('<code id="applyUrl" style="display: none"><!--"https://www.linkedin.com/jobs/view/externalApply/4111?url=https%3A%2F%2Fcareers.orbitex.example%2Fjobs%2F42%3Futm_source%3Dli&urlHash=ab12&trk=public_jobs_apply-link-offsite"--></code>',
     "https://careers.orbitex.example/jobs/42?utm_source=li"),
    # 2. same block, HTML-escaped ampersands
    ('<code id="applyUrl"><!--"https://www.linkedin.com/jobs/view/externalApply/4111?url=https%3A%2F%2Fcareers.orbitex.example%2Fjobs%2F42&amp;urlHash=ab12"--></code>',
     "https://careers.orbitex.example/jobs/42"),
    # 3. block already holding the employer URL
    ('<code id="applyUrl"><!--"https://careers.orbitex.example/apply?jobId=42"--></code>', "https://careers.orbitex.example/apply?jobId=42"),
    # 4. anchor with the offsite tracking name
    ('<a class="apply-button" data-tracking-control-name="public_jobs_apply-link-offsite_sign-up-modal" href="https://www.linkedin.com/jobs/view/externalApply/4111?url=https%3A%2F%2Forbitex.example%2Fcareers%2F42&amp;refId=x">Postuler</a>',
     "https://orbitex.example/careers/42"),
    # 5. anchor straight to the employer
    ('<a data-tracking-control-name="public_jobs_apply-link-offsite" href="https://orbitex.example/careers/42">Postuler</a>',
     "https://orbitex.example/careers/42"),
    # 6. /safety/go wrapper
    ('<a class="apply-button" href="https://www.linkedin.com/safety/go/?url=https%3A%2F%2Forbitex.example%2Fj%2F42&trk=x">Apply</a>',
     "https://orbitex.example/j/42"),
])
def test_external_apply_url_variants(apply, expected):
    html = _DETAIL_BODY.format(apply=apply)
    assert extract_external_apply_url(html) == expected
    d = parse_detail(html)
    assert d["external_url"] == expected and d["apply_kind"] == "external"
    card = parse_search_cards((FX / "linkedin_search.html").read_text())[0]
    p = apply_detail(card_to_posting(card), d)
    assert p.apply_url == expected and p.apply_method == "external"


def test_easy_apply_and_unserved_apply_url_leave_apply_url_empty():
    easy = _DETAIL_BODY.format(apply='<a class="apply-button" data-tracking-control-name="public_jobs_apply-link-onsite" '
                                     'href="https://www.linkedin.com/signup/cold-join?session_redirect=https%3A%2F%2Fwww.linkedin.com%2Fjobs%2Fview%2F4111">Candidature simplifiée</a>')
    d = parse_detail(easy)
    assert d["apply_kind"] == "easy_apply" and d["external_url"] is None
    card = parse_search_cards((FX / "linkedin_search.html").read_text())[0]
    p = apply_detail(card_to_posting(card), d)
    assert p.apply_url is None and p.apply_method == "easy_apply"
    # off-site job whose page does not serve the target: still "external", no url
    bare = _DETAIL_BODY.format(apply='<a class="apply-button" data-tracking-control-name="public_jobs_apply-link-offsite_sign-up-modal" href="https://www.linkedin.com/signup/cold-join?trk=x">Postuler</a>')
    d = parse_detail(bare)
    assert d["apply_kind"] == "external" and d["external_url"] is None
    # a target that is itself LinkedIn is never an employer URL
    assert extract_external_apply_url('<code id="applyUrl"><!--"https://www.linkedin.com/jobs/view/externalApply/1?url=https%3A%2F%2Fwww.linkedin.com%2Fjobs%2Fview%2F2"--></code>') is None


def test_redirect_unwrap_keeps_encoded_query_of_target():
    u = "https://www.linkedin.com/jobs/view/externalApply/1?url=https%3A%2F%2Fx.fr%2Fj%3Fnext%3Dhttps%253A%252F%252Fy.fr&trk=a"
    assert external_url_from_linkedin_redirect(u) == "https://x.fr/j?next=https%3A%2F%2Fy.fr"
