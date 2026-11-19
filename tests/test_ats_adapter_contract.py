"""Supply a loopback-only fixture factory here for every new adapter."""

import asyncio
import hashlib
import json
from contextlib import asynccontextmanager
from dataclasses import replace
from urllib.parse import urlsplit

import pytest

from apply_cv_helpers import make_app, make_job
from apply_fakes import FakeVerifier, FixtureServer, ScriptedPlanner, act, fast, page, sample_facts, server  # noqa: F401
from apply_service_fakes import FakePool
from cv_fakes import save_confirmed
from jobhunt import db
from jobhunt.apply import scoped_choices, service, smartrecruiters, store
from jobhunt.apply.adapters import Attempt, Dispatch, Receipt, Registry, Requisition, clean_receipt, registry as production_registry
from jobhunt.apply.facts import save_answers
from jobhunt.apply.snapshot import take_snapshot
from jobhunt.apply.types import Fact, FactSheet, StepPlan
from jobhunt.apply.workday import CandidateHomeEvidence, WorkdayAdapter, WorkdayPlanner
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


WORKDAY_POSTING = "https://demo.wd3.myworkdayjobs.com/en-US/External/job/Paris/Engineer_JR123"


@pytest.fixture
def workday_registry(server, monkeypatch):
    class LoopbackWorkday(WorkdayAdapter):
        def browser_request_allowed(self, attempt, url, method, navigation):
            return True

        def resolve(self, url, markers):
            parsed = urlsplit(url)
            if parsed.netloc != urlsplit(server.base).netloc:
                return None
            posting = WORKDAY_POSTING if parsed.path.startswith("/workday-") else origin_of_workday + parsed.path
            identity = super().resolve(posting, markers)
            return replace(identity, write_origins=(server.base,), endpoints=(
                ("cv_upload_parse", server.base + "/workday-upload/{tenant}/{requisition}"),)) if identity else None

        def matches(self, url, markers):
            return float(self.resolve(url, markers) is not None)

    origin_of_workday = "https://" + urlsplit(WORKDAY_POSTING).netloc
    registry = Registry()
    registry.register(LoopbackWorkday(), host="127.0.0.1", supervised_receipt_kind="candidate_home_listing")
    monkeypatch.setattr(service, "registry", registry)
    return registry


def test_workday_production_registry_enrolls_verified_tenants():
    match = production_registry.find(WORKDAY_POSTING, "careers_site")
    assert match.capability == "unattended" and match.receipt_kind == "candidate_home_listing"
    assert (match.requisition.tenant, match.requisition.requisition_id) == ("demo.wd3.myworkdayjobs.com/external", "JR123")
    assert ("cv_upload_parse", "https://demo.wd3.myworkdayjobs.com/wday/cxs/{tenant}/job/{requisition}/attachments") in match.requisition.endpoints
    assert production_registry.find("https://wd5.myworkdaysite.com/recruiting/example/External/job/Stage_R12/apply", None)
    assert production_registry.find(WORKDAY_POSTING.replace("en-US", "fr-FR") + "/apply?utm_source=fixture", None) == match
    for url in (WORKDAY_POSTING.replace(".com/", ".com.fixture.test/"), WORKDAY_POSTING.replace("demo.wd3.", ""),
                WORKDAY_POSTING.replace("/Paris/", "/%2E%2E/"), WORKDAY_POSTING.rsplit("/job", 1)[0],
                WORKDAY_POSTING.replace("JR123", "unknown") + "?reqId=JR123",
                "https://careers.example.test/job/JR123?applyUrl=" + WORKDAY_POSTING):
        assert production_registry.find(url, "workday") is None


@pytest.mark.parametrize("posting", [WORKDAY_POSTING,
    "https://wd5.myworkdaysite.com/recruiting/example/External/fr-FR/job/Stage_R12"])
def test_workday_live_step_identity(posting):
    adapter = WorkdayAdapter()
    reserved = adapter.resolve(posting, frozenset())
    assert reserved is not None
    for suffix in ("/apply", "/apply/applyManually/my-information", "/apply/myExperience",
                   "/apply/applicationQuestions", "/apply/review", "/apply/submit"):
        assert adapter.resolve(posting + suffix, frozenset()) == reserved
        other = posting.replace("JR123", "JR124").replace("R12", "R13")
        assert adapter.resolve(other + suffix, frozenset()).requisition_id != reserved.requisition_id
    assert adapter.resolve(posting + "/apply/other", frozenset()) is None
    assert adapter.resolve(posting + "#/job/Other_JR999", frozenset()) is None
    assert adapter.resolve(posting.split("/job/")[0] + "/apply/myInformation", frozenset()) is None


async def test_workday_planner_maps_synthetic_steps(server, page):
    planner = WorkdayPlanner()
    facts = sample_facts()
    facts.facts.extend([Fact(key="country", label="Country", value="France"),
                        Fact(key="job_source", label="Job source", value="University"),
                        Fact(key="graduation_date", label="Graduation date", value="2027-09-01"),
                        Fact(key="available_from", label="Start date", value="September 2027"),
                        Fact(key="custom_1", label="Have you built a robot?", value="Yes")])

    async def plan():
        snapshot, _ = await take_snapshot(page)
        return await planner.plan(snapshot, facts)

    await page.goto(server.url("workday-information"))
    result = await plan()
    assert [(action.fact, action.op) for action in result.actions] == [("first_name", "fill"), ("country", "select")]
    assert any(q.kind == "combobox" and q.options == ["University", "Referral"] for q in result.unknown)
    await page.locator("#country option[value=FR]").evaluate("option => option.disabled = true")
    await page.locator("#options li").first.evaluate("option => option.textContent = 'Other'")
    result = await plan()
    assert all(action.fact != "country" for action in result.actions)
    assert any(q.kind == "combobox" and q.options == ["Other", "Referral"] for q in result.unknown)

    await page.get_by_text("Save and Continue").click()
    assert [action.op for action in (await plan()).actions] == ["upload_cv"]
    await page.get_by_text("Save and Continue").click()
    result = await plan()
    assert [(action.fact, action.value) for action in result.actions] == [("graduation_date", "2027-09-01"), ("custom_1", "Yes")]
    assert any("Start Date" in q.label for q in result.unknown)
    for _ in range(2):
        await page.get_by_text("Save and Continue").click()
        assert not (await plan()).actions
    await page.goto(server.url("workday-auth"))
    assert (await plan()).page_kind == "account_login" and not (await plan()).actions
    await page.get_by_text("Continue to verification").click()
    assert (await plan()).page_kind == "email_verification" and not (await plan()).actions
    assert not server.posts


@pytest.mark.parametrize("stored,expected", [("Black", "noir"), ("", "optout")])
async def test_supervised_workday_ethnicity(stored, expected, workday_registry, server, page, profile, tmp_path, fast):
    server.html["/workday-race"] = (
        '<h1>Voluntary Disclosures</h1><form><label>Origine ethnique<select required>'
        '<option value="">Choisir</option><option value="noir">Noire</option>'
        '<option value="optout">Je ne souhaite pas répondre</option></select></label>'
        '<label>I accept the privacy policy<input type="checkbox"></label><button>Save and Continue</button></form>')
    master = save_confirmed(profile.id)
    job = make_job(profile, url=server.url("workday-race"))
    app = make_app(profile, job, tmp_path, master_hash=master.content_hash)
    cvs.approve(app.id, app.current_rev)
    save_answers(profile.id, {"race_ethnicity": stored})
    task = asyncio.create_task(service.run(app.id, mode="supervised", trigger="user", pool=FakePool(page)))
    try:
        await page.locator("aside").wait_for(timeout=60_000)
        assert await page.locator("select").input_value() == expected
        assert not await page.get_by_role("checkbox").is_checked()
        assert not server.posts
    finally:
        await page.close()
        await asyncio.wait_for(task, 60)


@pytest.mark.parametrize("step", ["experience", "autosave", "auth", "otp", "foreign-upload"])
async def test_workday_supervised_contract(step, workday_registry, server, page, profile, tmp_path, fast, monkeypatch):
    master = save_confirmed(profile.id)
    job = make_job(profile, url=server.url("workday-experience" if step == "foreign-upload" else "workday-" + step))
    app = make_app(profile, job, tmp_path, master_hash=master.content_hash)
    cvs.approve(app.id, app.current_rev)
    if step == "experience":
        check_pdf = service.is_bound_pdf

        def verify_reservation_at_upload(content_type, body, pdf):
            attempt = store.held_binding(store.last_run(app.id).id)
            assert attempt and store.binding_active(attempt) and attempt.nonce
            assert (attempt.rev, attempt.url, attempt.pdf_sha256) == (
                app.current_rev, job.origin_url, hashlib.sha256(pdf).hexdigest())
            return check_pdf(content_type, body, pdf)

        monkeypatch.setattr(service, "is_bound_pdf", verify_reservation_at_upload)

    class Pool(FakePool):
        async def goto(self, page, url):
            await super().goto(page, url)
            if step == "foreign-upload":
                await page.evaluate("""() => {
                    const field = document.querySelector('#resume');
                    field.addEventListener('change', event => {
                        event.stopImmediatePropagation();
                        fetch('http://localhost:' + location.port + '/workday-upload', {method: 'POST', body: field.files[0]});
                    }, true);
                }""")

    assert (await service.run(app.id, mode="submit", trigger="auto")).status == "blocked"
    task = asyncio.create_task(service.run(app.id, mode="supervised", trigger="user", pool=Pool(page)))
    if step in {"autosave", "foreign-upload"}:
        result = await asyncio.wait_for(task, timeout=10)
        assert service.RECIPIENT_REFUSED in result.message and page.is_closed() and not server.posts
        return
    try:
        await page.locator("aside").wait_for(timeout=8000)
        if step == "experience":
            assert await page.locator("#resume").evaluate("field => field.files[0].name") == "CV.pdf"
            assert len(server.posts) == 1 and cvs.safe_pdf(app).read_bytes() in server.posts[0]["body"]
        else:
            assert not server.posts
            assert await page.locator("input").first.input_value() == ""
    finally:
        await page.close()
        result = await asyncio.wait_for(task, timeout=8)
    attempt = store.held_binding(result.id)
    assert attempt and store.outstanding_attempt(app.id).id == result.id
    assert attempt.pdf_sha256 == hashlib.sha256(cvs.safe_pdf(app).read_bytes()).hexdigest()
    if step != "experience":
        assert result.result.steps[-1].page_kind == ("email_verification" if step == "otp" else "account_login")
        return

    duplicate = make_job(profile, url=server.url("workday-information"), title="Different title", company="Different company")
    second = make_app(profile, duplicate, tmp_path, master_hash=master.content_hash)
    cvs.approve(second.id, second.current_rev)
    assert (await service.run(second.id, mode="supervised", trigger="user")).status == "blocked"

    for evidence in (None, "Next worked", CandidateHomeEvidence(attempt.nonce, WORKDAY_POSTING, "draft"),
                     CandidateHomeEvidence("wrong-nonce", WORKDAY_POSTING, "submitted"),
                     CandidateHomeEvidence(attempt.nonce, WORKDAY_POSTING.replace("JR123", "JR124"), "submitted"),
                     CandidateHomeEvidence(attempt.nonce, WORKDAY_POSTING.replace("demo", "other"), "submitted")):
        assert await service.reconcile_attempt(result.id, evidence=evidence) is None
    receipt = await service.reconcile_workday_submission(app.id, result.id, WORKDAY_POSTING)
    assert clean_receipt(receipt, attempt) and receipt.kind == "candidate_home_listing" and receipt.reference == "JR123"
    assert store.outstanding_attempt(app.id) is None and db.get_job(job.id).status == "applied"
    with db.connect() as conn:
        record = dict(conn.execute("SELECT * FROM apply_reservations WHERE run_id=?", (result.id,)).fetchone())
    assert record["receipt_kind"] == "candidate_home_listing" and record["receipt_outcome"] == "confirmed"
    assert json.loads(record["binding_json"])["requisition_id"] == "JR123" and WORKDAY_POSTING not in str(record)


@pytest.mark.parametrize("scenario", ["choose", "deny-id", "deny-text", "deny-role", "deny-tag", "consent", "autosave",
                                       "changed-options", "changed-action", "date-segment"])
async def test_workday_widget_selection(scenario, workday_registry, server, page, profile, tmp_path, fast, monkeypatch):
    facts = sample_facts()
    facts.facts.append(Fact(key="job_source", label="How did you hear about us?", value="Referral", exact=True))
    facts.facts.append(Fact(key="available_from", label="Available from", value="2027-08-19", exact=True))
    job = make_job(profile, url=server.url("workday-widgets"))
    app = make_app(profile, job, tmp_path, master_hash=save_confirmed(profile.id).content_hash)
    cvs.approve(app.id, app.current_rev)
    monkeypatch.setattr(service, "_candidate_facts", lambda *_: facts)

    class Pool(FakePool):
        async def goto(self, page, url):
            await super().goto(page, url)
            await page.evaluate("""scenario => {
              const source = document.querySelector('#source');
              if (scenario === 'deny-id') source.id = 'bottom-navigation-next-button';
              if (scenario === 'deny-text') source.textContent = 'Save and Continue';
              if (scenario === 'deny-role') source.setAttribute('role', 'button'), source.setAttribute('aria-haspopup', 'listbox');
              if (scenario === 'deny-tag') {
                const wrapper = document.createElement('button'); wrapper.type = 'button';
                source.before(wrapper); wrapper.append(source);
              }
              if (scenario === 'consent') source.setAttribute('aria-label', 'Consent to disclose my identity');
              if (scenario === 'changed-options') source.addEventListener('click', () => {
                document.querySelector('#sources [role=option]').textContent = 'Other';
              });
              if (scenario === 'changed-action') source.addEventListener('click', () => {
                source.closest('form').action = '/other-requisition';
              });
              if (scenario === 'date-segment') {
                document.querySelector('h1').textContent = 'Application Questions';
                source.setAttribute('data-automation-id', 'availableFrom-month');
                source.setAttribute('aria-label', 'Available from (month)');
                const [july, august] = document.querySelectorAll('#sources [role=option]');
                july.textContent = '07'; august.textContent = '08';
              }
              document.body.dataset.autosave = scenario === 'autosave' ? 'true' : 'false';
            }""", scenario)

    task = asyncio.create_task(service.run(app.id, mode="supervised", trigger="user", pool=Pool(page), verifier=FakeVerifier()))
    if scenario == "autosave":
        result = await asyncio.wait_for(task, 8)
        assert service.RECIPIENT_REFUSED in result.message and page.is_closed()
    else:
        try:
            await page.locator("aside").wait_for(timeout=6000)
            clicks = await page.locator("body").get_attribute("data-clicks")
            if scenario == "date-segment":
                assert await page.locator("#source").text_content() == "08"
        finally:
            await page.close()
            result = await asyncio.wait_for(task, 8)
        assert clicks == {"choose": "open choose", "date-segment": "open choose", "changed-options": "open",
                          "changed-action": "open"}.get(scenario, ""), result.result
    assert not server.posts and store.held_binding(result.id) is not None


PUBLICATION = "df29561e-226a-4524-ae13-2133e84bdbef"
OTHER_PUBLICATION = "df29561e-226a-4524-ae13-2133e84bdbff"
PUBLICATION_PATH = f"/oneclick-ui/company/FixtureCo/publication/{PUBLICATION}"
SMART_UPLOAD_PATH = smartrecruiters.UPLOAD_PATH.format(tenant="fixtureco", requisition=PUBLICATION)
SMART_URL = "https://jobs.smartrecruiters.com" + PUBLICATION_PATH


def test_smartrecruiters_live_step_identity():
    adapter = smartrecruiters.SmartRecruitersAdapter()
    reserved = adapter.resolve(SMART_URL, frozenset())
    for suffix in ("/profile", "/apply", "/apply/experience", "/application/education", "/questions",
                   "/application/attachments", "/review", "/application/submit"):
        assert adapter.resolve(SMART_URL + suffix, frozenset()) == reserved
        assert adapter.resolve(SMART_URL.replace(PUBLICATION, OTHER_PUBLICATION) + suffix, frozenset()).requisition_id == OTHER_PUBLICATION
    for suffix in ("/login", "/apply/anything", "/confirmation", "/attachments", "?jobId=other", "#/publication/other"):
        assert adapter.resolve(SMART_URL + suffix, frozenset()) is None


@pytest.fixture
def smart_adapter(server, monkeypatch):
    """Register the actual supervised adapter with only hostname/transport replaced for offline tests."""
    adapter = smartrecruiters.SmartRecruitersAdapter()
    original = smartrecruiters._identity

    def loopback_identity(url, **kwargs):
        if url.startswith(server.base + "/"):
            url = "https://jobs.smartrecruiters.com" + url[len(server.base):]
        return original(url, **kwargs)

    class LoopbackAdapter(smartrecruiters.SmartRecruitersAdapter):
        def resolve(self, url, dom_markers):
            real = "https://jobs.smartrecruiters.com" + url[len(server.base):] if url.startswith(server.base + "/") else url
            return super().resolve(real, dom_markers)

    registry = Registry()
    registry.register(LoopbackAdapter(), host="127.0.0.1")
    monkeypatch.setattr(service, "registry", registry)
    monkeypatch.setattr(smartrecruiters, "_identity", loopback_identity)
    server.get_pages[PUBLICATION_PATH] = "smartrecruiters_guest.html"
    server.get_pages[PUBLICATION_PATH + "/confirmation"] = "smartrecruiters_confirmation.html"
    return adapter


def test_smartrecruiters_production_registry_and_canonical_aliases():
    adapter = smartrecruiters.SmartRecruitersAdapter()
    advert = "https://jobs.smartrecruiters.com/FIXTURECO/744000123456789-synthetic-role"
    branded = "https://careers.fixture.test/offers/synthetic-role"
    match = production_registry.find(SMART_URL, None)
    assert match.capability == "unattended" and match.receipt_kind == "first_party_confirmation"
    assert match.requisition.tenant == "fixtureco" and match.requisition.requisition_id == PUBLICATION
    assert production_registry.find(advert, None).requisition.requisition_id == "744000123456789"
    assert adapter.resolve(advert, frozenset({smartrecruiters.same_job_marker(advert, SMART_URL)})).requisition_id == PUBLICATION
    assert not production_registry.find(branded, adapter.name, frozenset({SMART_URL}))
    assert production_registry.find(branded, adapter.name, frozenset({smartrecruiters.same_job_marker(branded, SMART_URL)})).requisition.requisition_id == PUBLICATION
    assert not adapter.resolve(branded, frozenset({smartrecruiters.same_job_marker(advert, SMART_URL)}))
    for url in (SMART_URL.replace("jobs.", "guest.eu."), SMART_URL.lower(), SMART_URL + "?utm_source=test"):
        assert production_registry.find(url, None).requisition.requisition_id == PUBLICATION
    for bad in (SMART_URL.replace("jobs.", ""), SMART_URL.replace("smartrecruiters.com", "smartrecruiters.com.evil.test"),
                SMART_URL.replace("jobs.smart", "fakesmart"), SMART_URL.replace(".com/", ".com:443/"),
                SMART_URL.replace("https:", "http:"), SMART_URL + "/extra", SMART_URL + "?jobid=other",
                SMART_URL.replace(PUBLICATION, "..%2F"), SMART_URL.replace("//", "//fixture@")):
        assert not production_registry.find(bad, None)


def smart_attempt(url=SMART_URL):
    return Attempt(1, 1, 1, "fake-digest", url, urlsplit(url).hostname, "fixture-nonce", "smartrecruiters",
                   "fixtureco", PUBLICATION)


@pytest.mark.parametrize("change", [None, "wrong-publication", "wrong-nonce", "wrong-tab", "wrong-origin", "parse-only", "bare-200"])
async def test_smartrecruiters_same_tab_receipt(smart_adapter, change, server, page):
    form = server.base + PUBLICATION_PATH
    await page.goto(form)
    confirmation_path = PUBLICATION_PATH + "/confirmation"
    server.post_pages[confirmation_path] = "smartrecruiters_confirmation.html"
    await page.locator("form").evaluate("(form, path) => { form.action = path; form.noValidate = true; }", confirmation_path)
    await page.get_by_role("button", name="Submit application").click()
    attempt = smart_attempt(form)
    evidence = smartrecruiters.SameTabConfirmation(attempt.nonce, "tab-a", "tab-a", form, page.url,
                                                   await page.locator("h1").inner_text())
    evidence = {
        "wrong-publication": replace(evidence, confirmation_url=evidence.confirmation_url.replace(PUBLICATION, OTHER_PUBLICATION)),
        "wrong-nonce": replace(evidence, observed_nonce="wrong"),
        "wrong-tab": replace(evidence, confirmation_tab="tab-b"),
        "wrong-origin": replace(evidence, confirmation_url=SMART_URL + "/confirmation"),
        "parse-only": replace(evidence, acknowledgment="CV uploaded successfully"),
        "bare-200": {"status": 200},
    }.get(change, evidence)
    receipt = await smart_adapter.verify_receipt(attempt, evidence=evidence)
    assert receipt.outcome == ("confirmed" if change is None else "uncertain")
    if change is None:
        assert (receipt.kind, receipt.observed_requisition, receipt.observed_nonce) == (
            "first_party_confirmation", PUBLICATION, attempt.nonce)
    assert len(server.posts_to(confirmation_path)) == 1


async def test_smartrecruiters_supplied_boundaries_and_exact_recipients():
    adapter = smartrecruiters.SmartRecruitersAdapter()
    attempt = smart_attempt()
    assert adapter.resolve(SMART_URL, frozenset()).write_origins == (smartrecruiters.UPLOAD_ORIGIN,)
    assert adapter.browser_request_allowed(attempt, smartrecruiters.UPLOAD_ORIGIN + SMART_UPLOAD_PATH, "POST", False)
    for url, nav in [(smartrecruiters.UPLOAD_ORIGIN + SMART_UPLOAD_PATH, True),
                     (smartrecruiters.UPLOAD_ORIGIN + "/attachments", False),
                     (smartrecruiters.UPLOAD_ORIGIN + "/publications/other/applications", False),
                     ("https://api.smartrecruiters.com.evil.test/attachments", False),
                     ("https://jobs.smartrecruiters.com/smart-profile/login", False),
                     ("https://jobs.smartrecruiters.com/%69mport/indeed", False),
                     ("https://jobs.smartrecruiters.com/import/linkedin", False)]:
        assert not adapter.browser_request_allowed(attempt, url, "POST", nav)
    assert adapter.boundaries["challenge"] == adapter.boundaries["final_submit"] == "pause_for_human"
    assert adapter.boundaries["import_profile"] == adapter.boundaries["login_otp"] == "forbidden"
    with pytest.raises(ValueError):
        Registry().register(adapter, host="jobs.smartrecruiters.com", unattended_tenants={"fixtureco": "first_party_confirmation"})


@pytest.mark.parametrize("choice", ["known", "outside-options", "scoped", "stale-scoped"])
async def test_smartrecruiters_supervised_fill_holds_and_dedupes(smart_adapter, choice, server, page, profile, tmp_path, fast, monkeypatch):
    master = save_confirmed(profile.id)
    url = server.base + PUBLICATION_PATH
    job = make_job(profile, url=url)
    app = make_app(profile, job, tmp_path, master_hash=master.content_hash)
    cvs.approve(app.id, app.current_rev)
    fake_values = {"first_name": "Camille", "last_name": "Fixture", "email": "fiction@example.test", "phone": "+33123456789",
                   "linkedin": "https://www.linkedin.com/in/synthetic", "city": "Toulouse", "school": "Fixture Academy"}
    facts = [Fact(key=key, label=key, value=value, exact=key in {"first_name", "last_name", "email", "phone", "linkedin"})
             for key, value in fake_values.items()]
    facts.extend([Fact(key="custom_1", label="Experience Employer", value="Fixture Labs"),
                  Fact(key="custom_2", label="Message to hiring manager", value="Synthetic cover message.")])
    if choice in {"known", "outside-options"}:
        facts.append(Fact(key="custom_3", label="Preferred working language",
                          value="French" if choice == "known" else "Klingon"))
    else:
        await page.goto(url)
        if choice == "stale-scoped":
            await page.locator('#language option[value="fr"]').evaluate("option => { option.textContent = 'Spanish'; }")
        snap, _ = await take_snapshot(page)
        field = next(field for field in snap.fields if field.name == "language")
        binding, indices = scoped_choices.field_choices(url, field)
        monkeypatch.setattr(scoped_choices, "load", lambda *_: {binding: indices[-1]})
        await page.goto("about:blank")
    monkeypatch.setattr(service, "_candidate_facts", lambda *_: FactSheet(facts=facts))
    pool = FakePool(page)
    assert (await service.run(app.id, mode="submit", trigger="user", pool=pool)).status == "blocked"
    assert not pool.opened
    task = asyncio.create_task(service.run(app.id, mode="supervised", trigger="user", pool=FakePool(page)))
    try:
        await page.locator("aside").wait_for(timeout=8000)
        assert store.active_reservations(profile.id)
        for form_id, key in {"first": "first_name", "last": "last_name", "email": "email", "phone": "phone",
                             "city": "city", "linkedin": "linkedin", "school": "school"}.items():
            assert await page.input_value("#" + form_id) == fake_values[key]
        assert await page.input_value("#employer") == "Fixture Labs"
        assert await page.input_value("#letter") == "Synthetic cover message."
        assert await page.locator("#cv").evaluate("e => e.files[0].name") == "CV.pdf"
        assert await page.input_value("#language") == ("fr" if choice in {"known", "scoped"} else "")
        assert not await page.is_checked("#privacy") and not await page.is_checked("#notice")
        assert not server.posts
    finally:
        await page.close()
        result = await asyncio.wait_for(task, timeout=8)
    assert store.outstanding_attempt(app.id).id == result.id
    duplicate = make_job(profile, url=url.lower() + "?utm_source=other", title="Other synthetic row", company="Other display name")
    second = make_app(profile, duplicate, tmp_path, master_hash=master.content_hash)
    cvs.approve(second.id, second.current_rev)
    pool = FakePool()
    assert (await service.run(second.id, mode="supervised", trigger="user", pool=pool)).status == "blocked"
    assert not pool.opened and not server.posts


@pytest.mark.parametrize("scenario", ["approved", "different-pdf", "before-upload", "foreign", "sign-in"])
async def test_smartrecruiters_browser_upload_boundary(smart_adapter, scenario, server, page, profile, tmp_path, fast, monkeypatch):
    upload = FixtureServer()
    foreign = FixtureServer()
    task = None
    try:
        monkeypatch.setattr(smartrecruiters, "UPLOAD_ORIGIN", upload.base)
        url = server.base + PUBLICATION_PATH
        target = (foreign if scenario == "foreign" else upload).base + SMART_UPLOAD_PATH
        if scenario == "sign-in":
            target = server.url("smart-profile/login")
        job = make_job(profile, url=url)
        app = make_app(profile, job, tmp_path, master_hash=save_confirmed(profile.id).content_hash)
        cvs.approve(app.id, app.current_rev)
        expected = cvs.safe_pdf(app).read_bytes()
        monkeypatch.setattr(service, "_candidate_facts", lambda *_: FactSheet())

        class UploadPool(FakePool):
            async def goto(self, page, url):
                await super().goto(page, url)
                await page.evaluate("""([target, scenario]) => {
                    const post = file => {
                        const body = new FormData();
                        body.append('resume', file, 'CV.pdf');
                        return fetch(target, {mode: 'no-cors', method: 'POST', body}).catch(() => {});
                    };
                    if (scenario === 'before-upload') return post(new Blob(['%PDF-1.4 fake one page'], {type: 'application/pdf'}));
                    document.querySelector('#cv').addEventListener('change', event => {
                        void post(scenario === 'different-pdf' ? new Blob(['%PDF-tampered'], {type: 'application/pdf'}) : event.target.files[0]);
                    });
                }""", [target, scenario])

        task = asyncio.create_task(service.run(app.id, mode="supervised", trigger="user", pool=UploadPool(page)))
        if scenario == "approved":
            await page.locator("aside").wait_for(timeout=8000)
            posts = upload.posts_to(SMART_UPLOAD_PATH)
            assert store.active_reservations(profile.id) and len(posts) == 1 and expected in posts[0]["body"]
            await page.close()
        result = await asyncio.wait_for(task, timeout=8)
        assert not foreign.requests and not foreign.posts and not server.posts
        if scenario != "approved":
            assert page.is_closed() and not upload.posts
        assert store.outstanding_attempt(app.id).id == result.id
    finally:
        if task is not None and not task.done():
            await page.close()
            await asyncio.wait_for(task, timeout=8)
        upload.close()
        foreign.close()


@pytest.mark.parametrize("platform", ["workday", "smart_adapter"])
@pytest.mark.parametrize("change", [None, "another-posting", "other-form-action", "same-posting-step", "autosave"])
async def test_adapter_fill_step_identity(platform, change, request, server, page, profile, tmp_path, fast, monkeypatch):
    from jobhunt.apply.errors import ApplyError
    from jobhunt.apply.supervised import CONFIRM_AGAIN, WRONG_POSTING

    workday = platform == "workday"
    request.getfixturevalue("workday_registry" if workday else platform)
    posting = urlsplit(WORKDAY_POSTING).path if workday else PUBLICATION_PATH
    req = "JR123" if workday else PUBLICATION
    other_req = "JR124" if workday else OTHER_PUBLICATION
    paths = [posting + suffix for suffix in (("/apply/myInformation", "/apply/myExperience", "/apply/applicationQuestions")
                                             if workday else ("/profile", "/experience", "/questions"))]
    facts = FactSheet(facts=[Fact(key="first_name", label="First name", value="Camille", exact=True),
                             Fact(key="job_source", label="How did you hear about us?", value="Referral", exact=True),
                             Fact(key="custom_1", label="Preferred working language", value="French", exact=True),
                             Fact(key="custom_2", label="My Experience Preferred working language", value="French", exact=True),
                             Fact(key="available_from", label="Available from", value="2027-08-19", exact=True)])
    monkeypatch.setattr(service, "_candidate_facts", lambda *_: facts)

    for index, (path, heading) in enumerate(zip(paths, ("My Information", "My Experience", "Application Questions"), strict=True)):
        marker, label, answer = (("sourcePrompt", "How did you hear about us?", "Referral") if index == 0 else
                                 ("availableFrom-month", "Available from (month)", "08")) if workday else (
                                     "language", "Preferred working language", "French")
        widget = f'''<div id="choice" data-automation-id="{marker}" role="combobox" aria-label="{label}"
                      aria-controls="options">Select</div>
                     <ul id="options" role="listbox" hidden><li role="option">{answer}</li><li role="option">Other</li></ul>
                     <script>
                      const control = document.querySelector('#choice'), options = document.querySelector('#options');
                      control.onclick = () => {{ options.hidden = false; }};
                      options.onclick = event => {{ control.textContent = event.target.textContent; options.hidden = true; }};
                     </script>'''
        controls = ('<label>First name<input id="name" data-automation-id="legalNameCountry-firstName"></label>' if not index else '')
        if index == 1 and workday:
            controls += '<label>Resume<input id="resume" type="file" data-automation-id="resumeUpload"></label>'
        else:
            controls += widget
        next_link = f'<a href="{paths[index + 1]}">Next manually</a>' if index < 2 else ''
        server.html[path] = (f'<!doctype html><section><h1>{heading}</h1><form method="post" action="{path}">{controls}'
                             '<button>Submit manually</button></form></section>' + next_link)
    server.html[posting] = f'<h1>Fixture role</h1><a href="{paths[0]}">Start manually</a>'

    job = make_job(profile, url=server.base + posting)
    app = make_app(profile, job, tmp_path, master_hash=save_confirmed(profile.id).content_hash)
    cvs.approve(app.id, app.current_rev)
    task = asyncio.create_task(service.run(app.id, mode="supervised", trigger="user", pool=FakePool(page), verifier=FakeVerifier()))
    try:
        await page.locator("aside").wait_for(timeout=6000)
        await page.get_by_text("Start manually").click()
        for index, path in enumerate(paths):
            current = await service.preview_fill_step(app.id)
            assert current.title == job.title and not current.requires_confirmation and current.requisition_id == req
            assert current.tenant == ("demo.wd3.myworkdayjobs.com/external" if workday else "fixtureco")
            attempt = store.held_binding(current.run_id)
            assert attempt and store.binding_active(attempt)
            if index == 1 and change == "another-posting":
                await page.evaluate("path => history.pushState({}, '', path)", path.replace(req, other_req))
                if workday:
                    result = await service.fill_step(app.id, current.token, run_id=attempt.run_id)
                    assert result.status == "blocked" and result.message == WRONG_POSTING
                else:
                    with pytest.raises(ApplyError, match="Destination non vérifiée"):
                        await service.fill_step(app.id, current.token, run_id=attempt.run_id)
                assert page.is_closed()
                break
            if index == 1 and change == "other-form-action":
                await page.locator("form").evaluate("(form, path) => { form.action = path; }", path.replace(req, other_req))
                result = await service.fill_step(app.id, current.token, run_id=attempt.run_id)
                assert result.status == "blocked" and page.is_closed()
                break
            if index == 1 and change == "same-posting-step":
                await page.evaluate("path => history.pushState({}, '', path)", paths[2])
                with pytest.raises(ApplyError, match=CONFIRM_AGAIN):
                    await service.fill_step(app.id, current.token, run_id=attempt.run_id)
                assert not page.is_closed()
                break
            if index == 2 and change == "autosave":
                await page.locator("#options").evaluate("list => list.addEventListener('click', () => {"
                                                        "void fetch('/undeclared-autosave', {method: 'POST', body: 'fixture'}); })")
                await service.fill_step(app.id, current.token, run_id=attempt.run_id)
                assert page.is_closed()
                break
            result = await service.fill_step(app.id, current.token, run_id=attempt.run_id)
            assert result.status not in ("blocked", "failed") and page.url == server.base + path, result
            if index == 0:
                assert await page.input_value("#name") == "Camille", result
            if index == 1 and workday:
                assert await page.locator("#resume").evaluate("input => input.files[0].name") == "CV.pdf"
            else:
                assert await page.locator("#choice").text_content() == (("Referral" if not index else "08") if workday else "French")
            assert store.held_binding(attempt.run_id) == attempt
            with pytest.raises(ApplyError, match=CONFIRM_AGAIN):
                await service.fill_step(app.id, current.token, run_id=attempt.run_id)
            if index < 2:
                await page.get_by_text("Next manually").click()
    finally:
        if not page.is_closed():
            await page.close()
        result = await asyncio.wait_for(task, timeout=8)
    assert not server.posts and store.outstanding_attempt(app.id).id == result.id
