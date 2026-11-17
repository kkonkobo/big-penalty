import json
from pathlib import Path

from jobhunt.models import ContractType
from jobhunt.sources.base import SourceContext
from jobhunt.sources.jobteaser import JobTeaserSource

FIX = Path(__file__).parent / "fixtures"


def test_hit_mapping():
    src, ctx = JobTeaserSource(), SourceContext(profile_id=1)
    hits = json.loads((FIX / "jobteaser_search.json").read_text())["hits"]
    a, b = (src._from_hit(h, ctx) for h in hits)
    assert a.source == "jobteaser" and a.contract_type == ContractType.STAGE
    assert a.url.startswith("https://www.jobteaser.com/fr/job-offers/") and a.source_id in a.url
    assert a.location and "Toulouse" in a.location and a.country == "FR"
    assert a.remote == "none" and a.posted_at and a.description and "<" not in a.description
    assert a.apply_method == "platform" and a.apply_url == a.url
    assert b.apply_method == "external" and b.apply_url is None
    assert JobTeaserSource.login_url.startswith("https://www.jobteaser.com") and not JobTeaserSource.requires_login
