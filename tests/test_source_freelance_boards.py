"""Offline parsing tests for the freelance-board connectors (fixtures under tests/fixtures/)."""

import json
from datetime import datetime, timezone
from pathlib import Path

from jobhunt.models import ContractType
from jobhunt.sources import registry
from jobhunt.sources._freelance_util import (
    keyword_attempts, parse_daily_rate, parse_iso, parse_relative_fr, yearly_from_daily,
)
from jobhunt.sources.codeur import CodeurSource
from jobhunt.sources.collective import CollectiveSource
from jobhunt.sources.freelance_informatique import FreelanceInformatiqueSource
from jobhunt.sources.freework import FreeWorkSource
from jobhunt.sources.malt import MaltSource

FIX = Path(__file__).parent / "fixtures"


def test_registry_has_connectors():
    reg = registry()
    for n in ("freework", "collective", "codeur", "freelance_informatique", "malt"):
        assert n in reg
    assert reg["malt"].enabled_by_default is False and reg["malt"].requires_login and reg["malt"].login_url
    for n in ("freework", "collective", "codeur", "freelance_informatique", "malt"):
        assert ContractType.FREELANCE in reg[n].contract_types


def test_util():
    assert parse_daily_rate("400 à 450 €/j") == (400, 450)
    assert parse_daily_rate("450-530TJM") == (450, 530)
    assert parse_daily_rate("410") == (410, 410)
    assert parse_daily_rate("55K€ - 60K€") == (None, None)
    assert parse_daily_rate(None) == (None, None)
    assert yearly_from_daily(400) == 87200
    now = datetime(2026, 11, 17, tzinfo=timezone.utc)
    assert parse_relative_fr("Il y a 11 jours", now).day == 6
    assert parse_iso("2026-11-17").tzinfo is not None
    assert keyword_attempts("développeur C++ embarqué")[:2] == ["développeur C++ embarqué", "embarqué"]


def test_freework_parse():
    data = json.loads((FIX / "freework_search.json").read_text())
    posts = [FreeWorkSource.parse_item(i, ContractType.FREELANCE) for i in data["hydra:member"]]
    p = posts[0]
    assert p.source == "freework" and p.contract_type == ContractType.FREELANCE
    assert p.url.startswith("https://www.free-work.com/fr/tech-it/job-mission/")
    assert p.salary_text == "310-440 €/jour" and p.salary_min_eur == 310 * 218 and p.salary_max_eur == 440 * 218
    assert p.remote == "partial" and p.location and p.company and p.posted_at and p.posted_at.tzinfo
    assert p.apply_method == "platform" and "Durée" in p.description and "<" not in p.description
    assert {x.remote for x in posts} >= {"partial", "none", "full"}
    no_rate = next(x for x in posts if x.salary_min_eur is None)
    assert no_rate.salary_text is None


def test_collective_parse():
    data = json.loads((FIX / "collective_search.json").read_text())
    posts = [CollectiveSource.parse_item(i) for i in data["data"]["results"]["projects"]]
    assert any(p.contract_type == ContractType.FREELANCE for p in posts)
    # the API's Freelance filter also lets other contract types through; search() drops them
    assert {p.contract_type for p in posts} <= {ContractType.FREELANCE, ContractType.STAGE, ContractType.UNKNOWN, ContractType.CDI, ContractType.CDD}
    p = next(x for x in posts if x.salary_min_eur and x.contract_type == ContractType.FREELANCE)
    assert "€/jour" in p.salary_text and p.salary_min_eur == 218 * int(p.salary_text.split("€")[0].split("-")[0])
    assert p.url.startswith("https://www.collective.work/jobs/fr/") and p.remote in ("full", "partial", "none")
    assert p.posted_at and p.location and p.description


def test_codeur_parse():
    posts = CodeurSource.parse_list((FIX / "codeur_list.html").read_text())
    assert len(posts) == 3
    p = posts[0]
    assert p.source_id.isdigit() and p.url.startswith("https://www.codeur.com/projects/")
    assert p.salary_text.startswith("Budget projet :") and p.salary_min_eur is None
    assert p.contract_type == ContractType.FREELANCE and p.posted_at and p.title
    d = CodeurSource.apply_detail(p, (FIX / "codeur_project.html").read_text())
    assert d.posted_at.year == 2026 and d.posted_at.month == 11 and "Délai estimé : 15 jours" in d.description


def test_freelance_informatique_parse():
    posts = FreelanceInformatiqueSource.parse_list((FIX / "freelance_informatique_list.html").read_text())
    assert posts
    p = posts[0]
    assert p.contract_type == ContractType.FREELANCE and p.source_id and p.url.startswith("https://www.freelance-informatique.fr/mission-")
    assert p.location and p.posted_at and p.description and "<" not in p.description
    d = FreelanceInformatiqueSource.apply_detail(p, (FIX / "freelance_informatique_detail.html").read_text())
    assert "Durée : 6 mois (renouvelables)" in d.description and "Démarrage : ASAP" in d.description


def test_malt_is_disabled_login_connector():
    assert MaltSource.kind == "browser"
