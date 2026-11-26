"""Guardrails: what may be sent, to whom, how often (no browser, no LLM)."""

from __future__ import annotations

import itertools
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from jobhunt import db
from jobhunt.apply import policy
from jobhunt.apply import service as apply_service
from jobhunt.apply import store as astore
from jobhunt.apply.types import RunResult
from jobhunt.config import get_settings
from jobhunt.cv import service as cvs
from jobhunt.cv import store
from jobhunt.cv.master import load_master, save_master
from jobhunt.freshness import start_of_today_paris

from apply_cv_helpers import ERROR, make_app, make_job
from cv_fakes import save_confirmed


@pytest.fixture(autouse=True)
def auto_on(monkeypatch):
    monkeypatch.setattr(get_settings(), "apply_auto", True)


@pytest.fixture
def master(profile):
    return save_confirmed(profile.id)


@pytest.fixture
def job(profile, master):
    return make_job(profile)


@pytest.fixture
def app(profile, job, master, tmp_path):
    return cvs.auto_approve(make_app(profile, job, tmp_path, master_hash=master.content_hash).id)


def out_dir(run_id):
    return get_settings().data_dir / "runs" / str(run_id)


_reservations = itertools.count(1000)


def auto_submit_run(_app_id, when):
    """One automatic send that reached the final click at `when` (the reservation of some other application)."""
    n = next(_reservations)
    with db.connect() as c:
        c.execute("INSERT INTO apply_reservations(profile_id, application_id, run_id, trigger, role_key, url_key, created_at)"
                  " VALUES (1,?,?,'auto',?,?,?)", (n, n, f"role{n}", f"url{n}", db._iso(when)))


def problems(app, job, trigger="auto", **kw):
    return policy.submit_problems(store.get_application(app.id), job, trigger=trigger, **kw)


# ------------------------------------------------------------------ normalize_role / duplicates
def test_normalize_role_ignores_case_accents_punctuation_and_gender_markers():
    base = policy.normalize_role("Exemple S.A.S.", "Ingénieur Logiciel Embarqué")
    for title in ("Ingénieur logiciel embarqué (H/F)", "INGENIEUR LOGICIEL EMBARQUE F/H", "Ingénieur logiciel embarqué - H/F/X",
                  "Ingénieur  logiciel  embarqué (h/f)"):
        assert policy.normalize_role("Exemple SAS", title) == base
    assert policy.normalize_role("Exemple SAS", "Ingénieur logiciel") != base
    assert policy.normalize_role("Autre", "Ingénieur logiciel embarqué") != base


def test_duplicate_by_company_and_role(profile, master, tmp_path):
    first = make_job(profile, title="Stage développeur C (H/F)", company="Exemple SAS", url="https://careers.exemple.test/1")
    second = make_job(profile, title="Stage développeur C F/H", company="EXEMPLE", url="https://careers.exemple.test/2",
                      contract=first.contract_type)
    other = make_job(profile, title="Stage développeur Rust", company="Exemple SAS", url="https://careers.exemple.test/3")
    a1 = make_app(profile, first, tmp_path, master_hash=master.content_hash)
    a2 = make_app(profile, second, tmp_path, master_hash=master.content_hash)
    a3 = make_app(profile, other, tmp_path, master_hash=master.content_hash)
    assert policy.duplicate_of(a2, second) is None  # nothing submitted yet
    cvs.auto_approve(a1.id)
    store.mark_submitted(a1.id, 1)
    assert policy.duplicate_of(a2, second).id == a1.id
    assert policy.duplicate_of(a3, other) is None
    assert "déjà envoyée" in " ".join(policy.submit_problems(a2, second, trigger="user"))


def test_duplicate_by_origin_url_and_not_across_profiles(profile, master, tmp_path):
    first = make_job(profile, title="Stage A", company="Alpha", url="https://careers.exemple.test/jobs/9?utm_source=x")
    second = make_job(profile, title="Poste différent", company="Beta", url="https://careers.exemple.test/jobs/9")
    a1 = make_app(profile, first, tmp_path, master_hash=master.content_hash)
    a2 = make_app(profile, second, tmp_path, master_hash=master.content_hash)
    cvs.auto_approve(a1.id)
    store.mark_submitted(a1.id, 1)
    assert policy.duplicate_of(a2, second).id == a1.id
    from jobhunt.models import Profile
    p2 = db.save_profile(Profile(name="Autre", headline="x"))
    j3 = make_job(p2, title="Stage A", company="Alpha")
    save_confirmed(p2.id)
    a3 = make_app(p2, j3, tmp_path, master_hash=load_master(p2.id).content_hash)
    assert policy.duplicate_of(a3, j3) is None  # another family member may apply to the same company
    items = {item.app.id: item for item in apply_service.queue()}
    assert items[a2.id].bucket == "blocked" and items[a3.id].bucket == "to_validate"


def test_deleted_job_row_is_still_a_duplicate_through_sent_origin_url(profile, master, tmp_path):
    first = make_job(profile, title="Stage Y", company="Delta", url="https://careers.delta.test/jobs/5?utm_source=x")
    second = make_job(profile, title="Autre intitulé", company="Autre société", url="https://careers.delta.test/jobs/5")
    a1 = cvs.auto_approve(make_app(profile, first, tmp_path, master_hash=master.content_hash).id)
    a2 = make_app(profile, second, tmp_path, master_hash=master.content_hash)
    store.mark_submitted(a1.id, 1, sent_origin_url=policy.canonical_url(first.origin_url))
    with db.connect() as c:  # dedupe merged / deleted the job row of the sent application
        c.execute("DELETE FROM jobs WHERE id=?", (first.id,))
    assert db.get_job(first.id) is None
    assert policy.duplicate_of(a2, second).id == a1.id
    assert any("déjà envoyée" in p for p in problems(a2, second, "user"))
    item = next(item for item in apply_service.queue(profile.id) if item.app.id == a2.id)
    assert item.problems == problems(cvs.auto_approve(a2.id), second, "user")


def test_deleted_job_row_is_still_a_duplicate_through_the_stored_role(profile, master, tmp_path):
    first = make_job(profile, title="Stage Z (H/F)", company="Epsilon", url="https://careers.epsilon.test/1")
    second = make_job(profile, title="Stage Z F/H", company="EPSILON", url="https://careers.epsilon.test/other")
    a1 = cvs.auto_approve(make_app(profile, first, tmp_path, master_hash=master.content_hash).id)
    a2 = make_app(profile, second, tmp_path, master_hash=master.content_hash)
    store.mark_submitted(a1.id, 1)
    with db.connect() as c:
        c.execute("DELETE FROM jobs WHERE id=?", (first.id,))
    assert policy.duplicate_of(a2, second).id == a1.id  # falls back to the application's stored company / title


def test_a_possibly_sent_application_is_a_duplicate_through_its_reservation(profile, master, tmp_path):
    first = make_job(profile, title="Stage W", company="Zeta", url="https://careers.zeta.test/1")
    second = make_job(profile, title="Stage W (H/F)", company="Zeta", url="https://careers.zeta.test/2")
    a1 = cvs.auto_approve(make_app(profile, first, tmp_path, master_hash=master.content_hash).id)
    a2 = make_app(profile, second, tmp_path, master_hash=master.content_hash)
    run = astore.start_run(a1.id, 1, "submit", "user", out_dir)
    astore.reserve_send(run.id, profile_id=profile.id, application_id=a1.id, trigger="user",
                        role_key=policy.normalize_role("Zeta", "Stage W"), url_key=policy.canonical_url(first.origin_url), daily_cap=9)
    store.mark_failed(a1.id, "Envoi non confirmé")
    with db.connect() as c:
        c.execute("DELETE FROM jobs WHERE id=?", (first.id,))
    assert policy.duplicate_of(a2, second).id == a1.id
    blocked = next(item for item in apply_service.queue(profile.id) if item.app.id == a2.id)
    assert blocked.bucket == "blocked" and not blocked.selectable
    astore.release_reservation(a1.id)
    assert policy.duplicate_of(a2, second) is None
    ready = next(item for item in apply_service.queue(profile.id) if item.app.id == a2.id)
    assert ready.bucket == "to_validate" and ready.supervised and not ready.selectable


def test_a_failed_or_draft_application_is_not_a_duplicate(profile, master, tmp_path):
    first = make_job(profile, title="Stage X", company="Gamma", url="https://careers.gamma.test/1")
    second = make_job(profile, title="Stage X (H/F)", company="Gamma", url="https://careers.gamma.test/2")
    a1 = make_app(profile, first, tmp_path, master_hash=master.content_hash)
    a2 = make_app(profile, second, tmp_path, master_hash=master.content_hash)
    assert policy.duplicate_of(a2, second) is None
    cvs.auto_approve(a1.id)
    store.mark_failed(a1.id, "échec")
    assert policy.duplicate_of(a2, second) is None


# ------------------------------------------------------------------ submit_problems
def test_clean_auto_candidate_has_no_problem(app, job):
    assert problems(app, job) == [] and problems(app, job, "user") == []


def test_unknown_job_or_other_profile(profile, app, job):
    assert problems(app, None) == ["offre introuvable pour ce profil"]
    from jobhunt.models import Profile
    p2 = db.save_profile(Profile(name="Autre", headline="x"))
    foreign = make_job(p2, title="Autre offre")
    assert problems(app, foreign) == ["offre introuvable pour ce profil"]


@pytest.mark.parametrize("url, text", [
    (None, "pas de lien http"), ("ftp://careers.exemple.test/1", "pas de lien http"), ("/relatif", "pas de lien http"),
    ("https://www.linkedin.com/jobs/view/123", "agrégateur"), ("https://fr.indeed.com/viewjob?jk=1", "agrégateur"),
])
def test_origin_must_be_the_company_site(profile, app, job, url, text):
    job.origin_url = url
    assert any(text in p for p in problems(app, job, "user"))


def test_job_older_than_14_days_is_refused(profile, app, job):
    job.posted_at = datetime.now(UTC) - timedelta(days=30)
    job.first_seen = datetime.now(UTC) - timedelta(days=30)
    assert any("plus de 14 jours" in p for p in problems(app, job, "user"))
    job.posted_at = job.first_seen = datetime.now(UTC) - timedelta(days=13)
    assert problems(app, job, "user") == []


@pytest.mark.parametrize("trigger", ["auto", "user"])
def test_age_limit_is_strict_without_the_search_tolerance(profile, app, job, trigger):
    now = datetime(2026, 7, 10, 12, 0, tzinfo=UTC)
    job.posted_at = job.first_seen = now - timedelta(days=14, hours=12)  # the shortlist still shows it (14 d + 1 d tolerance)
    assert any("plus de 14 jours" in p for p in problems(app, job, trigger, now=now))
    job.posted_at = job.first_seen = now - timedelta(days=14)
    assert not any("plus de 14 jours" in p for p in problems(app, job, trigger, now=now))
    job.posted_at = job.first_seen = now - timedelta(days=13, hours=23)
    assert not any("plus de 14 jours" in p for p in problems(app, job, trigger, now=now))


def test_unknown_age_blocks_auto_only(profile, app, job):
    job.posted_at = job.first_seen = job.last_seen = None
    job.extra = {}
    assert any("date de publication" in p for p in problems(app, job, "auto"))
    assert problems(app, job, "user") == []


@pytest.mark.parametrize("status", ["rejected", "applied"])
@pytest.mark.parametrize("trigger", ["auto", "user"])
def test_rejected_or_applied_job_is_never_sent(profile, app, job, status, trigger):
    job.status = status
    assert "offre déjà rejetée ou déjà postulée" in problems(app, job, trigger)
    job.status = "approved"
    assert problems(app, job, trigger) == []


def test_expired_job_blocks_auto_only(profile, app, job):
    now = datetime.now(UTC)
    job.last_seen = now - timedelta(days=9)
    assert problems(app, job, "auto") == []  # no completed run checked its sources since: not expired
    with db.connect() as c:
        c.execute("INSERT INTO runs(profile_id, started_at, finished_at, status, stats) VALUES (?,?,?,?,?)",
                  (profile.id, db._iso(now - timedelta(hours=2)), db._iso(now - timedelta(hours=1)), "done",
                   '{"sources_ok": ["linkedin"]}'))
    assert any("offre expirée" in p for p in problems(app, job, "auto"))
    assert problems(app, job, "user") == []


def test_low_origin_confidence_blocks_auto_only(profile, app, job):
    job.origin_confidence = 0.59
    assert any("lien de l'offre non vérifié" in p for p in problems(app, job, "auto"))
    assert problems(app, job, "user") == []  # the human saw the link
    job.origin_confidence = None
    assert any("lien de l'offre non vérifié" in p for p in problems(app, job, "auto"))
    job.origin_confidence = 0.6
    assert problems(app, job, "auto") == []


@pytest.mark.parametrize("trigger", ["auto", "user"])
def test_an_earlier_unconfirmed_send_blocks_everything(profile, app, job, trigger):
    run = astore.start_run(app.id, 1, "submit", "user", out_dir)
    astore.reserve_send(run.id, profile_id=app.profile_id, application_id=app.id, trigger="user", role_key="r", url_key="u", daily_cap=9)
    astore.finish_run(run.id, RunResult(status="needs_human", submit_clicked=True))
    assert any("un envoi précédent n'a pas été confirmé" in p for p in problems(app, job, trigger))
    paused = apply_service.queue(profile.id)[0]
    assert paused.bucket == "needs_verification" and not paused.selectable
    assert paused.outstanding.id == run.id
    assert paused.problems == problems(app, job, "user")
    astore.release_reservation(app.id)  # a human said "not sent"
    assert not any("envoi précédent" in p for p in problems(app, job, trigger))
    ready = apply_service.queue(profile.id)[0]
    assert ready.supervised and not ready.selectable


def test_unapproved_or_stale_approval(profile, job, master, tmp_path):
    draft = make_app(profile, job, tmp_path, master_hash=master.content_hash)
    assert "CV non approuvé" in problems(draft, job, "user")
    cvs.approve(draft.id, 1)
    assert problems(draft, job, "user") == []
    # a new revision (edit) clears the approval
    store.add_revision(profile_id=profile.id, job_id=job.id, job_title=None, company=None, ats=None, tailored=draft.tailored, flags=[],
                       verifier=draft.verifier, fit_notes=draft.fit_notes, master_hash=draft.master_hash, tmp_pdf=None,
                       pdf_target=lambda r: Path("/nonexistent"), app_id=draft.id, base_rev=1)
    assert "CV non approuvé" in problems(draft, job, "user")


def test_tampered_pdf_and_changed_master_are_refused(profile, app, job):
    pdf = Path(store.get_application(app.id).cv_path)
    original = pdf.read_bytes()
    pdf.write_bytes(b"%PDF-tampered")
    assert any("PDF modifié" in p for p in problems(app, job, "user"))
    pdf.write_bytes(original)
    assert problems(app, job, "user") == []
    m = load_master(profile.id)
    m.summary += " Changement."
    save_master(profile.id, m)
    assert "CV maître non confirmé" in problems(app, job, "user")
    save_confirmed(profile.id, m)
    assert "CV maître modifié depuis la génération" in problems(app, job, "user")


def test_override_approval_allowed_for_user_but_never_for_auto(profile, job, master, tmp_path):
    a = make_app(profile, job, tmp_path, master_hash=master.content_hash, flags=[ERROR])
    cvs.approve(a.id, 1, ignore_errors=True)
    assert problems(a, job, "user") == []
    auto = problems(a, job, "auto")
    assert len(auto) == 1 and auto[0].startswith("CV non éligible à l'envoi automatique") and "approbation manuelle" in auto[0]


def test_auto_needs_master_switch_and_score(monkeypatch, app, job):
    monkeypatch.setattr(get_settings(), "apply_auto", False)
    assert problems(app, job) == ["envoi automatique désactivé"]
    assert problems(app, job, "user") == []  # the switch only concerns automatic sending
    monkeypatch.setattr(get_settings(), "apply_auto", True)
    job.score = 59.9
    assert problems(app, job) == ["score inférieur à 60"]
    assert problems(app, job, "user") == []  # score not checked for a manual send
    job.score = None
    assert problems(app, job) == ["score inférieur à 60"]
    job.score = 60
    assert problems(app, job) == []


def test_daily_cap_counts_since_paris_midnight(monkeypatch, app, job):
    monkeypatch.setattr(get_settings(), "apply_daily_cap", 2)
    now = datetime(2026, 7, 10, 22, 30, tzinfo=UTC)  # 00:30 Paris on 11 July
    job.posted_at = job.first_seen = now - timedelta(days=1)
    midnight = start_of_today_paris(now)
    auto_submit_run(app.id, midnight - timedelta(minutes=1))  # yesterday in Paris
    auto_submit_run(app.id, midnight - timedelta(hours=3))
    auto_submit_run(app.id, midnight + timedelta(minutes=10))  # today: 1 of 2
    assert problems(app, job, now=now) == []
    auto_submit_run(app.id, midnight + timedelta(minutes=20))  # today: 2 of 2
    assert problems(app, job, now=now) == ["plafond de 2 envois automatiques par jour atteint"]
    assert problems(app, job, "user", now=now) == []  # a manual send ignores the cap
    assert problems(app, job, now=now + timedelta(hours=24)) == []  # next Paris day: counter restarts


# ------------------------------------------------------------------ auto_candidates
def test_auto_candidates_filters_and_orders(profile, master, tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "apply_prepare_min_score", 60)
    good = make_job(profile, title="Stage bon", company="C1", score=70, url="https://careers.c1.test/1")
    best = make_job(profile, title="Stage meilleur", company="C2", score=95, url="https://careers.c2.test/1")
    edge = make_job(profile, title="Stage limite", company="C3", score=60, url="https://careers.c3.test/1")
    low = make_job(profile, title="Stage faible", company="C4", score=59, url="https://careers.c4.test/1")
    agg = make_job(profile, title="Stage agrégé", company="C5", score=90, url="https://www.indeed.com/viewjob?jk=1")
    noorigin = make_job(profile, title="Stage sans lien", company="C6", score=90, url=None)
    old = make_job(profile, title="Stage vieux", company="C7", score=90, url="https://careers.c7.test/1", age_days=30)
    rejected = make_job(profile, title="Stage rejeté", company="C8", score=90, url="https://careers.c8.test/1")
    applied = make_job(profile, title="Stage postulé", company="C9", score=90, url="https://careers.c9.test/1")
    done = make_job(profile, title="Stage envoyé", company="C10", score=90, url="https://careers.c10.test/1")
    failed = make_job(profile, title="Stage échoué", company="C11", score=90, url="https://careers.c11.test/1")
    withdrawn = make_job(profile, title="Stage retiré", company="C12", score=90, url="https://careers.c12.test/1")
    draft = make_job(profile, title="Stage brouillon", company="C13", score=80, url="https://careers.c13.test/1")
    db.set_status([rejected.id], "rejected", profile.id)
    db.set_status([applied.id], "applied", profile.id)
    mk = lambda j: make_app(profile, j, tmp_path, master_hash=master.content_hash)  # noqa: E731
    a = cvs.auto_approve(mk(done).id)
    store.mark_submitted(a.id, 1)
    store.mark_failed(cvs.auto_approve(mk(failed).id).id, "échec")
    store.set_status(mk(withdrawn).id, "withdrawn")
    mk(draft)
    names = [j.title for j in policy.auto_candidates(profile.id)]
    assert names == ["Stage meilleur", "Stage brouillon", "Stage bon", "Stage limite"]
    assert good.id and best.id and edge.id and low.id and agg.id and noorigin.id and old.id  # silence unused


def test_auto_candidates_go_by_score_times_freshness(profile, monkeypatch):
    monkeypatch.setattr(get_settings(), "apply_prepare_min_score", 60)
    make_job(profile, title="Semaine 95", company="W", score=95, url="https://careers.w.test/1", age_days=6)
    make_job(profile, title="Hier 90", company="Y", score=90, url="https://careers.y.test/1", age_days=1)
    make_job(profile, title="Aujourd'hui 70", company="T1", score=70, url="https://careers.t1.test/1", age_days=0)
    make_job(profile, title="Aujourd'hui 90", company="T2", score=90, url="https://careers.t2.test/1", age_days=0)
    # equal score: the fresher one first; a much better fit from yesterday still beats a weak one from today
    assert [j.title for j in policy.auto_candidates(profile.id)] == ["Aujourd'hui 90", "Hier 90", "Aujourd'hui 70",
                                                                     "Semaine 95"]


def test_auto_candidates_respects_the_prepare_threshold_and_hides_expired(profile, monkeypatch):
    make_job(profile, title="Stage A", company="A", score=75, url="https://careers.a.test/1")
    monkeypatch.setattr(get_settings(), "apply_prepare_min_score", 80)
    assert policy.auto_candidates(profile.id) == []
    monkeypatch.setattr(get_settings(), "apply_prepare_min_score", 70)
    assert [j.title for j in policy.auto_candidates(profile.id)] == ["Stage A"]
    far = datetime.now(UTC) + timedelta(days=30)  # as if a month later: older than 14 days -> stale
    assert policy.auto_candidates(profile.id, now=far) == []
