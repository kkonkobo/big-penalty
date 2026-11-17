"""Fetch a page's HTML with escalating strategies: plain HTTP -> real browser -> Firecrawl."""

from __future__ import annotations

import asyncio
import logging
from urllib.parse import urlparse

from jobhunt.browser import CHALLENGE_MARKERS
from jobhunt.sources.base import FR_HEADERS, SourceContext, SourceError

log = logging.getLogger("jobhunt.fetch")


def is_http_url(url: str | None) -> bool:
    try:
        parsed = urlparse(url or "")
        return parsed.scheme.lower() in ("http", "https") and bool(parsed.hostname)
    except ValueError:
        return False


async def require_http_request(request) -> None:
    if not is_http_url(str(request.url)):
        raise SourceError("Only HTTP(S) URLs may be fetched")


def looks_blocked(status: int, text: str) -> bool:
    return status in (401, 403, 429, 503) or any(m in text[:200_000] for m in CHALLENGE_MARKERS)


async def get_html(url: str, ctx: SourceContext, *, site: str = "generic", allow_browser: bool = True, allow_firecrawl: bool = True) -> str:
    import httpx

    if not is_http_url(url):
        raise SourceError("Only HTTP(S) URLs may be fetched")
    try:
        async with httpx.AsyncClient(headers=FR_HEADERS, follow_redirects=True, timeout=30,
                                    event_hooks={"request": [require_http_request]}) as c:
            r = await c.get(url)
        if not looks_blocked(r.status_code, r.text) and r.status_code < 400:
            return r.text
        log.debug("http blocked for %s (%s)", url, r.status_code)
    except httpx.HTTPError as e:
        log.debug("http error for %s: %s", url, e)

    if allow_browser and ctx.browser is not None:
        try:
            async with ctx.browser.page(ctx.profile_id, site) as page:
                await ctx.browser.goto(page, url)
                return await page.content()
        except SourceError as e:
            log.info("browser failed for %s: %s", url, e)

    if allow_firecrawl and ctx.settings.firecrawl_api_key:
        return await firecrawl_html(url, ctx.settings.firecrawl_api_key)

    raise SourceError(f"could not fetch {url}")


async def firecrawl_html(url: str, api_key: str) -> str:
    if not is_http_url(url):
        raise SourceError("Only HTTP(S) URLs may be fetched")
    from firecrawl import Firecrawl

    def _run() -> str:
        doc = Firecrawl(api_key=api_key).scrape(url, formats=["html"], location={"country": "FR", "languages": ["fr-FR"]})
        return getattr(doc, "html", None) or ""

    html = await asyncio.to_thread(_run)
    if not html:
        raise SourceError(f"firecrawl returned nothing for {url}")
    return html
