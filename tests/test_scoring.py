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
    assert "senior" in prefilter(p, J("Senior firmware engineer", ct=ContractType.CDI)).lower()
    assert "10 ans" in prefilter(p, J("Ingénieur firmware", "Vous justifiez de 10 ans d'expérience", ContractType.CDI))
    assert prefilter(p, J("Ingénieur firmware", "2 ans d'expérience", ContractType.CDI)) is None
    exp = Profile(name="y", headline="Ingénieur", experience_years=8)
    assert prefilter(exp, J("Senior firmware", ct=ContractType.CDI)) is None


def test_keyword_score_flags_location():
    s = keyword_score(STUDENT.model_copy(update={"locations": ["Toulouse"]}), J("Stage firmware", loc="Lille"))
    assert "location_mismatch" in s.flags
    assert keyword_score(STUDENT, J("Boulanger")).fit < keyword_score(STUDENT, J("Stage firmware")).fit
