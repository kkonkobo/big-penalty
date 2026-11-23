"""BrowserPool.context: apply-* contexts block service workers (a dry run's request guard cannot be bypassed). No browser."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from jobhunt import browser
from jobhunt.config import get_settings


class FakeChromium:
    def __init__(self):
        self.calls: list[dict] = []
        self.bounds = dict(left=458, top=1480, width=1280, height=990, windowState="normal")
        self.closed = False

    async def launch_persistent_context(self, **kw):
        self.calls.append(kw)
        return SimpleNamespace(pages=[object()], new_cdp_session=self.new_cdp_session, close=self.close)

    async def new_cdp_session(self, page):
        return self

    async def send(self, method):
        assert method == "Browser.getWindowForTarget"
        return {"bounds": self.bounds}

    async def detach(self):
        pass

    async def close(self):
        self.closed = True


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


async def test_forced_headless_wins_over_context_override(pool, monkeypatch):
    monkeypatch.setenv("JOBHUNT_FORCE_HEADLESS", "1")
    await pool.context(1, "apply-supervised-example.test", headless=False)
    assert pool.chromium.calls[-1]["headless"] is True


@pytest.fixture
def mac_display(monkeypatch):
    monkeypatch.setattr(browser.sys, "platform", "darwin")
    monkeypatch.delenv("JOBHUNT_FORCE_HEADLESS", raising=False)
    monkeypatch.setattr(browser, "_builtin_display", lambda: (438, 1440, 1710, 1070))


async def test_visible_apply_window_ignores_external_position_override(pool, mac_display, monkeypatch):
    monkeypatch.setenv("JOBHUNT_WINDOW_SCREEN", "any")
    monkeypatch.setenv("JOBHUNT_WINDOW_POSITION", "0,0")
    await pool.context(1, "apply-auto-exemple.test", headless=False)
    args = pool.chromium.calls[-1]["args"]
    assert "--window-position=458,1480" in args
    assert "--window-size=1280,990" in args
    assert "--window-position=0,0" not in args
    await pool.context(1, "linkedin", headless=False)
    assert "--window-position=0,0" in pool.chromium.calls[-1]["args"]


async def test_visible_apply_does_not_launch_without_builtin_display(pool, mac_display, monkeypatch):
    monkeypatch.setattr(browser, "_builtin_display", lambda: None)
    with pytest.raises(browser.BuiltinDisplayUnavailable):
        await pool.context(1, "apply-auto-exemple.test", headless=False)
    assert pool.chromium.calls == []
    await pool.context(1, "apply-auto-exemple.test", headless=True)
    assert pool.chromium.calls[-1]["headless"] is True


@pytest.mark.parametrize("top", [0, 1439])
async def test_visible_apply_closes_window_outside_builtin_display(pool, mac_display, top):
    pool.chromium.bounds["top"] = top
    with pytest.raises(browser.BuiltinDisplayUnavailable):
        await pool.context(1, "apply-auto-exemple.test", headless=False)
    assert pool.chromium.closed
    assert not pool._contexts
