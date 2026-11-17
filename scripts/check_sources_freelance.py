"""Live smoke test of the freelance connectors (few requests, <=10 results each).

    JOBHUNT_HEADLESS=1 uv run python scripts/check_sources_freelance.py [source ...]
"""

from __future__ import annotations

import asyncio
import os
import sys

os.environ.setdefault("JOBHUNT_HEADLESS", "1")

from jobhunt.browser import BrowserPool  # noqa: E402
from jobhunt.models import ContractType, SearchQuery  # noqa: E402
from jobhunt.sources import registry  # noqa: E402
from jobhunt.sources.base import SourceContext  # noqa: E402

NAMES = ["freework", "collective", "codeur", "freelance_informatique", "malt"]
QUERIES = [
    SearchQuery(keywords="systèmes embarqués", contract_types=[ContractType.FREELANCE], max_results=10),
    SearchQuery(keywords="développeur C++ embarqué", location="Paris", contract_types=[ContractType.FREELANCE], max_results=10),
]


async def main(names: list[str]) -> None:
    reg = registry()
    pool = BrowserPool()
    ctx = SourceContext(profile_id=1, browser=pool)
    try:
        for name in names:
            src = reg[name]()
            for q in QUERIES:
                try:
                    posts = await src.search(q, ctx)
                except Exception as e:  # report, keep going
                    print(f"[{name}] {q.keywords!r} loc={q.location}: {type(e).__name__}: {e}")
                    continue
                print(f"[{name}] {q.keywords!r} loc={q.location}: {len(posts)} results")
                for p in posts[:3]:
                    print(f"    - {p.title[:70]} | {p.company} | {p.location} | {p.remote} | {p.salary_text} | {p.posted_at:%Y-%m-%d}" if p.posted_at else f"    - {p.title[:70]} | {p.company} | {p.location} | {p.remote} | {p.salary_text}")
                if posts and q is QUERIES[0]:
                    d = await src.fetch_details(posts[0], ctx)
                    print(f"      details: description {len(d.description or '')} chars, apply={d.apply_method}")
    finally:
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:] or NAMES))
