from datetime import datetime, timezone
from pathlib import Path

from jobhunt.models import ContractType
from jobhunt.sources.base import SourceContext
from jobhunt.sources.jobijoba import JobijobaSource, parse_pub_date

FIX = Path(__file__).parent / "fixtures"


def test_parse_results():
    src = JobijobaSource()
    assert src.is_aggregator
    ps = src.parse_results((FIX / "jobijoba_search.html").read_text(), SourceContext(profile_id=1))
    assert len(ps) == 2
    for p in ps:
        assert p.source == "jobijoba" and p.title and p.company
        assert p.apply_method == "external" and p.apply_url.startswith("https://www.jobijoba.com/fr/redirect/")
        assert p.posted_at is not None
    assert any(p.salary_text and p.salary_min_eur for p in ps) or any(p.contract_type == ContractType.STAGE for p in ps)


def test_pub_date():
    now = datetime(2026, 11, 17, 12, tzinfo=timezone.utc)
    assert parse_pub_date("Il y a 14 h", now).hour == 22 and parse_pub_date("Il y a 14 h", now).day == 16
    assert parse_pub_date("23 octobre", now).month == 10 and parse_pub_date("23 octobre", now).year == 2026
    assert parse_pub_date("30 décembre", now).year == 2025
