"""Test doubles for the apply engine: loopback fixture server, scripted planner, fictional candidate, browser fixture.

Everything is offline: pages come from tests/fixtures/apply/*.html served on 127.0.0.1; fictional data only.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

import pytest

from jobhunt.apply import engine
from jobhunt.apply.adapters import Attempt, Dispatch, Receipt, Registry, Requisition, WriteGate
from jobhunt.apply.errors import ApplyError
from jobhunt.apply.types import Action, Fact, FactSheet, PageSnapshot, RunResult, StepLog, StepPlan

FIXTURES = Path(__file__).parent / "fixtures" / "apply"
TEST_PASSWORD = "Hunter2-Test-Pw"
FACT_PASSWORD = "FACT-VALUE-NEVER-USED"


# ------------------------------------------------------------------ candidate
def sample_facts(**overrides: str | None) -> FactSheet:
    facts = [
        Fact(key="first_name", label="Prénom", value="Camille", exact=True),
        Fact(key="last_name", label="Nom", value="Durand", exact=True),
        Fact(key="email", label="E-mail", value="camille@example.test", exact=True),
        Fact(key="phone", label="Téléphone", value="+33 6 00 00 00 00", exact=True, variants=["0600000000", "06 00 00 00 00"]),
        Fact(key="situation", label="Situation recherchée", value="Alternance"),
        Fact(key="available_now", label="Disponible immédiatement", value="Oui"),
        Fact(key="city", label="Ville souhaitée", value="Toulouse"),
        Fact(key="consent_required", label="Consentement obligatoire", value="oui"),
        Fact(key="consent_optional", label="Consentement facultatif / ne souhaite pas répondre", value="non"),
        Fact(key="account_email", label="E-mail du compte", value="camille@example.test", exact=True),
        Fact(key="account_password", label="Mot de passe du compte", value=FACT_PASSWORD, exact=True, secret=True),
        Fact(key="salary", label="Prétentions salariales", value=None),
    ]
    for f in facts:
        if f.key in overrides:
            f.value = overrides[f.key]
    return FactSheet(facts=facts)


def password_for(host: str) -> str:
    return TEST_PASSWORD


# ------------------------------------------------------------------ planner
class ScriptedPlanner:
    """Deterministic planner: `steps` is a list of StepPlan | callable(snapshot) -> StepPlan, consumed in order (the last one
    repeats), or a single callable. Records every call."""

    def __init__(self, steps):
        self.steps = steps
        self.calls: list[dict] = []

    async def plan(self, snapshot: PageSnapshot, facts: FactSheet, *, job_title: str, company: str, history: list[StepLog],
                   mode: str) -> StepPlan:
        self.calls.append({"snapshot": snapshot, "history": list(history), "mode": mode, "facts": facts})
        if callable(self.steps):
            return self.steps(snapshot)
        step = self.steps[min(len(self.calls), len(self.steps)) - 1]
        return step(snapshot) if callable(step) else step


def ref_of(snap: PageSnapshot, label_re: str, kind: str | None = None) -> str:
    for f in snap.fields:
        if re.search(label_re, f.label, re.I) and (kind is None or f.kind == kind):
            return f.ref
    raise AssertionError(f"no field matching {label_re!r} in {[(f.kind, f.label) for f in snap.fields]}")


def act(snap: PageSnapshot, label_re: str, op: str, fact: str | None = None, value: str | None = None, role: str | None = None,
        kind: str | None = None) -> Action:
    return Action(ref=ref_of(snap, label_re, kind), op=op, fact=fact, value=value, click_role=role)  # type: ignore[arg-type]


def form_actions(snap: PageSnapshot, **extra) -> list[Action]:
    """Everything the one-page fixture form needs, as a good planner would answer it."""
    return [
        act(snap, r"^Prénom", "fill", "first_name"),
        act(snap, r"^Nom", "fill", "last_name"),
        act(snap, r"E-mail", "fill", "email"),
        act(snap, r"Téléphone", "fill", "phone"),
        act(snap, r"Situation", "select", "situation", "alternance"),
        act(snap, r"disponible", "select", "available_now", "Oui"),
        act(snap, r"Curriculum|CV", "upload_cv", kind="file"),
        act(snap, r"^Disponible immédiatement", "check", "available_now"),
    ]


def form_plan(final_op: str = "submit", label: str = r"Envoyer ma candidature", **kw) -> Callable[[PageSnapshot], StepPlan]:
    def plan(snap: PageSnapshot) -> StepPlan:
        return StepPlan(page_kind="form", actions=[*form_actions(snap), act(snap, label, "click", role=final_op)], **kw)
    return plan


def job_plan(snap: PageSnapshot) -> StepPlan:
    return StepPlan(page_kind="job_page", actions=[act(snap, r"Postuler", "click", role="open_form")])


CONFIRMED = StepPlan(page_kind="confirmation")


class FakeVerifier:
    """AnswerVerifier double: `rule(item) -> bool | str` decides (default: everything supported; a bool is read as supported /
    contradiction, a string is the verdict itself). Records every call (items in `calls`, the facts catalogue in `facts`)."""

    def __init__(self, rule: Callable | None = None, error: Exception | None = None):
        self.rule, self.error = rule, error
        self.calls: list[list] = []
        self.facts: list[list] = []

    async def check(self, items, facts=None):
        self.calls.append(list(items))
        self.facts.append(list(facts or []))
        if self.error:
            raise self.error
        return {i.ref: (self.rule(i) if self.rule else "supported") for i in items}


# ------------------------------------------------------------------ loopback server
class FixtureServer:
    """Serve static fixture HTML and synthetic receipts without any HTTP redirects."""

    def __init__(self):
        self.posts: list[dict] = []
        self.requests: list[str] = []
        self.sent: list[dict] = []          # EVERY request that carries candidate data: a GET with a query string, any POST
        self.post_pages: dict[str, str] = {}  # POST path -> fixture served in answer (default: confirmation.html)
        self.post_json: dict[str, dict] = {}
        self.get_pages: dict[str, str] = {}
        self.redirects: dict[str, str] = {}
        self.html: dict[str, str] = {}
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _html(self, name: str, status: int = 200, *, receipt: str = ""):
                body = (FIXTURES / name).read_bytes()
                self.send_response(status)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                if receipt:
                    self.send_header("X-Fixture-Receipt", receipt)
                    self.send_header("X-Fixture-Requisition", self.headers.get("X-Application-Requisition", ""))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                path = self.path.split("?")[0]
                outer.requests.append(self.path)
                if path in outer.redirects:
                    self.send_response(307)
                    self.send_header("Location", outer.redirects[path])
                    self.end_headers()
                    return
                if "?" in self.path:
                    outer.sent.append({"method": "GET", "path": self.path})
                if path in outer.html:
                    body = outer.html[path].encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if path == "/collect":
                    return self._html("confirmation.html")
                if path.startswith("/recaptcha/"):
                    return self._html("recaptcha_stub.html")
                name = outer.get_pages.get(path, path.strip("/") + ".html")
                if path != "/" and (FIXTURES / name).is_file():
                    return self._html(name)
                self.send_response(404)
                self.end_headers()

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n)
                outer.posts.append({"path": self.path, "body": body})
                outer.sent.append({"method": "POST", "path": self.path, "body": body})
                if self.path in outer.redirects:
                    self.send_response(307)
                    self.send_header("Location", outer.redirects[self.path])
                    self.end_headers()
                    return
                if self.path in outer.post_json:
                    body = json.dumps(outer.post_json[self.path]).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if self.path == "/adapter-contract/fixture-tenant/req-1" and body.startswith(b"%PDF-") and self.headers.get("X-Application-Attempt"):
                    self.send_response(200)
                    self.send_header("X-Fixture-Receipt", self.headers["X-Application-Attempt"])
                    self.send_header("X-Fixture-Requisition", "req-1")
                    self.end_headers()
                    self.wfile.write(b"<p>Candidature recue (fixture).</p>")
                    return
                name = outer.post_pages.get(self.path.split("?")[0], "confirmation.html")
                receipt = self.headers.get("X-Fixture-Nonce", "") if self.path == "/apply" and name == "confirmation.html" else ""
                self._html(name, receipt=receipt)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.base = f"http://127.0.0.1:{self.httpd.server_port}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def url(self, name: str) -> str:
        return f"{self.base}/{name}"

    def posts_to(self, path: str) -> list[dict]:
        return [p for p in self.posts if p["path"] == path]

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


class FixtureFlow(engine._Run):
    """Test-only consumer of the actual planner/fill/navigation implementation."""

    @property
    def _inspection_only(self):
        return False

    def _account_handoff(self):
        return self._end("ready", "Le site demande un compte, créé uniquement pour un vrai envoi : rien n'a été envoyé (simulation).")

    async def _execute(self, item, target, snapshot, log):
        boundary = "cv_upload_parse" if item.act.op.startswith("upload_") else "draft_write"
        self.write_gate._enter(boundary, self.url)
        return await super()._execute(item, target, snapshot, log)

    async def _click(self, target, *, recheck=None):
        boundary = "final_submit" if self.final_clicked else "draft_write"
        self.write_gate._enter(boundary, self.url)
        await super()._click(target, recheck=recheck)


class LoopbackFlowAdapter:
    """Rehearse or dispatch the shared browser flow on one fixture server, never a real ATS."""

    name = "loopback-browser-fixture"
    capability = "unattended"
    retry_rule = "never"
    boundaries = {"draft_write": "auto", "cv_upload_parse": "auto", "final_submit": "auto"}
    tenant = "browser-fixture"

    def __init__(self, server, page, *, capture_receipt=False, **options):
        self.server, self.page, self.options = server, page, options
        self.fixture = urlsplit(server.base)
        self.capture_receipt = capture_receipt

    def matches(self, url, dom_markers):
        parsed = urlsplit(url)
        return float(parsed.scheme == "http" and parsed.netloc == self.fixture.netloc and parsed.hostname == "127.0.0.1")

    def resolve(self, url, dom_markers):
        return Requisition(self.tenant, "fixture" + urlsplit(url).path) if self.matches(url, dom_markers) else None

    async def dispatch(self, request, gate):
        return await self._drive(request, gate, "submit")

    async def rehearse(self, request, gate):
        return await self._drive(request, gate, "dry_run")

    async def verify_receipt(self, attempt, *, evidence=None):
        if isinstance(evidence, tuple) and (attempt.nonce, attempt.requisition_id) in evidence[1]:
            return Receipt(attempt, "confirmed", "first_party_confirmation", "fixture-apply", attempt.nonce, attempt.requisition_id)
        return Receipt(attempt, "uncertain")

    async def _drive(self, request, gate, mode):
        url = request.attempt.url
        if not self.matches(url, frozenset()) or hashlib.sha256(request.pdf).hexdigest() != request.attempt.pdf_sha256:
            raise ApplyError("Fixture destination or PDF mismatch")
        page, options = self.page, self.options.copy()
        aliases = {self.fixture.netloc}
        if not self.capture_receipt:
            aliases.update({f"localhost:{self.fixture.port}", f"thirdparty.test:{self.fixture.port}"})
        else:
            options["uploads"] = replace(options["uploads"], cv_bytes=request.pdf)
        receipt_nonces = []

        def observe(response):
            if response.url == self.server.url("apply") and response.request.method == "POST":
                receipt_nonces.append((response.headers.get("x-fixture-receipt"), response.headers.get("x-fixture-requisition")))

        async def local_only(route, browser_request):
            target = urlsplit(browser_request.url)
            if target.scheme != "http" or target.netloc not in aliases:
                await route.abort()
            elif target.hostname == "thirdparty.test":
                if browser_request.method not in {"GET", "HEAD"}:
                    await route.abort()
                    return
                response = await route.fetch(url=self.server.base + target.path, max_redirects=0)
                await route.fulfill(response=response)
            elif self.capture_receipt:
                headers = {key: value for key, value in browser_request.headers.items() if key != "x-fixture-nonce"}
                if gate.final_called and not gate.stopped and browser_request.method == "POST" and target.path == "/apply":
                    headers["x-fixture-nonce"] = request.attempt.nonce
                    headers["x-application-requisition"] = request.attempt.requisition_id or ""
                await route.continue_(headers=headers)
            else:
                await route.continue_()

        allowed = options.pop("allowed_hosts", None)
        if allowed is not None and "host_allowed" not in options:
            options["host_allowed"] = lambda host: host.lower() in {name.lower() for name in allowed}
        for name in ("before_submit", "on_submit_click", "on_nav_click", "verifier", "host_allowed", "goto", "password"):
            options.setdefault(name, None)
        if mode == "submit" and options["before_submit"] is None:
            return RunResult(status="blocked", message="Envoi bloqué : garde-fou d'envoi absent.")
        flow = FixtureFlow(page, url=url, mode=mode, **options)
        flow.write_gate = gate
        await page.context.route("**/*", local_only)
        if self.capture_receipt:
            page.on("response", observe)
        try:
            result = await flow._run_page()
            return (result, receipt_nonces) if self.capture_receipt else result
        finally:
            if self.capture_receipt:
                page.remove_listener("response", observe)
            await page.context.unroute("**/*", local_only)


async def exercise_fixture_flow(page, server, *, url, mode, uploads, **options):
    adapter = LoopbackFlowAdapter(server, page, uploads=uploads, **options)
    registry = Registry()
    registry.register(adapter, host="127.0.0.1", unattended_tenants={adapter.tenant: "first_party_confirmation"})
    match = registry.find(url, None)
    assert match and match.capability == "unattended"
    pdf = uploads.cv_pdf.read_bytes()
    attempt = Attempt(1, 1, 1, hashlib.sha256(pdf).hexdigest(), url, "127.0.0.1", "fixture", adapter.name,
                      adapter.tenant, match.requisition.requisition_id)

    def recheck():
        if registry.find(url, None) != match:
            raise ApplyError("Fixture adapter is no longer registered")

    gate = WriteGate(capability="unattended" if mode == "submit" else "supervised", policies=adapter.boundaries,
                     origins=frozenset({server.base}), recheck=recheck)
    run = adapter.dispatch if mode == "submit" else adapter.rehearse
    return await run(Dispatch(attempt, pdf), gate)


# "thirdparty.test" is another SITE (a widget / an untrusted destination) that resolves to the loopback fixture server
HOST_RULES = ["--host-resolver-rules=MAP thirdparty.test 127.0.0.1",
              "--disable-features=LocalNetworkAccessChecks,BlockInsecurePrivateNetworkRequests,PrivateNetworkAccessSendPreflights"]


# ------------------------------------------------------------------ fixtures (import into test modules)
@pytest.fixture
def server():
    s = FixtureServer()
    yield s
    s.close()


@pytest.fixture
async def page():
    """A headless Chromium page, or skip when no browser is installed."""
    from patchright.async_api import async_playwright

    async with async_playwright() as pw:
        try:
            browser = await pw.chromium.launch(headless=True, args=HOST_RULES)
        except Exception:
            try:
                browser = await pw.chromium.launch(headless=True, channel="chrome", args=HOST_RULES)
            except Exception:
                pytest.skip("no Chromium/Chrome available")
        ctx = await browser.new_context(locale="fr-FR")
        p = await ctx.new_page()
        yield p
        await browser.close()


@pytest.fixture(autouse=False)
def fast(monkeypatch):
    """No human-like pauses in tests."""
    monkeypatch.setenv("JOBHUNT_APPLY_LOCAL_SCREENSHOTS", "1")
    async def nop(*a, **k):
        return None
    monkeypatch.setattr(engine, "human_pause", nop)
    monkeypatch.setattr(engine, "IDLE_TIMEOUT_MS", 100)
    monkeypatch.setattr(engine, "CLICK_WAIT_S", 0.15)
    monkeypatch.setattr(engine, "TYPE_DELAY_MS", (0, 1))
    # tests inspect the final DOM of the page after a dry run: the end-of-run about:blank release is switched off here and
    # exercised on purpose by the tests that use the `release` fixture
    monkeypatch.setattr(engine, "RELEASE_PAGES", False)
    monkeypatch.setattr(engine, "SPLIT_UPLOADS", False)  # scripted plans describe one step per page
    monkeypatch.setattr(engine, "UPLOAD_SETTLE_S", 0)
    monkeypatch.setattr(engine, "OBSERVE_S", 0.5)
    monkeypatch.setattr(engine, "OBSERVE_POLL_S", 0.1)


@pytest.fixture
def cv_pdf(tmp_path) -> Path:
    p = tmp_path / "cv-camille-durand.pdf"
    p.write_bytes(b"%PDF-1.4\n% fictional test CV\n%%EOF\n")
    return p


@pytest.fixture
def release(monkeypatch):
    """Real end-of-run release of the pages (dry run), like in production."""
    monkeypatch.setattr(engine, "RELEASE_PAGES", True)
