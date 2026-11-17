"""Live smoke test of the official / public-sector connectors. Usage:
  JOBHUNT_HEADLESS=1 uv run python scripts/check_sources_official.py [source ...]
Few requests, max 10 results per query. labonnealternance needs LABONNEALTERNANCE_API_KEY (skipped otherwise)."""

from __future__ import annotations

import asyncio
import logging
import sys
import time

from jobhunt.config import get_settings
from jobhunt.models import ContractType, SearchQuery
from jobhunt.sources import registry
from jobhunt.sources.base import SourceContext

QUERIES = [
    SearchQuery(
        keywords="systèmes embarqués", location="Toulouse", contract_types=[ContractType.STAGE, ContractType.ALTERNANCE],
        max_results=10, rome_codes=["M1805", "M1892", "H1206"],
    ),
    SearchQuery(keywords="mécatronique", location="Paris", max_results=10, rome_codes=["H1206", "I1302"]),
    # public sector rarely publishes interns; a broad query shows the connector working
    SearchQuery(keywords="ingénieur", location="Île-de-France", max_results=10, rome_codes=["H1206"]),
    # VIE is abroad and only carries ContractType.VIE: query it with no contract filter
    SearchQuery(keywords="systèmes embarqués", max_results=10),
]
ONLY = {"vie": [3], "choisir_service_public": [0, 1, 2], "france_travail": [0, 1], "labonnealternance": [0, 1]}


async def main(names: list[str]) -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    reg = registry()
    ctx = SourceContext(profile_id=0)
    s = get_settings()
    for name in names:
        src = reg[name]()
        if not src.is_configured(s):
            print(f"\n=== {name}: SKIPPED (missing credentials {src.requires_credentials})")
            continue
        for qi in ONLY.get(name, range(len(QUERIES))):
            q = QUERIES[qi]
            t0 = time.monotonic()
            print(f"\n=== {name} | {q.keywords!r} @ {q.location} {[c.value for c in q.contract_types]}")
            try:
                res = await src.search(q, ctx)
                if res:  # exercise fetch_details on the first two results
                    res[:2] = [await src.fetch_details(p, ctx) for p in res[:2]]
            except Exception as e:
                print(f"  ERROR {type(e).__name__}: {e}")
                continue
            kinds: dict[str, int] = {}
            methods: dict[str, int] = {}
            for p in res:
                kinds[p.contract_type.value] = kinds.get(p.contract_type.value, 0) + 1
                methods[p.apply_method] = methods.get(p.apply_method, 0) + 1
            print(
                f"  {len(res)} results in {time.monotonic() - t0:.1f}s | description={sum(1 for p in res if p.description)} "
                f"apply_url={sum(1 for p in res if p.apply_url)} salary={sum(1 for p in res if p.salary_min_eur)} "
                f"posted_at={sum(1 for p in res if p.posted_at)} company={sum(1 for p in res if p.company)} "
                f"contracts={kinds} apply_methods={methods}"
            )
            for p in res[:4]:
                print(f"   - {p.title[:70]} | {p.company} | {p.location} | {p.contract_type.value} | {p.remote} | {p.posted_at and p.posted_at.date()} | {p.apply_method}")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:] or ["france_travail", "labonnealternance", "choisir_service_public", "vie"]))
