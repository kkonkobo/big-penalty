from __future__ import annotations

import json
from pathlib import Path

from jobhunt.models import ContractType
from jobhunt.sources import registry
from jobhunt.sources.hellowork import HelloWorkSource, row_to_hellowork_posting

FX = Path(__file__).parent / "fixtures"


def test_registered_not_aggregator():
    cls = registry()["hellowork"]
    assert cls is HelloWorkSource and cls.is_aggregator is False


def test_rows_map():
    rows = json.loads((FX / "hellowork_rows.json").read_text())
    ps = [row_to_hellowork_posting(r) for r in rows]
    assert all(ps)
    p = ps[0]
    assert p.source == "hellowork" and p.url.startswith("https://www.hellowork.com/fr-fr/emplois/")
    assert not p.source_id.startswith("hw-") and p.source_id.isdigit()
    assert p.location == "Toulouse (31)" and p.department == "31"
    assert (p.salary_min_eur, p.salary_max_eur) == (40000, 55000)
    assert p.apply_method == "platform" and p.description
    # "temporary, fulltime" + monthly pay
    assert ps[1].contract_type in (ContractType.CDD, ContractType.INTERIM)
    assert ps[1].salary_min_eur == 18600
