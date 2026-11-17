import json
from datetime import datetime, timezone
from pathlib import Path

from jobhunt.models import ContractType
from jobhunt.sources.base import SourceContext
from jobhunt.sources.cadremploi import CadremploiSource, extract_offer, parse_relative_date

FIX = Path(__file__).parent / "fixtures"


def test_search_mapping():
    src, ctx = CadremploiSource(), SourceContext(profile_id=1)
    data = json.loads((FIX / "cadremploi_search.json").read_text())
    ps = [src._from_posting(j, ctx) for j in data["jobPostings"]]
    a = ps[0]
    assert a.source == "cadremploi" and a.contract_type == ContractType.CDI
    assert a.url == f"https://www.cadremploi.fr/emploi/detail_offre?offreId={a.source_id}"
    assert (a.salary_min_eur, a.salary_max_eur) == (29000, 32000)
    assert a.apply_method == "external"  # fixture marks this one as "redirection"
    assert a.posted_at is not None and a.company == "Michael Page"
    assert "<" not in (a.description or "")


def test_detail_nuxt_payload():
    offer = extract_offer((FIX / "cadremploi_detail.html").read_text())
    assert offer and offer["offreId"] == "156589814864933793"
    src, ctx = CadremploiSource(), SourceContext(profile_id=1)
    base = src._from_posting(
        {"id": offer["offreId"], "title": "STAGE", "contract": "Stage", "location": "Toulouse", "company": {"name": "Thales"}}, ctx
    )
    p = src.apply_offer(base, offer)
    assert p.apply_method == "external" and p.apply_url.startswith("https://ars2.equest.com/")
    assert p.postal_code == "31000" and p.department == "31"
    assert p.contract_type == ContractType.STAGE
    assert p.company_website == "https://www.thalesgroup.com"
    assert p.posted_at and p.posted_at.year == 2026
    assert p.salary_min_eur is None  # remuneration.masquer = true -> hidden range is not trusted
    assert "Thales est un leader" in p.description


def test_relative_dates():
    now = datetime(2026, 11, 17, tzinfo=timezone.utc)
    assert parse_relative_date("Publiée il y a 8 jours", now).day == 9
    assert parse_relative_date("Publiée il y a 3 heures", now).hour == 21
    assert parse_relative_date("n/a", now) is None
