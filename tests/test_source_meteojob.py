import json
from pathlib import Path

from jobhunt.models import ContractType
from jobhunt.sources.base import SourceContext
from jobhunt.sources.meteojob import MeteojobSource

FIX = Path(__file__).parent / "fixtures"


def test_offer_mapping():
    src, ctx = MeteojobSource(), SourceContext(profile_id=1)
    offers = json.loads((FIX / "meteojob_search.json").read_text())["content"]
    a, b = (src._from_offer(o, ctx) for o in offers)
    assert a.source == "meteojob" and a.url.startswith("https://www.meteojob.com/jobs/")
    assert a.apply_method == "external" and a.apply_url.startswith("http")
    assert a.salary_min_eur and a.salary_min_eur >= 8000 and a.salary_text and "\xa0" not in a.salary_text
    assert a.posted_at and a.description and "<" not in a.description
    assert a.department and a.location
    assert b.apply_method == "platform" and b.apply_url == b.url
    assert a.contract_type != ContractType.UNKNOWN
