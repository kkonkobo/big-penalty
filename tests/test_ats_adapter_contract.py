"""Supply a loopback-only fixture factory here for every new adapter."""

import hashlib
import json
from contextlib import asynccontextmanager
from dataclasses import replace
from urllib.parse import urlsplit

import httpx
import pytest

from apply_cv_helpers import make_app, make_job
from apply_fakes import server  # noqa: F401
from apply_service_fakes import FakePool
from cv_fakes import save_confirmed
from jobhunt import db
from jobhunt.apply import service, store
from jobhunt.apply.adapters import Dispatch, Receipt, Registry, Requisition
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
        return Requisition("fixture-tenant", "req-1")

    async def supervise(self, page, request, gate):
        await gate.perform("final_submit", request.attempt.url, lambda: self._post(request))

    async def _post(self, request: Dispatch):
        assert self.matches(request.attempt.url, frozenset())
        with db.connect() as conn:
            row = conn.execute("SELECT binding_json FROM apply_reservations WHERE run_id=? AND released_at IS NULL",
                               (request.attempt.run_id,)).fetchone()
            assert row
            binding = json.loads(row["binding_json"])
            for key in ("nonce", "url", "host", "rev", "pdf_sha256", "tenant", "requisition_id", "profile_id"):
                assert binding[key] == getattr(request.attempt, key)
        assert hashlib.sha256(request.pdf).hexdigest() == request.attempt.pdf_sha256
        self.sent.append(request)
        async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as client:
            response = await client.post(self.url, content=request.pdf, headers={"X-Fixture-Nonce": request.attempt.nonce})
        if self.scenario == "interrupt":
            raise RuntimeError("fixture interrupted")
        self.evidence = response
        return response

    async def dispatch(self, request, gate):
        if self.scenario == "blocker":
            gate.stop("captcha")
        if self.scenario == "redirect":
            return await gate.perform("final_submit", "https://unapproved.example.test/", lambda: self._post(request))
        if self.scenario == "paused-write":
            return await gate.perform("draft_write", request.attempt.url, lambda: self._post(request))
        if self.scenario == "policy-change":
            async def invalidate():
                with db.connect() as conn:
                    conn.execute("UPDATE applications SET approved_sha256=NULL WHERE id=?", (request.attempt.application_id,))

            await gate.perform("cv_upload_parse", request.attempt.url, invalidate)
        return await gate.perform("final_submit", request.attempt.url, lambda: self._post(request))

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
            return Receipt(attempt, "not_sent", "candidate_home_listing", "synthetic-rejection")
        return Receipt(attempt, "confirmed", "candidate_home_listing" if later else "first_party_confirmation", "synthetic-receipt")


@pytest.fixture(params=[FixtureAdapter], ids=lambda factory: factory.name)
def adapter_factory(request):
    return request.param


@pytest.mark.parametrize("scenario", ["verified", "text-only", "wrong-attempt", "interrupt", "stale", "unregistered", "default",
                                      "wrong-tenant", "paused-write", "redirect", "blocker", "policy-change", "duplicate",
                                      "reconcile", "reconcile-not-sent"])
async def test_adapter_contract(adapter_factory, scenario, server, profile, tmp_path, monkeypatch):
    master = save_confirmed(profile.id)
    job = make_job(profile, url=server.url("adapter-contract"))
    app = make_app(profile, job, tmp_path, master_hash=master.content_hash)
    cvs.approve(app.id, app.current_rev)
    adapter = adapter_factory(job.origin_url, scenario)
    registry = Registry()
    if scenario != "unregistered":
        allowlist = None if scenario == "default" else {("other" if scenario == "wrong-tenant" else "fixture-tenant"):
                                                         "first_party_confirmation"}
        registry.register(adapter, host="127.0.0.1", origin_ats="lever", unattended_tenants=allowlist)
    monkeypatch.setattr(service, "registry", registry)
    monkeypatch.setattr(get_settings(), "apply_auto", True)

    if scenario == "stale":
        original = service._reserve

        def changed(*args):
            with db.connect() as conn:
                conn.execute("UPDATE jobs SET origin_url=? WHERE id=?", (server.url("other"), job.id))
            return original(*args)

        monkeypatch.setattr(service, "_reserve", changed)

    result = await service.run(app.id, mode="submit", trigger="auto")
    if scenario in ("unregistered", "stale", "default", "wrong-tenant"):
        assert result.status == "blocked"
        assert not adapter.sent and not server.sent and not store.active_reservations(profile.id)
        return
    if scenario in ("paused-write", "redirect", "blocker", "policy-change"):
        assert not adapter.sent and not server.sent and store.outstanding_attempt(app.id).id == result.id
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
