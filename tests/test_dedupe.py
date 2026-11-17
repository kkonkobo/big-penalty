from datetime import UTC, datetime, timedelta, timezone

from jobhunt.dedupe import (MergedJob, canonical_url, consolidate, dedupe_key, merge_postings, normalize_city,
                            normalize_company, normalize_title)
from jobhunt.models import ContractType, JobPosting


def P(source="indeed", sid="1", title="Ingénieur systèmes embarqués H/F", company="Airbus SAS", location="Toulouse (31)",
      url=None, **kw):
    return JobPosting(source=source, source_id=sid, url=url or f"https://{source}.com/job/{sid}", title=title,
                      company=company, location=location, **kw)


def test_normalize_company():
    assert normalize_company("Société Générale S.A.") == "societe generale"
    assert normalize_company("Airbus Group SAS") == "airbus"
    assert normalize_company("Capgemini France") == "capgemini"
    assert normalize_company("L'Oréal") == "l oreal"
    assert normalize_company("SAS") == "sas"  # never empties a name
    assert normalize_company(None) == ""


def test_normalize_title_and_city():
    assert normalize_title("Développeur(se) Firmware (H/F)") == normalize_title("developpeur firmware F/H")
    assert normalize_title("Stage - Ingénieur C embarqué") == "ingenieur c embarque"
    assert normalize_city("Toulouse (31)") == "toulouse"
    assert normalize_city("Paris 8e, Île-de-France") == "paris"
    assert normalize_city("31000 Toulouse") == "toulouse"
    assert dedupe_key("Airbus SAS", "Ingénieur H/F", "Toulouse (31)") == "airbus|ingenieur|toulouse"


def test_canonical_url_strips_tracking_only():
    assert canonical_url("https://www.Site.com/jobs/12/?utm_source=x&fbclid=1#frag") == "site.com/jobs/12"
    assert canonical_url("https://fr.indeed.com/viewjob?jk=abc&from=serp") == "fr.indeed.com/viewjob?jk=abc"


def test_fuzzy_merge_across_boards_keeps_all_sightings():
    now = datetime(2026, 11, 1, tzinfo=UTC)
    a = P("linkedin", "L1", description="court", posted_at=now)
    b = P("indeed", "I1", title="Ingénieur Systèmes Embarqués (F/H)", company="AIRBUS", description="Une description bien plus longue et riche",
          posted_at=now - timedelta(days=3))
    c = P("wttj", "W1", location="Toulouse", company="Airbus Group")
    jobs, stats = merge_postings([a, b, c])
    assert len(jobs) == 1 and stats.new_jobs == 1 and stats.fuzzy_matches == 2
    j = jobs[0]
    assert j.sources == ["linkedin", "indeed", "wttj"]
    assert j.description.startswith("Une description bien plus longue")
    assert j.posted_at == now - timedelta(days=3)  # earliest


def test_different_jobs_not_merged():
    jobs, _ = merge_postings([
        P(sid="1"),
        P(sid="2", title="Ingénieur comptable"),
        P(sid="3", company="Thales"),
        P(sid="4", location="Lyon (69)"),
        P(sid="5", contract_type=ContractType.STAGE),
        P(sid="6", contract_type=ContractType.CDI),
    ])
    # sid 1 absorbs nothing; stage vs cdi must stay apart even with identical title/company/city
    assert len(jobs) == 5
    keys = [(j.title, j.company, j.location, j.contract_type) for j in jobs]
    assert any(k[3] == ContractType.STAGE for k in keys) and any(k[3] == ContractType.CDI for k in keys)


def test_exact_url_and_origin_url_match_even_if_titles_differ():
    a = P("linkedin", "1", title="Totally different title", company="X", url="https://x.com/jobs/42?utm_campaign=a")
    b = P("wttj", "2", title="Another words", company="Y", url="https://x.com/jobs/42")
    jobs, stats = merge_postings([a, b])
    assert len(jobs) == 1 and stats.exact_matches == 1
    c = P("indeed", "3", title="Zzz", company="Q", url="https://indeed.com/3", origin_url="https://boards.greenhouse.io/acme/jobs/99")
    d = P("linkedin", "4", title="Qqq", company="R", url="https://linkedin.com/4", origin_url="https://boards.greenhouse.io/acme/jobs/99")
    assert len(merge_postings([c, d])[0]) == 1


def test_generic_origin_homepage_is_not_an_identity():
    c = P("indeed", "3", title="Aaa job", company="Q", url="https://indeed.com/3", origin_url="https://acme.com/careers")
    d = P("linkedin", "4", title="Bbb role", company="R", url="https://linkedin.com/4", origin_url="https://acme.com/careers")
    assert len(merge_postings([c, d])[0]) == 2


def test_merge_into_existing_and_idempotent():
    jobs, _ = merge_postings([P()])
    jobs[0].id = 7
    again, stats = merge_postings([P()], jobs)
    assert len(again) == 1 and again[0].id == 7 and stats.new_jobs == 0
    assert len(again[0].sightings) == 1  # same (source, source_id) not duplicated
    more, stats = merge_postings([P("wttj", "9")], again)
    assert len(more) == 1 and len(more[0].sightings) == 2


def test_missing_company_needs_strong_title_and_city():
    a = P("indeed", "1", company=None)
    b = P("wttj", "2")
    assert len(merge_postings([a, b])[0]) == 2
    c = P("indeed", "3", company=None, location=None)
    assert len(merge_postings([c, b])[0]) == 2


def test_naive_and_aware_datetimes_compare():
    a = P("a", "1", posted_at=datetime(2026, 1, 2))
    b = P("b", "2", posted_at=datetime(2026, 1, 1, tzinfo=UTC))
    jobs, _ = merge_postings([a, b])
    assert len(jobs) == 1 and jobs[0].posted_at == datetime(2026, 1, 1, tzinfo=UTC)


def test_consolidate_by_origin():
    j1 = MergedJob.from_posting(P("a", "1", title="Foo bar", company="Z", origin_url="https://z.com/jobs/1"))
    j2 = MergedJob.from_posting(P("b", "2", title="Different", company="W", origin_url="https://z.com/jobs/1"))
    kept, removed = consolidate([j1, j2])
    assert len(kept) == 1 and len(removed) == 1 and len(kept[0].sightings) == 2


def test_missing_company_requires_url_or_substantial_description():
    a = P("indeed", "1", company=None)
    b = P("wttj", "2")
    assert len(merge_postings([a, b])[0]) == 2
    shared = "https://acme.fr/jobs/shared"
    assert len(merge_postings([a.model_copy(update={"apply_url": shared}), b.model_copy(update={"apply_url": shared})])[0]) == 1
    body = "Design the unique automotive control firmware and validate signal acquisition on our STM32 platform. " * 4
    assert len(merge_postings([a.model_copy(update={"description": body}), b.model_copy(update={"description": body})])[0]) == 1
    assert len(merge_postings([a.model_copy(update={"description": "Join us"}), b.model_copy(update={"description": "Join us"})])[0]) == 2


def test_apply_origin_keeps_source_date_and_stores_origin_date_separately():
    job = MergedJob.from_posting(P(posted_at=datetime(2026, 11, 1, 10, tzinfo=UTC)))
    job.changed = False
    job.apply_origin(P(origin_url="https://acme.test/j/1", origin_ats="lever", origin_confidence=0.9,
                       origin_posted_at=datetime(2026, 11, 1, 11, tzinfo=timezone(timedelta(hours=2)))))
    assert job.posted_at == datetime(2026, 11, 1, 10, tzinfo=UTC) and job.changed
    assert job.extra["origin_posted_at"] == datetime(2026, 11, 1, 9, tzinfo=UTC).isoformat()


def test_apply_origin_does_not_replace_better_origin_and_recompute_repairs_posted_at():
    job = MergedJob.from_posting(P(posted_at=datetime(2026, 11, 3, tzinfo=UTC)))
    job.origin_url, job.origin_ats, job.origin_confidence = "https://acme.test/j/1", "lever", 0.9
    job.apply_origin(P(origin_url="https://other.test/j/2", origin_ats="x", origin_confidence=0.7))
    assert job.origin_url == "https://acme.test/j/1"
    job.posted_at = datetime(2026, 10, 1, tzinfo=UTC)  # old run folded an ATS date in
    job.recompute_posted_at()
    assert job.posted_at == datetime(2026, 11, 3, tzinfo=UTC)


def test_representative_origin_posting_preserves_remote_and_apply_method():
    job = MergedJob.from_posting(P(remote="full", apply_method="external"))
    assert job.to_posting().remote == "full" and job.to_posting().apply_method == "external"


def test_absorb_reconciles_user_status_precedence():
    first = MergedJob.from_posting(P("a", "1", origin_url="https://acme.fr/jobs/1"))
    second = MergedJob.from_posting(P("b", "2", origin_url=first.origin_url))
    first.id, second.id, second.status = 1, 2, "applied"
    kept, removed = consolidate([second, first])
    assert kept == [first] and removed == [second] and first.status == "applied"
