"""Doubles for the apply orchestration tests: a tiny browser pool and an engine stub (fictional data only)."""

from __future__ import annotations

from contextlib import asynccontextmanager

from jobhunt.apply import service
from jobhunt.apply.types import RunResult, StepLog


class FakePool:
    """Quacks like BrowserPool: `page(...)` async context manager + `goto`; records what the service asked for."""

    def __init__(self, page=None, goto_error: Exception | None = None):
        self._page, self.goto_error = page, goto_error
        self.opened: list[tuple[int, str, bool | None]] = []
        self.closed = False

    @asynccontextmanager
    async def page(self, profile_id: int, site: str, headless: bool | None = None):
        self.opened.append((profile_id, site, headless))
        yield self._page

    async def goto(self, page, url: str, **kw) -> None:
        if self.goto_error:
            raise self.goto_error
        await page.goto(url, wait_until="domcontentloaded")

    async def close(self) -> None:
        self.closed = True


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
