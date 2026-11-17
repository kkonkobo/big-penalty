from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from jobhunt.fetch import firecrawl_html, get_html
from jobhunt.sources.base import SourceError


@pytest.mark.parametrize("url", ["file:///etc/passwd", "javascript:alert(1)", "data:text/html,hello", "ftp://host/job", "//host/job"])
async def test_fetch_refuses_non_http_urls_before_any_strategy(url):
    context = SimpleNamespace(browser=SimpleNamespace(page=AsyncMock()))
    with pytest.raises(SourceError, match="HTTP"):
        await get_html(url, context)
    with pytest.raises(SourceError, match="HTTP"):
        await firecrawl_html(url, "offline")
    context.browser.page.assert_not_called()


async def test_fetch_rejects_non_http_redirect(monkeypatch):
    requests = []
    client_type = httpx.AsyncClient

    def respond(request):
        requests.append(str(request.url))
        return httpx.Response(302, headers={"Location": "file:///etc/passwd"})

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client_type(
        **kwargs, transport=httpx.MockTransport(respond)))
    with pytest.raises(SourceError):
        await get_html("https://acme.test/jobs/1", SimpleNamespace(), allow_browser=False, allow_firecrawl=False)
    assert requests == ["https://acme.test/jobs/1"]
