"""Read job identity from verified origin metadata without depending on the scraper."""

import json
import re
from difflib import SequenceMatcher

from jobhunt.dedupe import normalize_company, normalize_title

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
    return canonical_identity(resolved.get("canonical_tenant", resolved.get("tenant")),
                              resolved.get("requisition_id", resolved.get("requisition")))


def similar_role(company, title, other_company, other_title) -> bool:
    employer = normalize_company(company)
    left, right = normalize_title(title), normalize_title(other_title)
    return bool(employer and employer == normalize_company(other_company) and left and right
                and SequenceMatcher(None, left, right, autojunk=False).ratio() >= 0.84)
