import json
import logging
from pathlib import Path

from jobhunt.models import ContractType
from jobhunt.sources.base import SourceContext
from jobhunt.sources.wttj import WTTJSource

FIX = Path(__file__).parent / "fixtures"


def _load(name):
    return json.loads((FIX / name).read_text())


def test_hit_mapping_and_detail():
    src = WTTJSource()
    ctx = SourceContext(profile_id=1)
    p = src._from_hit(_load("wttj_hit.json"), ctx)
    assert p and p.source == "wttj" and p.contract_type == ContractType.STAGE
    assert p.url.startswith("https://www.welcometothejungle.com/fr/companies/capgemini/jobs/")
    assert p.company == "Capgemini" and p.remote == "none"
    assert p.posted_at and p.posted_at.year == 2026
    assert "Issy" in (p.location or "")
    # internships: salary text kept, but no yearly salary derived
    assert p.salary_text and p.salary_min_eur is None

    p = src.apply_detail(p, _load("wttj_detail.json")["job"])
    assert p.apply_method == "external" and p.apply_url.startswith("https://capgemini-service-sas.contactrh.com/")
    assert p.postal_code == "92130" and p.department == "92"
    assert p.description and "<" not in p.description and "robots" in p.description
    assert p.company_website and p.company_website.startswith("http")


def test_salary_cdi():
    src = WTTJSource()
    h = _load("wttj_hit.json")
    h["contract_type"] = "FULL_TIME"
    p = src._from_hit(h, SourceContext(profile_id=1, log=logging.getLogger("t")))
    assert p.contract_type == ContractType.CDI
    assert (p.salary_min_eur, p.salary_max_eur) == (19000, 20000)
