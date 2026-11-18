"""service.daily_apply: caps, auto approval, automatic sending, paused / failed applications, unconfirmed master."""

from __future__ import annotations

import pytest

from jobhunt import db
from jobhunt.apply import policy, service
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
        self.untrusted_host = ""
        self.reserve_click = False
        self.raises: dict[int, Exception] = {}
        self.order: list[tuple[str, int]] = []
        self.prepare_errors: dict[int, Exception] = {}
        self.flags: dict[int, list] = {}
        monkeypatch.setattr(cvs, "prepare_application", self.prepare)
        monkeypatch.setattr(service, "run", self.run)

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
        res = astore.finish_run(r.id, RunResult(status=self.run_status, message="msg", untrusted_host=self.untrusted_host))
        if res.status == "submitted":
            store.mark_submitted(app_id, app.current_rev)
        return res


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


async def test_auto_switch_off_approves_good_scores_only_and_sends_nothing(env, profile, settings, monkeypatch):
    monkeypatch.setattr(settings, "apply_auto", False)
    hi, lo = env.job(1, 75), env.job(2, 50)
    s, _ = await daily(profile)
    assert not env.runs and (s.sent, s.waiting, s.prepared) == (0, 2, 2)
    apps = {a.job_id: a for a in store.list_applications(profile.id)}
    assert apps[hi.id].status == "approved" and apps[hi.id].approved_by == "auto"
    assert apps[lo.id].status == "draft"  # < 60: never auto-approved, never sent


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


async def test_shared_pool_is_not_closed_when_given_and_created_lazily_otherwise(env, profile, monkeypatch):
    pool = FakePool()
    env.job(1)
    await daily(profile, pool=pool)
    assert not pool.closed and env.runs[0][1]["pool"] is pool
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
    assert len(created) == 1 and created[0].closed


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
async def test_existing_cvs_are_sent_first_then_the_others_are_prepared_and_sent_one_by_one(env, profile):
    best, ready, other = env.job(1, 95), env.job(2, 70), env.job(3, 80)
    cvs.approve(env.draft(ready).id, 1)  # already has an approved CV
    s, _ = await daily(profile)
    assert [(kind, job_id) for kind, job_id in env.order] == [("run", ready.id), ("prepare", best.id), ("run", best.id),
                                                                ("prepare", other.id), ("run", other.id)]
    assert s.sent == 3 and s.prepared == 2


async def test_a_prepared_draft_with_an_existing_cv_also_goes_before_new_preparations(env, profile):
    new, old = env.job(1, 99), env.job(2, 65)
    env.draft(old)  # an existing draft (auto-approvable)
    await daily(profile)
    assert env.order[0] == ("run", old.id) and env.order[1] == ("prepare", new.id)


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


async def test_headless_and_attended_are_passed_to_every_run(env, profile):
    env.job(1)
    await daily(profile)
    assert env.runs[0][1]["headless"] is True and env.runs[0][1]["attended"] is False
    env.runs.clear()
    env.job(2)
    await daily(profile, headless=False, attended=True)
    assert env.runs[0][1]["headless"] is False and env.runs[0][1]["attended"] is True


async def test_a_visible_pool_is_created_when_headful(env, profile, monkeypatch):
    seen = []

    class Spy(FakePool):
        def __init__(self, *a, headless=None, **k):
            super().__init__()
            seen.append(headless)
    monkeypatch.setattr(service, "BrowserPool", Spy)
    env.job(1)
    await daily(profile, pool=None, headless=False)
    assert seen == [False]


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
    assert by["Société 1"].endswith(": envoyée") and "en pause (" in by["Société 2"] and "échec (msg)" in by["Société 4"]
    assert "à vérifier (envoi non confirmé : msg)" in by["Société 5"]
    assert (s.sent, s.paused, s.failed, s.to_check) == (2, 1, 1, 1)
    assert lines[-1].startswith("Passe terminée : 2 envoyée(s), 1 en pause, 1 à vérifier, 0 à corriger")


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
