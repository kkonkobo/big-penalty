"""apply/service.py: run (dry run / submit), bookkeeping, queue buckets, send_selected, answers. Fictional data only."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from jobhunt import db
from jobhunt.apply import accounts, facts, policy, service
from jobhunt.apply import store as astore
from jobhunt.apply.errors import ApplyError
from jobhunt.apply.types import FilledField, Question, RunResult, StepLog, StepPlan
from jobhunt.config import get_settings
from jobhunt.cv import service as cvs
from jobhunt.cv import store
from jobhunt.sources.base import ChallengeRequired

from apply_cv_helpers import ERROR, make_app, make_job
from apply_fakes import CONFIRMED, ScriptedPlanner, act, cv_pdf, fast, page, server  # noqa: F401
from apply_service_fakes import FakePool, stub_engine
from cv_fakes import save_confirmed


@pytest.fixture(autouse=True)
def no_keychain(monkeypatch):
    monkeypatch.setattr(accounts, "create_password", lambda host, email: "Pw-Test-1234")
    monkeypatch.setattr(accounts, "get_password", lambda host, email, **kw: None)


@pytest.fixture
def master(profile):
    return save_confirmed(profile.id)


def new_app(profile, master, tmp_path, n=1, *, approve=False, score=88.0, url=None, age_days=1, flags=None, with_pdf=True):
    """Job + draft application (optionally approved by the user) with a fake one-page PDF; no browser, no LLM."""
    job = make_job(profile, title=f"Stage numéro {n}", company=f"Société {n}", score=score, age_days=age_days,
                   url=url or f"https://careers.exemple.test/jobs/{n}")
    (tmp_path / f"a{n}").mkdir()
    app = make_app(profile, job, tmp_path / f"a{n}", master_hash=master.content_hash, flags=flags, with_pdf=with_pdf)
    return job, (cvs.approve(app.id, app.current_rev) if approve else app)


# ------------------------------------------------------------------ the loopback two-step form
def step1(snap):
    return StepPlan(page_kind="form", actions=[act(snap, r"^Prénom", "fill", "first_name"), act(snap, r"^Nom", "fill", "last_name"),
                                               act(snap, r"E-mail", "fill", "email"), act(snap, r"Suivant", "click", role="next")])


def step2(snap):
    return StepPlan(page_kind="form", actions=[act(snap, r"^CV", "upload_cv", kind="file"),
                                               act(snap, r"confidentialité", "check", "consent_required"),
                                               act(snap, r"Envoyer", "click", role="submit")])


# ------------------------------------------------------------------ refusals
async def test_blocked_run_is_recorded_and_opens_no_browser(profile, master, tmp_path, monkeypatch):
    job, app = new_app(profile, master, tmp_path)  # draft: "CV non approuvé"
    calls = stub_engine(monkeypatch)
    pool = FakePool()
    r = await service.run(app.id, mode="submit", trigger="user", pool=pool)
    assert r.status == "blocked" and "CV non approuvé" in r.message and r.finished_at is not None
    assert not pool.opened and not calls and not pool.closed
    assert astore.last_run(app.id).id == r.id and store.get_application(app.id).status == "draft"
    assert db.get_job(job.id).status != "applied"


async def test_dry_run_refusals(profile, master, tmp_path, monkeypatch):
    stub_engine(monkeypatch)
    _, no_pdf = new_app(profile, master, tmp_path, 1, with_pdf=False)
    r = await service.run(no_pdf.id, mode="dry_run", trigger="user", pool=FakePool())
    assert r.status == "blocked" and "PDF absent" in r.message
    _, agg = new_app(profile, master, tmp_path, 2, url="https://www.indeed.com/viewjob?jk=1")
    r = await service.run(agg.id, mode="dry_run", trigger="user", pool=FakePool())
    assert r.status == "blocked" and "site de l'entreprise" in r.message
    _, sent = new_app(profile, master, tmp_path, 3, approve=True)
    store.mark_submitted(sent.id, 1)
    r = await service.run(sent.id, mode="dry_run", trigger="user", pool=FakePool())
    assert r.status == "blocked" and "déjà envoyée" in r.message


async def test_run_already_in_progress_propagates(profile, master, tmp_path, monkeypatch):
    _, app = new_app(profile, master, tmp_path, approve=True)
    astore.start_run(app.id, 1, "submit", "user", lambda rid: tmp_path / str(rid))
    with pytest.raises(ApplyError):
        await service.run(app.id, mode="submit", trigger="user", pool=FakePool())


# ------------------------------------------------------------------ dry run / submit with the real engine
async def test_dry_run_on_a_draft_fills_and_never_changes_status(profile, master, tmp_path, server, page, fast):
    job, app = new_app(profile, master, tmp_path, url=server.url("step1"))
    pool = FakePool(page)
    r = await service.run(app.id, mode="dry_run", trigger="user", pool=pool, planner=ScriptedPlanner([step1, step2]))
    assert r.status == "ready" and not server.posts_to("/apply") and not pool.closed
    assert pool.opened[0][0] == profile.id and pool.opened[0][1].startswith("applydry-")
    assert (r.out_dir and str(job.id) in r.out_dir and f"runs/{r.id}" in r.out_dir.replace("\\", "/"))
    a = store.get_application(app.id)
    assert a.status == "draft" and a.submitted_at is None and db.get_job(job.id).status != "applied"
    # the engine's dry run stops after the first page of a multi-step form (it never clicks "next" without a real send)
    assert [s.page_kind for s in r.result.steps] == ["form"] and r.result.steps[0].screenshot


async def test_submit_marks_application_and_job(profile, master, tmp_path, server, page, fast):
    job, app = new_app(profile, master, tmp_path, url=server.url("step1"), approve=True)
    r = await service.run(app.id, mode="submit", trigger="user", pool=FakePool(page),
                          planner=ScriptedPlanner([step1, step2, CONFIRMED]))
    assert r.status == "submitted" and len(server.posts_to("/apply")) == 1
    a = store.get_application(app.id)
    assert a.status == "submitted" and a.submitted_at is not None
    assert db.get_job(job.id).status == "applied"


async def test_before_submit_rechecks_the_pdf_right_before_the_click(profile, master, tmp_path, server, page, fast):
    job, app = new_app(profile, master, tmp_path, url=server.url("step1"), approve=True)
    pdf = cvs.safe_pdf(app)

    def tamper(snap):
        pdf.write_bytes(b"%PDF-1.4 tampered")  # between the start of the run and the final click
        return step2(snap)

    r = await service.run(app.id, mode="submit", trigger="user", pool=FakePool(page), planner=ScriptedPlanner([step1, tamper]))
    assert r.status == "blocked" and "PDF" in r.message and not server.posts_to("/apply")
    assert store.get_application(app.id).status == "approved" and db.get_job(job.id).status != "applied"


async def test_unconfirmed_submit_marks_application_failed(profile, master, tmp_path, server, page, fast):
    job, app = new_app(profile, master, tmp_path, url=server.url("step1"), approve=True)
    r = await service.run(app.id, mode="submit", trigger="user", pool=FakePool(page),
                          planner=ScriptedPlanner([step1, step2, StepPlan(page_kind="form")]))
    assert r.status == "failed" and r.result.submit_clicked and len(server.posts_to("/apply")) == 1
    a = store.get_application(app.id)
    assert a.status == "failed" and "Envoi non confirmé" in (a.notes or "")
    assert db.get_job(job.id).status != "applied"


async def test_challenge_from_goto_ends_as_needs_human(profile, master, tmp_path, server, page, fast):
    _, app = new_app(profile, master, tmp_path, url=server.url("step1"), approve=True)
    pool = FakePool(page, goto_error=ChallengeRequired("bot challenge"))
    r = await service.run(app.id, mode="submit", trigger="user", pool=pool, planner=ScriptedPlanner([step1]))
    assert r.status == "needs_human" and store.get_application(app.id).status == "approved"


# ------------------------------------------------------------------ stubbed engine
async def test_other_failures_leave_the_application_approved_and_never_raise(profile, master, tmp_path, monkeypatch):
    _, app = new_app(profile, master, tmp_path, approve=True)
    stub_engine(monkeypatch, RunResult(status="failed", message="Aucune action possible sur cette page.",
                                       steps=[StepLog(index=1, url="x", page_kind="form", note="rien")]))
    r = await service.run(app.id, mode="submit", trigger="user", pool=FakePool())
    assert r.status == "failed" and store.get_application(app.id).status == "approved"
    stub_engine(monkeypatch, raises=RuntimeError("secret page content"))
    r = await service.run(app.id, mode="submit", trigger="user", pool=FakePool())
    assert r.status == "failed" and "RuntimeError" in r.message and "secret" not in r.message
    assert store.get_application(app.id).status == "approved"


async def test_engine_receives_hooks_and_password_comes_from_accounts(profile, master, tmp_path, monkeypatch):
    _, app = new_app(profile, master, tmp_path, approve=True)
    seen = []
    monkeypatch.setattr(accounts, "create_password", lambda host, email: seen.append((host, email)) or "Pw-Test-1234")
    calls = stub_engine(monkeypatch)
    pool = FakePool()
    await service.run(app.id, mode="submit", trigger="user", pool=pool, headless=True)
    kw = calls[0]
    assert kw["password"]("careers.exemple.test") == "Pw-Test-1234" and seen == [("careers.exemple.test", "camille@example.test")]
    assert pool.opened == [(profile.id, "apply-exemple.test", True)] and kw["mode"] == "submit"
    assert kw["max_steps"] == get_settings().apply_max_steps and kw["uploads"].cv_pdf == cvs.safe_pdf(app)
    assert await kw["before_submit"]() == []
    cvs.withdraw(app.id)
    assert await kw["before_submit"]()  # re-checked against the DB at call time
    calls = stub_engine(monkeypatch)
    cvs.restore(app.id)
    await service.run(app.id, mode="dry_run", trigger="user", pool=pool)
    assert calls[0]["before_submit"] is None and calls[0]["mode"] == "dry_run"


# ------------------------------------------------------------------ queue
def set_started(run_id, delta):
    from jobhunt.db import _iso, connect, now
    with connect() as c:
        c.execute("UPDATE application_runs SET started_at=? WHERE id=?", (_iso(now() - delta), run_id))


def finish(app, status, rev=None, questions=None, message=""):
    r = astore.start_run(app.id, rev if rev is not None else app.current_rev, "submit", "user", lambda rid: get_settings().data_dir / "r" / str(rid))
    return astore.finish_run(r.id, RunResult(status=status, message=message, questions=questions or []))


def test_queue_buckets_and_order(profile, master, tmp_path):
    _, ok_low = new_app(profile, master, tmp_path, 1, score=61)
    _, ok_high = new_app(profile, master, tmp_path, 2, score=95)
    _, errs = new_app(profile, master, tmp_path, 3, flags=[ERROR])
    _, nopdf = new_app(profile, master, tmp_path, 4, with_pdf=False)
    _, old = new_app(profile, master, tmp_path, 5, age_days=40)
    _, paused = new_app(profile, master, tmp_path, 6)
    finish(paused, "needs_input", questions=[Question(label="Permis ?")])
    _, paused_old_rev = new_app(profile, master, tmp_path, 7)
    finish(paused_old_rev, "needs_email", rev=0)
    _, sending = new_app(profile, master, tmp_path, 8, approve=True)
    r = astore.start_run(sending.id, 1, "submit", "user", lambda rid: tmp_path / str(rid))
    _, stale = new_app(profile, master, tmp_path, 9)
    set_started(astore.start_run(stale.id, 1, "submit", "user", lambda rid: tmp_path / str(rid)).id, timedelta(hours=2))
    _, failed_run = new_app(profile, master, tmp_path, 10, approve=True)
    finish(failed_run, "failed", message="boum")
    _, failed_app = new_app(profile, master, tmp_path, 11, approve=True)
    store.mark_failed(failed_app.id, "Envoi non confirmé")
    _, sent = new_app(profile, master, tmp_path, 12, approve=True)
    store.mark_submitted(sent.id, 1)
    _, gone = new_app(profile, master, tmp_path, 13)
    cvs.withdraw(gone.id)

    items = {i.app.id: i for i in service.queue(profile.id)}
    assert gone.id not in items
    bucket = lambda a: items[a.id].bucket  # noqa: E731
    assert [bucket(a) for a in (ok_low, ok_high, errs, nopdf, old, paused, paused_old_rev, sending, stale, failed_run,
                                failed_app, sent)] == [
        "to_validate", "to_validate", "blocked", "blocked", "blocked", "paused", "blocked", "sending", "to_validate",
        "failed", "failed", "sent"]
    assert "intervention manuelle requise sur le site" in items[paused_old_rev.id].problems
    assert items[errs.id].problems and any("erreur" in p for p in items[errs.id].problems)
    assert any("PDF" in p for p in items[nopdf.id].problems) and any("jours" in p for p in items[old.id].problems)
    p = items[paused.id]
    assert p.pause_reason == "input" and [q.label for q in p.questions] == ["Permis ?"] and p.selectable
    assert items[ok_high.id].auto and items[ok_low.id].auto and items[ok_high.id].origin_host == "careers.exemple.test"
    assert items[ok_high.id].selectable and not items[errs.id].selectable and not items[sending.id].selectable
    assert items[failed_run.id].selectable and not items[failed_app.id].selectable and not items[sent.id].selectable
    order = [i.bucket for i in service.queue(profile.id)]
    assert order == sorted(order, key=service.BUCKET_ORDER.index)
    todo = [i.app.id for i in service.queue(profile.id) if i.bucket == "to_validate"]
    assert todo.index(ok_high.id) < todo.index(ok_low.id)  # best score first
    assert r.status == "running" and {i.app.id for i in service.queue()} == set(items)  # all profiles


def test_queue_auto_flag_follows_the_threshold(profile, master, tmp_path):
    _, a = new_app(profile, master, tmp_path, 1, score=59)
    assert not service.queue(profile.id)[0].auto
    assert a.id


# ------------------------------------------------------------------ send_selected / answers
async def test_send_selected_approves_drafts_and_refuses_blocking_errors(profile, master, tmp_path, monkeypatch):
    _, draft = new_app(profile, master, tmp_path, 1)
    _, approved = new_app(profile, master, tmp_path, 2, approve=True)
    _, bad = new_app(profile, master, tmp_path, 3, flags=[ERROR])
    calls = stub_engine(monkeypatch, RunResult(status="submitted", message="Candidature envoyée."))
    pool, progress = FakePool(), []
    runs = await service.send_selected([draft.id, approved.id, bad.id], pool=pool, on_progress=lambda i, r: progress.append((i, r.status)))
    assert [r.status for r in runs] == ["submitted", "submitted", "blocked"] and "erreur" in runs[2].message.lower()
    assert [p[0] for p in progress] == [draft.id, approved.id, bad.id] and len(calls) == 2 and len(pool.opened) == 2
    d = store.get_application(draft.id)
    assert d.status == "submitted" and d.approved_by == "user" and not d.approval_override
    assert store.get_application(bad.id).status == "draft" and not pool.closed
    assert all(r.trigger == "user" for r in runs)


def test_answer_questions_stores_non_blank_answers(profile, master, tmp_path):
    _, app = new_app(profile, master, tmp_path)
    service.answer_questions(app.id, [("Permis B ?", "Oui"), ("Autre ?", "  "), ("", "x")])
    custom = facts.load_answers(profile.id)["custom"]
    assert [(c["question"], c["answer"]) for c in custom] == [("Permis B ?", "Oui")]


# ------------------------------------------------------------------ CLI
def test_cli_list_and_answer(profile, master, tmp_path):
    from typer.testing import CliRunner

    from jobhunt.cli import app as cli

    _, paused = new_app(profile, master, tmp_path)
    finish(paused, "needs_input", questions=[Question(label="Permis B ?")], message="Informations manquantes")
    r = CliRunner().invoke(cli, ["apply", "list", "-p", str(profile.id)])
    assert r.exit_code == 0 and "en pause" in r.output
    r = CliRunner().invoke(cli, ["apply", "answer", str(paused.id)], input="Oui\n")
    assert r.exit_code == 0 and facts.load_answers(profile.id)["custom"][0]["answer"] == "Oui"
    r = CliRunner().invoke(cli, ["apply", "send", str(paused.id)], input="n\n")  # confirmation refused: nothing sent
    assert r.exit_code == 1 and "Confirmer" in r.output and store.get_application(paused.id).status == "draft"


# ------------------------------------------------------------------ crash-safe "possibly sent" bookkeeping
def click_hook(**flags):
    """Engine stub hook: runs the guards like the real engine, then announces the final click."""
    async def hook(kw):
        assert await kw["before_submit"]() == []
        await kw["on_submit_click"]()
    return hook


async def test_the_click_reserves_the_send_and_a_submission_records_where_it_went(profile, master, tmp_path, monkeypatch):
    job, app = new_app(profile, master, tmp_path, approve=True, url="https://careers.exemple.test/jobs/7?utm_source=x")
    stub_engine(monkeypatch, RunResult(status="submitted", message="Candidature envoyée.", submit_clicked=True), hook=click_hook())
    r = await service.run(app.id, mode="submit", trigger="user", pool=FakePool())
    assert r.status == "submitted" and r.submit_clicked_at is not None
    a = store.get_application(app.id)
    assert a.status == "submitted" and a.sent_origin_url == "careers.exemple.test/jobs/7"
    res = astore.active_reservations(profile.id)
    assert [(x["application_id"], x["trigger"]) for x in res] == [(app.id, "user")] and res[0]["url_key"] == a.sent_origin_url
    assert res[0]["role_key"] == policy.normalize_role(job.company, job.title)  # never released after a click
    assert db.get_job(job.id).status == "applied"


@pytest.mark.parametrize("status", ["needs_human", "needs_email", "blocked", "failed"])
async def test_any_outcome_after_the_click_but_submitted_marks_the_application_unconfirmed(profile, master, tmp_path, monkeypatch, status):
    job, app = new_app(profile, master, tmp_path, approve=True)
    stub_engine(monkeypatch, RunResult(status=status, message="x", submit_clicked=True), hook=click_hook())
    r = await service.run(app.id, mode="submit", trigger="user", pool=FakePool())
    assert r.status == status
    a = store.get_application(app.id)
    assert a.status == "failed" and a.notes == service.UNCONFIRMED_NOTE and a.approved_rev is None
    assert db.get_job(job.id).status != "applied"
    runs_before = len(astore.list_runs(app.id))
    again = await service.run(app.id, mode="submit", trigger="user", pool=FakePool())  # never resent by itself
    assert again.status == "blocked" and "pas confirmé" in again.message
    assert len(astore.list_runs(app.id)) == runs_before  # no new row: the outstanding run stays the one to settle
    assert again.id == astore.outstanding_attempt(app.id).id


async def test_the_submit_clicked_token_in_a_note_is_ignored(profile, master, tmp_path, monkeypatch):
    _, app = new_app(profile, master, tmp_path, approve=True)  # the planner could forge a note: only RunResult.submit_clicked counts
    stub_engine(monkeypatch, RunResult(status="failed", steps=[StepLog(index=1, url="x", page_kind="form", note="submit_clicked")]))
    await service.run(app.id, mode="submit", trigger="user", pool=FakePool())
    assert store.get_application(app.id).status == "approved" and not astore.active_reservations(profile.id)


async def test_a_failure_before_the_click_leaves_the_application_approved(profile, master, tmp_path, monkeypatch):
    _, app = new_app(profile, master, tmp_path, approve=True)
    stub_engine(monkeypatch, RunResult(status="needs_human", message="CAPTCHA"))
    await service.run(app.id, mode="submit", trigger="user", pool=FakePool())
    assert store.get_application(app.id).status == "approved" and not astore.active_reservations(profile.id)


async def test_crash_after_the_click_fails_the_application_even_without_a_result_flag(profile, master, tmp_path, monkeypatch):
    _, app = new_app(profile, master, tmp_path, approve=True)

    async def clicks_then_crashes(kw):
        await kw["on_submit_click"]()

    stub_engine(monkeypatch, raises=RuntimeError("browser died"), hook=clicks_then_crashes)
    r = await service.run(app.id, mode="submit", trigger="user", pool=FakePool())
    assert r.status == "failed" and r.submit_clicked_at is not None
    assert store.get_application(app.id).status == "failed"


async def test_a_dead_process_after_the_click_blocks_the_next_run(profile, master, tmp_path, monkeypatch):
    _, app = new_app(profile, master, tmp_path, approve=True)
    dead = astore.start_run(app.id, 1, "submit", "user", lambda rid: tmp_path / str(rid))  # the process died: still "running"
    astore.reserve_send(dead.id, profile_id=profile.id, application_id=app.id, trigger="user", role_key="r", url_key="u", daily_cap=9)
    monkeypatch.setattr(astore, "pid_alive", lambda pid: False)  # its owner died
    calls = stub_engine(monkeypatch)
    pool = FakePool()
    with pytest.raises(ApplyError, match="Un envoi précédent a peut-être abouti"):
        await service.run(app.id, mode="submit", trigger="user", pool=pool)
    assert store.get_application(app.id).status == "failed" and not calls and not pool.opened
    assert astore.get_run(dead.id).message == "interrompu"


async def test_bookkeeping_failure_is_reported_and_the_application_is_never_resent(profile, master, tmp_path, monkeypatch):
    _, app = new_app(profile, master, tmp_path, approve=True)
    stub_engine(monkeypatch, RunResult(status="submitted", message="ok", submit_clicked=True), hook=click_hook())

    def boom(*a, **kw):
        raise RuntimeError("disk full")

    monkeypatch.setattr(store, "tx_mark_submitted", boom)
    r = await service.run(app.id, mode="submit", trigger="user", pool=FakePool())
    assert "Envoyée sur le site" in r.message and "ne pas la renvoyer" in r.message
    a = store.get_application(app.id)
    assert a.status == "failed" and "ne pas la renvoyer" in a.notes
    assert astore.has_unconfirmed_click(app.id)


def barrier_hook(n: int, outcomes: list, order: list[str] | None = None):
    """Engine stub hook: waits until `n` runs are inside the engine (all passed their gates), then announces the final click
    (in the order of the job URLs `order`, when given)."""
    state = {"arrived": 0, "turn": 0}
    both = asyncio.Event()

    async def hook(kw):
        state["arrived"] += 1
        if state["arrived"] == n:
            both.set()
        await both.wait()
        while order and order[state["turn"]] != kw["url"]:
            await asyncio.sleep(0)
        try:
            await kw["on_submit_click"]()
        except ApplyError as e:
            outcomes.append(str(e))
            raise
        finally:
            state["turn"] += 1
        outcomes.append("ok")
    return hook


async def test_two_concurrent_sends_with_nine_reserved_only_the_first_gets_the_last_slot(profile, master, tmp_path, monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "apply_auto", True)
    monkeypatch.setattr(s, "apply_daily_cap", 10)
    for n in range(9):
        with db.connect() as c:
            c.execute("INSERT INTO apply_reservations(profile_id, application_id, run_id, trigger, role_key, url_key, created_at)"
                      " VALUES (?,?,?,?,?,?,?)", (profile.id, 10_000 + n, 10_000 + n, "auto", f"x{n}", f"y{n}", db._iso(datetime.now(UTC))))
    _, a1 = new_app(profile, master, tmp_path, 1, approve=True)
    _, a2 = new_app(profile, master, tmp_path, 2, approve=True)
    outcomes: list[str] = []
    stub_engine(monkeypatch, hook=barrier_hook(2, outcomes))
    await asyncio.gather(*(service.run(a.id, mode="submit", trigger="auto", pool=FakePool(), _lock_held=True) for a in (a1, a2)))
    assert sorted(outcomes) == ["Plafond de 10 envois automatiques par jour atteint", "ok"]
    assert astore.count_auto_submitted(profile.id, datetime(2000, 1, 1, tzinfo=UTC)) == 10


async def test_a_reservation_blocks_a_second_application_for_the_same_role_or_url(profile, master, tmp_path, monkeypatch):
    j1 = make_job(profile, title="Stage W", company="Zeta", url="https://careers.zeta.test/1")
    j2 = make_job(profile, title="Stage W (H/F)", company="Zeta", url="https://careers.zeta.test/2")
    j3 = make_job(profile, title="Autre", company="Autre", url="https://careers.zeta.test/1?utm_source=a")
    for k, j in enumerate((j1, j2, j3)):
        (tmp_path / f"d{k}").mkdir()
    apps = [cvs.approve((a := make_app(profile, j, tmp_path / f"d{k}", master_hash=master.content_hash)).id, 1)
            for k, j in enumerate((j1, j2, j3))]
    outcomes: list[str] = []
    stub_engine(monkeypatch, hook=barrier_hook(3, outcomes, [j.origin_url for j in (j1, j2, j3)]))
    await asyncio.gather(*(service.run(a.id, mode="submit", trigger="user", pool=FakePool(), _lock_held=True) for a in apps))
    assert outcomes[0] == "ok" and all("même offre" in o for o in outcomes[1:]) and len(outcomes) == 3  # same role / same canonical URL


async def test_a_navigation_click_reserves_pending_and_a_run_without_final_click_releases_it(profile, master, tmp_path, monkeypatch):
    _, app = new_app(profile, master, tmp_path, approve=True)
    seen = []

    async def nav(kw):
        assert await kw["before_submit"]() == []
        await kw["on_nav_click"]()
        await kw["on_nav_click"]()  # idempotent per run
        seen.append([r["pending"] for r in astore.active_reservations(profile.id)])
        assert await kw["before_submit"]() == []  # the run's own pending reservation never blocks its next click

    stub_engine(monkeypatch, RunResult(status="needs_input", message="question"), hook=nav)
    r = await service.run(app.id, mode="submit", trigger="user", pool=FakePool())
    assert seen == [[1]] and r.nav_clicked_at is not None and r.submit_clicked_at is None
    assert astore.active_reservations(profile.id) == [] and store.get_application(app.id).status == "approved"


async def test_a_navigation_click_then_a_crash_keeps_the_reservation_and_fails_the_application(profile, master, tmp_path, monkeypatch):
    _, app = new_app(profile, master, tmp_path, approve=True)

    async def nav_then_crash(kw):
        await kw["on_nav_click"]()

    stub_engine(monkeypatch, raises=RuntimeError("browser died"), hook=nav_then_crash)
    r = await service.run(app.id, mode="submit", trigger="user", pool=FakePool())
    assert r.status == "failed" and store.get_application(app.id).status == "failed"
    assert [x["pending"] for x in astore.active_reservations(profile.id)] == [0]


# ------------------------------------------------------------------ one send at a time
async def test_a_held_send_lock_records_a_blocked_run(profile, master, tmp_path, monkeypatch):
    from jobhunt.filelock import try_lock, unlock
    import os

    _, app = new_app(profile, master, tmp_path, approve=True)
    calls = stub_engine(monkeypatch)
    lock = get_settings().data_dir / service.SEND_LOCK_NAME
    lock.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock, os.O_RDWR | os.O_CREAT)
    assert try_lock(fd)  # "another process" holds it
    try:
        pool = FakePool()
        r = await service.run(app.id, mode="submit", trigger="user", pool=pool)
        assert r.status == "blocked" and "un autre envoi est en cours" in r.message and not calls and not pool.opened
        assert store.get_application(app.id).status == "approved"
        d = await service.run(app.id, mode="dry_run", trigger="user", pool=pool)  # a dry run never takes the lock
        assert d.status == "ready" and len(calls) == 1
    finally:
        unlock(fd)
        os.close(fd)
    assert (await service.run(app.id, mode="submit", trigger="user", pool=FakePool())).status == "ready"  # lock free again


async def test_send_lock_is_not_inherited_by_child_tasks_nor_reentrant():
    with service.send_lock():
        async def child():
            with service.send_lock():
                return "entered"

        with pytest.raises(service.SendLocked):  # a child task started while the parent holds the lock must NOT enter
            await asyncio.create_task(child())
        with pytest.raises(service.SendLocked):
            with service.send_lock():
                pass
    with service.send_lock():  # released
        pass


async def test_send_selected_takes_the_lock_once_and_runs_inside_it(profile, master, tmp_path, monkeypatch):
    _, app = new_app(profile, master, tmp_path, approve=True)
    held = []

    async def probe(kw):
        try:
            with service.send_lock():
                held.append(False)
        except service.SendLocked:
            held.append(True)

    stub_engine(monkeypatch, RunResult(status="submitted", message="ok"), hook=probe)
    runs = await service.send_selected([app.id], pool=FakePool())
    assert held == [True] and [r.status for r in runs] == ["submitted"]


async def test_send_selected_with_the_lock_held_elsewhere_records_blocked_runs(profile, master, tmp_path, monkeypatch):
    _, app = new_app(profile, master, tmp_path, approve=True)
    calls = stub_engine(monkeypatch)
    with service.send_lock():
        runs = await service.send_selected([app.id], pool=FakePool())
    assert [r.status for r in runs] == ["blocked"] and "un autre envoi" in runs[0].message and not calls


# ------------------------------------------------------------------ origin link / verifier / revisions
async def test_origin_link_changed_during_the_run_blocks_the_click(profile, master, tmp_path, monkeypatch):
    job, app = new_app(profile, master, tmp_path, approve=True)
    seen = []

    async def hook(kw):
        assert await kw["before_submit"]() == []
        with db.connect() as c:
            c.execute("UPDATE jobs SET origin_url=? WHERE id=?", ("https://careers.exemple.test/jobs/other", job.id))
        seen.append(await kw["before_submit"]())

    stub_engine(monkeypatch, hook=hook)
    await service.run(app.id, mode="submit", trigger="user", pool=FakePool())
    assert "le lien de l'offre a changé pendant l'envoi" in seen[0]


async def test_the_injected_verifier_and_hooks_reach_the_engine(profile, master, tmp_path, monkeypatch):
    _, app = new_app(profile, master, tmp_path, approve=True)
    calls = stub_engine(monkeypatch)
    verifier = object()
    await service.run(app.id, mode="submit", trigger="user", pool=FakePool(), verifier=verifier)
    assert calls[0]["verifier"] is verifier and callable(calls[0]["on_submit_click"]) and callable(calls[0]["before_submit"])
    assert callable(calls[0]["on_nav_click"]) and callable(calls[0]["host_allowed"])
    calls = stub_engine(monkeypatch)
    await service.run(app.id, mode="dry_run", trigger="user", pool=FakePool())
    assert calls[0]["on_submit_click"] is None and calls[0]["on_nav_click"] is None and calls[0]["before_submit"] is None
    assert callable(calls[0]["host_allowed"])  # dry runs are bound to the destination too


async def test_send_selected_refuses_a_revision_the_user_was_not_shown(profile, master, tmp_path, monkeypatch):
    _, shown = new_app(profile, master, tmp_path, 1)
    _, changed = new_app(profile, master, tmp_path, 2)
    _, missing = new_app(profile, master, tmp_path, 3)
    calls = stub_engine(monkeypatch, RunResult(status="submitted", message="ok"))
    runs = await service.send_selected([shown.id, changed.id, missing.id], pool=FakePool(),
                                       expected_revs={shown.id: shown.current_rev, changed.id: changed.current_rev + 1})
    assert [r.status for r in runs] == ["submitted", "blocked", "blocked"] and len(calls) == 1
    assert "le CV a changé depuis l'affichage" in runs[1].message
    assert store.get_application(changed.id).status == "draft" and store.get_application(missing.id).status == "draft"


# ------------------------------------------------------------------ human reconciliation
def failed_after_click(profile, master, tmp_path, n=1):
    job, app = new_app(profile, master, tmp_path, n, approve=True)
    run = astore.start_run(app.id, 1, "submit", "auto", lambda rid: tmp_path / f"r{rid}")
    astore.reserve_send(run.id, profile_id=profile.id, application_id=app.id, trigger="auto",
                        role_key=policy.normalize_role(job.company, job.title), url_key=f"careers.exemple.test/jobs/{n}", daily_cap=9)
    astore.finish_and_settle(run.id, RunResult(status="needs_email", submit_clicked=True))
    return job, store.get_application(app.id)


def test_resolve_unconfirmed_as_sent(profile, master, tmp_path):
    job, app = failed_after_click(profile, master, tmp_path)
    out = service.resolve_unconfirmed(app.id, sent=True, run_id=astore.last_run(app.id).id)
    assert out.status == "submitted" and out.submitted_at is not None and out.sent_origin_url == "careers.exemple.test/jobs/1"
    assert db.get_job(job.id).status == "applied" and len(astore.active_reservations(profile.id)) == 1  # still counts / blocks


def test_resolve_unconfirmed_as_not_sent_releases_everything(profile, master, tmp_path):
    job, app = failed_after_click(profile, master, tmp_path)
    assert astore.count_auto_submitted(profile.id, datetime(2000, 1, 1, tzinfo=UTC)) == 1
    out = service.resolve_unconfirmed(app.id, sent=False, run_id=astore.last_run(app.id).id)
    assert out.status == "draft" and astore.active_reservations(profile.id) == []
    assert astore.count_auto_submitted(profile.id, datetime(2000, 1, 1, tzinfo=UTC)) == 0
    assert not astore.has_unconfirmed_click(app.id) and db.get_job(job.id).status != "applied"
    again = cvs.approve(app.id, 1)  # approved again, then sendable again
    assert policy.submit_problems(again, db.get_job(job.id), trigger="user") == []


def test_resolve_unconfirmed_needs_a_failed_application(profile, master, tmp_path):
    _, ok = new_app(profile, master, tmp_path, approve=True)
    with pytest.raises(ApplyError, match="pas en échec"):
        service.resolve_unconfirmed(ok.id, sent=True)
    with pytest.raises(ApplyError, match="introuvable"):
        service.resolve_unconfirmed(9999, sent=False)


def test_cli_resolve(profile, master, tmp_path):
    from typer.testing import CliRunner

    from jobhunt.cli import app as cli

    _, a = failed_after_click(profile, master, tmp_path, 1)
    _, b = failed_after_click(profile, master, tmp_path, 2)
    assert CliRunner().invoke(cli, ["apply", "resolve", str(a.id), "--run-id", str(astore.last_run(a.id).id), "--sent"]).exit_code == 0
    assert store.get_application(a.id).status == "submitted"
    args = ["apply", "resolve", str(b.id), "--run-id", str(astore.last_run(b.id).id)]
    r = CliRunner().invoke(cli, [*args, "--not-sent"])
    assert r.exit_code == 0 and store.get_application(b.id).status == "draft"
    r = CliRunner().invoke(cli, [*args, "--sent"])
    assert r.exit_code == 1 and "pas en échec" in r.output


# ------------------------------------------------------------------ destination binding (host_allowed)
ORIGIN = "https://careers.exemple.test/jobs/42"


@pytest.mark.parametrize("host, expected", [
    ("careers.exemple.test", True),                    # the job's own host
    ("www.exemple.test", True), ("apply.exemple.test", True), ("EXEMPLE.test.", True),  # same registrable domain
    ("exemple.test", True),
    ("jobs.lever.co", True), ("boards.greenhouse.io", True), ("acme.recruitee.com", True),  # known ATS hosts
    ("acme.myworkdayjobs.com", True), ("jobs.smartrecruiters.com", True),
    ("evil-exemple.test", False), ("exemple.test.evil.example", False), ("autre-site.test", False),  # look-alikes
    ("lever.co", False), ("evillever.co", False), ("greenhouse.io.evil.test", False),
    ("jobs.trusted-ats.test", True), ("sub.trusted2.test", True),  # trusted host / registrable domain of a trusted host
    ("trusted-ats.test", False),                       # trusting a subdomain does not trust its parent
    ("127.0.0.1", False), ("10.0.0.5", False), ("localhost", False), ("[::1]", False), ("::1", False), ("", False), ("intranet", False),
])
def test_host_allowed_truth_table(host, expected):
    allowed = service.make_host_allowed(ORIGIN, ["jobs.trusted-ats.test", "TRUSTED2.test"])
    assert allowed(host) is expected


@pytest.mark.parametrize("origin, host, expected", [
    ("https://jobs.company.co.uk/a", "www.company.co.uk", True),         # same registrable domain under a ccSLD
    ("https://jobs.company.co.uk/a", "evil.co.uk", False), ("https://jobs.company.co.uk/a", "co.uk", False),
    ("https://a.github.io/x", "a.github.io", True), ("https://a.github.io/x", "b.github.io", False),
    ("https://a.github.io/x", "github.io", False), ("https://co.uk/x", "co.uk", False), ("https://co.uk/x", "evil.co.uk", False),
    ("https://jobs.exemple.test/x", "notdigitalrecruiters.com", False), ("https://jobs.exemple.test/x", "x.digitalrecruiters.com", True),
    ("https://jobs.exemple.test/x", "digitalrecruiters.com", True), ("https://jobs.exemple.test/x", "evilmyworkdayjobs.com", False),
    ("https://jobs.exemple.test/x", "acme.talent-soft.com", True), ("https://jobs.exemple.test/x", "nottalent-soft.com", False),
    ("https://jobs.exemple.test/x", "xn--exemple-9ya.test", False), ("https://jobs.exemple.test/x", "exemple.test.", True),
    ("https://jobs.exemple.test/x", "EXEMPLE.TEST", True), ("https://jobs.exemple.test/x", "user@jobs.exemple.test", True),
    ("https://jobs.exemple.test/x", "evil.test@jobs.exemple.test", True), ("https://jobs.exemple.test/x", "jobs.exemple.test:8443", True),
    ("https://jobs.exemple.test/x", "exemple.test..", False), ("https://jobs.exemple.test/x", "exe mple.test", False),
    ("https://jobs.exemple.test/x", "\u00e9xemple.test", False), ("https://jobs.exemple.test/x", "exemple.test\\@evil.test", False),
    ("https://jobs.exemple.test/x", "1.2.3.4", False), ("https://jobs.exemple.test/x", "[2001:db8::1]", False),
    ("https://jobs.exemple.test/x", "localhost", False), ("https://jobs.exemple.test/x", "10.0.0.1:80", False),
    ("https://caf\u00e9.test/x", "xn--caf-dma.test", True), ("https://caf\u00e9.test/x", "www.caf\u00e9.test", True),
    ("https://caf\u00e9.test/x", "cafe.test", False),
])
def test_host_allowed_canonicalisation_and_public_suffixes(origin, host, expected):
    assert service.make_host_allowed(origin, [])(host) is expected


@pytest.mark.parametrize("host, expected", [
    ("1.2.3.4", True), ("1.2.3.4:8080", True), ("[2001:db8::1]", True), ("2001:db8::1", True), ("[2001:DB8::1]:99", True),
    ("localhost", True), ("LOCALHOST.", True), ("10.0.0.2", False), ("127.0.0.1", False),
    ("jobs.trusted-ats.test", True), ("JOBS.trusted-ats.test.:443", True), ("x.trusted-ats.test", False),
])
def test_host_allowed_trusted_literals_exactly(host, expected):
    allowed = service.make_host_allowed(ORIGIN, ["1.2.3.4", "2001:db8::1", "localhost", "jobs.trusted-ats.test"])
    assert allowed(host) is expected


def test_host_allowed_ip_and_localhost_only_when_listed_exactly():
    assert service.make_host_allowed(ORIGIN, ["127.0.0.1", "localhost"])("127.0.0.1")
    assert service.make_host_allowed(ORIGIN, ["localhost"])("localhost") and not service.make_host_allowed(ORIGIN, [])("localhost")
    assert not service.make_host_allowed("http://127.0.0.1:8000/job", [])("127.0.0.1")  # an IP origin is not a "site"
    assert not service.make_host_allowed(None, [])("careers.exemple.test")


async def test_run_passes_host_allowed_built_from_the_origin_and_the_trusted_list(profile, master, tmp_path, monkeypatch):
    _, app = new_app(profile, master, tmp_path, approve=True)
    facts.add_trusted_host(profile.id, "recrutement.autre.test")
    calls = stub_engine(monkeypatch)
    for mode in ("submit", "dry_run"):
        await service.run(app.id, mode=mode, trigger="user", pool=FakePool())
    for kw in calls:
        ha = kw["host_allowed"]
        assert ha("careers.exemple.test") and ha("jobs.lever.co") and ha("recrutement.autre.test")
        assert not ha("pirate.example") and not ha("127.0.0.1")


async def test_a_blocked_run_with_an_untrusted_host_is_recorded_and_shown_as_a_host_pause(profile, master, tmp_path, monkeypatch):
    _, app = new_app(profile, master, tmp_path, approve=True)
    stub_engine(monkeypatch, RunResult(status="blocked", message="hôte non approuvé", untrusted_host="jobs.autre-site.test"))
    r = await service.run(app.id, mode="submit", trigger="user", pool=FakePool())
    assert r.result.untrusted_host == "jobs.autre-site.test" and store.get_application(app.id).status == "approved"
    (it,) = [i for i in service.queue(profile.id) if i.app.id == app.id]
    assert (it.bucket, it.pause_reason, it.untrusted_host) == ("paused", "host", "jobs.autre-site.test") and not it.selectable
    assert service.untrusted_host_of(r) == "jobs.autre-site.test" and service.untrusted_host_of(None) == ""


def test_cli_trust(profile):
    from typer.testing import CliRunner

    from jobhunt.cli import app as cli

    r = CliRunner().invoke(cli, ["apply", "trust", str(profile.id), "Jobs.Autre-Site.test"])
    assert r.exit_code == 0 and facts.trusted_hosts(profile.id) == ["jobs.autre-site.test"]
    for bad in ("http://x.test", "127.0.0.1", "a b"):
        r = CliRunner().invoke(cli, ["apply", "trust", str(profile.id), bad])
        assert r.exit_code == 1 and "invalide" in r.output
    assert CliRunner().invoke(cli, ["apply", "trust", "999", "x.test"]).exit_code != 0
    assert facts.trusted_hosts(profile.id) == ["jobs.autre-site.test"]


# ------------------------------------------------------------------ resolution bound to one attempt
def test_resolve_unconfirmed_bound_to_the_last_run(profile, master, tmp_path):
    _, app = failed_after_click(profile, master, tmp_path)
    last = astore.last_run(app.id)
    with pytest.raises(ApplyError, match="périmée"):
        service.resolve_unconfirmed(app.id, sent=True, run_id=last.id + 1)
    assert store.get_application(app.id).status == "failed"
    assert service.resolve_unconfirmed(app.id, sent=True, run_id=last.id).status == "submitted"


def test_resolve_unconfirmed_refuses_an_application_that_never_clicked(profile, master, tmp_path):
    _, app = new_app(profile, master, tmp_path, approve=True)
    store.force_failed(app.id, "x")  # failed by some other path: no click, no reservation
    run = astore.start_run(app.id, 1, "dry_run", "user", lambda rid: tmp_path / f"q{rid}")
    astore.finish_run(run.id, RunResult(status="ready"))
    for kw in ({}, {"run_id": run.id}):
        with pytest.raises(ApplyError, match="jamais atteint"):
            service.resolve_unconfirmed(app.id, sent=False, **kw)
    assert store.get_application(app.id).status == "failed"


def test_cli_resolve_requires_the_displayed_run_id_and_decision(profile, master, tmp_path):
    from typer.testing import CliRunner

    from jobhunt.cli import app as cli

    _, a = failed_after_click(profile, master, tmp_path, 1)
    run_id = astore.last_run(a.id).id
    args = ["apply", "resolve", str(a.id)]
    for options in (["--not-sent"], ["--run-id", str(run_id)], ["--run-id", str(run_id + 1), "--not-sent"]):
        assert CliRunner().invoke(cli, [*args, *options]).exit_code != 0
        assert store.get_application(a.id).status == "failed"
    r = CliRunner().invoke(cli, [*args, "--run-id", str(run_id), "--not-sent"])
    assert r.exit_code == 0 and store.get_application(a.id).status == "draft"


def test_cli_prints_deduced_answers_and_the_untrusted_host():
    from io import StringIO

    from rich.console import Console

    from jobhunt import cli

    run = astore.ApplyRun(id=1, application_id=1, rev=1, mode="dry_run", trigger="user", status="blocked", message="m",
                          started_at=datetime.now(UTC), result=RunResult(status="blocked", untrusted_host="jobs.autre-site.test", steps=[
                              StepLog(index=1, url="https://x.test", page_kind="form", filled=[
                                  FilledField(
                                      label="Anglais", op="fill", fact="candidate_profile", value="B2", inferred=True, basis="CV : B2"),
                                  FilledField(label="Prénom", op="fill", fact="first_name")])]))
    buf = StringIO()
    old = cli.console
    cli.console = Console(file=buf, width=200)
    try:
        cli._print_run(run)
    finally:
        cli.console = old
    out = buf.getvalue()
    assert "Anglais ← candidate_profile (déduite : CV : B2)" in out and "Prénom ← first_name\n" in out
    assert "jobs.autre-site.test" in out and "apply trust" in out


# ------------------------------------------------------------------ finding round 3
async def test_a_refused_later_attempt_never_orphans_the_recovery(profile, master, tmp_path, monkeypatch):
    _, app = new_app(profile, master, tmp_path, approve=True)
    stub_engine(monkeypatch, RunResult(status="needs_email", message="x", submit_clicked=True), hook=click_hook())
    first = await service.run(app.id, mode="submit", trigger="user", pool=FakePool())
    # even if a newer row exists (older code, another path), the outstanding attempt is the one with the click
    newer = astore.start_run(app.id, 1, "dry_run", "user", lambda rid: tmp_path / f"d{rid}")
    astore.finish_run(newer.id, RunResult(status="blocked", message="refus"))
    assert astore.last_run(app.id).id == newer.id and astore.outstanding_attempt(app.id).id == first.id
    with pytest.raises(ApplyError, match="périmée"):
        service.resolve_unconfirmed(app.id, sent=True, run_id=newer.id)
    assert service.resolve_unconfirmed(app.id, sent=True, run_id=first.id).status == "submitted"
    assert len(astore.active_reservations(profile.id)) == 1


async def test_not_sent_releases_only_the_outstanding_reservation(profile, master, tmp_path):
    job, app = failed_after_click(profile, master, tmp_path)
    _, other = failed_after_click(profile, master, tmp_path, 2)
    service.resolve_unconfirmed(app.id, sent=False, run_id=astore.outstanding_attempt(app.id).id)
    assert [r["application_id"] for r in astore.active_reservations(profile.id)] == [other.id]


def test_outstanding_attempt_without_a_reservation_falls_back_to_the_newest_click(profile, master, tmp_path):
    _, app = failed_after_click(profile, master, tmp_path)
    with db.connect() as c:
        c.execute("UPDATE apply_reservations SET released_at='2026-01-01T00:00:00+00:00'")
    run = astore.last_run(app.id)
    assert astore.outstanding_attempt(app.id).id == run.id  # failed + a recorded click
    _, ok = new_app(profile, master, tmp_path, 5, approve=True)
    assert astore.outstanding_attempt(ok.id) is None and astore.outstanding_attempt(9999) is None


@pytest.mark.parametrize("released", [False, True])
def test_the_queue_exposes_the_outstanding_attempt(profile, master, tmp_path, released):
    _, app = failed_after_click(profile, master, tmp_path)
    if released:
        astore.release_reservation(app.id)
    later = astore.start_run(app.id, 1, "dry_run", "user", lambda rid: tmp_path / f"d{rid}")
    astore.finish_run(later.id, RunResult(status="ready"))
    it = next(i for i in service.queue(profile.id) if i.app.id == app.id)
    assert it.bucket == "needs_verification" and it.last_run.id == later.id and it.outstanding.id != later.id
    assert not it.selectable and not it.auto
    assert it.outstanding.id == astore.outstanding_attempt(app.id).id and it.origin_sha == service.origin_digest(it.origin_url)


def test_finish_and_settle_needs_the_newest_running_run(profile, master, tmp_path):
    _, app = new_app(profile, master, tmp_path, approve=True)
    old = astore.start_run(app.id, 1, "submit", "user", lambda rid: tmp_path / f"o{rid}")
    with db.connect() as c:  # a newer row appears behind its back
        c.execute("INSERT INTO application_runs(application_id, rev, mode, trigger, status, started_at) VALUES (?,?,?,?,?,?)",
                  (app.id, 1, "dry_run", "user", "ready", "2026-01-01T00:00:00+00:00"))
    with pytest.raises(ApplyError):
        astore.finish_and_settle(old.id, RunResult(status="submitted", submit_clicked=True))
    assert astore.get_run(old.id).status == "running" and store.get_application(app.id).status == "approved"


async def test_manual_sent_confirmation_keeps_the_deduced_answers_of_the_sending_attempt(profile, master, tmp_path, monkeypatch):
    _, app = new_app(profile, master, tmp_path, approve=True)
    older = astore.start_run(app.id, 1, "dry_run", "user", lambda rid: tmp_path / f"a{rid}")
    step = lambda label: StepLog(index=1, url="https://x.test", page_kind="form", filled=[
        FilledField(label=label, op="fill", fact="candidate_profile", value="oui", inferred=True, basis="CV")])
    astore.finish_run(older.id, RunResult(status="ready", steps=[step("Du dry run")]))
    res = RunResult(status="needs_email", message="x", submit_clicked=True, steps=[step("De l'envoi")])
    stub_engine(monkeypatch, res, hook=click_hook())
    sending = await service.run(app.id, mode="submit", trigger="user", pool=FakePool())
    assert [f.label for f in service.inferred_answers(app.id)] == ["Du dry run"]  # before the confirmation: the ready dry run
    service.resolve_unconfirmed(app.id, sent=True, run_id=sending.id)
    assert [f.label for f in service.inferred_answers(app.id)] == ["De l'envoi"]
    assert astore.sent_run(app.id).id == sending.id


async def test_a_confirmed_submission_records_its_run_as_the_sending_attempt(profile, master, tmp_path, monkeypatch):
    _, app = new_app(profile, master, tmp_path, approve=True)
    stub_engine(monkeypatch, RunResult(status="submitted", message="ok", submit_clicked=True), hook=click_hook())
    r = await service.run(app.id, mode="submit", trigger="user", pool=FakePool())
    assert astore.sent_run(app.id).id == r.id


async def test_run_binds_the_revision_and_the_destination_the_user_saw(profile, master, tmp_path, monkeypatch):
    job, app = new_app(profile, master, tmp_path, approve=True)
    calls = stub_engine(monkeypatch, RunResult(status="ready", message="ok"))
    r = await service.run(app.id, mode="dry_run", trigger="user", pool=FakePool(), expected_rev=app.current_rev + 1)
    assert r.status == "blocked" and "le CV a changé depuis l'affichage" in r.message and not calls
    r = await service.run(app.id, mode="dry_run", trigger="user", pool=FakePool(), expected_origin=service.origin_digest("https://x.test/"))
    assert r.status == "blocked" and "le lien de l'offre a changé depuis l'affichage" in r.message and not calls
    r = await service.run(app.id, mode="dry_run", trigger="user", pool=FakePool(), expected_rev=app.current_rev,
                          expected_origin=service.origin_digest(job.origin_url))
    assert r.status == "ready" and len(calls) == 1


async def test_send_selected_refuses_a_destination_that_changed(profile, master, tmp_path, monkeypatch):
    j1, shown = new_app(profile, master, tmp_path, 1)
    j2, changed = new_app(profile, master, tmp_path, 2)
    _, missing = new_app(profile, master, tmp_path, 3)
    calls = stub_engine(monkeypatch, RunResult(status="submitted", message="ok"))
    revs = {a.id: a.current_rev for a in (shown, changed, missing)}
    runs = await service.send_selected([shown.id, changed.id, missing.id], pool=FakePool(), expected_revs=revs,
                                       expected_origins={shown.id: service.origin_digest(j1.origin_url),
                                                         changed.id: service.origin_digest("https://old.exemple.test/x")})
    assert [r.status for r in runs] == ["submitted", "blocked", "blocked"] and len(calls) == 1
    assert "le lien de l'offre a changé depuis l'affichage" in runs[1].message
    assert store.get_application(changed.id).status == "draft"  # nothing approved


async def test_send_selected_on_an_unresolved_attempt_creates_no_row(profile, master, tmp_path, monkeypatch):
    _, app = new_app(profile, master, tmp_path, approve=True)
    stub_engine(monkeypatch, RunResult(status="needs_email", submit_clicked=True), hook=click_hook())
    first = await service.run(app.id, mode="submit", trigger="user", pool=FakePool())
    n = len(astore.list_runs(app.id))
    runs = await service.send_selected([app.id], pool=FakePool(), expected_revs={app.id: 99})
    assert [r.status for r in runs] == ["blocked"] and len(astore.list_runs(app.id)) == n
    assert astore.outstanding_attempt(app.id).id == first.id


class SlowClosePool(FakePool):
    async def close(self):
        self.closed = True
        await asyncio.sleep(10)


async def test_cancellation_during_cleanup_still_settles_the_run(profile, master, tmp_path, monkeypatch):
    _, app = new_app(profile, master, tmp_path, approve=True)
    stub_engine(monkeypatch, RunResult(status="submitted", message="ok", submit_clicked=True), hook=click_hook())
    pool = SlowClosePool()
    monkeypatch.setattr(service, "BrowserPool", lambda headless=None: pool)
    task = asyncio.create_task(service.run(app.id, mode="submit", trigger="user"))
    for _ in range(200):
        if pool.closed:
            break
        await asyncio.sleep(0.01)
    assert pool.closed
    task.cancel()  # arrives while pool.close() is awaited
    with pytest.raises(asyncio.CancelledError):
        await task
    run = astore.last_run(app.id)
    assert run.status == "submitted" and store.get_application(app.id).status == "submitted"


async def test_cancellation_while_driving_settles_as_unconfirmed_after_a_click(profile, master, tmp_path, monkeypatch):
    _, app = new_app(profile, master, tmp_path, approve=True)

    async def hook(kw):
        await click_hook()(kw)
        await asyncio.sleep(10)

    stub_engine(monkeypatch, RunResult(status="submitted"), hook=hook)
    task = asyncio.create_task(service.run(app.id, mode="submit", trigger="user", pool=FakePool()))
    for _ in range(200):
        last = astore.last_run(app.id)
        if last and last.submit_clicked_at:
            break
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert astore.last_run(app.id).status == "failed" and store.get_application(app.id).status == "failed"
    assert astore.outstanding_attempt(app.id).id == astore.last_run(app.id).id


# ------------------------------------------------------------------ fix round 4
def inferred_step(label="Disponibilité"):
    return StepLog(index=1, url="https://x.test", page_kind="form", filled=[
        FilledField(label=label, op="fill", fact="candidate_profile", value="Septembre", inferred=True, basis="CV")])


def progress_hook(*, then_sleep=False, nav=False):
    """Engine stub hook: a deduction is filled, progress is reported, the click is announced (and the task may hang)."""
    async def hook(kw):
        assert await kw["before_submit"]() == []
        await (kw["on_nav_click"] if nav else kw["on_submit_click"])()
        await kw["on_progress"](RunResult(status="running", steps=[inferred_step()], submit_clicked=not nav))
        if then_sleep:
            await asyncio.sleep(10)
    return hook


async def _cancel_when_clicked(task, app_id):
    for _ in range(300):
        last = astore.last_run(app_id)
        if last and (last.submit_clicked_at or last.nav_clicked_at) and last.result is not None:
            break
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_cancel_between_the_click_and_the_receipt_keeps_the_deductions(profile, master, tmp_path, monkeypatch):
    _, app = new_app(profile, master, tmp_path, approve=True)
    stub_engine(monkeypatch, RunResult(status="submitted"), hook=progress_hook(then_sleep=True))
    task = asyncio.create_task(service.run(app.id, mode="submit", trigger="user", pool=FakePool()))
    await _cancel_when_clicked(task, app.id)
    run = astore.last_run(app.id)
    assert run.status == "failed" and run.submit_clicked_at and store.get_application(app.id).status == "failed"
    assert [f.label for st in run.result.steps for f in st.filled] == ["Disponibilité"] and run.result.submit_clicked
    service.resolve_unconfirmed(app.id, sent=True, run_id=astore.outstanding_attempt(app.id).id)
    assert [f.label for f in service.inferred_answers(app.id)] == ["Disponibilité"]


async def test_an_engine_crash_after_the_click_keeps_the_progress(profile, master, tmp_path, monkeypatch):
    _, app = new_app(profile, master, tmp_path, approve=True)

    async def hook(kw):
        await progress_hook()(kw)
        raise RuntimeError("boom")

    stub_engine(monkeypatch, RunResult(status="submitted"), hook=hook)
    run = await service.run(app.id, mode="submit", trigger="user", pool=FakePool())
    assert run.status == "failed" and [f.label for st in run.result.steps for f in st.filled] == ["Disponibilité"]


class SlowPagePool(FakePool):
    """Cleanup of the page (after the engine returned its result) hangs: that is where the cancellation arrives."""
    def page(self, profile_id, site, headless=None):
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def cm():
            try:
                yield self._page
            finally:
                self.cleaning = True
                await asyncio.sleep(10)
        self.opened.append((profile_id, site, headless))
        return cm()


async def test_cancel_during_page_cleanup_stores_the_result_the_engine_returned(profile, master, tmp_path, monkeypatch):
    _, app = new_app(profile, master, tmp_path, approve=True)
    final = RunResult(status="submitted", message="Candidature envoyée.", submit_clicked=True, steps=[inferred_step("Niveau")])
    stub_engine(monkeypatch, final, hook=progress_hook())
    pool = SlowPagePool()
    task = asyncio.create_task(service.run(app.id, mode="submit", trigger="user", pool=pool))
    for _ in range(300):
        if getattr(pool, "cleaning", False):
            break
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    run = astore.last_run(app.id)
    assert run.status == "submitted" and run.message == "Candidature envoyée."
    assert [f.label for st in run.result.steps for f in st.filled] == ["Niveau"]
    assert store.get_application(app.id).status == "submitted"


def test_save_progress_only_for_the_running_newest_run(profile, master, tmp_path):
    _, app = new_app(profile, master, tmp_path, approve=True)
    run = astore.start_run(app.id, 1, "submit", "user", lambda rid: tmp_path / f"p{rid}")
    astore.save_progress(run.id, RunResult(status="running", steps=[inferred_step()]))
    got = astore.get_run(run.id)
    assert got.status == "running" and got.result.steps and got.finished_at is None
    astore.finish_run(run.id, RunResult(status="ready"))
    with pytest.raises(ApplyError):
        astore.save_progress(run.id, RunResult(status="running"))  # no longer running
    assert astore.get_run(run.id).result.status == "ready"


@pytest.mark.parametrize("headless, wait", [(True, 0.0), (False, 180.0), (None, None)])
async def test_captcha_wait_follows_headful_runs(profile, master, tmp_path, monkeypatch, headless, wait):
    _, app = new_app(profile, master, tmp_path, approve=True)
    calls = stub_engine(monkeypatch, RunResult(status="ready", message="ok"))
    if headless is None:  # the pool's own default decides
        monkeypatch.setattr(get_settings(), "headless", True)
        wait = 0.0
    await service.run(app.id, mode="dry_run", trigger="user", pool=FakePool(), headless=headless)
    assert calls[0]["captcha_wait_s"] == wait


async def test_captcha_wait_setting(profile, master, tmp_path, monkeypatch):
    _, app = new_app(profile, master, tmp_path, approve=True)
    calls = stub_engine(monkeypatch, RunResult(status="ready", message="ok"))
    monkeypatch.setattr(get_settings(), "apply_captcha_wait_s", 42.0)
    await service.run(app.id, mode="dry_run", trigger="user", pool=FakePool(), headless=False)
    assert calls[0]["captcha_wait_s"] == 42.0


def test_captcha_wait_env_default_and_override(monkeypatch):
    from jobhunt import config

    fresh = config.get_settings.__wrapped__  # builds a new Settings without touching the cached one other tests patch
    monkeypatch.delenv("JOBHUNT_APPLY_CAPTCHA_WAIT_S", raising=False)
    assert fresh().apply_captcha_wait_s == 180
    monkeypatch.setenv("JOBHUNT_APPLY_CAPTCHA_WAIT_S", "90")
    assert fresh().apply_captcha_wait_s == 90


async def test_progress_is_not_offered_for_dry_runs(profile, master, tmp_path, monkeypatch):
    _, app = new_app(profile, master, tmp_path, approve=True)
    calls = stub_engine(monkeypatch, RunResult(status="ready", message="ok"))
    await service.run(app.id, mode="dry_run", trigger="user", pool=FakePool())
    assert calls[0]["on_progress"] is None


async def test_a_prefilled_form_reserves_and_settles_like_a_typed_one(profile, master, tmp_path, monkeypatch):
    """The engine calls the hook although nothing was typed (pre-filled draft): same reservation, same settlement."""
    _, app = new_app(profile, master, tmp_path, approve=True)

    async def hook(kw):
        assert await kw["before_submit"]() == []
        await kw["on_nav_click"]()

    stub_engine(monkeypatch, RunResult(status="needs_email", message="x", submit_clicked=True, steps=[
        StepLog(index=1, url="https://x.test", page_kind="form")]), hook=hook)
    run = await service.run(app.id, mode="submit", trigger="user", pool=FakePool())
    assert run.nav_clicked_at and store.get_application(app.id).status == "failed"
    [r] = astore.active_reservations(profile.id)
    assert r["pending"] == 0 and r["run_id"] == run.id


async def test_failed_with_submit_clicked_after_only_a_navigation_click_keeps_a_firm_reservation(profile, master, tmp_path, monkeypatch):
    """Planning failed after an unchanged form cleared the click: the engine still says submit_clicked -> possibly sent."""
    _, app = new_app(profile, master, tmp_path, approve=True)

    async def hook(kw):
        await kw["on_nav_click"]()

    stub_engine(monkeypatch, RunResult(status="failed", message="plan", submit_clicked=True), hook=hook)
    run = await service.run(app.id, mode="submit", trigger="user", pool=FakePool())
    a = store.get_application(app.id)
    assert run.status == "failed" and a.status == "failed" and a.notes == service.UNCONFIRMED_NOTE
    [r] = astore.active_reservations(profile.id)
    assert r["pending"] == 0 and astore.outstanding_attempt(app.id).id == run.id


async def test_only_submit_clicked_false_releases_a_pending_reservation(profile, master, tmp_path, monkeypatch):
    _, app = new_app(profile, master, tmp_path, approve=True)

    async def hook(kw):
        await kw["on_nav_click"]()

    stub_engine(monkeypatch, RunResult(status="failed", message="plan", submit_clicked=False), hook=hook)
    await service.run(app.id, mode="submit", trigger="user", pool=FakePool())
    assert astore.active_reservations(profile.id) == [] and store.get_application(app.id).status == "approved"


# ------------------------------------------------------------------ fix round 6 (service side)
async def test_dry_runs_and_sends_use_different_browser_profiles(profile, master, tmp_path, monkeypatch):
    _, app = new_app(profile, master, tmp_path, approve=True)
    stub_engine(monkeypatch, RunResult(status="ready", message="ok"))
    dry = FakePool()
    await service.run(app.id, mode="dry_run", trigger="user", pool=dry)
    stub_engine(monkeypatch, RunResult(status="submitted", message="ok", submit_clicked=True), hook=click_hook())
    real = FakePool()
    await service.run(app.id, mode="submit", trigger="user", pool=real)
    assert dry.opened[0][1] == "applydry-exemple.test" and real.opened[0][1] == "apply-exemple.test"
    assert dry.opened[0][1] != real.opened[0][1]


def auto_failed(profile, master, tmp_path, n=1, *, by="auto"):
    """An application approved by `by`, whose send was clicked then ended unconfirmed."""
    job, app = new_app(profile, master, tmp_path, n)
    app = cvs.auto_approve(app.id) if by == "auto" else cvs.approve(app.id, 1)
    run = astore.start_run(app.id, 1, "submit", "auto", lambda rid: tmp_path / f"r{rid}")
    astore.reserve_send(run.id, profile_id=profile.id, application_id=app.id, trigger="auto",
                        role_key=policy.normalize_role(job.company, job.title), url_key=f"careers.exemple.test/jobs/{n}", daily_cap=9)
    astore.finish_and_settle(run.id, RunResult(status="failed", message="Erreur de validation", submit_clicked=True))
    return job, store.get_application(app.id), run


def test_the_attempt_records_who_approved_the_application(profile, master, tmp_path):
    _, app, run = auto_failed(profile, master, tmp_path, 1, by="auto")
    assert astore.get_run(run.id).approved_by == "auto" and app.status == "failed" and app.approved_by is None
    _, _, run2 = auto_failed(profile, master, tmp_path, 2, by="user")
    assert astore.get_run(run2.id).approved_by == "user"


def test_not_sent_does_not_reapprove_or_allow_automatic_retry(profile, master, tmp_path, monkeypatch):
    job, app, run = auto_failed(profile, master, tmp_path, 1, by="auto")
    out = service.resolve_unconfirmed(app.id, sent=False, run_id=run.id)
    assert out.status == "draft" and out.approved_rev is None
    assert astore.active_reservations(profile.id) == []
    monkeypatch.setattr(get_settings(), "apply_auto", True)
    approved = cvs.approve(app.id, out.current_rev)
    assert policy.submit_problems(approved, db.get_job(job.id), trigger="user") == []
    assert policy.submit_problems(approved, db.get_job(job.id), trigger="auto") == ["nouvel envoi manuel requis après vérification"]


def test_not_sent_leaves_a_user_approved_application_a_draft(profile, master, tmp_path):
    _, app, run = auto_failed(profile, master, tmp_path, 1, by="user")
    assert service.resolve_unconfirmed(app.id, sent=False, run_id=run.id).status == "draft"


def test_not_sent_leaves_a_draft_when_the_cv_is_no_longer_eligible(profile, master, tmp_path):
    _, app, run = auto_failed(profile, master, tmp_path, 1, by="auto")
    cvs.safe_pdf(app).unlink()  # the PDF disappeared meanwhile
    assert service.resolve_unconfirmed(app.id, sent=False, run_id=run.id).status == "draft"


def test_sent_does_not_re_approve(profile, master, tmp_path):
    _, app, run = auto_failed(profile, master, tmp_path)
    assert service.resolve_unconfirmed(app.id, sent=True, run_id=run.id).status == "submitted"


@pytest.mark.parametrize("trigger, headless, attended, wait", [
    ("auto", True, False, 0.0), ("auto", False, False, 0.0), ("auto", False, True, 180.0), ("user", False, False, 180.0),
    ("user", True, False, 0.0), ("auto", True, True, 0.0)])
async def test_captcha_wait_for_the_automatic_trigger_needs_an_attended_visible_window(
        profile, master, tmp_path, monkeypatch, trigger, headless, attended, wait):
    _, app = new_app(profile, master, tmp_path, approve=True)
    calls = stub_engine(monkeypatch, RunResult(status="ready", message="ok"))
    await service.run(app.id, mode="dry_run", trigger=trigger, pool=FakePool(), headless=headless, attended=attended)
    assert calls[0]["captcha_wait_s"] == wait


# ------------------------------------------------------------------ batch fixes: accounts, unusable link, resolve-many
_REAL = {n: getattr(accounts, n) for n in ("create_password", "get_password", "account_known")}


class _Mem:
    name = "mémoire"

    def __init__(self):
        self.items = {}

    def get(self, service_, account):
        return self.items.get((service_, account))

    def set(self, service_, account, password, label):
        self.items[(service_, account)] = password

    def delete(self, service_, account):
        self.items.pop((service_, account), None)


@pytest.fixture
def real_accounts(monkeypatch):
    """The real accounts module on an in-memory credential store (the file's autouse fixture stubs the keychain out)."""
    mem = _Mem()
    monkeypatch.setattr(accounts, "_backend_override", mem)
    for name, fn in _REAL.items():
        monkeypatch.setattr(accounts, name, fn)
    return mem


def _steps(*kinds):
    return [StepLog(index=i, url="https://x.test", page_kind=k) for i, k in enumerate(kinds, 1)]


async def _signup_run(profile, master, tmp_path, monkeypatch, result, *, n=1):
    _, app = new_app(profile, master, tmp_path, n, approve=True)
    seen = {}

    async def hook(kw):
        seen["known_before"] = kw["account_known"]("jobs.exemple.test")
        seen["pw"] = kw["password"]("jobs.exemple.test")
        seen["known_after_create"] = kw["account_known"]("jobs.exemple.test")

    stub_engine(monkeypatch, result, hook=hook)
    await service.run(app.id, mode="dry_run", trigger="user", pool=FakePool())
    return seen


async def test_a_signup_that_never_got_past_the_account_page_stays_unknown(profile, master, tmp_path, monkeypatch, real_accounts):
    result = RunResult(status="failed", message="x", steps=_steps("job_page", "account_signup"))
    seen = await _signup_run(profile, master, tmp_path, monkeypatch, result)
    assert seen["known_before"] is False and seen["pw"] and seen["known_after_create"] is False  # created, but pending
    [row] = accounts.list_accounts()
    assert row["host"] == "jobs.exemple.test" and row["state"] == "pending"
    again = await _signup_run(profile, master, tmp_path, monkeypatch, result, n=2)
    assert again["known_before"] is False  # a failed signup is not mistaken for an existing account


@pytest.mark.parametrize("steps, status", [(("job_page", "account_signup", "form"), "ready"),
                                           (("account_login", "confirmation"), "ready"), (("account_signup",), "needs_email")])
async def test_an_account_becomes_active_once_the_run_got_past_the_account_pages(profile, master, tmp_path, monkeypatch, real_accounts,
                                                                              steps, status):
    await _signup_run(profile, master, tmp_path, monkeypatch, RunResult(status=status, message="x", steps=_steps(*steps)))
    assert [r["state"] for r in accounts.list_accounts()] == ["active"]
    again = await _signup_run(profile, master, tmp_path, monkeypatch, RunResult(status="ready", steps=_steps("form")), n=2)
    assert again["known_before"] is True  # the next run logs in instead of signing up


async def test_account_known_gets_the_same_account_email_as_the_password(profile, master, tmp_path, monkeypatch):
    _, app = new_app(profile, master, tmp_path, approve=True)
    asked = []
    monkeypatch.setattr(accounts, "account_known", lambda host, email: asked.append((host, email)) or True)
    monkeypatch.setattr(accounts, "create_password", lambda host, email: asked.append(("pw", host, email)) or "Pw")

    async def hook(kw):
        kw["password"]("a.exemple.test")
        assert kw["account_known"]("a.exemple.test") is True

    stub_engine(monkeypatch, RunResult(status="ready"), hook=hook)
    await service.run(app.id, mode="dry_run", trigger="user", pool=FakePool())
    assert asked[0][0] == "pw" and asked[0][2] == asked[1][1] and asked[1][0] == "a.exemple.test"


async def test_an_unusable_link_is_forgotten_and_the_run_is_not_a_failed_attempt(profile, master, tmp_path, monkeypatch):
    job, app = new_app(profile, master, tmp_path, approve=True, url="https://gozapp.exemple.test/offre/1")
    with db.connect() as c:
        c.execute("UPDATE jobs SET origin_ats='x', origin_confidence=0.9 WHERE id=?", (job.id,))
    stub_engine(monkeypatch, RunResult(status="blocked", message="Lien de l'offre inexploitable : page de redirection"))
    r = await service.run(app.id, mode="submit", trigger="user", pool=FakePool())
    assert r.status == "blocked"
    live = db.get_job(job.id)
    assert live.origin_url is None and live.origin_ats is None and live.origin_confidence is None
    assert live.extra["bad_origin_url"] == "https://gozapp.exemple.test/offre/1"
    assert store.get_application(app.id).status == "approved" and not astore.active_reservations(profile.id)
    assert not any(x.status == "failed" for x in astore.list_runs(app.id))
    again = await service.run(app.id, mode="dry_run", trigger="user", pool=FakePool())  # no link any more: refused, nothing opened
    assert again.status == "blocked" and "pas de lien" in again.message


async def test_other_blocked_messages_keep_the_link(profile, master, tmp_path, monkeypatch):
    job, app = new_app(profile, master, tmp_path, approve=True)
    stub_engine(monkeypatch, RunResult(status="blocked", message="Formulaire hébergé ailleurs"))
    await service.run(app.id, mode="dry_run", trigger="user", pool=FakePool())
    assert db.get_job(job.id).origin_url == job.origin_url and "bad_origin_url" not in db.get_job(job.id).extra


def test_cli_resolve_many(profile, master, tmp_path):
    from typer.testing import CliRunner

    from jobhunt.cli import app as cli

    ids = [failed_after_click(profile, master, tmp_path, n)[1].id for n in (1, 2, 3)]
    _, ok = new_app(profile, master, tmp_path, 4, approve=True)  # not failed: refused
    pairs = [f"{app_id}:{astore.last_run(app_id).id}" for app_id in ids]
    r = CliRunner().invoke(cli, ["apply", "resolve-many", "--not-sent", pairs[0], pairs[1], f"{ok.id}:1"])
    assert r.exit_code == 1 and "2 essai(s) confirmé(s) non envoyé(s), 1 refusé(s)" in r.output and "pas en échec" in r.output
    assert [store.get_application(i).status for i in ids] == ["draft", "draft", "failed"]
    r = CliRunner().invoke(cli, ["apply", "resolve-many", "--sent", pairs[2], pairs[2]])
    assert r.exit_code == 0 and "1 essai(s) confirmé(s) envoyé(s)." in r.output
    assert store.get_application(ids[2]).status == "submitted"
    assert CliRunner().invoke(cli, ["apply", "resolve-many", "--sent"]).exit_code != 0


# ------------------------------------------------------------------ send_selected forwards its bindings to run
async def test_send_selected_forwards_the_expected_revision_and_destination_to_run(profile, master, tmp_path, monkeypatch):
    j1, a1 = new_app(profile, master, tmp_path, 1)
    j2, a2 = new_app(profile, master, tmp_path, 2)
    seen = {}
    real = service.run

    async def spy(app_id, **kw):
        seen[app_id] = {k: kw.get(k) for k in ("expected_rev", "expected_origin", "_lock_held")}
        return await real(app_id, **kw)

    monkeypatch.setattr(service, "run", spy)
    stub_engine(monkeypatch, RunResult(status="submitted", message="ok"))
    revs = {a1.id: a1.current_rev, a2.id: a2.current_rev}
    origins = {a1.id: service.origin_digest(j1.origin_url), a2.id: service.origin_digest(j2.origin_url)}
    runs = await service.send_selected([a1.id, a2.id], pool=FakePool(), expected_revs=revs, expected_origins=origins)
    assert [r.status for r in runs] == ["submitted", "submitted"]
    assert seen == {a1.id: {"expected_rev": 1, "expected_origin": origins[a1.id], "_lock_held": True},
                    a2.id: {"expected_rev": 1, "expected_origin": origins[a2.id], "_lock_held": True}}
    seen.clear()
    _, a3 = new_app(profile, master, tmp_path, 3)
    await service.send_selected([a3.id], pool=FakePool())  # nothing to bind: nothing forwarded
    assert seen[a3.id]["expected_rev"] is None and seen[a3.id]["expected_origin"] is None


async def test_a_change_between_the_early_check_and_the_run_is_caught_by_run_itself(profile, master, tmp_path, monkeypatch):
    j, a = new_app(profile, master, tmp_path, 1)
    calls = stub_engine(monkeypatch, RunResult(status="submitted", message="ok"))
    real = service._bound_problems

    def late_change(app, job, rev, origin):
        out = real(app, job, rev, origin)  # the early check passes ...
        with db.connect() as c:  # ... then the link changes before the run starts
            c.execute("UPDATE jobs SET origin_url='https://evil.exemple.test/job' WHERE id=?", (job.id,))
        return out

    monkeypatch.setattr(service, "_bound_problems", lambda *a_, **k: late_change(*a_, **k) if a_[2] is not None else real(*a_, **k))
    runs = await service.send_selected([a.id], pool=FakePool(), expected_revs={a.id: a.current_rev},
                                       expected_origins={a.id: service.origin_digest(j.origin_url)})
    assert runs[0].status == "blocked" and "le lien de l'offre a changé depuis l'affichage" in runs[0].message and not calls
