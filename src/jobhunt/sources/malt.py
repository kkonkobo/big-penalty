"""Malt (malt.fr): freelance marketplace.

Findings (investigated 2026-11):
- Malt is a marketplace where *clients* search *freelancer profiles*. There is NO public list of client
  missions/projects: https://www.malt.fr/s?... and every other malt.fr page sit behind a Cloudflare
  interactive challenge ("Just a moment...") that even a headless real Chrome does not pass, and the search
  pages list freelancers, not jobs.
- Missions only reach a freelancer through his logged-in space (Malt "opportunités" / project invitations and
  the messaging inbox). That space is not scrapable without an authenticated session.

So this connector is a best-effort, **disabled by default**, logged-in browser scraper: run
`jobhunt login malt` once (headful, solve the Cloudflare check and sign in as a freelancer), then it opens the
freelancer opportunities pages in that persistent profile and collects anything that looks like a mission link.
The exact page/markup of the freelancer feed could not be verified without an account, so when nothing
recognisable is found it raises SourceError with an explicit message instead of returning silently empty results.
Applying is always on the platform (apply_method="platform").
"""

from __future__ import annotations

import re
from urllib.parse import urljoin

from jobhunt.models import ContractType, JobPosting, SearchQuery
from jobhunt.sources.base import LoginRequired, Source, SourceContext, SourceError

BASE = "https://www.malt.fr"
# Candidate freelancer-side pages that list missions / opportunities (tried in order).
FEED_URLS = (
    f"{BASE}/dashboard/freelancer/opportunities",
    f"{BASE}/messages",
)
MISSION_LINK = re.compile(r"/(?:project|projects|opportunit(?:y|ies|es)|mission|missions)/[\w-]{4,}")


class MaltSource(Source):
    name = "malt"
    label = "Malt (missions, session freelance)"
    kind = "browser"
    requires_login = True
    login_url = f"{BASE}/login"
    contract_types = frozenset({ContractType.FREELANCE})
    enabled_by_default = False  # no public mission listing exists; needs a freelancer account (see module docstring)

    async def search(self, query: SearchQuery, ctx: SourceContext) -> list[JobPosting]:
        if ctx.browser is None:
            raise SourceError("malt: needs the browser pool (logged-in freelancer session)")
        found: dict[str, JobPosting] = {}
        async with ctx.browser.page(ctx.profile_id, "malt") as page:
            for url in FEED_URLS:
                await ctx.browser.goto(page, url)  # raises ChallengeRequired if a human did not clear Cloudflare
                cur = page.url
                if re.search(r"/(login|signin|sign-in|auth)", cur):
                    raise LoginRequired("malt: not logged in; run `jobhunt login malt` (freelancer account)")
                anchors = await page.eval_on_selector_all(
                    "a[href]", "els => els.map(e => [e.href, (e.innerText || '').trim()])"
                )
                for href, text in anchors:
                    if not MISSION_LINK.search(href) or len(text) < 6:
                        continue
                    title = text.splitlines()[0].strip()
                    if query.keywords and not any(w in text.lower() for w in query.keywords.lower().split() if len(w) > 3):
                        continue
                    sid = MISSION_LINK.search(href).group(0).rsplit("/", 1)[-1]
                    found.setdefault(sid, JobPosting(
                        source="malt", source_id=sid, url=urljoin(BASE, href.split("?")[0]), title=title,
                        contract_type=ContractType.FREELANCE, description=text, apply_method="platform",
                    ))
                if found:
                    break
        if not found:
            raise SourceError(
                "malt: logged in but no mission listing recognised. Malt has no public mission board (clients search "
                "freelancer profiles); the freelancer opportunities page markup must be checked by hand "
                "(see src/jobhunt/sources/malt.py FEED_URLS / MISSION_LINK)."
            )
        return list(found.values())[: query.max_results]
