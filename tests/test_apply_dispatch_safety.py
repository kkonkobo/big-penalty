"""Local dispatch boundary regressions; the employer page, candidate and PDF are fictional."""

from apply_cv_helpers import make_app, make_job
import asyncio
from contextlib import asynccontextmanager

import pytest
from apply_fakes import FakeVerifier, ScriptedPlanner, act, cv_pdf, fast, page, sample_facts, server  # noqa: F401
from apply_service_fakes import FakePool
from cv_fakes import save_confirmed

from jobhunt import db
from jobhunt.apply import service, store
from jobhunt.apply.engine import DRY_UNTESTED, UNSUPPORTED_DISPATCH, Uploads, run_application
from jobhunt.apply.types import RunResult, StepPlan
from jobhunt.cv import service as cvs
from jobhunt.cv import store as cvstore


def form(snapshot):
    return StepPlan(page_kind="form", actions=[
        act(snapshot, "Prénom", "fill", "first_name"),
        act(snapshot, "CV", "upload_cv", kind="file"),
        act(snapshot, "Envoyer", "click", role="submit"),
    ])


async def test_dry_does_not_type_upload_or_click_even_on_a_page_with_background_sends(page, server, fast, cv_pdf, tmp_path):
    class ProbePlanner(ScriptedPlanner):
        async def plan(self, *args, **kwargs):
            assert await page.evaluate("""async () => {
                try { await fetch('/collect?offline_probe=1'); return false; }
                catch { return true; }
            }""")
            return await super().plan(*args, **kwargs)

    result = await run_application(page, url=server.url("dry_egress"), facts=sample_facts(),
                                   planner=ProbePlanner(form), verifier=FakeVerifier(), uploads=Uploads(cv_pdf=cv_pdf),
                                   mode="dry_run", out_dir=tmp_path / "shots")
    assert result.status == "blocked" and result.message == DRY_UNTESTED
    assert await page.input_value("input[name=first_name]") == ""
    assert await page.locator("input[type=file]").evaluate("el => el.files.length") == 0
    assert not result.steps[-1].filled and not result.steps[-1].clicked
    assert server.sent == []


async def test_generic_live_engine_refuses_before_navigation_or_callbacks(page, server, cv_pdf, tmp_path):
    async def unexpected():
        raise AssertionError("unadapted live run must not reserve or dispatch")

    planner = ScriptedPlanner(form)
    result = await run_application(page, url=server.url("dry_egress"), facts=sample_facts(), planner=planner,
                                   uploads=Uploads(cv_pdf=cv_pdf), mode="submit", out_dir=tmp_path / "shots",
                                   before_submit=unexpected, on_nav_click=unexpected, on_submit_click=unexpected)
    assert result.status == "blocked" and result.message == UNSUPPORTED_DISPATCH
    assert not planner.calls and not server.requests and not result.submit_clicked


async def test_even_an_approved_live_service_run_opens_no_ats_session(profile, tmp_path):
    master = save_confirmed(profile.id)
    job = make_job(profile)
    app = make_app(profile, job, tmp_path, master_hash=master.content_hash)
    cvs.approve(app.id, app.current_rev)
    pool = FakePool()

    result = await service.run(app.id, mode="submit", trigger="user", pool=pool)

    assert result.status == "blocked" and service.SUPERVISED_ONLY in result.message
    assert not pool.opened and not store.active_reservations(profile.id)


@pytest.mark.parametrize("decision", [True, False])
async def test_supervised_holds_before_headful_browser_and_only_operator_settles(page, server, fast, profile, tmp_path, decision):
    master = save_confirmed(profile.id)
    job = make_job(profile, url=server.url("dry_egress"))
    app = make_app(profile, job, tmp_path, master_hash=master.content_hash)
    cvs.approve(app.id, app.current_rev)

    class Pool(FakePool):
        @asynccontextmanager
        async def page(self, profile_id, site, headless):
            reservations = store.active_reservations(profile_id)
            assert headless is False and len(reservations) == 1 and not server.requests
            with db.connect() as conn:
                import json
                binding = json.loads(conn.execute("SELECT binding_json FROM apply_reservations").fetchone()[0])
            assert binding["pdf_sha256"] == app.pdf_sha256 and binding["url"] == job.origin_url
            assert binding["rev"] == app.current_rev and binding["host"] == "127.0.0.1"
            yield page

    task = asyncio.create_task(service.run(app.id, mode="supervised", trigger="user", pool=Pool(),
                                           planner=ScriptedPlanner(form), verifier=FakeVerifier()))
    try:
        await page.locator("aside").wait_for(timeout=6000)
        assert await page.input_value("input[name=first_name]")
        assert await page.locator("input[type=file]").evaluate("el => el.files[0].name") == "CV.pdf"
        assert not server.posts_to("/apply")
        assert cvstore.get_application(app.id).status == "approved"
    finally:
        await page.close()
        result = await asyncio.wait_for(task, timeout=5)
    assert result.status == "failed" and not result.submit_clicked_at
    assert store.outstanding_attempt(app.id).id == result.id
    assert db.get_job(job.id).status != "applied"
    service.resolve_unconfirmed(app.id, sent=decision, run_id=result.id)
    assert cvstore.get_application(app.id).status == ("submitted" if decision else "draft")
    if not decision:
        assert store.requires_manual_retry(app.id) and not store.active_reservations(profile.id)


async def test_supervised_browser_start_crash_is_held_without_relaunch(profile, tmp_path):
    master = save_confirmed(profile.id)
    job = make_job(profile)
    app = make_app(profile, job, tmp_path, master_hash=master.content_hash)
    cvs.approve(app.id, app.current_rev)

    class CrashedPool(FakePool):
        @asynccontextmanager
        async def page(self, profile_id, site, headless):
            self.opened.append((profile_id, site, headless))
            assert store.active_reservations(profile_id)
            raise RuntimeError("fixture launch crashed")
            yield

    pool = CrashedPool()
    result = await service.run(app.id, mode="supervised", trigger="user", pool=pool, planner=ScriptedPlanner(form))
    assert result.status == "failed" and store.outstanding_attempt(app.id).id == result.id
    refused = await service.run(app.id, mode="supervised", trigger="user", pool=pool)
    assert refused.status == "blocked" and len(pool.opened) == 1


def test_unbacked_positive_status_does_not_automatically_settle(profile, tmp_path):
    master = save_confirmed(profile.id)
    job = make_job(profile)
    app = make_app(profile, job, tmp_path, master_hash=master.content_hash)
    cvs.approve(app.id, app.current_rev)
    run = store.start_run(app.id, app.current_rev, "submit", "user", lambda run_id: tmp_path / str(run_id))

    result = store.finish_and_settle(run.id, RunResult(status="submitted", message="Merci pour votre candidature"))

    assert result.status == "failed" and result.result.submit_clicked
    assert store.outstanding_attempt(app.id).id == run.id
    assert cvstore.get_application(app.id).status == "failed"
    assert db.get_job(job.id).status != "applied"
