"""BrowserPool.context: apply-* contexts block service workers (a dry run's request guard cannot be bypassed). No browser."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from jobhunt import browser
from jobhunt.config import get_settings


class FakeChromium:
    def __init__(self):
        self.calls: list[dict] = []

    async def launch_persistent_context(self, **kw):
        self.calls.append(kw)
        return SimpleNamespace(close=lambda: asyncio.sleep(0))


@pytest.fixture
def pool(monkeypatch):
    chromium = FakeChromium()
    p = browser.BrowserPool(get_settings(), headless=True)
    p._pw = SimpleNamespace(chromium=chromium)
    p.chromium = chromium
    return p


async def test_apply_contexts_block_service_workers(pool):
    await pool.context(1, "apply-exemple.test")
    assert pool.chromium.calls[-1]["service_workers"] == "block"


async def test_dry_run_contexts_block_service_workers_too(pool):
    await pool.context(1, "applydry-exemple.test")
    assert pool.chromium.calls[-1]["service_workers"] == "block"


async def test_other_contexts_keep_the_default(pool):
    await pool.context(1, "linkedin")
    assert "service_workers" not in pool.chromium.calls[-1]
