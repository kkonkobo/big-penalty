from jobhunt.dedupe import MergedJob
from jobhunt.models import ContractType, JobPosting, Profile
from jobhunt.scoring import keyword_score, prefilter


def J(title="Stage firmware", desc="", ct=ContractType.STAGE, loc="Toulouse"):
    return MergedJob.from_posting(JobPosting(source="s", source_id=title, url="https://s/" + title, title=title,
                                             description=desc, contract_type=ct, location=loc))


STUDENT = Profile(name="x", headline="Étudiant ingénieur", contract_types=[ContractType.STAGE], interests=["firmware"])


def test_prefilter():
    assert prefilter(STUDENT, J()) is None
    assert "Contrat" in prefilter(STUDENT, J(ct=ContractType.CDI))
    assert prefilter(STUDENT, J(ct=ContractType.UNKNOWN)) is None
    p = STUDENT.model_copy(update={"contract_types": []})
    # seniority / years of experience are soft (scorer's job), only contract type is a hard drop
    assert prefilter(p, J("Senior firmware engineer", ct=ContractType.CDI)) is None
    assert prefilter(p, J("Ingénieur firmware", "Vous justifiez de 10 ans d'expérience", ContractType.CDI)) is None
    assert prefilter(p, J("Ingénieur firmware", "2 ans d'expérience", ContractType.CDI)) is None
    exp = Profile(name="y", headline="Ingénieur", experience_years=8)
    assert prefilter(exp, J("Senior firmware", ct=ContractType.CDI)) is None


def test_keyword_score_flags_location():
    s = keyword_score(STUDENT.model_copy(update={"locations": ["Toulouse"]}), J("Stage firmware", loc="Lille"))
    assert "location_mismatch" in s.flags
    assert keyword_score(STUDENT, J("Boulanger")).fit < keyword_score(STUDENT, J("Stage firmware")).fit


def test_invalidate_score_rescores_job_no_longer_prefiltered():
    from jobhunt.dedupe import MergedJob
    from jobhunt.models import ContractType, Profile
    from jobhunt.scoring import invalidate_score, scoring_fingerprint

    profile = Profile(name="t", headline="Étudiant ingénieur mécatronique", contract_types=[ContractType.STAGE])
    job = MergedJob(title="Stage ingénieur senior systèmes embarqués", contract_type=ContractType.STAGE)
    job.score, job.score_json = 0, {"prefiltered": True, "rationale": "Intitulé senior pour un profil junior"}
    job.extra["scoring_fingerprint"] = scoring_fingerprint(profile, job)
    invalidate_score(profile, job)
    assert job.score is None and job.score_json == {}
