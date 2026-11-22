"""Read job identity from verified origin metadata without depending on the scraper."""

import json
import hashlib
import re
from difflib import SequenceMatcher
from urllib.parse import parse_qsl, urlsplit

from jobhunt.dedupe import normalize_company, normalize_title
from jobhunt.apply.destination import clean_url, origin, posting_url

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


def dedupe_identity(identity: tuple[str, str] | None) -> tuple[str, str] | None:
    """Compare Workday posting variants without changing adapter or receipt identifiers."""
    from jobhunt.origin import workday_requisition

    canonical = canonical_identity(*identity) if identity else None
    return (canonical[0], workday_requisition(canonical[1])) if canonical else None


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


def ats_target(url: str) -> tuple[str | None, tuple[str, str] | None]:
    from jobhunt.origin import _identity, classify_ats, workday_requisition

    parsed = urlsplit(url)
    ats = classify_ats(url)
    if (not ats and parsed.hostname in {"www.smartrecruiters.com", "smartrecruiters.com"}
            and parsed.path.startswith("/oneclick-ui/company/")):
        ats = "smartrecruiters"
    identity = canonical_identity(*_identity(ats, url)[:2]) if ats else None
    if identity and any(value.casefold() != identity[1] for _, value in parse_qsl(parsed.query)):
        identity = None
    if ats == "workday" and identity:
        identity = identity[0], workday_requisition(identity[1])
    return ats, identity


def dispatch_url(original: str | None, extra, *, autonomous: bool) -> str:
    fallback = posting_url(original)
    if not autonomous or not fallback or not resolved_identity(extra):
        return fallback
    from jobhunt.origin import MIN_CONFIDENCE

    if isinstance(extra, str):
        extra = json.loads(extra)
    resolution = extra["origin_resolution"]
    target = clean_url(resolution.get("apply_url") if isinstance(resolution.get("apply_url"), str) else None, navigation=True)
    posting = posting_url(resolution.get("posting_url") if isinstance(resolution.get("posting_url"), str) else None)
    confidence = resolution.get("confidence")
    ats = resolution.get("submit_ats")
    if (resolution.get("canonical_ok") is not True or resolution.get("state") != "resolved"
            or not isinstance(confidence, (int, float)) or not MIN_CONFIDENCE <= confidence <= 1
            or fallback not in (posting, target) or not origin(target) or not ats):
        return fallback
    expected = resolved_identity(extra)
    if ats == "workday":
        from jobhunt.origin import workday_requisition
        expected = expected[0], workday_requisition(expected[1])
    return target if ats_target(target) == (ats, expected) else fallback


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
