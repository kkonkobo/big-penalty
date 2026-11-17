from __future__ import annotations

import json
from pathlib import Path

from jobhunt.models import ContractType, SearchQuery
from jobhunt.sources import registry
from jobhunt.sources._common import (
    html_to_text,
    infer_contract,
    infer_remote,
    jobspy_job_types,
    parse_location,
    salary_from_parts,
)
from jobhunt.sources.indeed import IndeedSource, row_to_indeed_posting

FX = Path(__file__).parent / "fixtures"


def rows():
    return json.loads((FX / "indeed_rows.json").read_text())


def test_registered():
    assert registry()["indeed"] is IndeedSource and IndeedSource.is_aggregator


def test_rows_map():
    ps = [row_to_indeed_posting(r) for r in rows()]
    assert all(ps)
    p = ps[0]
    assert p.source == "indeed" and not p.source_id.startswith("in-") and p.url.startswith("https://fr.indeed.com/viewjob")
    assert p.apply_url and "myworkdayjobs" in p.apply_url and p.apply_method == "external"
    assert p.location == "Toulouse" and p.company == "Airbus" and p.posted_at is not None
    assert p.description and "<" not in p.description
    assert ps[1].contract_type == ContractType.STAGE  # internship
    assert ps[1].apply_url == "https://grnh.se/9h73cl667us"


def test_no_direct_url_is_easy_apply():
    r = dict(rows()[0], job_url_direct=None)
    p = row_to_indeed_posting(r)
    assert p.apply_url is None and p.apply_method == "easy_apply"


def test_job_type_mapping():
    q = SearchQuery(keywords="x", contract_types=[ContractType.STAGE, ContractType.ALTERNANCE])
    assert jobspy_job_types(q) == "internship"
    assert jobspy_job_types(SearchQuery(keywords="x", contract_types=[ContractType.CDI, ContractType.STAGE])) is None
    assert jobspy_job_types(SearchQuery(keywords="x")) is None
    assert jobspy_job_types(SearchQuery(keywords="x", contract_types=[ContractType.VIE])) is None


def test_contract_inference():
    assert infer_contract("Alternance - Ingénieur", None) == ContractType.ALTERNANCE
    assert infer_contract("Développeur", "Contrat d'apprentissage de 24 mois", ["internship"]) == ContractType.ALTERNANCE
    assert infer_contract("Développeur", None, ["internship"]) == ContractType.STAGE
    assert infer_contract("Développeur", "Poste en CDI à pourvoir", ["fulltime"]) == ContractType.CDI
    assert infer_contract("Dev (CDD 6 mois)", None) == ContractType.CDD
    assert infer_contract("Dev", "CDI ou CDD", ["fulltime"]) == ContractType.UNKNOWN
    assert infer_contract("Dev", "mission d'intérim", ["temporary"]) == ContractType.INTERIM
    assert infer_contract("Dev freelance", None) == ContractType.FREELANCE


def test_remote_and_salary_and_location():
    assert infer_remote("Télétravail complet possible") == "full"
    assert infer_remote("2 jours de télétravail par semaine") == "partial"
    assert infer_remote("rien", True) == "full"
    assert infer_remote("rien") == "unknown"
    assert salary_from_parts("yearly", 40000, 55000, "EUR") == ("40 000 - 55 000 € / an", 40000, 55000)
    t, lo, hi = salary_from_parts("monthly", 1550, 1550, "EUR")
    assert lo == hi == 18600 and t == "1 550 € / mois"
    assert salary_from_parts("hourly", 4.5, 4.5, "EUR")[1] == 8190
    assert salary_from_parts("yearly", 50000, 60000, "USD")[1:] == (None, None)
    assert salary_from_parts(None, 1, 2, "EUR") == (None, None, None)
    assert parse_location("Toulouse, O, France") == ("Toulouse", None, None)
    assert parse_location("Toulouse, 31, France") == ("Toulouse (31)", None, "31")
    assert parse_location("Lyon 3e - 69") == ("Lyon 3e (69)", None, "69")
    assert parse_location("Paris 75008") == ("Paris 75008", None, None)


def test_html_to_text():
    t = html_to_text("<p>Hello&nbsp;world</p><ul><li>A</li><li>B</li></ul><br>End")
    assert [x for x in t.splitlines() if x] == ["Hello world", "- A", "- B", "End"]
