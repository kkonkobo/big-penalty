from __future__ import annotations

import json
from pathlib import Path

from jobhunt.models import ContractType, SearchQuery
from jobhunt.sources import registry
from jobhunt.sources._fr_public import detect_remote, parse_fr_date, parse_salary
from jobhunt.sources._rome import lookup_rome
from jobhunt.sources.france_travail import (
    FranceTravailSource, api_to_posting, listing_to_posting, parse_contact_html, parse_detail_json, parse_search_html,
)

FX = Path(__file__).parent / "fixtures"


def _rows():
    return parse_search_html((FX / "france_travail_search.html").read_text())


def test_registered():
    cls = registry()["france_travail"]
    assert cls is FranceTravailSource and cls.requires_credentials == ()


def test_parse_listing():
    rows = _rows()
    assert [r["id"] for r in rows][:2] == ["216DVVY", "215LBDG"]
    p = listing_to_posting(rows[0])
    assert p.title.startswith("Architecte systèmes embarqués") and p.company == "ADECCO"
    assert p.location == "Toulouse (31)" and p.department == "31"
    assert p.contract_type == ContractType.CDI and p.posted_at is not None
    assert p.url.endswith("/offres/recherche/detail/216DVVY")
    assert p.raw["origin_board"] is None and p.raw["native"] is True
    partner = listing_to_posting(rows[2])
    assert partner.raw["origin_board"] == "HELLOWORK" and partner.company == "Scalian"
    inter = [listing_to_posting(r) for r in rows if "Intérim" in (r["contract"] or "")]
    assert all(p.contract_type == ContractType.INTERIM for p in inter)


def test_stage_detected_from_title_despite_cdi_label():
    rows = _rows()
    stage = next(listing_to_posting(r) for r in rows if r["title"].startswith("STAGE"))
    assert stage.contract_type == ContractType.STAGE
    assert stage.company is None and stage.location == "Toulouse (31)"


def test_detail_json_merge():
    rows = _rows()
    p = listing_to_posting(next(r for r in rows if r["id"] == "8833200"))
    d = json.loads((FX / "france_travail_detail_8833200.json").read_text())
    q = parse_detail_json(d, p)
    assert (q.salary_min_eur, q.salary_max_eur) == (40000, 55000)
    assert q.postal_code == "31000" and q.experience_level == "8 An(s)"
    assert "Ingénieur Qualité Projet" in q.description and "<" not in q.description
    assert q.raw["detail"]["codeRome"] == "H1520"
    d2 = json.loads((FX / "france_travail_detail_216DVVY.json").read_text())
    q2 = parse_detail_json(d2, listing_to_posting(rows[0]))
    assert q2.salary_min_eur is None  # "Salaire selon profil"


def test_contact_apply_links():
    c = json.loads((FX / "france_travail_contact_8833200.json").read_text())
    url, method = parse_contact_html(c["_tapestry"]["content"][0][1])
    assert method == "external" and url.startswith("https://www.hellowork.com/fr-fr/emplois/")
    assert "&amp;" not in url
    c = json.loads((FX / "france_travail_contact_216DVVY.json").read_text())
    url, method = parse_contact_html(c["_tapestry"]["content"][0][1])
    assert method == "external" and "jometer" in url
    assert parse_contact_html("<p>rien</p>") == (None, "unknown")


def test_api_mapping():
    res = json.loads((FX / "france_travail_api.json").read_text())["resultats"]
    a, b, c = (api_to_posting(o) for o in res)
    assert a.contract_type == ContractType.CDI and a.location == "Toulouse (31)" and a.postal_code == "31000"
    assert (a.salary_min_eur, a.salary_max_eur) == (40000, 48000)
    assert a.apply_url == "https://acme-aero.example/jobs/123" and a.apply_method == "external"
    assert a.remote == "partial" and a.company_website == "https://www.acme-aero.example"
    assert a.posted_at.year == 2026 and a.raw["native"] is True
    assert b.contract_type == ContractType.ALTERNANCE and b.salary_min_eur == 14400
    assert b.apply_method == "email" and b.apply_url == "mailto:rh@robotix.example"
    assert b.raw["origin_board"] == "HELLOWORK" and b.description == "Préparation d'un BTS."
    assert b.department == "75" and b.location == "Paris 15e Arrondissement (75)"
    assert c.contract_type == ContractType.STAGE


def test_site_groups_and_helpers():
    s = FranceTravailSource()
    g = s._site_groups(SearchQuery(keywords="x", contract_types=[ContractType.STAGE, ContractType.ALTERNANCE, ContractType.CDI]))
    assert [x[0] for x in g] == ["CDI", "alternance", "stage"]
    assert parse_salary("Annuel de 40000.0 Euros à 55000.0 Euros") == (40000, 55000)
    assert parse_salary("Mensuel de 2500 Euros sur 13 mois") == (32500, 32500)
    assert parse_salary("Horaire de 12.0 Euros") == (19284, 19284)
    assert parse_salary("Selon profil") == (None, None)
    assert parse_fr_date("Publié il y a 3 jours") is not None and parse_fr_date("13 novembre 2026").day == 13
    assert detect_remote("pas de télétravail") == "none" and detect_remote("full remote") == "full"
    assert lookup_rome("systèmes embarqués")[0] == "M1892" and lookup_rome("boulangerie") == []
