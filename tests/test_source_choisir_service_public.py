from __future__ import annotations

from pathlib import Path

from jobhunt.models import ContractType, SearchQuery
from jobhunt.sources import registry
from jobhunt.sources.choisir_service_public import (
    ChoisirServicePublicSource, listing_to_posting, location_id, parse_detail, parse_listing,
)

FX = Path(__file__).parent / "fixtures"


def test_registered():
    assert registry()["choisir_service_public"] is ChoisirServicePublicSource


def test_listing():
    items = parse_listing((FX / "choisir_service_public_search.html").read_text())
    assert len(items) == 4
    p = listing_to_posting(items[0])
    assert p.source_id == "2026-2466648" and p.title == "Ingénieur(e) d'études en pédagogie"
    assert p.company == "Université de Valenciennes" and p.department == "59" and p.location == "Nord (59)"
    assert p.posted_at.date().isoformat() == "2026-11-10"
    assert p.url.startswith("https://choisirleservicepublic.gouv.fr/offre-emploi/")
    assert p.raw["domain"] == "Enseignement et formation" and p.raw["versant"] == "Fonction publique de l'État"


def test_detail():
    items = parse_listing((FX / "choisir_service_public_search.html").read_text())
    base = listing_to_posting(items[1])
    q = parse_detail((FX / "choisir_service_public_detail.html").read_text(), base)
    assert q.company == "Météo France"
    assert "Vos missions en quelques mots" in q.description and "Profil recherché" in q.description
    assert "Afficher la suite" not in q.description and "<" not in q.description
    assert q.apply_method == "external" and q.apply_url.startswith("https://careers.flatchr.io/")
    assert q.raw["facts"]["Catégorie"] == "Catégorie A (cadre)"
    assert q.contract_type == ContractType.UNKNOWN  # "ouvert aux titulaires et aux contractuels"
    assert q.salary_min_eur is None and q.remote in ("unknown", "partial", "none")


def test_internship_title_detection():
    it = {"url": "https://choisirleservicepublic.gouv.fr/offre-emploi/stage-chargee-de-communication-reference-2026-1/", "title": "Stage - Chargé(e) de communication F/H", "location": "Paris (75)"}
    assert listing_to_posting(it).contract_type == ContractType.STAGE
    it["title"] = "Apprenti Contrôle de Gestion"
    assert listing_to_posting(it).contract_type == ContractType.ALTERNANCE


def test_location_ids():
    assert location_id(department="Haute-Garonne") == "305"
    assert location_id(department="Paris") == "284"
    assert location_id(region="Île-de-France") == "208"
    assert location_id(region="Occitanie") == "209" and location_id(department="Atlantide") is None
    assert ChoisirServicePublicSource.contract_types is None and SearchQuery(keywords="x")
