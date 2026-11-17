import json
from pathlib import Path

from jobhunt.models import ContractType, SearchQuery
from jobhunt.sources.apec import ApecSource, _dept_from_lieu
from jobhunt.sources.base import SourceContext

FIX = Path(__file__).parent / "fixtures"


def test_results_mapping():
    src, ctx = ApecSource(), SourceContext(profile_id=1)
    data = json.loads((FIX / "apec_search.json").read_text())
    out: list = []
    src._collect(data, SearchQuery(keywords="x", posted_within_days=None), ctx, out, set())
    assert [p.source_id for p in out] == ["170000001W", "170000002W"]
    a, b = out
    assert a.url.endswith("/detail-offre/170000001W") and a.company == "ACME Aero"
    assert (a.salary_min_eur, a.salary_max_eur) == (45000, 55000)
    assert a.department == "31" and "- Concevoir" in a.description
    assert b.contract_type == ContractType.STAGE and b.salary_text is None and b.department == "75"


def test_contract_filter_and_dept():
    src, ctx = ApecSource(), SourceContext(profile_id=1)
    data = json.loads((FIX / "apec_search.json").read_text())
    out: list = []
    src._collect(data, SearchQuery(keywords="x", posted_within_days=None, contract_types=[ContractType.STAGE]), ctx, out, set())
    # unknown contract is kept (the board often omits it); explicit mismatches are dropped
    assert [p.source_id for p in out] == ["170000001W", "170000002W"]
    out = []
    src._collect(data, SearchQuery(keywords="x", posted_within_days=None, contract_types=[ContractType.CDI]), ctx, out, set())
    assert [p.source_id for p in out] == ["170000001W"]
    assert _dept_from_lieu("Paris 08 - 75") == "75"
