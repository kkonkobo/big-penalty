"""Read job identity from verified origin metadata without depending on the scraper."""

import json
import hashlib
import re
from difflib import SequenceMatcher

from jobhunt.dedupe import normalize_company, normalize_title
from jobhunt.apply.destination import origin

_ID = re.compile(r"[a-z0-9][a-z0-9_.:/-]{0,127}\Z")


def canonical_identity(tenant, requisition) -> tuple[str, str] | None:
    values = []
    for item in (tenant, requisition):
        if not isinstance(item, (str, int)) or isinstance(item, bool):
            return None
        item = str(item).strip().casefold()
        if not _ID.fullmatch(item) or "://" in item:
            return None
        values.append(item)
    return values[0], values[1]


def resolved_identity(extra) -> tuple[str, str] | None:
    if isinstance(extra, str):
        try:
            extra = json.loads(extra)
        except ValueError:
            return None
    resolved = extra.get("origin_resolution") if isinstance(extra, dict) else None
    if not isinstance(resolved, dict):
        return None
    if "canonical_ok" in resolved and (resolved["canonical_ok"] is not True
                                       or not isinstance(resolved.get("submit_ats"), str) or not resolved["submit_ats"]):
        return None
    return canonical_identity(resolved.get("canonical_tenant", resolved.get("tenant")),
                              resolved.get("requisition_id", resolved.get("requisition")))


def posting_fingerprint(row) -> str:
    canonical = resolved_identity(row["extra"])
    identity = ["canonical", *canonical] if canonical else [
        "posting", normalize_company(row["company"]), normalize_title(row["title"]),
        normalize_title(row["location"]), origin(row["origin_url"] or ""),
        hashlib.sha256((row["description"] or "").encode()).hexdigest(),
    ]
    return hashlib.sha256(json.dumps(identity).encode()).hexdigest()


def similar_role(company, title, other_company, other_title) -> bool:
    employer = normalize_company(company)
    left, right = normalize_title(title), normalize_title(other_title)
    return bool(employer and employer == normalize_company(other_company) and left and right
                and SequenceMatcher(None, left, right, autojunk=False).ratio() >= 0.84)
