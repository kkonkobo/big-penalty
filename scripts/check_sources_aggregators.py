"""Live smoke test of the aggregator connectors. Usage:
  JOBHUNT_HEADLESS=1 uv run python scripts/check_sources_aggregators.py [source ...]
Few requests, max 10 results per query."""

from __future__ import annotations

import asyncio
import logging
import sys
import time

from jobhunt.models import ContractType, SearchQuery
from jobhunt.sources import registry
from jobhunt.sources.base import SourceContext

QUERIES = [
    SearchQuery(
        keywords="systèmes embarqués",
        location="Toulouse",
        contract_types=[ContractType.STAGE, ContractType.ALTERNANCE],
        max_results=10,
    ),
    SearchQuery(keywords="ingénieur mécatronique", location="Paris", max_results=10),
]


async def main(names: list[str]) -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    reg = registry()
    ctx = SourceContext(profile_id=0)
    for name in names:
        src = reg[name]()
        for q in QUERIES:
            t0 = time.monotonic()
            print(f"\n=== {name} | {q.keywords!r} @ {q.location} {[c.value for c in q.contract_types]}")
            try:
                res = await src.search(q, ctx)
            except Exception as e:  # report, keep going
                print(f"  ERROR {type(e).__name__}: {e}")
                continue
            ext = sum(1 for p in res if p.apply_url)
            desc = sum(1 for p in res if p.description)
            sal = sum(1 for p in res if p.salary_min_eur)
            kinds: dict[str, int] = {}
            for p in res:
                kinds[p.contract_type.value] = kinds.get(p.contract_type.value, 0) + 1
            methods: dict[str, int] = {}
            for p in res:
                methods[p.apply_method] = methods.get(p.apply_method, 0) + 1
            print(
                f"  {len(res)} results in {time.monotonic() - t0:.1f}s | description={desc} apply_url={ext} "
                f"salary={sal} contracts={kinds} apply_methods={methods}"
            )
            for p in res[:4]:
                print(f"   - {p.title[:70]} | {p.company} | {p.location} | {p.contract_type.value} | {p.remote} | {p.posted_at and p.posted_at.date()} | apply_url={p.apply_url}")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:] or ["linkedin", "indeed", "glassdoor", "hellowork"]))
