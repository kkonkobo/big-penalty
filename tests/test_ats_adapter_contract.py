"""Supply a loopback-only fixture factory here for every new adapter."""

import asyncio
import hashlib
import json
from contextlib import asynccontextmanager
from dataclasses import replace
from urllib.parse import urlsplit

import pytest

from apply_cv_helpers import make_app, make_job
from apply_fakes import FixtureServer, ScriptedPlanner, act, fast, page, server  # noqa: F401
from apply_service_fakes import FakePool
from cv_fakes import save_confirmed
from jobhunt import db
from jobhunt.apply import service, store
from jobhunt.apply.adapters import Dispatch, Receipt, Registry, Requisition
from jobhunt.apply.types import StepPlan
from jobhunt.config import get_settings
from jobhunt.cv import service as cvs


class FixtureAdapter:
    name = "fixture-only"
    capability = "unattended"
    retry_rule = "never"
    boundaries = {"cv_upload_parse": "auto", "draft_write": "pause_for_human", "final_submit": "auto"}

    def __init__(self, url, scenario):
        self.url, self.scenario = url, scenario
        self.sent = []

    def matches(self, url, dom_markers):
        return float(url in {self.url, self.url + "-duplicate"} and urlsplit(url).hostname == "127.0.0.1")

    def resolve(self, url, dom_markers):
        endpoint = self.url + "/{tenant}/{requisition}"
        return Requisition("fixture-tenant", "req-1", endpoints=(("final_submit", endpoint),))

    async def supervise(self, request, fill):
        assert not hasattr(fill, "click") and not hasattr(fill, "evaluate")
        raise service.ApplyError("Pas de primitive de clic.")

    async def _post(self, request: Dispatch, gate):
        assert self.matches(request.attempt.url, frozenset())
        with db.connect() as conn:
            row = conn.execute("SELECT binding_json FROM apply_reservations WHERE run_id=? AND released_at IS NULL",
                               (request.attempt.run_id,)).fetchone()
            assert row
            binding = json.loads(row["binding_json"])
            assert binding == store.binding_record(request.attempt)
            for key in ("nonce", "host", "rev", "pdf_sha256", "tenant", "requisition_id", "profile_id"):
                assert binding[key] == getattr(request.attempt, key)
        assert hashlib.sha256(request.pdf).hexdigest() == request.attempt.pdf_sha256
        self.sent.append(request)
        response = await gate.perform("final_submit", self.url + "/fixture-tenant/req-1", attach_pdf=True)
        if self.scenario == "interrupt":
            raise RuntimeError("fixture interrupted")
        self.evidence = response
        return response

    async def dispatch(self, request, gate):
        if self.scenario == "blocker":
            gate.stop("captcha")
        if self.scenario == "otp":
            gate.stop("otp")
        if self.scenario == "redirect":
            return await gate.perform("final_submit", "https://unapproved.example.test/", attach_pdf=True)
        if self.scenario == "wrong-endpoint":
            return await gate.perform("final_submit", self.url + "/fixture-tenant/other", attach_pdf=True)
        if self.scenario == "paused-write":
            return await gate.perform("draft_write", request.attempt.url, attach_pdf=True)
        if self.scenario == "policy-change":
            with db.connect() as conn:
                conn.execute("UPDATE applications SET approved_sha256=NULL WHERE id=?", (request.attempt.application_id,))
            return await gate.perform("final_submit", self.url + "/fixture-tenant/req-1", attach_pdf=True)
        return await self._post(request, gate)

    async def verify_receipt(self, attempt, *, evidence=None):
        later = evidence is None
        if later and self.scenario in ("reconcile", "reconcile-not-sent"):
            evidence = self.evidence
        if (not evidence or self.scenario in ("text-only", "duplicate") or evidence.status_code != 200
                or evidence.headers.get("X-Fixture-Receipt") != attempt.nonce
                or (not later and self.scenario in ("reconcile", "reconcile-not-sent"))):
            return Receipt(attempt, "uncertain")
        attempt = replace(attempt, nonce="different") if self.scenario == "wrong-attempt" else attempt
        if self.scenario == "reconcile-not-sent":
            return Receipt(attempt, "not_sent", "candidate_home_listing", "synthetic-rejection",
                           evidence.headers["X-Fixture-Receipt"], evidence.headers["X-Fixture-Requisition"])
        if self.scenario.startswith("negative-"):
            return Receipt(attempt, "not_sent", "candidate_home_listing" if self.scenario == "negative-kind" else
                           "first_party_confirmation", "synthetic-rejection",
                           "wrong" if self.scenario == "negative-nonce" else evidence.headers["X-Fixture-Receipt"],
                           "wrong" if self.scenario == "negative-req" else evidence.headers["X-Fixture-Requisition"])
        kind = "candidate_home_listing" if later or self.scenario == "wrong-receipt-kind" else "first_party_confirmation"
        requisition = "different" if self.scenario == "wrong-requisition" else evidence.headers.get("X-Fixture-Requisition", "")
        return Receipt(attempt, "confirmed", kind, "synthetic-receipt", evidence.headers["X-Fixture-Receipt"], requisition)


@pytest.fixture(params=[FixtureAdapter], ids=lambda factory: factory.name)
def adapter_factory(request):
    return request.param


@pytest.mark.parametrize("scenario", ["verified", "text-only", "wrong-attempt", "interrupt", "stale", "unregistered", "default",
                                      "wrong-tenant", "paused-write", "redirect", "blocker", "otp", "policy-change", "duplicate",
                                      "wrong-receipt-kind", "wrong-requisition", "reconcile", "reconcile-not-sent",
                                      "wrong-endpoint", "negative-kind", "negative-nonce", "negative-req"])
async def test_adapter_contract(adapter_factory, scenario, server, profile, tmp_path, monkeypatch):
    master = save_confirmed(profile.id)
    job = make_job(profile, url=server.url("adapter-contract"))
    app = make_app(profile, job, tmp_path, master_hash=master.content_hash)
    cvs.approve(app.id, app.current_rev)
    adapter = adapter_factory(job.origin_url, scenario)
    registry = Registry()
    if scenario != "unregistered":
        allowlist = None if scenario == "default" else {("other" if scenario == "wrong-tenant" else "fixture-tenant"):
                                                         ("candidate_home_listing" if scenario.startswith("reconcile") else "first_party_confirmation")}
        registry.register(adapter, host="127.0.0.1", origin_ats="lever", unattended_tenants=allowlist)
    monkeypatch.setattr(service, "registry", registry)
    monkeypatch.setattr(get_settings(), "apply_auto", True)

    if scenario == "stale":
        original = service._reserve

        def changed(*args, **kwargs):
            with db.connect() as conn:
                conn.execute("UPDATE jobs SET origin_url=? WHERE id=?", (server.url("other"), job.id))
            return original(*args, **kwargs)

        monkeypatch.setattr(service, "_reserve", changed)

    result = await service.run(app.id, mode="submit", trigger="auto")
    if scenario in ("unregistered", "stale", "default", "wrong-tenant"):
        assert result.status == "blocked"
        assert not adapter.sent and not server.sent and not store.active_reservations(profile.id)
        return
    if scenario in ("paused-write", "redirect", "blocker", "otp", "policy-change", "wrong-endpoint"):
        assert not adapter.sent and not server.sent and store.outstanding_attempt(app.id).id == result.id
        if scenario in {"blocker", "otp"}:
            assert result.status == ("needs_email" if scenario == "otp" else "needs_human")
            assert result.result.dispatch_diagnostics.receipt_reason == ("verification_required" if scenario == "otp" else "human_required")
        return
    assert len(adapter.sent) == len(server.posts) == 1
    assert server.posts[0]["body"] == adapter.sent[0].pdf
    if scenario == "verified":
        assert result.status == "submitted" and store.sent_run(app.id).id == result.id
        assert db.get_job(job.id).status == "applied"
        with db.connect() as conn:
            record = dict(conn.execute("SELECT * FROM apply_reservations WHERE run_id=?", (result.id,)).fetchone())
        assert record["receipt_kind"] == "first_party_confirmation" and record["receipt_at"]
        assert record["receipt_outcome"] == "confirmed" and "synthetic-receipt" not in str(record)
    else:
        assert result.status == "failed" and store.outstanding_attempt(app.id).id == result.id
        assert db.get_job(job.id).status != "applied"
    if scenario == "duplicate":
        duplicate = make_job(profile, url=job.origin_url + "-duplicate", title="Titre différent", company="Nom différent")
        second = make_app(profile, duplicate, tmp_path, master_hash=master.content_hash)
        cvs.approve(second.id, second.current_rev)
        assert (await service.run(second.id, mode="submit", trigger="auto")).status == "blocked"
    if scenario in ("reconcile", "reconcile-not-sent"):
        receipt = await service.reconcile_attempt(result.id)
        assert receipt.kind == "candidate_home_listing"
        assert store.outstanding_attempt(app.id) is None
        assert (db.get_job(job.id).status == "applied") == (scenario == "reconcile")
    refused = await service.run(app.id, mode="submit", trigger="auto")
    assert refused.status == "blocked" and len(server.posts) == 1


async def test_default_supervised_adapter_cannot_use_final_boundary(adapter_factory, server, profile, tmp_path, monkeypatch):
    master = save_confirmed(profile.id)
    job = make_job(profile, url=server.url("adapter-contract"))
    app = make_app(profile, job, tmp_path, master_hash=master.content_hash)
    cvs.approve(app.id, app.current_rev)
    adapter = adapter_factory(job.origin_url, "verified")
    registry = Registry()
    registry.register(adapter, host="127.0.0.1")
    monkeypatch.setattr(service, "registry", registry)

    class ClosedPage:
        def is_closed(self):
            return True

    class Pool(FakePool):
        @asynccontextmanager
        async def page(self, profile_id, site, headless):
            assert headless is False and store.active_reservations(profile_id)
            yield ClosedPage()

    result = await service.run(app.id, mode="supervised", trigger="user", pool=Pool())
    assert not adapter.sent and not server.sent
    assert store.outstanding_attempt(app.id).id == result.id


async def test_adapter_transport_does_not_replay_post_on_307(server, profile, tmp_path, monkeypatch):
    foreign = FixtureServer()
    try:
        master = save_confirmed(profile.id)
        job = make_job(profile, url=server.url("adapter-contract"))
        app = make_app(profile, job, tmp_path, master_hash=master.content_hash)
        cvs.approve(app.id, app.current_rev)
        adapter = FixtureAdapter(job.origin_url, "verified")
        registry = Registry()
        registry.register(adapter, host="127.0.0.1", unattended_tenants={"fixture-tenant": "first_party_confirmation"})
        monkeypatch.setattr(service, "registry", registry)
        server.redirects["/adapter-contract/fixture-tenant/req-1"] = foreign.url("collect")
        result = await service.run(app.id, mode="submit", trigger="user")
        assert len(server.posts) == 1 and not foreign.posts and not foreign.requests
        assert store.outstanding_attempt(app.id).id == result.id and result.status != "submitted"
    finally:
        foreign.close()


async def test_supervised_adapter_fill_uses_live_consent_guard(server, page, profile, tmp_path, fast, monkeypatch):
    master = save_confirmed(profile.id)
    job = make_job(profile, url=server.url("adapter_consent"))
    app = make_app(profile, job, tmp_path, master_hash=master.content_hash)
    cvs.approve(app.id, app.current_rev)

    class SupervisedFixture(FixtureAdapter):
        capability = "supervised"
        boundaries = {"draft_write": "auto", "final_submit": "pause_for_human"}

        async def supervise(self, request, fill):
            def plan(snapshot):
                return StepPlan(page_kind="form", actions=[act(snapshot, "Prénom", "fill", "first_name"),
                                                           act(snapshot, "consentement", "check", "consent_required"),
                                                           act(snapshot, "Envoyer", "click", role="submit")])
            return await fill(ScriptedPlanner(plan))

    registry = Registry()
    registry.register(SupervisedFixture(job.origin_url, "ignored"), host="127.0.0.1")
    monkeypatch.setattr(service, "registry", registry)
    task = asyncio.create_task(service.run(app.id, mode="supervised", trigger="user", pool=FakePool(page)))
    try:
        await page.locator("aside").wait_for(timeout=6000)
        assert await page.input_value("#name") == "" and not await page.is_checked("#consent")
        assert not server.posts
    finally:
        await page.close()
        result = await asyncio.wait_for(task, timeout=6)
    assert result.status == "needs_input" and store.outstanding_attempt(app.id).id == result.id
