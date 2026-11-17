"""Live check of jobhunt.origin: pulls current postings from LinkedIn's public guest search
(discovery only) and resolves each to the company-hosted posting.

    uv run python scripts/check_origin.py [--per-query 3] [--no-cache] [query ...]

Default queries target companies known to use Workday / Greenhouse / Lever / Ashby / SmartRecruiters.
Paced: one guest search every ~3s, then the resolver's own per-host pacing.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import re
import shutil
import sys
from datetime import datetime, timezone
from urllib.parse import quote, unquote

from selectolax.lexbor import LexborHTMLParser

from jobhunt.config import get_settings
from jobhunt.models import JobPosting
from jobhunt.origin import resolve_origins
from jobhunt.sources.base import SourceContext

DEFAULT_QUERIES = [
    "Doctolib", "Alan assurance", "Back Market", "Mirakl", "Thales stage", "Airbus ingénieur",
    "Valeo", "BlaBlaCar", "Schneider Electric", "Datadog Paris", "Parrot ingénieur", "Alten ingénieur",
]


async def linkedin_search(ctx: SourceContext, q: str, n: int) -> list[JobPosting]:
    url = (f"https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search?keywords={quote(q)}"
           "&location=France&start=0&f_TPR=r2592000")
    async with ctx.http() as c:
        r = await c.get(url)
    if r.status_code != 200:
        print(f"  linkedin search {q!r}: HTTP {r.status_code}")
        return []
    out = []
    for li in LexborHTMLParser(r.text).css("li"):
        a = li.css_first("a.base-card__full-link")
        t = li.css_first(".base-search-card__title")
        co = li.css_first(".base-search-card__subtitle")
        if not (a and t and co):
            continue
        href = a.attributes["href"].split("?")[0]
        m = re.search(r"-(\d+)$", href)
        loc = li.css_first(".job-search-card__location")
        dt = li.css_first("time")
        out.append(JobPosting(
            source="linkedin", source_id=m.group(1) if m else href, url=href, title=t.text().strip(),
            company=co.text().strip(), location=loc.text().strip() if loc else None,
            posted_at=datetime.fromisoformat(dt.attributes["datetime"]).replace(tzinfo=timezone.utc) if dt and dt.attributes.get("datetime") else None,
        ))
        if len(out) >= n:
            break
    return out


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("queries", nargs="*")
    ap.add_argument("--per-query", type=int, default=2)
    ap.add_argument("--no-cache", action="store_true", help="wipe the origin cache first")
    ap.add_argument("-v", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO if a.v else logging.WARNING, format="%(name)s %(message)s")
    s = get_settings()
    if a.no_cache:
        shutil.rmtree(s.cache_dir / "origin", ignore_errors=True)
    ctx = SourceContext(profile_id=0)
    postings: list[JobPosting] = []
    for q in a.queries or DEFAULT_QUERIES:
        postings += await linkedin_search(ctx, q, a.per_query)
        await asyncio.sleep(3)
    seen, uniq = set(), []
    for p in postings:
        if p.source_id not in seen:
            seen.add(p.source_id)
            uniq.append(p)
    print(f"{len(uniq)} postings; resolving...")
    t0 = datetime.now()
    res = await resolve_origins(uniq, ctx)
    ok = 0
    for p, r in zip(uniq, res):
        ok += bool(r.origin_url)
        print(f"\n{p.title}  @ {p.company}  [{p.location}]  (linkedin date {p.posted_at.date() if p.posted_at else None})")
        if r.origin_url:
            print(f"  -> {r.origin_url}\n     ats={r.origin_ats} conf={r.origin_confidence} posted_at={r.posted_at.date() if r.posted_at else None}"
                  f" desc={'yes' if r.description else 'no'}")
        else:
            print("  -> NOT RESOLVED")
    print(f"\nresolved {ok}/{len(uniq)} in {(datetime.now() - t0).seconds}s")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
