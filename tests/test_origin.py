"""Offline tests for jobhunt.origin (fixtures are trimmed real API responses)."""

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from jobhunt import origin as o

FX = Path(__file__).parent / "fixtures"


def fx(name):
    return json.loads((FX / name).read_text())


@pytest.mark.parametrize("url,ats", [
    ("https://thales.wd3.myworkdayjobs.com/Careers/job/Paris/Stage_R1", "workday"),
    ("https://job-boards.greenhouse.io/mirakl/jobs/123", "greenhouse"),
    ("https://boards.greenhouse.io/embed/job_app?for=algolia&token=1", "greenhouse"),
    ("https://jobs.lever.co/blablacar/abc", "lever"),
    ("https://jobs.eu.lever.co/foo/abc", "lever"),
    ("https://jobs.ashbyhq.com/alan/uuid", "ashby"),
    ("https://jobs.smartrecruiters.com/BoschGroup/744-x", "smartrecruiters"),
    ("https://apply.workable.com/j/81B46579FE", "workable"),
    ("https://bunq.recruitee.com/o/x", "recruitee"),
    ("https://career.teamtailor.com/jobs/1-x", "teamtailor"),
    ("https://www.welcometothejungle.com/fr/companies/doctolib/jobs/dev_paris", "welcome_kit"),
    ("https://acme.digitalrecruiters.com/offre/1", "digitalrecruiters"),
    ("https://app.taleez.com/apply/x", "taleez"),
    ("https://careers.flatchr.io/vacancy/x", "flatchr"),
    ("https://acme.jobaffinity.fr/offre/1", "jobaffinity"),
    ("https://acme.beetween.com/x", "beetween"),
    ("https://acme.talent-soft.com/job-offer/1.aspx", "talentsoft"),
    ("https://career5.successfactors.eu/career?company=x", "successfactors"),
    ("https://acme.taleo.net/careersection/x", "taleo"),
    ("https://careers-acme.icims.com/jobs/1", "icims"),
    ("https://jobs.jobvite.com/acme/job/x", "jobvite"),
    ("https://acme.bamboohr.com/careers/1", "bamboohr"),
    ("https://acme.jobs.personio.de/job/1", "personio"),
    ("https://join.com/companies/acme/1-x", "join"),
    ("https://acme.softy.pro/offer/1", "softy"),
    ("https://acme.altays.com/offre/1", "altays"),
    ("https://acme.gestmax.fr/1/x", "gestmax"),
    ("https://parrot-drones.talentview.io/jobs/kvzln6", "talentview"),
    ("https://careers.acme.com/job/1?gh_jid=4567", None),
    ("https://careers.acme.com/job/1", None),
])
def test_classify_ats(url, ats):
    assert o.classify_ats(url) == ats


def test_classify_html_hint_and_fallback():
    assert o.classify_ats("https://acme.com/jobs", '<script src="https://cdn.teamtailor.com/x.js">') is None
    assert o.ats_or_site("https://acme.com/carrieres/1") == "careers_site"


def test_is_aggregator():
    assert o.is_aggregator("https://fr.linkedin.com/jobs/view/123")
    assert o.is_aggregator("https://www.indeed.fr/rc/clk?jk=1")
    assert o.is_aggregator("https://www.glassdoor.fr/job-listing/x.htm")
    assert not o.is_aggregator("https://jobs.lever.co/x/1")
    assert o.is_aggregator("https://www.welcometothejungle.com/fr/companies/x/jobs/y")
    assert not o.is_aggregator("https://notlinkedin.com/x")


def test_clean_url():
    u = "https://boards.greenhouse.io/acme/jobs/1?gh_src=abc&utm_source=linkedin&utm_medium=x&gh_jid=1#apply"
    assert o.clean_url(u) == "https://boards.greenhouse.io/acme/jobs/1?gh_jid=1"
    assert o.clean_url("https://acme.com/job/1?source=linkedin&id=5") == "https://acme.com/job/1?id=5"
    assert o.clean_url("https://acme.com/job/1?source=LinkedIn&ref=x&lang=fr") == "https://acme.com/job/1?lang=fr"
    assert o.clean_url("https://jobs.lever.co/a/b?lever-source=Indeed") == "https://jobs.lever.co/a/b"


def test_embedded_target():
    u = "https://www.linkedin.com/jobs/view/externalApply/123?url=https%3A%2F%2Fjobs.lever.co%2Facme%2F1%3Futm_source%3Dli&urlHash=x"
    assert o.embedded_target(u) == "https://jobs.lever.co/acme/1?utm_source=li"
    assert o.embedded_target("https://www.linkedin.com/redirect?url=https%3A%2F%2Flinkedin.com%2Fx") is None


def test_board_ref():
    assert o.board_ref("https://job-boards.greenhouse.io/mirakl/jobs/1") == o.BoardRef("greenhouse", "mirakl")
    assert o.board_ref("https://boards.greenhouse.io/embed/job_app?for=algolia&token=1").slug == "algolia"
    assert o.board_ref("https://jobs.lever.co/blablacar/x").slug == "blablacar"
    assert o.board_ref("https://jobs.eu.lever.co/foo/x").host == "eu"
    assert o.board_ref("https://apply.workable.com/huggingface/j/1").slug == "huggingface"
    assert o.board_ref("https://bunq.recruitee.com/o/x").slug == "bunq"
    r = o.board_ref("https://thales.wd3.myworkdayjobs.com/en-US/Careers/job/Paris/x_R1")
    assert (r.slug, r.host, r.site) == ("thales", "thales.wd3.myworkdayjobs.com", "Careers")


def test_slug_candidates():
    assert o.slug_candidates("Back Market SAS")[:2] == ["backmarket", "back-market"]
    s = o.slug_candidates("Bosch Group", "https://www.bosch.fr/")
    assert s[0] == "bosch" and "boschgroup" in s
    assert o.slug_candidates("Société Générale")[0] == "societegenerale"
    assert o.slug_candidates("Schneider Electric France")[:3] == ["schneiderelectric", "schneider-electric", "schneider"] or "schneider" in o.slug_candidates("Schneider Electric France")
    assert o.slug_candidates(None) == []
    assert len(o.slug_candidates("A B C D E F G")) <= 6


@pytest.mark.parametrize("raw,norm", [
    ("Ingénieur Systèmes Embarqués (H/F)", "ingenieur systemes embarques"),
    ("Développeur Python - F/H", "developpeur python"),
    ("Software Engineer (m/f/d)", "software engineer"),
    ("Chargé(e) de projet (All genders)", "charge de projet"),
    ("Stage – Data Analyst H/F", "stage data analyst"),
])
def test_normalize_title(raw, norm):
    assert o.normalize_title(raw) == norm


def test_title_score():
    assert o.title_score("Ingénieur logiciel embarqué H/F", "Ingenieur Logiciel Embarque (F/H)") == 100
    assert o.title_score("Ingénieur logiciel embarqué", "Ingénieur logiciel embarqué Linux Senior") < 90
    assert o.title_score("Stage Data Analyst", "Alternance Data Analyst") < 90
    assert o.title_score("Data Analyst", "Senior Data Analyst") < 90
    assert o.title_score("Account Executive - Mirakl Connect", "Account Executive, Mirakl Connect") >= 95
    assert o.title_score("Chef de projet", "Boulanger") < 40


def test_location_and_confidence():
    assert o.location_match("Paris (75)", "Paris, France") is True
    assert o.location_match("Lyon", "Paris, France") is False
    assert o.location_match(None, "Paris") is None
    assert o.score_to_confidence(100, None) == 0.92
    assert o.score_to_confidence(100, True) == 0.95
    assert o.score_to_confidence(90, None) >= 0.6 > o.score_to_confidence(88, None)
    assert o.score_to_confidence(100, False) < o.MIN_CONFIDENCE


def test_parsers():
    gh = o.parse_greenhouse(fx("origin_greenhouse_mirakl.json"), "mirakl")
    assert gh[0].url.startswith("https://job-boards.greenhouse.io/mirakl/jobs/") and gh[0].posted_at.year >= 2025
    assert gh[0].location and gh[0].description
    lv = o.parse_lever(fx("origin_lever_blablacar.json"), "blablacar")
    assert lv[0].url.startswith("https://jobs.lever.co/blablacar/") and lv[0].posted_at and lv[0].location
    ab = o.parse_ashby(fx("origin_ashby_alan.json"), "alan")
    assert ab[0].url.startswith("https://jobs.ashbyhq.com/alan/") and ab[0].posted_at
    sr = o.parse_smartrecruiters(fx("origin_smartrecruiters_bosch.json"), "BoschGroup")
    assert sr[0].url.startswith("https://jobs.smartrecruiters.com/BoschGroup/") and sr[0].posted_at
    wk = o.parse_workable(fx("origin_workable_hf.json"), "huggingface")
    assert wk[0].url.startswith("https://apply.workable.com/j/") and wk[0].location
    rc = o.parse_recruitee(fx("origin_recruitee_bunq.json"), "bunq")
    assert rc[0].url.startswith("https://careers.bunq.com/o/") and rc[0].posted_at


def test_best_match_from_fixture():
    from jobhunt.models import JobPosting

    cands = o.parse_greenhouse(fx("origin_greenhouse_mirakl.json"), "mirakl")
    first = cands[0]
    p = JobPosting(source="linkedin", source_id="1", url="https://linkedin.com/jobs/view/1", title=first.title.upper() + " (H/F)",
                   company="Mirakl", location=first.location)
    r = o.Resolver.__new__(o.Resolver)
    m = o.Resolver.best_match(r, p, cands, "ats_api")
    assert m and m.cand.url == first.url and m.conf >= 0.9
    p2 = p.model_copy(update={"title": "Boulanger pâtissier"})
    assert o.Resolver.best_match(r, p2, cands, "ats_api") is None


def test_workday_posted_and_jsonld():
    assert o._workday_posted("Posted Today") is not None
    lb = o._workday_posted("Posted 30+ Days Ago")
    assert lb is not None and (datetime.now(timezone.utc) - lb).days >= 29
    d = o._workday_posted("Posted 3 Days Ago")
    assert d is not None
    html = '<script type="application/ld+json">{"@context":"x","@graph":[{"@type":"JobPosting","title":"Dev","datePosted":"2026-10-01"}]}</script>'
    assert o.jsonld_jobposting(html)["title"] == "Dev"
    assert o._parse_date("2026-11-16 14:19:38 UTC").day == 16
    assert o._parse_date(1790150675664).year == 2026


def test_workday_fixture_shape():
    d = fx("origin_workday_thales.json")
    assert d["jobPostings"][0]["externalPath"].startswith("/job/")


def test_cache_key_and_roundtrip(tmp_path):
    k1 = o.DiskCache.key("Back Market SAS", "Dev (H/F)", "Paris", None)
    k2 = o.DiskCache.key("back market", "dev f/h", "Paris (75)", None)
    assert k1 == k2
    c = o.DiskCache(tmp_path)
    assert c.get(k1) is None
    c.put(k1, {"origin_url": "https://x", "origin_ats": "lever"})
    assert c.get(k1)["origin_url"] == "https://x"
    c.board_result("lever::x:", False)
    assert c.board_known_missing("lever::x:")
