"""Cross-board deduplication. Pure functions, no I/O.

A `MergedJob` is one real-world job with every `Sighting` (board listing) we have seen for it.
Matching = exact identity (same canonical URL / origin URL / (source, source_id)) or fuzzy
(company, title, city) with rapidfuzz.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Iterable
from urllib.parse import parse_qsl, urlencode, urlparse

from rapidfuzz import fuzz

from jobhunt.models import ContractType, JobPosting

COMPANY_NOISE = {
    "sas", "sasu", "sa", "sarl", "eurl", "sci", "snc", "se", "spa", "gmbh", "ltd", "llc", "inc", "corp",
    "corporation", "co", "plc", "group", "groupe", "holding", "france", "french", "europe", "international",
    "cie", "et", "the", "le", "la", "les",
}
TITLE_NOISE = re.compile(
    r"\(?\b(h\s*/\s*f|f\s*/\s*h|m\s*/\s*f(\s*/\s*[dx])?|m\s*/\s*w(\s*/\s*d)?|h\s*/\s*f\s*/\s*x)\b\)?", re.I
)
CONTRACT_WORDS = {"stage", "stagiaire", "alternance", "alternant", "alternante", "apprentissage", "cdi", "cdd", "interim", "vie"}
BROAD_PLACES = {"", "france", "ile de france", "remote", "teletravail", "full remote", "toute la france", "idf"}
TRACKING_PARAMS = {
    "fbclid", "gclid", "trk", "trackingid", "refid", "ref", "source", "from", "lipi", "ebp", "origin",
    "src", "campaign", "mc_cid", "mc_eid", "tk", "vjs", "advn", "alid",
}
STATUS_PRECEDENCE = ("new", "shortlisted", "rejected", "approved", "applied")


def reconcile_status(*statuses: str) -> str:
    """Preserve decisions on consolidation: applied > approved > rejected > shortlisted > new."""
    return max(statuses, key=STATUS_PRECEDENCE.index)


def strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def _ascii_tokens(s: str) -> list[str]:
    s = strip_accents(s).lower().replace("&", " ").replace("'", " ").replace("’", " ")
    return re.findall(r"[a-z0-9+#]+", s)


def normalize_company(name: str | None) -> str:
    if not name:
        return ""
    name = re.sub(r"\b(?:[A-Za-z]\.){2,}", lambda m: m.group().replace(".", ""), name)
    toks = _ascii_tokens(name)
    kept = [t for t in toks if t not in COMPANY_NOISE]
    return " ".join(kept or toks)


def normalize_title(title: str | None) -> str:
    if not title:
        return ""
    t = re.sub(r"\((e|es|s|se|ne|euse|trice)\)", "", title, flags=re.I)
    t = TITLE_NOISE.sub(" ", t)
    toks = [x for x in _ascii_tokens(t) if x not in CONTRACT_WORDS and x not in {"h", "f"}]
    return " ".join(toks)


def normalize_city(loc: str | None) -> str:
    if not loc:
        return ""
    first = re.split(r"[,/|]| - | – ", loc)[0]
    first = re.sub(r"\(.*?\)", " ", first)
    toks = _ascii_tokens(first)
    out = []
    for t in toks:
        if t.isdigit() or re.fullmatch(r"\d+(e|er|eme)", t) or t in {"cedex", "arrondissement", "area", "metropolitan"}:
            continue
        out.append(t)
    return " ".join(out)


def canonical_url(url: str | None) -> str:
    if not url:
        return ""
    try:
        u = urlparse(url.strip())
    except ValueError:
        return ""
    host = (u.hostname or "").lower().removeprefix("www.")
    q = [(k, v) for k, v in parse_qsl(u.query) if k.lower() not in TRACKING_PARAMS and not k.lower().startswith("utm_")]
    q.sort()
    path = u.path.rstrip("/")
    return f"{host}{path}" + (f"?{urlencode(q)}" if q else "")


def _is_specific_url(canon: str) -> bool:
    """Homepages / bare careers roots would merge unrelated jobs; only index deep links."""
    if not canon:
        return False
    if "?" in canon:
        return True
    return canon.count("/") >= 2


def dedupe_key(company: str | None, title: str | None, location: str | None) -> str:
    return f"{normalize_company(company)}|{normalize_title(title)}|{normalize_city(location)}"


def _utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


@dataclass
class Sighting:
    source: str
    source_id: str
    url: str
    apply_url: str | None = None
    posted_at: datetime | None = None
    raw: dict[str, Any] = field(default_factory=dict)
    seen_at: datetime | None = None  # persisted last-seen instant (db-owned)
    seen_now: bool = False  # transient: sighted by the current search (db bumps seen_at only then)


@dataclass
class MergedJob:
    title: str
    company: str | None = None
    location: str | None = None
    contract_type: ContractType = ContractType.UNKNOWN
    remote: str = "unknown"
    salary_text: str | None = None
    salary_min_eur: int | None = None
    salary_max_eur: int | None = None
    description: str | None = None
    posted_at: datetime | None = None
    apply_url: str | None = None
    origin_url: str | None = None
    origin_ats: str | None = None
    origin_confidence: float | None = None
    extra: dict[str, Any] = field(default_factory=dict)  # postal_code, department, experience_level, ...
    sightings: list[Sighting] = field(default_factory=list)
    # persistence state (owned by db.py)
    id: int | None = None
    profile_id: int | None = None
    dedupe_key: str = ""
    score: float | None = None
    score_json: dict[str, Any] = field(default_factory=dict)
    status: str = "new"
    first_seen: datetime | None = None
    last_seen: datetime | None = None
    changed: bool = True  # needs saving
    sighted: bool = False  # transient: sighted by the current search (db bumps last_seen only then)

    def __post_init__(self) -> None:
        if not self.dedupe_key:
            self.dedupe_key = dedupe_key(self.company, self.title, self.location)

    # ---- derived
    @property
    def sources(self) -> list[str]:
        seen: list[str] = []
        for s in self.sightings:
            if s.source not in seen:
                seen.append(s.source)
        return seen

    @property
    def keys(self) -> tuple[str, str, str]:
        return (normalize_company(self.company), normalize_title(self.title), normalize_city(self.location))

    def identity_urls(self) -> set[str]:
        out = {canonical_url(s.url) for s in self.sightings}
        out.update(canonical_url(s.apply_url) for s in self.sightings)
        out.add(canonical_url(self.apply_url))
        out.add(canonical_url(self.origin_url))
        return {u for u in out if _is_specific_url(u)}

    def identity_ids(self) -> set[tuple[str, str]]:
        return {(s.source, s.source_id) for s in self.sightings}

    # ---- merging
    @classmethod
    def from_posting(cls, p: JobPosting) -> "MergedJob":
        j = cls(title=p.title.strip(), extra={})
        j.add(p)
        return j

    def add(self, p: JobPosting) -> None:
        self.changed = True
        self.sighted = True
        sid = (p.source, p.source_id)
        existing = next((s for s in self.sightings if (s.source, s.source_id) == sid), None)
        if existing:
            existing.seen_now = True
            existing.url = p.url or existing.url
            existing.apply_url = p.apply_url or existing.apply_url
            existing.posted_at = _utc(p.posted_at) or existing.posted_at
            existing.raw = p.raw or existing.raw
        else:
            self.sightings.append(Sighting(p.source, p.source_id, p.url, p.apply_url, _utc(p.posted_at), p.raw,
                                           seen_now=True))
        if not self.title:
            self.title = p.title.strip()
        self.company = self.company or (p.company or None)
        self.location = self.location or p.location
        if self.contract_type in (ContractType.UNKNOWN, ContractType.OTHER) and p.contract_type != ContractType.UNKNOWN:
            self.contract_type = p.contract_type
        if self.remote == "unknown" and p.remote != "unknown":
            self.remote = p.remote
        if not self.salary_text and p.salary_text:
            self.salary_text = p.salary_text
        if self.salary_min_eur is None and p.salary_min_eur is not None:
            self.salary_min_eur, self.salary_max_eur = p.salary_min_eur, p.salary_max_eur
        if p.description and len(p.description.strip()) > len((self.description or "").strip()):
            self.description = p.description.strip()
        pa = _utc(p.posted_at)
        if pa and (self.posted_at is None or pa < self.posted_at):
            self.posted_at = pa
        self.apply_url = self.apply_url or p.apply_url
        if p.origin_url and (not self.origin_url or (p.origin_confidence or 0) > (self.origin_confidence or 0)):
            self.origin_url, self.origin_ats, self.origin_confidence = p.origin_url, p.origin_ats, p.origin_confidence
        for k in ("postal_code", "department", "experience_level", "company_website", "apply_method"):
            v = getattr(p, k, None)
            if v and v != "unknown" and not self.extra.get(k):
                self.extra[k] = v
        self.dedupe_key = dedupe_key(self.company, self.title, self.location) if not self.dedupe_key else self.dedupe_key

    def absorb(self, other: "MergedJob") -> None:
        """Merge another MergedJob (e.g. after origin resolution reveals they are the same)."""
        self.status = reconcile_status(self.status, other.status)
        for s in other.sightings:
            if (s.source, s.source_id) not in self.identity_ids():
                self.sightings.append(s)
        self.sighted = self.sighted or other.sighted
        if other.last_seen and (self.last_seen is None or _utc(other.last_seen) > _utc(self.last_seen)):
            self.last_seen = other.last_seen
        self.changed = True
        self.company = self.company or other.company
        self.location = self.location or other.location
        if self.contract_type in (ContractType.UNKNOWN, ContractType.OTHER):
            self.contract_type = other.contract_type
        if len((other.description or "")) > len((self.description or "")):
            self.description = other.description
        for a, b in ((self.posted_at, other.posted_at),):
            if b and (a is None or _utc(b) < _utc(a)):
                self.posted_at = _utc(b)
        if other.origin_url and not self.origin_url:
            self.origin_url, self.origin_ats, self.origin_confidence = other.origin_url, other.origin_ats, other.origin_confidence
        for k, v in other.extra.items():
            self.extra.setdefault(k, v)

    def to_posting(self) -> JobPosting:
        """A representative posting (richest data, first sighting) for origin resolution."""
        s = self.sightings[0]
        return JobPosting(
            source=s.source, source_id=s.source_id, url=s.url, title=self.title, company=self.company,
            location=self.location, contract_type=self.contract_type, description=self.description, remote=self.remote,
            posted_at=self.posted_at, apply_url=s.apply_url or self.apply_url, origin_url=self.origin_url,
            origin_ats=self.origin_ats, origin_confidence=self.origin_confidence,
            company_website=self.extra.get("company_website"),
            apply_method=self.extra.get("apply_method", "unknown"),
        )

    def recompute_posted_at(self) -> None:
        """posted_at = earliest date given by the sightings (drops any date folded in from an origin page)."""
        dates = [_utc(s.posted_at) for s in self.sightings if s.posted_at]
        new = min(dates) if dates else None
        if new != self.posted_at:
            self.posted_at, self.changed = new, True

    def apply_origin(self, p: JobPosting) -> None:
        # posted_at stays the earliest SOURCE date; the origin ATS date lives in extra["origin_posted_at"].
        if p.origin_url and p.origin_url != self.origin_url and (
                not self.origin_url or (p.origin_confidence or 0) >= (self.origin_confidence or 0)):
            self.origin_url, self.origin_ats, self.origin_confidence = p.origin_url, p.origin_ats, p.origin_confidence
            self.changed = True
        if p.origin_posted_at and p.origin_url == self.origin_url:
            iso = _utc(p.origin_posted_at).isoformat()
            if self.extra.get("origin_posted_at") != iso:
                self.extra["origin_posted_at"] = iso
                self.changed = True
        if p.company_website and not self.extra.get("company_website"):
            self.extra["company_website"] = p.company_website
        if p.description and len(p.description) > len(self.description or ""):
            self.description = p.description
            self.changed = True


def _contract_compatible(a: ContractType, b: ContractType) -> bool:
    unk = {ContractType.UNKNOWN, ContractType.OTHER}
    return a in unk or b in unk or a == b


def _city_compatible(a: str, b: str) -> bool:
    if a in BROAD_PLACES or b in BROAD_PLACES:
        return True
    return a == b or fuzz.ratio(a, b) >= 88


def _company_match(a: str, b: str) -> bool | None:
    """True = same company, False = different, None = unknown (a side is empty)."""
    if not a or not b:
        return None
    if a == b or fuzz.ratio(a, b) >= 90:
        return True
    ta, tb = set(a.split()), set(b.split())
    short, long_ = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    return bool(short) and short <= long_ and len(" ".join(short)) >= 4 and len(short) >= 1 and fuzz.token_set_ratio(a, b) == 100


def is_same_job(a: MergedJob | JobPosting, b: MergedJob | JobPosting) -> bool:
    ka, kb = _keys(a), _keys(b)
    if not _contract_compatible(a.contract_type, b.contract_type):
        return False
    ca, ta, la = ka
    cb, tb, lb = kb
    if not ta or not tb:
        return False
    title_score = fuzz.token_sort_ratio(ta, tb)
    comp = _company_match(ca, cb)
    if comp is False:
        return False
    if comp is None:
        def urls(job):
            if isinstance(job, MergedJob):
                return job.identity_urls()
            return {canon for url in (job.url, job.apply_url, job.origin_url)
                    if _is_specific_url(canon := canonical_url(url))}

        same_url = bool(urls(a) & urls(b))
        description_a, description_b = (a.description or "").strip(), (b.description or "").strip()
        same_description = min(len(description_a), len(description_b)) >= 200 and fuzz.ratio(description_a, description_b) >= 95
        return title_score >= 97 and _city_compatible(la, lb) and (same_url or same_description)
    return title_score >= 88 and _city_compatible(la, lb)


def _keys(x: MergedJob | JobPosting) -> tuple[str, str, str]:
    if isinstance(x, MergedJob):
        return x.keys
    return (normalize_company(x.company), normalize_title(x.title), normalize_city(x.location))


@dataclass
class MergeStats:
    postings: int = 0
    new_jobs: int = 0
    merged_into_existing: int = 0
    exact_matches: int = 0
    fuzzy_matches: int = 0


def merge_postings(
    postings: Iterable[JobPosting], existing: Iterable[MergedJob] = ()
) -> tuple[list[MergedJob], MergeStats]:
    """Merge postings into `existing` jobs (mutated in place) and return (all jobs, stats)."""
    jobs: list[MergedJob] = list(existing)
    stats = MergeStats()
    by_url: dict[str, MergedJob] = {}
    by_sid: dict[tuple[str, str], MergedJob] = {}
    for j in jobs:
        for u in j.identity_urls():
            by_url.setdefault(u, j)
        for sid in j.identity_ids():
            by_sid.setdefault(sid, j)

    for p in postings:
        if not p.title or not p.title.strip():
            continue
        stats.postings += 1
        target = by_sid.get((p.source, p.source_id))
        exact = target is not None
        if target is None:
            for u in (canonical_url(p.url), canonical_url(p.apply_url), canonical_url(p.origin_url)):
                if _is_specific_url(u) and u in by_url:
                    target, exact = by_url[u], True
                    break
        if target is None:
            for j in jobs:
                if is_same_job(j, p):
                    target = j
                    break
        if target is None:
            j = MergedJob.from_posting(p)
            jobs.append(j)
            stats.new_jobs += 1
            target = j
        else:
            was_new_sighting = (p.source, p.source_id) not in target.identity_ids()
            target.add(p)
            if was_new_sighting:
                stats.merged_into_existing += 1
                if exact:
                    stats.exact_matches += 1
                else:
                    stats.fuzzy_matches += 1
        for u in target.identity_urls():
            by_url.setdefault(u, target)
        for sid in target.identity_ids():
            by_sid.setdefault(sid, target)
    return jobs, stats


def consolidate(jobs: list[MergedJob]) -> tuple[list[MergedJob], list[MergedJob]]:
    """Merge jobs that share an origin_url (found after origin resolution).

    Returns (kept, removed). Keeper = the one with a DB id / oldest first_seen when possible.
    """
    by_origin: dict[str, MergedJob] = {}
    kept: list[MergedJob] = []
    removed: list[MergedJob] = []
    for j in sorted(jobs, key=lambda x: (x.id is None, x.id or 0)):
        o = canonical_url(j.origin_url)
        if _is_specific_url(o) and o in by_origin:
            by_origin[o].absorb(j)
            removed.append(j)
            continue
        if _is_specific_url(o):
            by_origin[o] = j
        kept.append(j)
    return kept, removed
