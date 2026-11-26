"""service.daily_apply: caps, auto approval, automatic sending, paused / failed applications, unconfirmed master."""

from __future__ import annotations

import os
from urllib.parse import urlsplit

import pytest

from jobhunt import db
from jobhunt.apply import policy, service
from jobhunt.apply.adapters import Registry, Requisition
from jobhunt.apply import store as astore
from jobhunt.apply.types import RunResult
from jobhunt.config import get_settings
from jobhunt.cv import service as cvs
from jobhunt.cv import store
from jobhunt.cv.errors import CVError
from jobhunt.cv.master import save_master
from jobhunt.llm import LLMError

from apply_cv_helpers import ERROR, make_app, make_job
from apply_service_fakes import FakePool
from cv_fakes import sample_master, save_confirmed


@pytest.fixture(autouse=True)
def settings(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "apply_auto", True)
    monkeypatch.setattr(s, "apply_auto_min_score", 60)
    monkeypatch.setattr(s, "apply_prepare_min_score", 40)
    monkeypatch.setattr(s, "apply_daily_cap", 10)
    monkeypatch.setattr(s, "apply_prepare_daily_cap", 15)
    return s


@pytest.fixture
def master(profile):
    return save_confirmed(profile.id)


class Env:
    """Fakes `prepare_application` (no LLM / browser) and `run` (records, books a submitted automatic run)."""

    def __init__(self, monkeypatch, profile, master, tmp_path):
        self.profile, self.master, self.tmp = profile, master, tmp_path
        self.prepared: list[int] = []
        self.runs: list[tuple[int, dict]] = []
        self.run_status = "submitted"
        self.diagnostic_code = None
        self.untrusted_host = ""
        self.reserve_click = False
        self.raises: dict[int, Exception] = {}
        self.order: list[tuple[str, int]] = []
        self.prepare_errors: dict[int, Exception] = {}
        self.flags: dict[int, list] = {}
        monkeypatch.setattr(cvs, "prepare_application", self.prepare)
        monkeypatch.setattr(service, "run", self.run)
        registry = Registry()
        registry.register(DailyEligibilityAdapter(), host="careers.exemple.test",
                          unattended_tenants={"daily-fixture": "first_party_confirmation"})
        monkeypatch.setattr(service, "registry", registry)

    def job(self, n, score=88.0, **kw):
        return make_job(self.profile, title=f"Stage numéro {n}", company=f"Société {n}", score=score,
                        url=f"https://careers.exemple.test/jobs/{n}", **kw)

    def draft(self, job):
        d = self.tmp / f"a{job.id}"
        d.mkdir(exist_ok=True)
        return make_app(self.profile, job, d, master_hash=self.master.content_hash, flags=self.flags.get(job.id))

    async def prepare(self, profile_id, job_id, llm=None):
        self.prepared.append(job_id)
        self.order.append(("prepare", job_id))
        if job_id in self.prepare_errors:
            raise self.prepare_errors[job_id]
        from jobhunt import db
        return self.draft(db.get_job(job_id))

    async def run(self, app_id, *, mode, trigger, headless=None, pool=None, planner=None, llm=None, **kw):
        self.runs.append((app_id, {"mode": mode, "trigger": trigger, "headless": headless, "pool": pool, "attended": kw.get("attended")}))
        app = store.get_application(app_id)
        self.order.append(("run", app.job_id))
        if app.job_id in self.raises:
            raise self.raises[app.job_id]
        r = astore.start_run(app_id, app.current_rev, mode, trigger, lambda rid: self.tmp / "runs" / str(rid))
        if self.run_status == "submitted" or self.reserve_click:  # what the real run does right before the final click
            job = db.get_job(app.job_id)
            astore.reserve_send(r.id, profile_id=app.profile_id, application_id=app_id, trigger=trigger,
                                role_key=policy.normalize_role(job.company, job.title), url_key="", daily_cap=99)
        res = astore.finish_run(r.id, RunResult(status=self.run_status, message="msg", untrusted_host=self.untrusted_host,
                                                **({"diagnostic_code": self.diagnostic_code} if self.diagnostic_code else {})))
        if res.status == "submitted":
            store.mark_submitted(app_id, app.current_rev)
        return res


class DailyEligibilityAdapter:
    """Registry double only. Env.run records dispatch, and no ATS I/O is possible here."""

    name = "daily-fixture"
    capability = "unattended"
    retry_rule = "never"
    boundaries = {"final_submit": "auto"}

    def matches(self, url, dom_markers):
        return float(urlsplit(url).path.startswith("/jobs/"))

    def resolve(self, url, dom_markers):
        return Requisition("daily-fixture", urlsplit(url).path.rsplit("/", 1)[-1])


@pytest.fixture
def env(monkeypatch, profile, master, tmp_path):
    return Env(monkeypatch, profile, master, tmp_path)


async def daily(profile, **kw):
    lines: list[str] = []
    kw.setdefault("pool", FakePool())
    kw.setdefault("llm", object())
    return await service.daily_apply(profile.id, emit=lines.append, **kw), lines


async def test_prepares_approves_and_sends_automatically(env, profile):
    jobs = [env.job(1, 90), env.job(2, 70)]
    s, lines = await daily(profile)
    assert env.prepared == [j.id for j in jobs] and (s.prepared, s.sent, s.waiting, s.paused, s.failed) == (2, 2, 0, 0, 0)
    assert all(m["trigger"] == "auto" and m["headless"] is True and m["mode"] == "submit" for _, m in env.runs)
    assert all(store.get_application(a).approved_by == "auto" for a, _ in env.runs)
    assert any("Société 1" in line for line in lines) and not any("camille" in line.lower() for line in lines)


async def test_daily_lists_unadapted_application_without_launch(env, profile, monkeypatch):
    monkeypatch.setattr(service, "registry", Registry())
    app = env.draft(env.job(1))
    summary, lines = await daily(profile)
    assert not env.runs and summary.sent == 0
    assert store.get_application(app.id).approved_by is None
    assert any(service.SUPERVISED_ONLY.lower() in line for line in lines)


async def test_daily_cap_stops_sending(env, profile, settings, monkeypatch):
    monkeypatch.setattr(settings, "apply_daily_cap", 2)
    for n, sc in enumerate([95, 90, 80, 70], 1):
        env.draft(env.job(n, sc))
    s, _ = await daily(profile)
    assert len(env.runs) == 2 and (s.sent, s.waiting, s.prepared) == (2, 2, 0)
    assert [store.get_application(a).status for a, _ in env.runs] == ["submitted", "submitted"]


async def test_preparation_cap(env, profile, settings, monkeypatch):
    monkeypatch.setattr(settings, "apply_prepare_daily_cap", 2)
    monkeypatch.setattr(settings, "apply_auto", False)
    jobs = [env.job(n, 90 - n) for n in range(1, 5)]
    s, _ = await daily(profile)
    assert env.prepared == [jobs[0].id, jobs[1].id] and s.prepared == 2 and not env.runs
    assert len(store.list_applications(profile.id)) == 2


async def test_auto_switch_off_prepares_without_approving_or_sending(env, profile, settings, monkeypatch):
    monkeypatch.setattr(settings, "apply_auto", False)
    hi, lo = env.job(1, 75), env.job(2, 50)
    existing = env.draft(env.job(3, 90))
    s, _ = await daily(profile)
    assert not env.runs and (s.sent, s.waiting, s.prepared) == (0, 3, 2)
    apps = {a.job_id: a for a in store.list_applications(profile.id)}
    assert all(app.status == "draft" and app.approved_rev is None for app in apps.values())
    assert set(apps) == {hi.id, lo.id, existing.job_id}


async def test_low_score_is_never_sent_even_if_the_user_approved_it(env, profile):
    lo = env.job(1, 50)
    app = cvs.approve(env.draft(lo).id, 1)
    s, _ = await daily(profile)
    assert not env.runs and s.waiting == 1 and store.get_application(app.id).status == "approved"


async def test_blocking_errors_stay_drafts(env, profile):
    j = env.job(1, 90)
    env.flags[j.id] = [ERROR]
    s, lines = await daily(profile)
    assert not env.runs and (s.to_fix, s.waiting) == (1, 0) and store.list_applications(profile.id)[0].status == "draft"
    assert any("à corriger (1 erreur(s) bloquante(s) dans le CV)" in line for line in lines) and "1 à corriger" in lines[-1]


async def test_paused_and_twice_failed_applications_are_not_relaunched(env, profile):
    paused, failed, ok = env.job(1, 95), env.job(2, 90), env.job(3, 80)
    pa, fa, _ = (env.draft(j) for j in (paused, failed, ok))
    r = astore.start_run(pa.id, 1, "submit", "auto", lambda rid: env.tmp / "r" / str(rid))
    astore.finish_run(r.id, RunResult(status="needs_input"))
    for _ in range(2):
        r = astore.start_run(fa.id, 1, "submit", "auto", lambda rid: env.tmp / "r" / str(rid))
        astore.finish_run(r.id, RunResult(status="failed"))
    s, _ = await daily(profile)
    assert [a for a, _ in env.runs] == [store.find_application(profile.id, ok.id).id]
    assert (s.paused, s.failed, s.sent) == (1, 1, 1)


async def test_one_failed_run_is_retried_and_outcomes_are_counted(env, profile, monkeypatch):
    j1, j2, j3 = env.job(1, 95), env.job(2, 90), env.job(3, 80)
    a1 = env.draft(j1)
    r = astore.start_run(a1.id, 1, "submit", "auto", lambda rid: env.tmp / "r" / str(rid))
    astore.finish_run(r.id, RunResult(status="failed"))
    statuses = iter(["submitted", "needs_email", "failed"])
    orig = env.run

    async def run(app_id, **kw):
        env.run_status = next(statuses)
        return await orig(app_id, **kw)
    monkeypatch.setattr(service, "run", run)
    s, _ = await daily(profile)
    assert (s.sent, s.paused, s.failed) == (1, 1, 1) and env.prepared == [j2.id, j3.id]


async def test_unconfirmed_master_stops_early_with_one_error(env, profile, master):
    save_master(profile.id, sample_master())  # any save unconfirms
    for n in range(1, 4):
        env.job(n)
    s, lines = await daily(profile)
    assert not env.prepared and not env.runs and len(s.errors) == 1 and len(lines) == 1 and "non confirmé" in s.errors[0]


async def test_a_failed_preparation_is_recorded_and_the_next_job_still_runs(env, profile):
    j1, j2 = env.job(1, 95), env.job(2, 90)
    env.prepare_errors[j1.id] = CVError("Rendu du PDF impossible (X)")
    s, lines = await daily(profile)
    assert env.prepared == [j1.id, j2.id] and s.prepared == 1 and s.sent == 1
    assert len(s.errors) == 1 and "Société 1" in s.errors[0]
    env.prepare_errors[j1.id] = LLMError("openai: timeout")
    s, _ = await daily(profile)
    assert any("timeout" in e or "LLM" in e or "openai" in e for e in s.errors)


async def test_daily_adapter_dispatch_does_not_take_ownership_of_browser_pool(env, profile, monkeypatch):
    pool = FakePool()
    env.job(1)
    await daily(profile, pool=pool)
    assert not pool.closed and not pool.opened and env.runs[0][1]["pool"] is None
    created = []

    class Spy(FakePool):
        def __init__(self, *a, **k):
            super().__init__()
            created.append(self)
    monkeypatch.setattr(service, "BrowserPool", Spy)
    env.runs.clear()
    await daily(profile, pool=None)  # nothing left to send (already submitted): no pool is ever created
    assert not created
    env.job(2)
    await daily(profile, pool=None)
    assert len(env.runs) == 1 and not created


async def test_live_score_drop_before_auto_approval_keeps_the_draft(env, profile, monkeypatch):
    j1, j2 = env.job(1, 90), env.job(2, 80)
    orig = env.prepare

    async def prepare(profile_id, job_id, llm=None):
        out = await orig(profile_id, job_id, llm)
        if job_id == j1.id:  # the (long) preparation of the CVs: a rescore lowers this job meanwhile
            with db.connect() as c:
                c.execute("UPDATE jobs SET score=40 WHERE id=?", (j1.id,))
        return out

    monkeypatch.setattr(cvs, "prepare_application", prepare)
    s, _ = await daily(profile)
    assert [a for a, _ in env.runs] == [store.find_application(profile.id, j2.id).id] and s.sent == 1 and s.waiting == 1
    assert store.find_application(profile.id, j1.id).status == "draft"  # never auto-approved


async def test_job_status_changed_before_auto_approval_is_skipped(env, profile, monkeypatch):
    j1, j2 = env.job(1, 90), env.job(2, 80)
    orig = env.prepare

    async def prepare(profile_id, job_id, llm=None):
        out = await orig(profile_id, job_id, llm)
        if job_id == j1.id:
            db.set_status([j1.id], "rejected", profile.id)
        return out

    monkeypatch.setattr(cvs, "prepare_application", prepare)
    s, _ = await daily(profile)
    assert len(env.runs) == 1 and s.sent == 1 and store.find_application(profile.id, j1.id).status == "draft"


async def test_a_stale_revision_in_one_job_does_not_abort_the_pass(env, profile, monkeypatch):
    j1, j2, j3 = env.job(1, 95), env.job(2, 90), env.job(3, 80)
    real = cvs.auto_approve
    seen = []

    def auto_approve(app_id, *, expected_rev=None):
        seen.append(expected_rev)
        app = store.get_application(app_id)
        if app.job_id == j1.id:
            raise cvs.StaleRevision("Le CV a changé entre-temps")
        if app.job_id == j2.id:
            raise cvs.TransitionError("Seul un brouillon peut être approuvé")
        return real(app_id, expected_rev=expected_rev)

    monkeypatch.setattr(cvs, "auto_approve", auto_approve)
    s, lines = await daily(profile)
    assert s.sent == 1 and s.waiting == 2 and len(env.runs) == 1 and all(e is not None for e in seen)
    assert sum("à valider à la main" in line for line in lines) == 2


def test_auto_approve_refuses_a_revision_change(profile, master, tmp_path):
    job = make_job(profile)
    app = make_app(profile, job, tmp_path, master_hash=master.content_hash)
    with pytest.raises(cvs.StaleRevision):
        cvs.auto_approve(app.id, expected_rev=app.current_rev + 1)
    assert store.get_application(app.id).status == "draft"
    assert cvs.auto_approve(app.id, expected_rev=app.current_rev).status == "approved"


# ------------------------------------------------------------------ untrusted hosts are never trusted by the daily pass
async def test_daily_pass_does_not_relaunch_a_host_paused_application_and_counts_it_paused(env, profile):
    from jobhunt.apply import facts

    j = env.job(1, 95)
    env.run_status, env.untrusted_host = "blocked", "jobs.autre-site.test"
    s, lines = await daily(profile)
    assert len(env.runs) == 1 and (s.sent, s.paused, s.waiting, s.failed) == (0, 1, 0, 0)
    assert facts.trusted_hosts(profile.id) == []  # the pass never trusts a host by itself
    again, _ = await daily(profile)  # next day's pass: still waiting for the user
    assert len(env.runs) == 1 and again.paused == 1
    facts.add_trusted_host(profile.id, "jobs.autre-site.test")  # the user decided: the application is picked up again
    env.run_status, env.untrusted_host = "submitted", ""
    final, _ = await daily(profile)
    assert len(env.runs) == 2 and final.sent == 1 and store.find_application(profile.id, j.id).status == "submitted"


async def test_a_host_pause_of_an_older_revision_does_not_hold_the_application(env, profile):
    j = env.job(1, 95)
    a = env.draft(j)
    r = astore.start_run(a.id, 1, "submit", "auto", lambda rid: env.tmp / "r" / str(rid))
    astore.finish_run(r.id, RunResult(status="blocked", untrusted_host="jobs.autre-site.test"))
    with db.connect() as c:
        c.execute("UPDATE applications SET current_rev=2 WHERE id=?", (a.id,))
    assert not service._waiting_on_user(store.get_application(a.id), j, astore.last_run(a.id))


# ------------------------------------------------------------------ the large pass (apply auto)
async def test_jobs_go_by_score_times_freshness_whether_their_cv_exists_or_is_prepared_on_the_way(env, profile):
    """Owner's rule: of two otherwise equal jobs the fresher goes first, and an older job that already has an approved CV
    no longer jumps the line."""
    week_old_ready, today, yesterday = env.job(1, 95, age_days=6), env.job(2, 70, age_days=0), env.job(3, 75, age_days=1)
    cvs.approve(env.draft(week_old_ready).id, 1)  # already has an approved CV
    s, _ = await daily(profile)
    assert [(kind, job_id) for kind, job_id in env.order] == [("prepare", today.id), ("run", today.id),
                                                                ("prepare", yesterday.id), ("run", yesterday.id),
                                                                ("run", week_old_ready.id)]
    assert s.sent == 3 and s.prepared == 2


async def test_jobs_of_the_same_age_go_best_score_first(env, profile):
    low, high = env.job(1, 65), env.job(2, 99)
    env.draft(low)  # an existing CV does not jump the line
    await daily(profile)
    assert [job_id for kind, job_id in env.order if kind == "run"] == [high.id, low.id]


async def test_the_preparation_cap_skips_jobs_without_a_cv_but_older_jobs_with_one_still_go(env, profile, settings, monkeypatch):
    monkeypatch.setattr(settings, "apply_prepare_daily_cap", 1)
    fresh1, fresh2, old_ready = env.job(1, 90, age_days=0), env.job(2, 85, age_days=0), env.job(3, 80, age_days=5)
    env.draft(old_ready)
    s, lines = await daily(profile)
    assert env.prepared == [fresh1.id] and [job_id for kind, job_id in env.order if kind == "run"] == [fresh1.id, old_ready.id]
    assert s.sent == 2 and fresh2.id not in {job_id for _, job_id in env.order}
    assert sum("Plafond de 1 CV préparés atteint" in line for line in lines) == 1


async def test_limit_stops_the_pass_after_n_sent(env, profile):
    for n, score in ((1, 90), (2, 80), (3, 70)):
        env.job(n, score)
    s, lines = await daily(profile, limit=2)
    assert s.sent == 2 and len(env.runs) == 2 and env.prepared == [env.prepared[0], env.prepared[1]] and len(env.prepared) == 2
    assert any("Limite de 2 envois atteinte" in line for line in lines)


async def test_min_score_only_raises_the_thresholds(env, profile):
    hi, mid, lo = env.job(1, 90), env.job(2, 70), env.job(3, 50)
    s, _ = await daily(profile, min_score=80)
    assert env.prepared == [hi.id] and s.sent == 1
    env.runs.clear()
    s, _ = await daily(profile, min_score=10)  # lower than the configured thresholds: they still apply (lo < 60 is never sent)
    assert lo.id in env.prepared and store.find_application(profile.id, lo.id).status == "draft" and s.waiting == 1


async def test_legacy_browser_flags_do_not_enable_attended_daily_dispatch(env, profile):
    env.job(1)
    await daily(profile)
    assert env.runs[0][1]["headless"] is True and env.runs[0][1]["attended"] is None
    env.runs.clear()
    env.job(2)
    await daily(profile, headless=False, attended=True)
    assert env.runs[0][1]["headless"] is False and env.runs[0][1]["attended"] is None
    assert env.runs[0][1]["mode"] == "submit"


async def test_no_generic_browser_is_created_when_daily_is_headful(env, profile, monkeypatch):
    seen = []

    class Spy(FakePool):
        def __init__(self, *a, headless=None, **k):
            super().__init__()
            seen.append(headless)
    monkeypatch.setattr(service, "BrowserPool", Spy)
    env.job(1)
    await daily(profile, pool=None, headless=False)
    assert seen == [] and env.runs[0][1]["mode"] == "submit"


async def test_one_application_raising_never_stops_the_pass(env, profile):
    a, b, c = env.job(1, 95), env.job(2, 90), env.job(3, 85)
    env.raises[b.id] = RuntimeError("boom with secret-token")
    s, lines = await daily(profile)
    assert [j for _, j in env.order if _ == "run"] == [a.id, b.id, c.id]
    assert (s.sent, s.failed) == (2, 1) and any("erreur inattendue : RuntimeError" in line for line in lines)
    assert not any("secret-token" in line for line in lines + s.errors)


async def test_progress_lines_are_numbered_and_the_counts_close_the_pass(env, profile, monkeypatch):
    ok, paused, blocked, fail, check = (env.job(n, 100 - n) for n in range(1, 6))
    statuses = {ok.id: "submitted", paused.id: "needs_input", blocked.id: "submitted", fail.id: "failed", check.id: "failed"}
    original = env.run

    async def run(app_id, **kw):
        env.run_status = statuses[store.get_application(app_id).job_id]
        env.reserve_click = store.get_application(app_id).job_id == check.id
        return await original(app_id, **kw)

    monkeypatch.setattr(service, "run", run)
    s, lines = await daily(profile)
    body = [line for line in lines if line.startswith("[")]
    assert [line.split("]")[0] for line in body] == [f"[{n}/5" for n in range(1, 6)]
    by = {line.split("] ", 1)[1].split(" — ")[0]: line for line in body}
    assert by["Société 1"].endswith(": envoyée") and "en pause (" in by["Société 2"]
    assert "échec (La tentative a échoué.)" in by["Société 4"]
    assert "à vérifier (envoi non confirmé : La tentative a échoué.)" in by["Société 5"]
    assert (s.sent, s.paused, s.failed, s.to_check) == (2, 1, 1, 1)
    assert any(line.startswith("Passe terminée : 2 envoyée(s), 1 en pause, 1 à vérifier, 0 à corriger") for line in lines)
    assert "Détail : 2 envoyée(s) · 1 en pause (question 1) · 1 à vérifier · 0 à corriger · 0 bloquée(s) · 1 échec(s) technique(s)" in lines


async def test_the_pass_blocked_by_a_problem_names_the_reason(env, profile, monkeypatch):
    j = env.job(1, 90)
    monkeypatch.setattr(get_settings(), "apply_auto", False)
    s, lines = await daily(profile)
    assert s.waiting == 1 and any("bloquée (envoi automatique désactivé" in line for line in lines)


def test_a_second_unrelated_posting_at_the_same_company_is_never_a_duplicate(profile, master, tmp_path):
    from apply_cv_helpers import make_app, make_job
    first = make_job(profile, title="Stage Ingénieur Systèmes Embarqués (H/F)", company="Thales", url="https://careers.thales.test/jobs/1")
    second = make_job(profile, title="Stage Ingénieur Radar (H/F)", company="Thales SA", url="https://careers.thales.test/jobs/2")
    same = make_job(profile, title="Stage Ingénieur Systèmes Embarqués F/H", company="THALES", url="https://careers.thales.test/jobs/3")
    a1, a2, a3 = (make_app(profile, j, tmp_path, master_hash=master.content_hash) for j in (first, second, same))
    cvs.auto_approve(a1.id)
    store.mark_submitted(a1.id, 1)
    assert policy.duplicate_of(a2, second) is None  # another internship at the same company: sendable
    assert not any("déjà envoyée" in p for p in policy.submit_problems(a2, second, trigger="user"))
    assert policy.duplicate_of(a3, same).id == a1.id  # same company + same normalized title
    # a send attempt in flight (reservation) behaves the same way
    run = astore.start_run(a2.id, 1, "submit", "auto", lambda rid: tmp_path / f"r{rid}")
    astore.reserve_send(run.id, profile_id=profile.id, application_id=a2.id, trigger="auto", role_key=policy.normalize_role(
        second.company, second.title), url_key="careers.thales.test/jobs/2", daily_cap=9)
    other = make_job(profile, title="Stage Ingénieur Cybersécurité", company="Thales", url="https://careers.thales.test/jobs/4")
    a4 = make_app(profile, other, tmp_path, master_hash=master.content_hash)
    assert policy.duplicate_of(a4, other) is None
    same_url = make_job(profile, title="Un autre titre", company="Thales", url="https://careers.thales.test/jobs/2?utm_source=x")
    a5 = make_app(profile, same_url, tmp_path, master_hash=master.content_hash)
    assert policy.duplicate_of(a5, same_url).id == a2.id  # same origin URL


def test_apply_auto_command_passes_its_options(profile, monkeypatch):
    from typer.testing import CliRunner

    from jobhunt.apply.service import ApplySummary
    from jobhunt.cli import app as cli

    seen = {}

    async def fake(profile_id, **kw):
        seen.update(kw, profile_id=profile_id)
        return ApplySummary(profile_id=profile_id)

    monkeypatch.setattr(service, "daily_apply", fake)
    r = CliRunner().invoke(cli, ["apply", "auto", str(profile.id)])
    assert r.exit_code == 0 and seen["headless"] is True and seen["attended"] is False and seen["limit"] is None and seen["min_score"] is None
    r = CliRunner().invoke(cli, ["apply", "auto", str(profile.id), "--headful", "--attended", "--limit", "20", "--min-score", "75"])
    assert r.exit_code == 0 and seen["headless"] is False and seen["attended"] is True and seen["limit"] == 20 and seen["min_score"] == 75
    r = CliRunner().invoke(cli, ["apply", "auto", str(profile.id), "--attended"])  # nobody watches a hidden window
    assert r.exit_code != 0
    assert CliRunner().invoke(cli, ["apply", "auto", "999"]).exit_code != 0
    assert CliRunner().invoke(cli, ["apply", "auto", str(profile.id), "--limit", "0"]).exit_code != 0


# ------------------------------------------------------------------ batch fixes: retry / resume policy, breakdown
from datetime import UTC, datetime, timedelta  # noqa: E402

from jobhunt.apply import facts  # noqa: E402
from jobhunt.apply.types import Question  # noqa: E402


def paused_run(env, job, status, *, questions=(), message="", age=timedelta(0), rev=1):
    """An approved application of `job` whose last run ended `status` `age` ago."""
    app = store.find_application(env.profile.id, job.id) or cvs.approve(env.draft(job).id, 1)
    r = astore.start_run(app.id, rev, "submit", "auto", lambda rid: env.tmp / "runs" / str(rid))
    done = astore.finish_run(r.id, RunResult(status=status, message=message, questions=list(questions)))
    with db.connect() as c:
        c.execute("UPDATE application_runs SET finished_at=? WHERE id=?", (db._iso(datetime.now(UTC) - age), done.id))
    return app


async def test_a_paused_run_whose_questions_are_all_answered_is_resumed(env, profile):
    job = env.job(1)
    paused_run(env, job, "needs_input", questions=[Question(label="Date de naissance ?"), Question(label="Permis B ?")],
               age=-timedelta(minutes=5))  # finished "after" any answers file written now: only the answers themselves count
    s, lines = await daily(profile)
    assert not env.runs and s.paused == 1  # nothing answered yet
    facts.save_custom_answers(profile.id, [("date de naissance", "4 mars 1999")])
    s, _ = await daily(profile)
    assert not env.runs and s.paused == 1  # only one of the two
    facts.save_custom_answers(profile.id, [("Permis B ?", "Oui")])
    s, lines = await daily(profile)
    assert len(env.runs) == 1 and s.sent == 1 and any("envoyée" in line for line in lines)


async def test_a_paused_run_is_resumed_when_the_answers_file_changed_since(env, profile):
    job = env.job(1)
    paused_run(env, job, "needs_input", questions=[Question(label="Nationalité ?")], age=timedelta(minutes=5))
    facts.save_answers(profile.id, {"nationality": "Française"})  # written AFTER the run finished
    s, _ = await daily(profile)
    assert len(env.runs) == 1 and s.sent == 1


async def test_the_answers_file_older_than_the_pause_does_not_resume_it(env, profile):
    job = env.job(1)
    facts.save_answers(profile.id, {"nationality": "Française"})
    paused_run(env, job, "needs_input", questions=[Question(label="Nationalité ?")], age=timedelta(0))
    with db.connect() as c:
        c.execute("UPDATE application_runs SET finished_at=?", (db._iso(datetime.now(UTC) + timedelta(minutes=1)),))
    s, _ = await daily(profile)
    assert not env.runs and s.paused == 1


async def test_an_email_pause_does_not_resume_on_a_timer(env, profile):
    job = env.job(1)
    app = paused_run(env, job, "needs_email", age=timedelta(minutes=5))
    s, _ = await daily(profile)
    assert not env.runs and s.paused == 1  # too early: the candidate may not have clicked yet

    def age_last(minutes):
        with db.connect() as c:
            c.execute("UPDATE application_runs SET finished_at=? WHERE id=(SELECT MAX(id) FROM application_runs WHERE application_id=?)",
                      (db._iso(datetime.now(UTC) - timedelta(minutes=minutes)), app.id))

    for minutes in (45, 120, 1440):
        age_last(minutes)
        s, _ = await daily(profile)
        assert not env.runs and s.paused == 1


async def test_a_captcha_pause_needs_an_explicit_resume_even_on_attended_pass(env, profile):
    job = env.job(1)
    paused_run(env, job, "needs_human", message="CAPTCHA")
    for kw in ({}, {"headless": False}, {"attended": True}, {"headless": False, "attended": True}):
        s, _ = await daily(profile, **kw)
        assert not env.runs and s.paused == 1, kw


def engine_paused_run(env, job, code, *, started, submit_clicked=False):
    """An approved application of `job` paused `needs_human` by the engine itself (`code`), started at `started`."""
    app = store.find_application(env.profile.id, job.id) or cvs.approve(env.draft(job).id, 1)
    r = astore.start_run(app.id, 1, "submit", "auto", lambda rid: env.tmp / "runs" / str(rid))
    astore.finish_run(r.id, RunResult(status="needs_human", diagnostic_code=code, submit_clicked=submit_clicked))
    with db.connect() as c:
        c.execute("UPDATE application_runs SET started_at=? WHERE id=?", (started, r.id))
    return app


OLD = "2026-11-19T19:50:00+00:00"   # before astore.ENGINE_RETRY_EPOCH
NEW = (datetime.fromisoformat(astore.ENGINE_RETRY_EPOCH) + timedelta(minutes=1)).isoformat()   # after it (moves with it)
RECENT = (datetime.fromisoformat(astore.ENGINE_RETRY_EPOCH) - timedelta(hours=2)).isoformat()   # before it, less than a day


@pytest.mark.parametrize("code", ["recipient_refused_same_site_write", "recipient_refused_redirect", "public_page_shell",
                                  "apply_target_unverified", "job_page_no_apply_control"])
async def test_an_engine_pause_from_older_code_is_retried_automatically(env, profile, code):
    job = env.job(1)
    app = engine_paused_run(env, job, code, started=OLD)
    assert not astore.has_site_handoff(app.id)
    s, _ = await daily(profile)
    assert [a for a, _ in env.runs] == [app.id] and s.paused == 0


async def test_an_engine_pause_from_current_code_still_waits(env, profile):
    job = env.job(1)
    app = engine_paused_run(env, job, "recipient_refused_same_site_write", started=NEW)
    s, _ = await daily(profile)
    assert not env.runs and s.paused == 1 and astore.has_site_handoff(app.id)


@pytest.mark.parametrize("code", ["challenge_or_rate_limited", "human_required", "credential_page", "posting_mismatch"])
async def test_an_old_human_pause_is_never_retried(env, profile, code):
    job = env.job(1)
    app = engine_paused_run(env, job, code, started=OLD)
    s, _ = await daily(profile)
    assert not env.runs and s.paused == 1 and astore.has_site_handoff(app.id)


async def test_the_owners_switch_reads_a_captcha_in_the_message_of_a_pause_without_code(env, profile, monkeypatch):
    from jobhunt.config import get_settings

    monkeypatch.setattr(get_settings(), "apply_retry_stale_pauses", True)
    app = cvs.approve(env.draft(env.job(1)).id, 1)
    r = astore.start_run(app.id, 1, "submit", "auto", lambda rid: env.tmp / "runs" / str(rid))
    astore.finish_run(r.id, RunResult(status="needs_human", message="Un CAPTCHA doit être résolu à la main."))
    with db.connect() as c:
        c.execute("UPDATE application_runs SET started_at=? WHERE id=?", (RECENT, r.id))
    s, _ = await daily(profile)
    assert not env.runs and s.paused == 1 and astore.has_site_handoff(app.id)


@pytest.fixture
def retry_pauses(monkeypatch):
    from jobhunt.config import get_settings

    monkeypatch.setattr(get_settings(), "apply_retry_stale_pauses", True)


@pytest.mark.parametrize("code", ["human_required", "credential_page", "posting_mismatch", "supervised_only", "account_login_failed"])
async def test_with_the_owners_switch_an_old_pause_that_is_not_a_captcha_is_retried(env, profile, retry_pauses, code):
    job = env.job(1)
    app = engine_paused_run(env, job, code, started=OLD)
    assert not astore.has_site_handoff(app.id)
    s, _ = await daily(profile)
    assert [a for a, _ in env.runs] == [app.id] and s.paused == 0


@pytest.mark.parametrize(("code", "started", "clicked"), [
    ("challenge", RECENT, False), ("challenge_or_rate_limited", RECENT, False),  # a CAPTCHA of the last day waits for a person
    ("posting_mismatch", NEW, False),  # judged by the current code: it waits
    ("posting_mismatch", OLD, True),  # the send click was reached: never retried
])
async def test_the_owners_switch_never_retries_a_captcha_a_fresh_pause_or_a_possible_send(env, profile, retry_pauses, code, started, clicked):
    job = env.job(1)
    app = engine_paused_run(env, job, code, started=started, submit_clicked=clicked)
    s, _ = await daily(profile)
    assert not env.runs and s.paused == 1 and astore.has_site_handoff(app.id)


@pytest.mark.parametrize("code", ["challenge", "challenge_or_rate_limited"])
async def test_with_the_owners_switch_a_captcha_pause_more_than_a_day_old_is_tried_again(env, profile, retry_pauses, code):
    """Such walls come and go with the session; one that still stands only pauses the application again. Without the
    switch it waits for a person, whatever its age."""
    job = env.job(1)
    app = engine_paused_run(env, job, code, started=OLD)
    assert not astore.has_site_handoff(app.id)
    s, _ = await daily(profile)
    assert [a for a, _ in env.runs] == [app.id] and s.paused == 0


@pytest.mark.parametrize(("minutes", "retried"), [(-60, True), (60, False)])
async def test_a_captcha_pause_judged_before_the_detector_was_corrected_is_tried_again_at_once(env, profile, retry_pauses, monkeypatch,
                                                                                               minutes, retried):
    """The detector took invisible bot checks for challenges until a known instant. A pass started after it tries a stop
    judged before it again without waiting a day; a stop judged since still waits its day."""
    fixed = datetime.fromisoformat(astore.CHALLENGE_DETECTION_FIXED)
    monkeypatch.setattr(astore, "ENGINE_RETRY_EPOCH", (fixed + timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%S+00:00"))
    app = engine_paused_run(env, env.job(1), "challenge", started=(fixed + timedelta(minutes=minutes)).isoformat())
    assert astore.has_site_handoff(app.id) is not retried
    s, _ = await daily(profile)
    assert ([a for a, _ in env.runs] == [app.id] and s.paused == 0) if retried else (not env.runs and s.paused == 1)


async def test_with_the_owners_switch_an_old_wait_for_an_email_is_tried_again(env, profile, retry_pauses):
    """An application once left "waiting for an e-mail" (when a person had to follow the link) is run again: the engine reads
    the mailbox itself now. One paused by the current pass still waits for its mail."""
    app = cvs.approve(env.draft(env.job(1)).id, 1)
    r = astore.start_run(app.id, 1, "submit", "auto", lambda rid: env.tmp / "runs" / str(rid))
    astore.finish_run(r.id, RunResult(status="needs_email"))
    with db.connect() as c:
        c.execute("UPDATE application_runs SET started_at=? WHERE id=?", (NEW, r.id))
    assert astore.has_site_handoff(app.id)
    with db.connect() as c:
        c.execute("UPDATE application_runs SET started_at=? WHERE id=?", (OLD, r.id))
    assert not astore.has_site_handoff(app.id)
    s, _ = await daily(profile)
    assert [a for a, _ in env.runs] == [app.id] and s.paused == 0


async def test_an_old_engine_pause_after_a_submit_click_is_never_retried(env, profile):
    job = env.job(1)
    app = engine_paused_run(env, job, "recipient_refused_same_site_write", started=OLD, submit_clicked=True)
    s, _ = await daily(profile)
    assert not env.runs and s.paused == 1 and astore.has_site_handoff(app.id)


def autonomous_outcome(env, job, status, *, started, receipt=None, submit_clicked=False):
    """A finished autonomous run on job.example.test (its reservation bound to that host)."""
    app = store.find_application(env.profile.id, job.id) or cvs.approve(env.draft(job).id, 1)
    r = astore.start_run(app.id, 1, "submit", "auto", lambda rid: env.tmp / "runs" / str(rid))
    astore.finish_run(r.id, RunResult(status=status))
    with db.connect() as c:
        c.execute("UPDATE application_runs SET started_at=?, submit_clicked_at=? WHERE id=?",
                  (started, started if submit_clicked else None, r.id))
        c.execute("INSERT INTO apply_reservations(profile_id, application_id, run_id, trigger, role_key, url_key, created_at,"
                  " released_at, binding_json, receipt_outcome) VALUES (?,?,?,?,?,?,?,?,?,?)",
                  (env.profile.id, app.id, r.id, "auto", f"role-{r.id}", f"url-{r.id}", started, started,
                   '{"autonomous": 1, "host": "job.example.test"}', receipt))


@pytest.mark.parametrize("outcomes, paused", [
    ([("failed", OLD, None, False), ("failed", OLD, None, False)], False),        # judged by older code
    ([("failed", NEW, None, False), ("failed", NEW, None, False)], True),
    ([("failed", OLD, None, False), ("failed", NEW, None, False)], False),        # only one failure counts
    ([("failed", OLD, None, True), ("failed", OLD, "uncertain", False)], True),   # a possible send always counts
])
async def test_old_engine_failures_no_longer_pause_their_host(env, profile, host_pause, outcomes, paused):
    for n, (status, started, receipt, clicked) in enumerate(outcomes, 1):
        autonomous_outcome(env, env.job(n), status, started=started, receipt=receipt, submit_clicked=clicked)
    assert astore.host_paused("job.example.test") is paused
    assert ("job.example.test" in astore.paused_hosts()) is paused


async def test_two_failures_on_a_site_do_not_leave_its_other_applications_to_the_supervised_mode(env, profile, monkeypatch):
    """Default: no site pause. The pass keeps sending the site's other applications (each one stops after its own two
    failures); `apply_host_pause` restores the pause."""
    from jobhunt.config import get_settings

    for n in (1, 2):
        autonomous_outcome(env, env.job(n), "failed", started=NEW, submit_clicked=n == 1)
    assert not astore.host_paused("job.example.test") and astore.paused_hosts() == set()
    monkeypatch.setattr(get_settings(), "apply_host_pause", True)
    assert astore.host_paused("job.example.test") and astore.paused_hosts() == {"job.example.test"}


@pytest.mark.parametrize("code", ["supervised_only", "no_adapter"])
async def test_an_application_once_left_to_the_supervised_mode_is_sent_by_the_next_pass(env, profile, code):
    job = env.job(1)
    app = engine_paused_run(env, job, code, started=OLD)
    assert not astore.has_site_handoff(app.id)
    s, lines = await daily(profile)
    assert [a for a, _ in env.runs] == [app.id] and (s.sent, s.paused, s.supervised) == (1, 0, 0)
    assert not any("supervisé" in line for line in lines)


@pytest.mark.parametrize("status", ["needs_email", "needs_human"])
async def test_a_new_cv_revision_does_not_clear_a_site_handoff(env, profile, status):
    job = env.job(1)
    app = paused_run(env, job, status)
    assert service._waiting_on_user(app.model_copy(update={"current_rev": app.current_rev + 1}), job, astore.last_run(app.id))


@pytest.mark.parametrize("status", ["needs_email", "needs_human"])
async def test_dry_run_or_refusal_cannot_clear_a_site_handoff(env, profile, status):
    job = env.job(1)
    app = paused_run(env, job, status)
    for mode, outcome in (("dry_run", "ready"), ("submit", "blocked")):
        attempt = astore.start_run(app.id, app.current_rev, mode, "user", lambda rid: env.tmp / "runs" / str(rid))
        astore.finish_run(attempt.id, RunResult(status=outcome))
        summary, _ = await daily(profile)
        assert summary.paused == 1 and not env.runs
        entry = next(item for item in service.queue(profile.id) if item.app.id == app.id)
        assert not entry.auto
        assert "intervention manuelle requise sur le site" in entry.problems


async def test_a_technical_failure_without_a_click_is_retried_twice_per_revision(env, profile):
    job = env.job(1)
    env.run_status = "failed"
    for attempt in (1, 2):
        s, _ = await daily(profile)
        assert len(env.runs) == attempt and s.failed == 1
    s, lines = await daily(profile)
    assert len(env.runs) == 2 and s.failed == 1 and any("deux essais ont déjà échoué" in line for line in lines)


async def test_failures_from_before_the_engine_retry_epoch_without_a_click_do_not_end_retries(env, profile):
    job = env.job(1)
    env.run_status = "failed"
    for _ in (1, 2):
        await daily(profile)
    assert len(env.runs) == 2
    with db.connect() as conn:  # both failures were judged by older engine code and never clicked anything
        conn.execute("UPDATE application_runs SET started_at='2026-01-01T00:00:00+00:00'")
    s, _ = await daily(profile)
    assert len(env.runs) == 3 and s.failed == 1
    with db.connect() as conn:  # a click keeps the old failure counted: it may have sent something
        conn.execute("UPDATE application_runs SET started_at='2026-01-01T00:00:00+00:00', nav_clicked_at=started_at")
    s, lines = await daily(profile)
    assert len(env.runs) == 3 and any("deux essais ont déjà échoué" in line for line in lines)


async def test_a_blocked_unusable_link_run_never_counts_as_a_failed_attempt(env, profile):
    job = env.job(1)
    app = cvs.approve(env.draft(job).id, 1)
    for _ in range(3):
        r = astore.start_run(app.id, 1, "submit", "auto", lambda rid: env.tmp / "runs" / str(rid))
        astore.finish_run(r.id, RunResult(status="blocked", message="Lien de l'offre inexploitable : page vide"))
    s, _ = await daily(profile)
    assert len(env.runs) == 1 and s.sent == 1


async def test_an_application_already_resumed_and_sent_by_someone_else_is_skipped(env, profile, monkeypatch):
    job = env.job(1)
    app = paused_run(env, job, "needs_input", questions=[Question(label="Q ?")])
    facts.save_custom_answers(profile.id, [("Q ?", "R")])
    candidates = policy.auto_candidates

    def after_listing(pid, now=None):
        found = candidates(pid, now)
        cvs_sent(app)  # sent by someone else AFTER the pass listed its candidates
        return found

    monkeypatch.setattr(policy, "auto_candidates", after_listing)
    s, lines = await daily(profile)
    assert not env.runs and s.sent == 0 and any("déjà traitée par ailleurs" in line for line in lines)


def cvs_sent(app):
    """Another process (the web portal) resumed and sent it between the candidate list and its turn."""
    store.mark_submitted(app.id, 1)


async def test_an_application_being_resumed_elsewhere_is_left_alone(env, profile, monkeypatch):
    job = env.job(1)
    app = cvs.approve(env.draft(job).id, 1)
    r = astore.start_run(app.id, 1, "submit", "user", lambda rid: env.tmp / "runs" / str(rid))  # running, our own pid is alive
    with db.connect() as c:
        # the parent process (the test runner's shell) is alive and is not us, on every OS (Windows has no pid 1)
        c.execute("UPDATE application_runs SET pid=? WHERE id=?", (os.getppid(), r.id))
    s, lines = await daily(profile)
    assert not env.runs and s.sent == 0 and s.waiting == 1 and any("déjà en cours" in line for line in lines)


async def test_the_summary_breaks_down_pauses_and_lists_the_most_frequent_questions(env, profile, monkeypatch):
    jobs = [env.job(n, 100 - n) for n in range(1, 6)]
    plan = {jobs[0].id: ("needs_input", [Question(label="Date de naissance ?"), Question(label="Permis B ?")]),
            jobs[1].id: ("needs_input", [Question(label="Date de naissance ?")]),
            jobs[2].id: ("needs_email", []), jobs[3].id: ("needs_human", []), jobs[4].id: ("submitted", [])}
    original = env.run

    async def run(app_id, **kw):
        status, questions = plan[store.get_application(app_id).job_id]
        env.run_status = status
        res = await original(app_id, **kw)
        if questions:
            with db.connect() as c:
                c.execute("UPDATE application_runs SET result_json=? WHERE id=?",
                          (RunResult(status=status, questions=questions).model_dump_json(), res.id))
            res = astore.get_run(res.id)
        return res

    monkeypatch.setattr(service, "run", run)
    s, lines = await daily(profile)
    assert s.paused_by == {"question": 2, "e-mail": 1, "intervention": 1} and s.sent == 1
    assert s.top_questions() == [("date de naissance", 2), ("permis b", 1)]
    assert "Détail : 1 envoyée(s) · 4 en pause (e-mail 1, intervention 1, question 2)" in " ".join(lines)
    i = lines.index("Questions les plus fréquentes (à renseigner d'abord sur la page « Réponses aux formulaires ») :")
    assert lines[i + 1:i + 3] == ["  1. date de naissance (×2)", "  2. permis b (×1)"]


def test_pause_reason_labels():
    from jobhunt.apply.store import ApplyRun
    base = dict(id=1, application_id=1, rev=1, mode="submit", trigger="auto", started_at=datetime.now(UTC))
    assert service.pause_reason_label(ApplyRun(**base, status="needs_input")) == "question"
    assert service.pause_reason_label(ApplyRun(**base, status="needs_email")) == "e-mail"
    assert service.pause_reason_label(ApplyRun(**base, status="needs_human", message="Un CAPTCHA")) == "CAPTCHA"
    assert service.pause_reason_label(ApplyRun(**base, status="needs_human", message="Création de compte impossible")) == "compte"
    assert service.pause_reason_label(ApplyRun(**base, status="blocked", result=RunResult(status="blocked", untrusted_host="x.test"))) == "site non approuvé"


@pytest.mark.parametrize("code, label", [
    ("recipient_refused_other_navigation", "destination"), ("recipient_refused_response", "destination"),
    ("apply_target_unverified", "lien de candidature"), ("job_page_no_apply_control", "lien de candidature"),
    ("posting_mismatch", "lien de candidature"), ("challenge", "CAPTCHA"), ("challenge_or_rate_limited", "anti-robot"),
    ("credential_page", "compte"), ("human_required", "intervention"),
])
def test_pause_reason_labels_follow_the_diagnostic_code(code, label):
    """A paused run is labelled by its diagnostic code: a refused destination is not a CAPTCHA."""
    from jobhunt.apply.store import ApplyRun
    base = dict(id=1, application_id=1, rev=1, mode="submit", trigger="auto", started_at=datetime.now(UTC))
    run = ApplyRun(**base, status="needs_human", message="Destination ou transmission non autorisée : fenêtre fermée.",
                   result=RunResult(status="needs_human", diagnostic_code=code))
    assert service.pause_reason_label(run) == label
    assert "CAPTCHA" not in service._pause_detail(run) or label == "CAPTCHA"


# ------------------------------------------------------------------ automatic CV regeneration
def error_then_clean(env, job, bad_first):
    """Fake `prepare_application`: the first `bad_first` generations carry a blocking error, the next ones are clean."""
    state = {"calls": 0}

    async def prepare(profile_id, job_id, llm=None):
        state["calls"] += 1
        env.prepared.append(job_id)
        env.flags[job_id] = [ERROR] if state["calls"] <= bad_first else []
        return env.draft(db.get_job(job_id))

    return state, prepare


async def test_a_cv_with_blocking_errors_is_regenerated_then_approved_and_sent_in_the_same_pass(env, profile, monkeypatch):
    job = env.job(1)
    state, prepare = error_then_clean(env, job, bad_first=1)
    monkeypatch.setattr(cvs, "prepare_application", prepare)
    s, lines = await daily(profile)
    assert state["calls"] == 2 and s.prepared == 2 and s.sent == 1 and s.to_fix == 0
    assert any(line == "[1/1] Société 1 — Stage numéro 1 : CV régénéré (erreurs bloquantes), nouvel essai" for line in lines)
    app = store.find_application(profile.id, job.id)
    assert app.current_rev == 2 and app.status == "submitted" and app.approved_by == "auto"
    assert astore.auto_regens(app.id) == 1


async def test_a_cv_still_blocked_after_the_retries_is_left_to_fix(env, profile, monkeypatch):
    job = env.job(1)
    state, prepare = error_then_clean(env, job, bad_first=99)
    monkeypatch.setattr(cvs, "prepare_application", prepare)
    s, lines = await daily(profile)
    assert state["calls"] == 3 and s.to_fix == 1 and s.sent == 0 and not env.runs  # 1 generation + 2 retries (the default)
    app = store.find_application(profile.id, job.id)
    assert astore.auto_regens(app.id) == 2 and app.status == "draft"
    assert sum("CV régénéré" in line for line in lines) == 2 and any("à corriger" in line for line in lines)
    s, _ = await daily(profile)  # the counter persists: no more automatic regenerations on the next pass
    assert state["calls"] == 3 and s.to_fix == 1


async def test_the_retry_count_is_a_setting_and_regenerations_use_the_preparation_cap(env, profile, monkeypatch):
    job = env.job(1)
    state, prepare = error_then_clean(env, job, bad_first=99)
    monkeypatch.setattr(cvs, "prepare_application", prepare)
    monkeypatch.setattr(get_settings(), "apply_cv_retries", 0)
    s, _ = await daily(profile)
    assert state["calls"] == 1 and s.to_fix == 1  # retries switched off
    env2 = [env.job(2, 70)]
    monkeypatch.setattr(get_settings(), "apply_cv_retries", 5)
    monkeypatch.setattr(get_settings(), "apply_prepare_daily_cap", 3)
    state["calls"] = 0
    s, _ = await daily(profile)
    assert state["calls"] <= 3  # generations and regenerations share the cap


async def test_a_regeneration_that_raises_is_recorded_and_the_pass_goes_on(env, profile, monkeypatch):
    j1, j2 = env.job(1, 95), env.job(2, 90)
    calls = {"n": 0}

    async def prepare(profile_id, job_id, llm=None):
        calls["n"] += 1
        if calls["n"] == 2:  # the regeneration of job 1
            raise LLMError("openai: timeout")
        env.flags[job_id] = [ERROR] if job_id == j1.id else []
        env.prepared.append(job_id)
        return env.draft(db.get_job(job_id))

    monkeypatch.setattr(cvs, "prepare_application", prepare)
    s, lines = await daily(profile)
    assert s.to_fix == 1 and s.sent == 1 and any("timeout" in e for e in s.errors)


def test_the_cv_retries_setting_is_read_from_the_environment(monkeypatch):
    from jobhunt import config

    monkeypatch.delenv("JOBHUNT_APPLY_CV_RETRIES", raising=False)
    assert config.get_settings.__wrapped__().apply_cv_retries == 2
    monkeypatch.setenv("JOBHUNT_APPLY_CV_RETRIES", "4")
    assert config.get_settings.__wrapped__().apply_cv_retries == 4


def test_auto_regens_counter_is_atomic_and_per_application(profile, master, tmp_path):
    a = make_app(profile, make_job(profile, title="Un", company="A", url="https://c.exemple.test/1"), tmp_path, master_hash=master.content_hash)
    b = make_app(profile, make_job(profile, title="Deux", company="B", url="https://c.exemple.test/2"), tmp_path, master_hash=master.content_hash)
    assert astore.auto_regens(a.id) == 0 and astore.bump_auto_regens(a.id) == 1 and astore.bump_auto_regens(a.id) == 2
    assert astore.auto_regens(b.id) == 0 and astore.auto_regens(9999) == 0


# ------------------------------------------------------------------ CVs generated from an older master
async def test_an_approved_cv_from_an_older_master_is_rebuilt_reapproved_and_sent(env, profile):
    job = env.job(1)
    old = cvs.approve(env.draft(job).id, 1)
    newer = sample_master()
    newer.summary += " Nouveau projet ajouté."
    env.master = save_confirmed(profile.id, newer)
    assert old.master_hash != env.master.content_hash
    s, lines = await daily(profile)
    assert env.prepared == [job.id] and s.prepared == 1 and s.sent == 1
    assert "[1/1] Société 1 — Stage numéro 1 : CV régénéré (CV maître modifié)" in lines
    app = store.find_application(profile.id, job.id)
    assert app.current_rev == 2 and app.status == "submitted" and app.master_hash == env.master.content_hash
    assert astore.auto_regens(app.id) == 0  # not a retry of a broken CV


async def test_a_stale_cv_waits_when_the_preparation_cap_is_spent(env, profile, monkeypatch):
    job = env.job(1)
    cvs.approve(env.draft(job).id, 1)
    newer = sample_master()
    newer.summary += " Nouveau projet ajouté."
    env.master = save_confirmed(profile.id, newer)
    monkeypatch.setattr(get_settings(), "apply_prepare_daily_cap", 0)
    s, lines = await daily(profile)
    assert not env.prepared and not env.runs and s.sent == 0
    assert any("CV maître modifié depuis la génération" in line for line in lines)


async def test_a_generic_site_draft_above_the_threshold_is_auto_approved_and_sent(env, profile, monkeypatch):
    monkeypatch.setattr(get_settings(), "apply_autonomous", True)
    job = make_job(profile, title="Stage générique", company="Société G", score=88.0,
                   url="https://emplois.autre-site.test/offres/7")
    assert service._generic_enabled(db.get_job(job.id))
    s, lines = await daily(profile)
    app = store.find_application(profile.id, job.id)
    assert s.sent == 1 and app.status == "submitted" and app.approved_by == "auto", lines


async def test_a_generic_site_draft_below_the_threshold_stays_a_draft(env, profile, monkeypatch):
    monkeypatch.setattr(get_settings(), "apply_autonomous", True)
    job = make_job(profile, title="Stage générique", company="Société G", score=55.0,
                   url="https://emplois.autre-site.test/offres/7")
    s, _ = await daily(profile)
    app = store.find_application(profile.id, job.id)
    assert s.sent == 0 and app.status == "draft" and not env.runs


async def test_an_account_page_stop_from_before_the_epoch_is_retried_once(env, profile):
    job = env.job(1)
    env.run_status = "needs_input"
    await daily(profile)
    assert len(env.runs) == 1
    with db.connect() as conn:  # an account page the old engine could not get through, before the engine change
        conn.execute("UPDATE application_runs SET started_at=?, result_json=json_set(result_json, '$.diagnostic_code', "
                     "'credential_page')", (OLD,))
    s, _ = await daily(profile)
    assert len(env.runs) == 2


async def test_a_question_stop_from_before_the_epoch_is_retried_once_then_waits(env, profile):
    """A missing-answer pause judged by older engine code is retried once (the new engine may answer it); a new pause from
    current code waits for the candidate again; a pause after a click is never retried."""
    job = env.job(1)
    env.run_status = "needs_input"
    await daily(profile)
    assert len(env.runs) == 1
    with db.connect() as conn:
        conn.execute("UPDATE application_runs SET started_at=?, result_json=json_set(result_json, '$.diagnostic_code', "
                     "'required_fields')", (OLD,))
    await daily(profile)
    assert len(env.runs) == 2            # retried once with the newer engine
    await daily(profile)
    assert len(env.runs) == 2            # the new pause (current code) waits for an answer
    with db.connect() as conn:            # an old pause after a click: never retried
        conn.execute("UPDATE application_runs SET started_at=?, nav_clicked_at=?", (OLD, OLD))
    await daily(profile)
    assert len(env.runs) == 2


async def test_an_offer_the_site_closed_twice_in_a_row_leaves_the_queue(env, profile):
    """A closed offer was opened again at every pass and stayed in the to-do lists. After the second "closed" in a row the
    application is withdrawn and the job set aside; the next pass no longer opens it."""
    job = env.job(1)
    app = cvs.approve(env.draft(job).id, 1)
    env.run_status, env.diagnostic_code = "blocked", "job_closed"
    await daily(profile)
    assert store.get_application(app.id).status == "approved" and len(env.runs) == 1
    _, lines = await daily(profile)
    assert store.get_application(app.id).status == "withdrawn" and db.get_job(job.id).status == "rejected"
    assert len(env.runs) == 2 and any("retirée de la file" in line for line in lines)
    await daily(profile)
    assert len(env.runs) == 2
