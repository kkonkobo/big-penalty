"""ROME code helper: free text -> ROME codes for France Travail / La Bonne Alternance.

No public, key-less ROME lookup endpoint exists (France Travail's ROME 4.0 / "romeo" APIs need OAuth
credentials), so this ships a compact table in src/jobhunt/data/rome_codes.json covering engineering,
tech and industry functions. Codes are the ones France Travail actually puts on offers (ROME 4.0 era).
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from rapidfuzz import fuzz

from jobhunt.sources._fr_public import norm

_DATA = Path(__file__).resolve().parent.parent / "data" / "rome_codes.json"


@lru_cache
def rome_table() -> list[dict]:
    return json.loads(_DATA.read_text(encoding="utf-8"))["codes"]


def lookup_rome(text: str, limit: int = 3) -> list[str]:
    """Best ROME codes for a free-text function ('systèmes embarqués', 'ingénieur mécatronique').

    Scores each table row by its keywords: whole-phrase containment (best), then token overlap, then
    fuzzy match for typos. Returns [] when nothing scores — callers should fall back to keyword search.
    The generic 'ingénieur' keyword alone never selects more than H1206."""
    q = norm(text)
    if not q:
        return []
    qtokens = set(q.split())
    scored: list[tuple[float, str]] = []
    for row in rome_table():
        best = 0.0
        for kw in row["keywords"]:
            k = norm(kw)
            if not k:
                continue
            ktokens = set(k.split())
            if k in q or q in k and len(q) > 4:
                s = 60 + 10 * len(ktokens) + len(k) / 10
            elif ktokens and ktokens <= qtokens:
                s = 50 + 10 * len(ktokens)
            elif len(k) >= 5 and fuzz.partial_ratio(k, q) >= 90:
                s = 35 + len(k) / 10
            else:
                continue
            best = max(best, s)
        if best:
            scored.append((best, row["code"]))
    scored.sort(key=lambda x: (-x[0], x[1]))
    return [c for _, c in scored[:limit]]


def rome_label(code: str) -> str | None:
    for row in rome_table():
        if row["code"] == code:
            return row["label"]
    return None
