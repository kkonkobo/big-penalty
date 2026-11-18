"""Test doubles for the apply engine: loopback fixture server, scripted planner, fictional candidate, browser fixture.

Everything is offline: pages come from tests/fixtures/apply/*.html served on 127.0.0.1; fictional data only.
"""

from __future__ import annotations

import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable

import pytest

from jobhunt.apply import engine
from jobhunt.apply.types import Action, Fact, FactSheet, PageSnapshot, StepLog, StepPlan

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
        act(snap, r"RGPD", "check", "consent_required"),
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
    def __init__(self):
        self.posts: list[dict] = []
        self.requests: list[str] = []
        self.sent: list[dict] = []          # EVERY request that carries candidate data: a GET with a query string, any POST
        self.post_pages: dict[str, str] = {}  # POST path -> fixture served in answer (default: confirmation.html)
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _html(self, name: str, status: int = 200):
                body = (FIXTURES / name).read_bytes()
                self.send_response(status)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                path = self.path.split("?")[0]
                outer.requests.append(self.path)
                if "?" in self.path:
                    outer.sent.append({"method": "GET", "path": self.path})
                if path == "/collect":
                    return self._html("confirmation.html")
                if path.startswith("/recaptcha/"):
                    return self._html("recaptcha_stub.html")
                name = path.strip("/") + ".html"
                if path != "/" and (FIXTURES / name).is_file():
                    return self._html(name)
                self.send_response(404)
                self.end_headers()

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n)
                outer.posts.append({"path": self.path, "body": body})
                outer.sent.append({"method": "POST", "path": self.path, "body": body})
                self._html(outer.post_pages.get(self.path.split("?")[0], "confirmation.html"))

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
