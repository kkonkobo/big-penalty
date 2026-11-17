"""Live smoke test for the French board connectors (wttj, apec, cadremploi, jobteaser, meteojob, jobijoba).

    cd /Volumes/SSD/Dev/big-penalty && . ./env.sh
    JOBHUNT_HEADLESS=1 uv run python scripts/check_sources_fr_boards.py [source ...]

Browser sources (cadremploi, apec) are blocked in headless mode by Cloudflare / DataDome: run those with
JOBHUNT_HEADLESS=0 (and solve a CAPTCHA by hand if one shows up).
"""

from __future__ import annotations

import asyncio
import sys
import time

from jobhunt.browser import BrowserPool
from jobhunt.models import ContractType, SearchQuery
from jobhunt.sources import registry
from jobhunt.sources.base import SourceContext, SourceError

NAMES = ["wttj", "apec", "cadremploi", "jobteaser", "meteojob", "jobijoba"]
QUERIES = [
    SearchQuery(keywords="systèmes embarqués", location="Toulouse",
                contract_types=[ContractType.STAGE, ContractType.ALTERNANCE], max_results=10),
    SearchQuery(keywords="ingénieur mécatronique", location="Paris", max_results=10),
]


async def main(names: list[str]) -> None:
    reg = registry()
    pool = BrowserPool()
    ctx = SourceContext(profile_id=1, browser=pool)
    try:
        for name in names:
            cls = reg.get(name)
            if cls is None:
                print(f"== {name}: not registered (skipped)")
                continue
            src = cls()
            for q in QUERIES:
                t0 = time.monotonic()
                try:
                    res = await src.search(q, ctx)
                except SourceError as e:
                    print(f"== {name} | {q.keywords!r} @ {q.location}: {type(e).__name__}: {e}")
                    continue
                except Exception as e:  # noqa: BLE001
                    print(f"== {name} | {q.keywords!r} @ {q.location}: CRASH {type(e).__name__}: {e}")
                    continue
                filled = {f: sum(1 for p in res if getattr(p, f)) for f in
                          ("company", "location", "description", "posted_at", "salary_text", "apply_url", "company_website")}
                print(f"== {name} | {q.keywords!r} @ {q.location}: {len(res)} results in {time.monotonic() - t0:.1f}s | filled {filled}")
                for p in res[:5]:
                    print(f"   [{p.contract_type.value:10}] {p.title[:70]} | {p.company} | {p.location} | {p.apply_method}")
    finally:
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:] or NAMES))
