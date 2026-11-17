"""Source connector contract.

To add a board: create `jobhunt/sources/<name>.py` with one subclass of `Source`. It is picked up
automatically by `jobhunt.sources.registry()` — do not edit any shared file to register it.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, ClassVar, Literal

import httpx

from jobhunt.config import Settings, get_settings
from jobhunt.models import ContractType, JobPosting, SearchQuery

if TYPE_CHECKING:
    from jobhunt.browser import BrowserPool

FR_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "fr-FR,fr;q=0.9,en-US;q=0.6,en;q=0.5",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/json;q=0.8,*/*;q=0.7",
}


class SourceError(Exception):
    """Recoverable failure of one source; the pipeline logs it and continues with other sources."""


class ChallengeRequired(SourceError):
    """Bot challenge / CAPTCHA that needs a human (run headful, solve it in the window)."""


class LoginRequired(SourceError):
    """Board needs a logged-in session: run `jobhunt login <source> --profile <id>` once."""


@dataclass
class SourceContext:
    profile_id: int
    settings: Settings = field(default_factory=get_settings)
    browser: "BrowserPool | None" = None  # lazily provided by the pipeline for kind="browser" sources
    log: logging.Logger = field(default_factory=lambda: logging.getLogger("jobhunt.source"))

    def http(self, **kw) -> httpx.AsyncClient:
        """Async HTTP client with French browser-like headers. Use as `async with ctx.http() as c:`."""
        headers = {**FR_HEADERS, **kw.pop("headers", {})}
        return httpx.AsyncClient(
            headers=headers, timeout=kw.pop("timeout", 30), follow_redirects=True, http2=False, **kw
        )


class Source(ABC):
    name: ClassVar[str]  # short id, e.g. "wttj"
    label: ClassVar[str]  # display name, e.g. "Welcome to the Jungle"
    kind: ClassVar[Literal["api", "http", "browser"]]
    # Aggregators are discovery-only: postings are resolved to the company's own site before applying.
    is_aggregator: ClassVar[bool] = False
    domains: ClassVar[tuple[str, ...]] = ()
    detail_delay_s: ClassVar[float] = 1.0
    requires_login: ClassVar[bool] = False
    requires_credentials: ClassVar[tuple[str, ...]] = ()  # Settings attribute names, e.g. ("firecrawl_api_key",)
    # Contract types this board carries; None = all. The pipeline skips queries that can't match.
    contract_types: ClassVar[frozenset[ContractType] | None] = None
    enabled_by_default: ClassVar[bool] = True

    def is_configured(self, settings: Settings) -> bool:
        return all(getattr(settings, a, None) for a in self.requires_credentials)

    @abstractmethod
    async def search(self, query: SearchQuery, ctx: SourceContext) -> list[JobPosting]:
        """Return up to query.max_results postings, newest first when the board allows it.

        Raise SourceError (or a subclass) for expected failures. Never raise for a single bad item:
        skip it and log at debug level.
        """

    async def fetch_details(self, posting: JobPosting, ctx: SourceContext) -> JobPosting:
        """Fill description/apply_url etc. for a posting from search(). Default: return as-is."""
        return posting
