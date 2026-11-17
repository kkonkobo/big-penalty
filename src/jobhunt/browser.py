"""Real-browser access for boards that block plain HTTP.

Design (personal, low-volume use):
- patchright (Playwright fork that removes common automation tells) driving the installed Google Chrome.
- One persistent user-data dir per (profile, site) under data/browser-profiles/, so cookies, logins and
  bot-protection clearance tokens persist like a normal person's browser.
- Headful by default. When a CAPTCHA / challenge page shows up we do NOT solve it automatically: we
  wait for the human to solve it in the window (up to `challenge_timeout_s`), then continue.
- Per-domain pacing with jitter, gentle scrolling, small randomized pauses.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator
from urllib.parse import urlparse

from patchright.async_api import BrowserContext, Page, Playwright, async_playwright

from jobhunt.config import Settings, get_settings
from jobhunt.sources.base import ChallengeRequired

log = logging.getLogger("jobhunt.browser")

CHALLENGE_MARKERS = (
    "captcha-delivery.com",  # DataDome
    "geo.captcha-delivery",
    "challenges.cloudflare.com",
    "cf-chl",
    "Just a moment...",
    "Vérification que vous êtes humain",
    "Verify you are human",
    "px-captcha",  # PerimeterX / HUMAN
    # NOTE: no generic "recaptcha/api" / "hcaptcha.com" markers: many normal pages embed those
    # widgets in forms (e.g. Cadremploi), which caused false challenge detection.
    "/checkpoint/challenge",  # LinkedIn
    "authwall",  # LinkedIn logged-out wall
)


class DomainPacer:
    """Ensures at least a random [min,max] delay between two hits on the same domain."""

    def __init__(self, min_s: float, max_s: float):
        self.min_s, self.max_s = min_s, max_s
        self._last: dict[str, float] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def wait(self, url: str) -> None:
        dom = urlparse(url).netloc
        lock = self._locks.setdefault(dom, asyncio.Lock())
        async with lock:
            gap = random.uniform(self.min_s, self.max_s)
            elapsed = time.monotonic() - self._last.get(dom, 0)
            if elapsed < gap:
                await asyncio.sleep(gap - elapsed)
            self._last[dom] = time.monotonic()


class BrowserPool:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self.pacer = DomainPacer(self.settings.min_delay_s, self.settings.max_delay_s)
        self._pw: Playwright | None = None
        self._contexts: dict[tuple[int, str], BrowserContext] = {}
        self._lock = asyncio.Lock()

    def has_session(self, profile_id: int, site: str) -> bool:
        if (profile_id, site) in self._contexts:
            return True
        root = self.settings.browser_profiles_dir / str(profile_id) / site / "Default"
        return any((root / path).is_file() for path in ("Cookies", "Network/Cookies"))

    def profile_dir(self, profile_id: int, site: str) -> Path:
        d = self.settings.browser_profiles_dir / str(profile_id) / site
        d.mkdir(parents=True, exist_ok=True)
        return d

    async def context(self, profile_id: int, site: str, headless: bool | None = None) -> BrowserContext:
        async with self._lock:
            key = (profile_id, site)
            if key in self._contexts:
                return self._contexts[key]
            if self._pw is None:
                self._pw = await async_playwright().start()
            headless = self.settings.headless if headless is None else headless
            kwargs = dict(
                user_data_dir=str(self.profile_dir(profile_id, site)),
                headless=headless,
                no_viewport=True,  # real window size, like a human
                locale="fr-FR",
                timezone_id="Europe/Paris",
            )
            try:
                ctx = await self._pw.chromium.launch_persistent_context(channel="chrome", **kwargs)
            except Exception as e:  # Chrome not installed -> bundled Chromium
                log.info("Chrome channel unavailable (%s); using bundled Chromium", e)
                ctx = await self._pw.chromium.launch_persistent_context(**kwargs)
            self._contexts[key] = ctx
            return ctx

    @asynccontextmanager
    async def page(self, profile_id: int, site: str, headless: bool | None = None) -> AsyncIterator[Page]:
        ctx = await self.context(profile_id, site, headless)
        page = await ctx.new_page()
        try:
            yield page
        finally:
            await page.close()

    async def goto(self, page: Page, url: str, *, challenge_timeout_s: float = 180, wait_until: str = "domcontentloaded") -> None:
        """Paced navigation + challenge handling. Raises ChallengeRequired if a human didn't clear it."""
        await self.pacer.wait(url)
        await page.goto(url, wait_until=wait_until, timeout=60_000)
        await human_pause(0.6, 1.8)
        if await is_challenge(page):
            if self.settings.headless:
                raise ChallengeRequired(f"bot challenge on {url}; rerun with JOBHUNT_HEADLESS=0 and solve it")
            log.warning("Challenge on %s — solve it in the browser window (waiting %ss)", url, challenge_timeout_s)
            deadline = time.monotonic() + challenge_timeout_s
            while time.monotonic() < deadline:
                await asyncio.sleep(2)
                if not await is_challenge(page):
                    await human_pause(1, 2)
                    return
            raise ChallengeRequired(f"challenge on {url} not solved within {challenge_timeout_s}s")

    async def close(self) -> None:
        for ctx in self._contexts.values():
            try:
                await ctx.close()
            except Exception:
                pass
        self._contexts.clear()
        if self._pw:
            await self._pw.stop()
            self._pw = None


async def is_challenge(page: Page) -> bool:
    try:
        url = page.url
        html = await page.content()
    except Exception:
        return False
    hay = url + html[:200_000]
    return any(m in hay for m in CHALLENGE_MARKERS)


async def human_pause(lo: float = 0.4, hi: float = 1.4) -> None:
    await asyncio.sleep(random.uniform(lo, hi))


async def human_scroll(page: Page, steps: int = 6) -> None:
    """Scroll down in uneven increments (triggers lazy-loaded lists)."""
    for _ in range(steps):
        await page.mouse.wheel(0, random.randint(350, 900))
        await human_pause(0.3, 1.1)
