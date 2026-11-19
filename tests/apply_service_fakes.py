"""Doubles for the apply orchestration tests: a tiny browser pool and an engine stub (fictional data only)."""

from __future__ import annotations

from contextlib import asynccontextmanager
from urllib.parse import urlsplit

from jobhunt.apply import service
from jobhunt.apply import store as astore
from jobhunt.apply.adapters import Receipt, Registry, Requisition
from jobhunt.apply.errors import ApplyError
from jobhunt.apply.types import RunResult, StepLog


class FakePool:
    """Quacks like BrowserPool: `page(...)` async context manager + `goto`; records what the service asked for."""

    def __init__(self, page=None, goto_error: Exception | None = None):
        self._page, self.goto_error = page if page is not None else ClosedPage(), goto_error
        self.opened: list[tuple[int, str, bool | None]] = []
        self.closed = False

    @asynccontextmanager
    async def page(self, profile_id: int, site: str, headless: bool | None = None):
        self.opened.append((profile_id, site, headless))
        yield self._page

    async def goto(self, page, url: str, **kw) -> None:
        if self.goto_error:
            await page.close()
            raise self.goto_error
        await page.goto(url, wait_until="domcontentloaded")

    async def close(self) -> None:
        self.closed = True


class ClosedPage:
    def is_closed(self):
        return True


def register_fixture(monkeypatch, adapter, *hosts):
    registry = Registry()
    for host in hosts:
        registry.register(adapter, host=host, unattended_tenants={adapter.tenant: "first_party_confirmation"})
    monkeypatch.setattr(service, "registry", registry)


class LedgerAdapter:
    """No browser or transport: exercise genuine reservation and settlement with scripted boundary operations."""

    name = "ledger-fixture"
    tenant = "synthetic"
    capability = "unattended"
    retry_rule = "never"
    boundaries = {"draft_write": "auto", "final_submit": "auto"}

    def __init__(self, result, hook, raises, confirm, calls):
        self.result, self.hook, self.raises, self.confirm, self.calls = result, hook, raises, confirm, calls
        self.received = set()

    def matches(self, url, dom_markers):
        return float(urlsplit(url).hostname in {"careers.exemple.test", "careers.zeta.test"})

    def resolve(self, url, dom_markers):
        parsed = urlsplit(url)
        return Requisition(self.tenant, parsed.netloc + parsed.path) if self.matches(url, dom_markers) else None

    async def dispatch(self, request, gate):
        attempt = request.attempt

        async def noop():
            pass

        async def before_submit():
            try:
                await gate.perform("draft_write", attempt.url, noop)
            except ApplyError as exc:
                return [str(exc)]
            return []

        async def submit():
            async def accept():
                if self.confirm:
                    self.received.add(attempt.nonce)
            await gate.perform("final_submit", attempt.url, accept)

        async def progress(value):
            astore.save_progress(attempt.run_id, value)

        kw = {"url": attempt.url, "before_submit": before_submit, "on_submit_click": submit,
              "on_nav_click": lambda: gate.perform("draft_write", attempt.url, noop), "on_progress": progress}
        self.calls.append(kw)
        if self.hook:
            await self.hook(kw)
        if self.raises:
            raise self.raises
        if self.confirm and not gate.final_called:
            await submit()
        if self.result:
            await progress(self.result)

    async def verify_receipt(self, attempt, *, evidence=None):
        if attempt.nonce in self.received and self.confirm:
            return Receipt(attempt, "confirmed", "first_party_confirmation", "synthetic-ledger")
        return Receipt(attempt, "uncertain")


def stub_adapter(monkeypatch, result=None, *, hook=None, raises=None, confirm=False):
    calls = []
    register_fixture(monkeypatch, LedgerAdapter(result, hook, raises, confirm, calls), "careers.exemple.test", "careers.zeta.test")
    return calls


def stub_engine(monkeypatch, result: RunResult | None = None, *, raises: Exception | None = None, hook=None) -> list[dict]:
    """Replace `run_application` in the service; returns the list of its keyword arguments (one dict per call)."""
    calls: list[dict] = []

    async def fake(page, **kw):
        calls.append(kw)
        if hook:
            await hook(kw)
        if raises:
            raise raises
        return result or RunResult(status="ready", message="ok", steps=[StepLog(index=1, url=kw["url"], page_kind="form")])

    monkeypatch.setattr(service, "run_application", fake)
    return calls
