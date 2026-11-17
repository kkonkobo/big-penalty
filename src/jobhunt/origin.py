"""Origin resolver: find the company-hosted posting (careers site / ATS) behind an aggregator posting.

LinkedIn / Indeed / Glassdoor & co. are DISCOVERY ONLY: we always apply on the company's own page.

Ladder (stop at first confident hit), see `_resolve_one`:
  1. direct   apply_url points to a known ATS / corroborated employer careers page
  2. classify the URL / page into a stable ATS id (see `classify_ats`)
  3. probe public ATS JSON endpoints; guessed tenants require employer evidence
  4. company careers-page discovery (embedded ATS links / matching anchors)              -> <= 0.8
  5. web search (Firecrawl if key, else DuckDuckGo HTML) + page validation               -> <= 0.75
Below MIN_CONFIDENCE origin_url stays None (best guess only goes to the log).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlencode, urljoin, urlparse, urlunparse

import httpx
from rapidfuzz import fuzz
from selectolax.lexbor import LexborHTMLParser as HTMLParser

from jobhunt.fetch import is_http_url, require_http_request
from jobhunt.models import JobPosting
from jobhunt.sources import registry
from jobhunt.sources.base import SourceContext

log = logging.getLogger("jobhunt.origin")

MIN_CONFIDENCE = 0.6
CONCURRENCY = 4
HIT_TTL = timedelta(days=30)
MISS_TTL = timedelta(days=3)
BOARD_MISS_TTL = timedelta(days=7)

# --------------------------------------------------------------------------------------------
# Aggregators / job boards (never an origin)
# --------------------------------------------------------------------------------------------

AGGREGATOR_DOMAINS = (
    "linkedin.com", "indeed.com", "indeed.fr", "glassdoor.com", "glassdoor.fr", "monster.fr",
    "monster.com", "hellowork.com", "regionsjob.com", "jooble.org", "adzuna.fr", "adzuna.com",
    "talent.com", "jobijoba.com", "neuvoo.com", "cadremploi.fr", "apec.fr", "francetravail.fr",
    "pole-emploi.fr", "jobrapido.com", "ziprecruiter.com", "ziprecruiter.fr", "simplyhired.com",
    "optioncarriere.com", "meteojob.com", "keljob.com", "lehibou.com", "jobteaser.com",
    "staffme.fr", "studentjob.fr", "jobs.makesense.org", "joblift.com", "lesjeudis.com",
    "defencejobs.org", "welcometothejungle.com", "labonnealternance.apprentissage.beta.gouv.fr",
    "emploi-store.fr", "jobtome.com", "jobijoba.net", "careerjet.fr", "careerjet.com", "emploi.fr",
    "wizbii.com", "chooseyourboss.com", "freework.com", "free-work.com", "malt.fr", "indeed.co.uk",
    "facebook.com", "twitter.com", "x.com", "instagram.com", "youtube.com", "wikipedia.org",
    "pagepersonnel.fr", "michaelpage.fr", "randstad.fr", "hays.fr", "adecco.fr", "manpower.fr",
    "collective.work", "codeur.com", "freelance-informatique.fr", "malt.com", "civiweb.com",
    "mon-vie-via.businessfrance.fr", "choisirleservicepublic.gouv.fr", "place-emploi-public.gouv.fr",
    "francetravail.io", "apprentissage.beta.gouv.fr", "monster.co.uk", "reed.co.uk", "totaljobs.com",
    "wttj.co", "wttj.com", "civiweb.fr",
)


def aggregator_domains() -> set[str]:
    return set(AGGREGATOR_DOMAINS) | {domain.lower().removeprefix("www.")
                                    for source in registry().values() if source.is_aggregator
                                    for domain in source.domains}


def _host(url: str) -> str:
    try:
        h = (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""
    return h[4:] if h.startswith("www.") else h


def is_aggregator(url: str | None) -> bool:
    if not url:
        return False
    h = _host(url)
    return any(h == d or h.endswith("." + d) for d in aggregator_domains())


# --------------------------------------------------------------------------------------------
# URL cleaning
# --------------------------------------------------------------------------------------------

_TRACKING_EXACT = {
    "gh_src", "gclid", "fbclid", "msclkid", "mc_cid", "mc_eid", "trk", "trackingid", "refid",
    "ref", "referrer", "referral", "src", "sid", "lever-source", "lever-origin", "ss", "tm_source",
    "jr_id_src", "codes", "_ga", "igshid", "vjk", "jk", "tk", "from", "xpath", "xkcb", "campaignid",
    "adgroupid", "ittk", "sourcetype", "pipeline", "pipelineid", "origin", "trackinginfo",
    "applyfrom", "ats_source", "sourced", "iis", "iisn", "recommendedflag", "utm",
}
_TRACKING_PREFIX = ("utm_", "mkt_", "hsa_", "li_", "trk", "mc_", "pk_", "wt_", "cmp", "ga_", "fb_")
_SOURCE_VALUES = re.compile(
    r"linkedin|indeed|glassdoor|jobboard|job_board|google|facebook|twitter|hellowork|jobijoba|"
    r"welcometothejungle|wttj|monster|apec|francetravail|poleemploi|jooble|adzuna|talent|careerjet",
    re.I,
)


def clean_url(url: str) -> str:
    """Strip tracking params (utm_*, gh_src, source=linkedin...) and fragments; keep functional ids."""
    url = url.strip()
    try:
        p = urlparse(url)
    except ValueError:
        return url
    keep = []
    for k, v in parse_qsl(p.query, keep_blank_values=True):
        kl = k.lower()
        if kl in _TRACKING_EXACT or kl.startswith(_TRACKING_PREFIX):
            continue
        if kl in ("source", "utm_source", "src", "via", "channel", "medium") and (_SOURCE_VALUES.search(v) or not v):
            continue
        if kl in ("source", "medium", "channel", "via") and v.lower() in ("job_board", "jobboard", "board", "career"):
            continue
        keep.append((k, v))
    path = p.path
    return urlunparse((p.scheme, p.netloc, path, p.params, urlencode(keep), ""))


_REDIRECT_PARAMS = ("url", "u", "redirect", "redirect_url", "redirecturl", "dest", "destination", "target", "to", "link", "applyurl", "ad", "r")


def embedded_target(url: str) -> str | None:
    """Aggregator redirectors often carry the destination in a query param (LinkedIn externalApply/redirect...)."""
    try:
        qs = parse_qsl(urlparse(url).query, keep_blank_values=False)
    except ValueError:
        return None
    for k, v in qs:
        if k.lower() in _REDIRECT_PARAMS:
            v = unquote(v) if "%" in v else v
            if v.startswith(("http://", "https://")) and not is_aggregator(v):
                return v
    return None


def is_redirector(url: str) -> bool:
    """URL on an aggregator that is known to bounce to the employer."""
    u = url.lower()
    return any(
        s in u
        for s in (
            "/rc/clk", "/pagead/clk", "/applystart", "/viewjob", "/jobs/externalapply", "/externalapply/",
            "/partner/joblisting", "/job-listing/", "/jobs/view", "/redirect", "/clk?", "/apply/",
            "/emplois/", "/offres/", "/candidat/", "/job/", "/jobs/",
        )
    )


# --------------------------------------------------------------------------------------------
# ATS classifier
# --------------------------------------------------------------------------------------------

# (id, host regex, optional path regex). First match wins; order = specificity.
_ATS_RULES: list[tuple[str, re.Pattern, re.Pattern | None]] = [
    (a, re.compile(h, re.I), re.compile(p, re.I) if p else None)
    for a, h, p in [
        ("workday", r"(^|\.)myworkdayjobs\.com$|(^|\.)myworkdaysite\.com$", None),
        ("greenhouse", r"(^|\.)(boards|job-boards)(\.eu)?\.greenhouse\.io$|(^|\.)greenhouse\.io$", None),
        ("lever", r"(^|\.)jobs(\.eu)?\.lever\.co$", None),
        ("ashby", r"(^|\.)jobs\.ashbyhq\.com$|(^|\.)ashbyhq\.com$", None),
        ("smartrecruiters", r"(^|\.)(jobs|careers)\.smartrecruiters\.com$|(^|\.)smartrecruiters\.com$", None),
        ("workable", r"(^|\.)workable\.com$", None),
        ("recruitee", r"\.recruitee\.com$", None),
        ("teamtailor", r"\.teamtailor\.com$", None),
        ("welcome_kit", r"(^|\.)welcomekit\.co$|(^|\.)welcome-kit\.com$|(^|\.)welcometothejungle\.com$", r"^/(?:[a-z]{2}/)?companies/|^/$|^/jobs|^/o/"),
        ("digitalrecruiters", r"digitalrecruiters\.(com|app|io)$", None),
        ("taleez", r"(^|\.)taleez\.com$", None),
        ("flatchr", r"(^|\.)flatchr\.io$", None),
        ("jobaffinity", r"(^|\.)jobaffinity\.fr$", None),
        ("beetween", r"(^|\.)beetween\.(com|fr)$", None),
        ("talentsoft", r"(^|\.)talent-soft\.com$|(^|\.)cegid\.com$|(^|\.)talentsoft\.com$", None),
        ("successfactors", r"(^|\.)successfactors\.(com|eu)$|(^|\.)jobs2web\.com$|(^|\.)sapsf\.(com|eu)$", None),
        ("taleo", r"(^|\.)taleo\.net$", None),
        ("icims", r"(^|\.)icims\.com$", None),
        ("jobvite", r"(^|\.)jobvite\.com$", None),
        ("bamboohr", r"(^|\.)bamboohr\.com$", None),
        ("personio", r"(^|\.)personio\.(de|com|fr)$", None),
        ("join", r"(^|\.)join\.com$", None),
        ("softy", r"(^|\.)softy\.pro$", None),
        ("altays", r"(^|\.)altays\.(com|fr|net)$", None),
        ("gestmax", r"(^|\.)gestmax\.(fr|eu|com)$", None),
        ("talentview", r"(^|\.)talentview\.io$", None),
        ("oracle_cloud", r"(^|\.)oraclecloud\.com$", r"candidateexperience|hcmui"),
        ("eightfold", r"(^|\.)eightfold\.ai$", None),
        ("avature", r"(^|\.)avature\.net$", None),
        ("cornerstone", r"(^|\.)csod\.com$", None),
        ("phenom", r"(^|\.)phenompeople\.com$|(^|\.)phenom\.com$", None),
    ]
]
_ATS_HTML_HINTS: list[tuple[str, re.Pattern]] = [
    ("greenhouse", re.compile(r"boards\.greenhouse\.io/embed|grnhse_app|Grnhse\.Iframe", re.I)),
    ("lever", re.compile(r"jobs\.lever\.co/[\w-]+", re.I)),
    ("teamtailor", re.compile(r"cdn\.teamtailor\.com|teamtailor-careersite", re.I)),
    ("smartrecruiters", re.compile(r"smartrecruiters\.com/[\w-]+", re.I)),
    ("workable", re.compile(r"apply\.workable\.com/[\w-]+|workable\.com/widget", re.I)),
    ("recruitee", re.compile(r"[\w-]+\.recruitee\.com", re.I)),
]


def classify_ats(url: str, html: str | None = None) -> str | None:
    """Stable ATS id for a URL (and optionally its HTML). None if not a known ATS."""
    if not url:
        return None
    try:
        p = urlparse(url)
    except ValueError:
        return None
    host = p.netloc.lower().split(":")[0]
    for ats, hre, pre in _ATS_RULES:
        if hre.search(host) and (pre is None or pre.search(p.path + ("?" + p.query if p.query else ""))):
            return ats
    qs = {k.lower() for k, _ in parse_qsl(p.query)}
    if "gh_jid" in qs:
        return "greenhouse"  # company site embedding a Greenhouse board
    if "lever-source" in qs or "lever-origin" in qs:
        return "lever"
    if html:
        for ats, pat in _ATS_HTML_HINTS:
            if pat.search(html[:400_000]):
                return ats
    return None


def ats_or_site(url: str, html: str | None = None) -> str:
    return classify_ats(url, html) or "careers_site"


@dataclass(frozen=True)
class BoardRef:
    ats: str
    slug: str  # slug for most ATS; tenant for workday
    host: str | None = None  # workday host / teamtailor host
    site: str | None = None  # workday site

    @property
    def key(self) -> str:
        return f"{self.ats}:{self.host or ''}:{self.slug}:{self.site or ''}"


_WD_HOST = re.compile(r"^([a-z0-9-]+)\.(wd\d+)\.myworkdayjobs\.com$", re.I)


def board_ref(url: str) -> BoardRef | None:
    """Extract the board identifier (slug) from an ATS URL, so we can query its public API."""
    try:
        p = urlparse(url)
    except ValueError:
        return None
    host = p.netloc.lower().split(":")[0]
    parts = [x for x in p.path.split("/") if x]
    ats = classify_ats(url)
    if ats == "greenhouse" and parts and "greenhouse.io" in host:
        if parts[0] == "embed" and (m := dict(parse_qsl(p.query)).get("for")):
            return BoardRef("greenhouse", m)
        return BoardRef("greenhouse", parts[0])
    if ats == "lever" and parts:
        return BoardRef("lever", parts[0], host="eu" if ".eu." in host else None)
    if ats == "ashby" and parts:
        return BoardRef("ashby", parts[0])
    if ats == "smartrecruiters" and parts:
        return BoardRef("smartrecruiters", parts[0])
    if ats == "workable" and parts and host.startswith("apply."):
        return BoardRef("workable", parts[0]) if parts[0] != "j" else None
    if ats == "recruitee" and host.endswith(".recruitee.com"):
        return BoardRef("recruitee", host.split(".")[0])
    if ats == "teamtailor" and host.endswith(".teamtailor.com"):
        return BoardRef("teamtailor", host.split(".")[0], host=host)
    if ats == "workday" and (m := _WD_HOST.match(host)):
        segs = [s for s in parts if not re.fullmatch(r"[a-z]{2}-[A-Z]{2}", s)]
        if segs and segs[0] != "job":
            return BoardRef("workday", m.group(1), host=host, site=segs[0])
    return None


# --------------------------------------------------------------------------------------------
# Text normalization & matching
# --------------------------------------------------------------------------------------------

def _strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


_GENDER = re.compile(
    r"[\(\[]?\s*\b(?:[hfmwxd]\s*[/\\|\-–]\s*){1,3}[hfmwxd]\b\s*[\)\]]?|"
    r"[\(\[]?\s*\ball\s+genders?\s*[\)\]]?|\(e\)|\(ne\)|\(se\)|\(trice\)|\(euse\)",
    re.I,
)


def normalize_title(t: str) -> str:
    t = _strip_accents((t or "").lower().replace("’", "'"))
    t = _GENDER.sub(" ", t)
    t = re.sub(r"[^a-z0-9+#]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


_CONTRACT = {
    "stage": "stage", "stagiaire": "stage", "intern": "stage", "internship": "stage",
    "alternance": "alt", "alternant": "alt", "apprentissage": "alt", "apprenti": "alt", "apprenticeship": "alt",
}
_SENIORITY = {"senior", "junior", "confirme", "lead", "principal", "staff", "head", "manager", "directeur", "responsable", "stagiaire", "graduate", "executive"}


def title_score(a: str, b: str) -> float:
    """0..100 similarity of two job titles (accent/gender-insensitive, penalizes seniority/contract mismatch)."""
    na, nb = normalize_title(a), normalize_title(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 100.0
    ta, tb = set(na.split()), set(nb.split())
    score = (fuzz.token_set_ratio(na, nb) + fuzz.token_sort_ratio(na, nb)) / 2
    ca, cb = {_CONTRACT[t] for t in ta if t in _CONTRACT}, {_CONTRACT[t] for t in tb if t in _CONTRACT}
    if ca != cb:
        score -= 10 if (ca and cb) else 5
    if (ta ^ tb) & _SENIORITY:
        score -= 8
    return max(score, 0.0)


_STOP_LOC = {"france", "fr", "region", "idf", "ile", "area", "metropolitan", "remote", "hybrid", "teletravail", "de", "la", "le", "les", "du", "des", "sur", "en"}


def _loc_tokens(s: str | None) -> set[str]:
    if not s:
        return set()
    s = _strip_accents(s.lower())
    s = re.sub(r"\(\s*\d{2,3}\s*\)|\b\d{2,5}\b", " ", s)
    return {t for t in re.split(r"[^a-z]+", s) if len(t) >= 3 and t not in _STOP_LOC}


def location_match(a: str | None, b: str | None) -> bool | None:
    """True/False when both sides carry a location, None when unknown."""
    if any(re.search(r"\b(remote|full.?remote|telecommut\w*|multi.?location)\b", _strip_accents(value or "").lower())
           for value in (a, b)):
        return None
    ta, tb = _loc_tokens(a), _loc_tokens(b)
    if not ta or not tb:
        return None
    return bool(ta & tb)


def score_to_confidence(score: float, loc: bool | None) -> float:
    conf = 0.92 - (100 - score) * 0.03
    if loc is True:
        conf += 0.03
    elif loc is False:
        return 0.0
    return round(max(0.0, min(conf, 0.97)), 3)


_LEGAL = {"sas", "sa", "sarl", "sasu", "eurl", "snc", "inc", "ltd", "llc", "gmbh", "ag", "plc", "corp", "corporation", "co", "bv", "nv", "se", "spa", "srl", "limited", "company"}
_GENERIC = {"group", "groupe", "france", "international", "holding", "global", "technologies", "technology", "europe", "worldwide", "the", "et", "and"}


def _company_tokens(company: str) -> list[str]:
    c = _strip_accents(company.lower())
    c = re.sub(r"\(.*?\)", " ", c)
    c = c.replace("&", " ")
    return [t for t in re.split(r"[^a-z0-9]+", c) if t]


def slug_candidates(company: str | None, website: str | None = None, limit: int = 6) -> list[str]:
    """Plausible ATS board slugs for a company (best first, deduplicated)."""
    out: list[str] = []

    def add(s: str | None):
        if s and len(s) >= 2 and s not in out:
            out.append(s)

    if website:
        h = _host(website if "//" in website else "//" + website)
        parts = h.split(".")
        if len(parts) >= 2 and not is_aggregator(website):
            add(parts[-2] if parts[-2] not in ("co", "com", "org") or len(parts) < 3 else parts[-3])
    if company:
        toks = _company_tokens(company)
        core = [t for t in toks if t not in _LEGAL]
        brand = [t for t in core if t not in _GENERIC] or core
        if core:
            add("".join(brand))
            add("-".join(brand))
            add("".join(core))  # keeps "group": boschgroup
            add("-".join(core))
            if len(brand) > 1 and len(brand[0]) >= 5:
                add(brand[0])
    return out[:limit]


_SKIP_WEB = re.compile(r"^(www|fr|en)$")


def company_in_text(company: str | None, text: str) -> bool:
    if not company:
        return False
    nt = _strip_accents(text.lower())
    toks = [t for t in _company_tokens(company) if t not in _LEGAL and t not in _GENERIC and len(t) >= 2]
    if not toks:
        return False
    joined = "".join(toks)
    words = set(re.findall(r"[a-z0-9]+", nt))
    return joined in words or all(token in words for token in toks)


def employer_verified(company: str | None, candidate: Candidate) -> bool:
    employer = candidate.employer if isinstance(candidate.employer, str) else ""
    if company_in_text(company, employer + " " + (candidate.description or "")):
        return True
    expected = " ".join(token for token in _company_tokens(company or "") if token not in _LEGAL | _GENERIC)
    actual = " ".join(token for token in _company_tokens(employer) if token not in _LEGAL | _GENERIC)
    return bool(expected and actual) and fuzz.ratio(expected, actual) >= 90


# --------------------------------------------------------------------------------------------
# HTML helpers
# --------------------------------------------------------------------------------------------

def html_to_text(html: str | None) -> str:
    if not html:
        return ""
    try:
        tree = HTMLParser(html)
        for n in tree.css("script,style"):
            n.decompose()
        txt = tree.text(separator="\n")
    except Exception:
        txt = re.sub(r"<[^>]+>", " ", html)
    return re.sub(r"\n\s*\n+", "\n\n", re.sub(r"[ \t]+", " ", txt)).strip()


def _parse_date(s) -> datetime | None:
    if s is None or s == "":
        return None
    if isinstance(s, (int, float)):
        try:
            return datetime.fromtimestamp(s / 1000 if s > 1e11 else s, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    s = str(s).strip().replace(" UTC", "+00:00").replace("Z", "+00:00")
    if re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\+00:00", s):
        s = s.replace(" ", "T", 1)
    try:
        d = datetime.fromisoformat(s)
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _workday_posted(s: str | None) -> datetime | None:
    if not s:
        return None
    now = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    sl = s.lower()
    if "today" in sl:
        return now
    if "yesterday" in sl:
        return now - timedelta(days=1)
    m = re.search(r"(\d+)\+?\s*days?", sl)
    if m and "+" not in sl:
        return now - timedelta(days=int(m.group(1)))
    return None


def jsonld_jobposting(html: str) -> dict | None:
    try:
        tree = HTMLParser(html)
    except Exception:
        return None
    for node in tree.css('script[type="application/ld+json"]'):
        try:
            data = json.loads(node.text() or "")
        except ValueError:
            continue
        stack = data if isinstance(data, list) else [data]
        while stack:
            d = stack.pop()
            if isinstance(d, list):
                stack.extend(d)
            elif isinstance(d, dict):
                t = d.get("@type")
                if t == "JobPosting" or (isinstance(t, list) and "JobPosting" in t):
                    return d
                if "@graph" in d:
                    stack.append(d["@graph"])
    return None


def _ld_location(ld: dict) -> str | None:
    if ld.get("jobLocationType") == "TELECOMMUTE":
        return "remote"
    loc = ld.get("jobLocation")
    if isinstance(loc, list):
        return ", ".join(filter(None, (_ld_location({"jobLocation": place}) for place in loc))) or None
    if isinstance(loc, dict):
        a = loc.get("address")
        if isinstance(a, dict):
            return ", ".join(x for x in (a.get("addressLocality"), a.get("addressRegion")) if x) or None
    return None


# --------------------------------------------------------------------------------------------
# Candidates & ATS board fetchers
# --------------------------------------------------------------------------------------------

@dataclass
class Candidate:
    url: str
    title: str
    ats: str
    location: str | None = None
    posted_at: datetime | None = None
    description: str | None = None
    employer: str | None = None


@dataclass
class Match:
    cand: Candidate
    score: float
    conf: float
    via: str


class Pacer:
    """Min gap between two requests to the same host (we are a polite personal tool)."""

    def __init__(self, gap: float = 0.5):
        self.gap = gap
        self._last: dict[str, float] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def wait(self, url: str, gap: float | None = None) -> None:
        host = _host(url)
        lock = self._locks.setdefault(host, asyncio.Lock())
        async with lock:
            g = self.gap if gap is None else gap
            wait = g - (time.monotonic() - self._last.get(host, 0))
            if wait > 0:
                await asyncio.sleep(wait)
            self._last[host] = time.monotonic()


def parse_greenhouse(data: dict, slug: str) -> list[Candidate]:
    out = []
    for j in data.get("jobs", []):
        out.append(Candidate(
            url=clean_url(j.get("absolute_url") or f"https://job-boards.greenhouse.io/{slug}/jobs/{j.get('id')}"),
            title=j.get("title", ""), ats="greenhouse",
            location=(j.get("location") or {}).get("name"),
            posted_at=_parse_date(j.get("first_published")),
            description=_gh_content(j.get("content")),
        ))
    return out


def _gh_content(c: str | None) -> str | None:
    if not c:
        return None
    import html as _h
    return html_to_text(_h.unescape(c)) or None


def parse_lever(data: list, slug: str) -> list[Candidate]:
    out = []
    for j in data if isinstance(data, list) else []:
        cats = j.get("categories") or {}
        loc = cats.get("location") or ", ".join(cats.get("allLocations") or []) or None
        out.append(Candidate(
            url=clean_url(j.get("hostedUrl") or ""), title=j.get("text", ""), ats="lever", location=loc,
            posted_at=_parse_date(j.get("createdAt")), description=j.get("descriptionPlain") or None,
            employer=j.get("company"),
        ))
    return out


def parse_ashby(data: dict, slug: str) -> list[Candidate]:
    out = []
    for j in data.get("jobs", []):
        if j.get("isListed") is False:
            continue
        locs = [j.get("location")] + [s.get("location") for s in j.get("secondaryLocations") or []]
        out.append(Candidate(
            url=clean_url(j.get("jobUrl") or ""), title=j.get("title", ""), ats="ashby",
            location="remote" if j.get("isRemote") else ", ".join(x for x in locs if x) or None, posted_at=_parse_date(j.get("publishedAt")),
            description=j.get("descriptionPlain") or html_to_text(j.get("descriptionHtml")) or None,
            employer=(data.get("organization") or {}).get("name") if isinstance(data.get("organization"), dict) else data.get("organizationName"),
        ))
    return out


def parse_smartrecruiters(data: dict, slug: str) -> list[Candidate]:
    out = []
    for j in data.get("content", []):
        loc = (j.get("location") or {}).get("fullLocation") or (j.get("location") or {}).get("city")
        out.append(Candidate(
            url=f"https://jobs.smartrecruiters.com/{slug}/{j.get('id')}-" + re.sub(r"[^a-z0-9]+", "-", normalize_title(j.get("name", ""))).strip("-"),
            title=j.get("name", ""), ats="smartrecruiters", location=loc, posted_at=_parse_date(j.get("releasedDate")),
            employer=(j.get("company") or {}).get("name"),
        ))
    return out


def parse_workable(data: dict, slug: str) -> list[Candidate]:
    out = []
    for j in data.get("jobs", []):
        loc = ", ".join(x for x in (j.get("city"), j.get("state"), j.get("country")) if x) or None
        out.append(Candidate(
            url=clean_url(j.get("url") or j.get("shortlink") or ""), title=j.get("title", ""), ats="workable",
            location=loc, posted_at=_parse_date(j.get("published_on") or j.get("created_at")),
            description=html_to_text(j.get("description")) or None,
            employer=(data.get("account") or {}).get("name") if isinstance(data.get("account"), dict) else data.get("name"),
        ))
    return out


def parse_recruitee(data: dict, slug: str) -> list[Candidate]:
    out = []
    for j in data.get("offers", []):
        if j.get("status") not in (None, "published"):
            continue
        out.append(Candidate(
            url=clean_url(j.get("careers_url") or f"https://{slug}.recruitee.com/o/{j.get('slug')}"),
            title=j.get("title", ""), ats="recruitee", location=j.get("location"),
            posted_at=_parse_date(j.get("published_at") or j.get("created_at")),
            description=html_to_text(j.get("description")) or None,
            employer=(data.get("company") or {}).get("name") if isinstance(data.get("company"), dict) else data.get("company"),
        ))
    return out


def parse_teamtailor(html: str, host: str) -> list[Candidate]:
    out, seen = [], set()
    tree = HTMLParser(html)
    for a in tree.css('a[href*="/jobs/"]'):
        href = a.attributes.get("href") or ""
        if not re.search(r"/jobs/\d+", href):
            continue
        url = clean_url(urljoin(f"https://{host}/", href))
        if url in seen:
            continue
        seen.add(url)
        title = ((a.css_first("span") or a).text() or "").strip().split("\n")[0].strip()
        if title:
            out.append(Candidate(url=url, title=title, ats="teamtailor"))
    return out


# --------------------------------------------------------------------------------------------
# Disk cache
# --------------------------------------------------------------------------------------------

class DiskCache:
    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        self._boards_path = root / "_boards.json"
        try:
            self._boards: dict[str, dict] = json.loads(self._boards_path.read_text())
        except (OSError, ValueError):
            self._boards = {}

    @staticmethod
    def key(company: str | None, title: str, location: str | None, apply_url: str | None) -> str:
        loc = sorted(_loc_tokens(location))[:1]
        raw = "v2|" + "|".join([" ".join(t for t in _company_tokens(company or "") if t not in _LEGAL), normalize_title(title), "".join(loc), apply_url or ""])
        return hashlib.sha1(raw.encode()).hexdigest()[:20]

    def get(self, key: str) -> dict | None:
        try:
            d = json.loads((self.root / f"{key}.json").read_text())
        except (OSError, ValueError):
            return None
        age = datetime.now(timezone.utc) - datetime.fromisoformat(d["ts"])
        if age > (HIT_TTL if d.get("origin_url") else MISS_TTL):
            return None
        return d

    def put(self, key: str, data: dict) -> None:
        data = {**data, "ts": datetime.now(timezone.utc).isoformat()}
        tmp = self.root / f"{key}.json.tmp"
        try:
            tmp.write_text(json.dumps(data, ensure_ascii=False))
            tmp.replace(self.root / f"{key}.json")
        except OSError as e:
            log.debug("cache write failed: %s", e)

    def board_known_missing(self, key: str) -> bool:
        d = self._boards.get(key)
        if not d or d.get("found"):
            return False
        return datetime.now(timezone.utc) - datetime.fromisoformat(d["ts"]) < BOARD_MISS_TTL

    def board_result(self, key: str, found: bool) -> None:
        self._boards[key] = {"found": found, "ts": datetime.now(timezone.utc).isoformat()}
        try:
            self._boards_path.write_text(json.dumps(self._boards))
        except OSError:
            pass


# --------------------------------------------------------------------------------------------
# Resolver
# --------------------------------------------------------------------------------------------

WORKDAY_SEEDS = {  # verified live: slug -> (host, site)
    "thales": ("thales.wd3.myworkdayjobs.com", "Careers"),
    "airbus": ("ag.wd3.myworkdayjobs.com", "Airbus"),
    "valeo": ("valeo.wd3.myworkdayjobs.com", "valeo_jobs"),
}
WORKDAY_ALIASES = {"airbushelicopters": "airbus", "airbus-helicopters": "airbus", "airbusprotect": "airbus",
                   "thalesgroup": "thales", "valeo-group": "valeo", "valeogroup": "valeo"}
_API_ATS = ("greenhouse", "lever", "lever_eu", "ashby", "smartrecruiters", "workable", "recruitee", "teamtailor")
_CAREER_RE = re.compile(r"carri[eè]res?|careers?|recrut|rejoin|nous-rejoindre|join[- ]?us|work[- ]with[- ]us|\bjobs?\b|emplois?|offres?[- ]d|postuler", re.I)
_CAREER_PATHS = ("/careers", "/carrieres", "/recrutement", "/nous-rejoindre", "/jobs", "/fr/carrieres", "/en/careers")


class Resolver:
    def __init__(self, ctx: SourceContext):
        self.ctx = ctx
        self.cache = DiskCache(ctx.settings.cache_dir / "origin")
        self.pacer = Pacer(0.5)
        self.client = ctx.http(timeout=20, event_hooks={"request": [require_http_request]})
        self._boards: dict[str, asyncio.Future] = {}
        self.sem = asyncio.Semaphore(CONCURRENCY)
        self.ddg_lock = asyncio.Lock()
        self.ddg_blocked = False

    async def aclose(self):
        await self.client.aclose()

    # ---- http ----
    async def _get(self, url: str, *, gap: float | None = None, **kw) -> httpx.Response | None:
        if not is_http_url(url):
            return None
        await self.pacer.wait(url, gap)
        try:
            return await self.client.get(url, **kw)
        except (httpx.HTTPError, ValueError) as e:
            log.debug("GET %s failed: %s", url, e)
            return None

    async def _json(self, url: str, **kw):
        r = await self._get(url, headers={"Accept": "application/json"}, **kw)
        if r is None or r.status_code != 200:
            return None
        try:
            return r.json()
        except ValueError:
            return None

    # ---- board fetch (memoized per run, negative-cached on disk) ----
    async def board_jobs(self, ref: BoardRef) -> list[Candidate] | None:
        if ref.key not in self._boards:
            self._boards[ref.key] = asyncio.ensure_future(self._fetch_board(ref))
        return await self._boards[ref.key]

    async def _fetch_board(self, ref: BoardRef) -> list[Candidate] | None:
        if self.cache.board_known_missing(ref.key):
            return None
        s = ref.slug
        jobs: list[Candidate] | None = None
        try:
            if ref.ats == "greenhouse":
                d = await self._json(f"https://boards-api.greenhouse.io/v1/boards/{s}/jobs?content=true")
                jobs = parse_greenhouse(d, s) if isinstance(d, dict) and "jobs" in d else None
                if jobs:
                    metadata = await self._json(f"https://boards-api.greenhouse.io/v1/boards/{s}")
                    for candidate in jobs:
                        candidate.employer = metadata.get("name") if isinstance(metadata, dict) else None
            elif ref.ats in ("lever", "lever_eu"):
                eu = ref.ats == "lever_eu" or ref.host == "eu"
                d = await self._json(f"https://api{'.eu' if eu else ''}.lever.co/v0/postings/{s}?mode=json")
                jobs = parse_lever(d, s) if isinstance(d, list) and d else None
            elif ref.ats == "ashby":
                d = await self._json(f"https://api.ashbyhq.com/posting-api/job-board/{s}")
                jobs = parse_ashby(d, s) if isinstance(d, dict) and d.get("jobs") else None
            elif ref.ats == "smartrecruiters":
                jobs = await self._fetch_smartrecruiters(s)
            elif ref.ats == "workable":
                d = await self._json(f"https://apply.workable.com/api/v1/widget/accounts/{s}")
                jobs = parse_workable(d, s) if isinstance(d, dict) and d.get("jobs") else None
            elif ref.ats == "recruitee":
                d = await self._json(f"https://{s}.recruitee.com/api/offers/")
                jobs = parse_recruitee(d, s) if isinstance(d, dict) and d.get("offers") else None
            elif ref.ats == "teamtailor":
                host = ref.host or f"{s}.teamtailor.com"
                r = await self._get(f"https://{host}/jobs")
                if r is not None and r.status_code == 200 and _host(str(r.url)) == host:
                    jobs = parse_teamtailor(r.text, host) or None
        except Exception as e:  # never let one board break the run
            log.debug("board %s failed: %s", ref.key, e)
            return None
        self.cache.board_result(ref.key, jobs is not None)
        return jobs

    async def _fetch_smartrecruiters(self, slug: str) -> list[Candidate] | None:
        out: list[Candidate] = []
        offset = 0
        while offset < 600:  # cap: huge boards (Bosch: 5000) are searched via q= instead
            d = await self._json(f"https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit=100&offset={offset}")
            if not isinstance(d, dict) or not d.get("content"):
                break
            out += parse_smartrecruiters(d, slug)
            offset += 100
            if offset >= d.get("totalFound", 0):
                break
        return out or None

    async def search_smartrecruiters(self, slug: str, title: str) -> list[Candidate]:
        q = " ".join(normalize_title(title).split()[:6])
        d = await self._json(f"https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit=50&q={q}")
        return parse_smartrecruiters(d, slug) if isinstance(d, dict) else []

    async def search_workday(self, ref: BoardRef, title: str) -> list[Candidate]:
        assert ref.host and ref.site
        q = " ".join(normalize_title(title).split()[:6])
        d = None
        delay = 4.0
        for attempt in range(3):
            await self.pacer.wait(ref.host, 1.2)
            try:
                r = await self.client.post(
                    f"https://{ref.host}/wday/cxs/{ref.slug}/{ref.site}/jobs",
                    json={"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": q},
                )
                if r.status_code == 200:
                    d = r.json()
                    break
                if r.status_code not in (429, 500, 502, 503):
                    break
                ra = r.headers.get("retry-after", "")
                delay = min(float(ra), 30) if ra.replace(".", "").isdigit() else 4 * 2**attempt
            except (httpx.HTTPError, ValueError):
                delay = 2
            log.debug("workday %s throttled; sleeping %.0fs", ref.host, delay)
            await asyncio.sleep(delay)
        out = []
        for j in (d or {}).get("jobPostings", []):
            if not j.get("externalPath"):
                continue
            out.append(Candidate(
                url=f"https://{ref.host}/{ref.site}{j['externalPath']}", title=j.get("title", ""), ats="workday",
                location=j.get("locationsText"), posted_at=_workday_posted(j.get("postedOn")),
            ))
        return out

    # ---- matching ----
    def best_match(self, p: JobPosting, cands: list[Candidate], via: str, scale: float = 1.0) -> Match | None:
        best: Match | None = None
        for c in cands:
            if not is_http_url(c.url) or is_aggregator(c.url) or not c.title:
                continue
            sc = title_score(p.title, c.title)
            if sc < 85:
                continue
            loc = None if p.remote == "full" else location_match(p.location, c.location)
            if loc is False:
                continue
            conf = round(score_to_confidence(sc, loc) * scale, 3)
            if via == "ats_api" and not employer_verified(p.company, c):
                conf = min(conf, 0.59)
            if best is None or conf > best.conf:
                best = Match(c, sc, conf, via)
        return best

    # ---- step 3 ----
    async def probe_company(self, p: JobPosting, extra_slugs: list[str] = ()) -> Match | None:
        slugs = list(dict.fromkeys([*extra_slugs, *slug_candidates(p.company, p.company_website)]))
        if not slugs:
            return None
        best: Match | None = None
        for slug in slugs:
            refs = [BoardRef(a, slug) for a in _API_ATS]
            seed = WORKDAY_ALIASES.get(slug, slug)
            if seed in WORKDAY_SEEDS:
                host, site = WORKDAY_SEEDS[seed]
                tenant = host.split(".")[0]
                refs.append(BoardRef("workday", tenant, host, site))
            results = await asyncio.gather(*(self._search_ref(r, p) for r in refs))
            hit_board = False
            for found, cands in results:
                hit_board |= found and any(employer_verified(p.company, candidate) for candidate in cands)
                if cands and (m := self.best_match(p, cands, "ats_api")):
                    if best is None or m.conf > best.conf:
                        best = m
            if best and best.conf >= 0.85:
                break
            if hit_board:
                break  # the company's board exists under this slug: don't try sloppier slugs
        return best

    async def _search_ref(self, ref: BoardRef, p: JobPosting) -> tuple[bool, list[Candidate]]:
        if ref.ats == "workday":
            c = await self.search_workday(ref, p.title)
            return bool(c), c
        jobs = await self.board_jobs(ref)
        if ref.ats == "smartrecruiters" and jobs is None:
            # board may be too large to list or q-only: try a targeted search
            c = await self.search_smartrecruiters(ref.slug, p.title)
            return bool(c), c
        if ref.ats == "smartrecruiters" and jobs and len(jobs) >= 600:
            jobs = await self.search_smartrecruiters(ref.slug, p.title)
        return jobs is not None, jobs or []

    # ---- step 4 ----
    async def discover_careers(self, p: JobPosting) -> Match | None:
        site = p.company_website
        if site and is_aggregator(site):
            site = None
        if not site:
            site = await self._guess_site(p)
        if not site:
            return None
        if "//" not in site:
            site = "https://" + site
        if not is_http_url(site):
            return None
        pages = await self._careers_pages(site, p.company)
        refs: list[BoardRef] = []
        best: Match | None = None
        for url in pages[:3]:
            if (ref := board_ref(url)) and not is_aggregator(url):
                if ref not in refs:
                    refs.append(ref)
            r = await self._get(url)
            if r is None or r.status_code >= 400:
                continue
            html = r.text
            if is_aggregator(str(r.url)) or not company_in_text(p.company, _host(str(r.url)) + " " + html_to_text(html)):
                continue
            for u in _ats_urls(html, str(r.url)):
                if (ref := board_ref(u)) and ref not in refs:
                    refs.append(ref)
            if ld := jsonld_jobposting(html):  # the page itself is the posting
                if title_score(p.title, ld.get("title", "")) >= 90 and (p.remote == "full" or location_match(p.location, _ld_location(ld)) is not False):
                    return Match(Candidate(str(r.url), ld["title"], ats_or_site(str(r.url), html), location=_ld_location(ld),
                                           posted_at=_parse_date(ld.get("datePosted")), description=html_to_text(ld.get("description"))), 90, 0.8, "careers_page")
            m = self.best_match(p, _anchor_candidates(html, str(r.url)), "careers_anchor", 0.85)
            if m and (best is None or m.conf > best.conf):
                best = m
        for ref in refs[:3]:
            _, cands = await self._search_ref(ref, p)
            if cands and (m := self.best_match(p, cands, "careers_ats", 0.95)):
                if best is None or m.conf > best.conf:
                    best = m
        if best:
            best.cand.ats = classify_ats(best.cand.url) or "careers_site"
        return best

    async def _guess_site(self, p: JobPosting) -> str | None:
        for slug in slug_candidates(p.company)[:2]:
            for tld in ("com", "fr"):
                url = f"https://www.{slug}.{tld}"
                r = await self._get(url, gap=1.0, timeout=8)
                if r is not None and r.status_code < 400 and not is_aggregator(str(r.url)):
                    title = (HTMLParser(r.text[:100_000]).css_first("title") or None)
                    if title is not None and company_in_text(p.company, title.text() or ""):
                        return f"{r.url.scheme}://{r.url.host}"
        return None

    async def _careers_pages(self, site: str, company: str | None = None) -> list[str]:
        r = await self._get(site)
        if r is None or r.status_code >= 400 or is_aggregator(str(r.url)):
            return []
        if not company_in_text(company, _host(str(r.url)) + " " + html_to_text(r.text)):
            return []
        found: list[str] = []
        if r is not None and r.status_code < 400:
            base = str(r.url)
            for a in HTMLParser(r.text).css("a[href]"):
                href = a.attributes.get("href") or ""
                txt = (a.text() or "").strip()
                if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
                    continue
                u = urljoin(base, href)
                if is_aggregator(u):
                    continue
                if _CAREER_RE.search(urlparse(u).path) or (len(txt) < 40 and _CAREER_RE.search(txt)):
                    same = _host(u).split(".")[-2:] == _host(base).split(".")[-2:]
                    if same or classify_ats(u):
                        found.append(u.split("#")[0])
        if not found:
            root = f"{urlparse(site).scheme}://{urlparse(site).netloc}"
            for path in _CAREER_PATHS:
                rr = await self._get(root + path, gap=1.0)
                if rr is not None and rr.status_code < 400:
                    found.append(str(rr.url))
                    break
        # prefer ATS hosts, then shortest path
        found = list(dict.fromkeys(found))
        found.sort(key=lambda u: (0 if classify_ats(u) else 1, len(urlparse(u).path)))
        return found

    # ---- step 5 ----
    async def web_search(self, p: JobPosting) -> Match | None:
        q = f'"{p.title}" "{p.company}" (carrieres OR careers OR jobs)' if p.company else f'"{p.title}" careers'
        urls = await self._search_urls(q)
        best: Match | None = None
        checked = 0
        for u in urls:
            if is_aggregator(u) or checked >= 4:
                continue
            checked += 1
            m = await self._validate_candidate(p, u)
            if m and (best is None or m.conf > best.conf):
                best = m
            if best and best.conf >= 0.75:
                break
        return best

    async def _search_urls(self, query: str) -> list[str]:
        key = self.ctx.settings.firecrawl_api_key
        if key:
            try:
                from firecrawl import Firecrawl

                def run():
                    res = Firecrawl(api_key=key).search(
                        query + " -site:linkedin.com -site:indeed.com -site:glassdoor.com",
                        limit=8, country="FR", exclude_domains=["linkedin.com", "indeed.com", "indeed.fr", "glassdoor.com", "glassdoor.fr"],
                    )
                    web = getattr(res, "web", None) or []
                    return [getattr(w, "url", None) or (w.get("url") if isinstance(w, dict) else None) for w in web]

                return [u for u in await asyncio.to_thread(run) if u]
            except Exception as e:
                log.info("firecrawl search failed (%s); falling back to DuckDuckGo", e)
        if self.ddg_blocked:
            return []
        async with self.ddg_lock:  # one DDG query at a time, ~3s apart
            if self.ddg_blocked:
                return []
            q = query + " -site:linkedin.com -site:indeed.com -site:glassdoor.com -site:glassdoor.fr -site:indeed.fr"
            url = "https://html.duckduckgo.com/html/?" + urlencode({"q": q, "kl": "fr-fr"})
            r = await self._get(url, gap=3.0)
            html = r.text if r is not None and r.status_code == 200 else None
            if html is None and r is not None and r.status_code in (202, 403, 429):
                html = await self._ddg_via_browser(url)
                if html is None:
                    log.info("DuckDuckGo is challenging us (HTTP %s); web-search fallback disabled for this run", r.status_code)
                    self.ddg_blocked = True
        if not html:
            return []
        out = []
        for a in HTMLParser(html).css("a.result__a"):
            href = a.attributes.get("href") or ""
            if "uddg=" in href:
                href = dict(parse_qsl(urlparse(href).query)).get("uddg", "")
            if href.startswith("http"):
                out.append(href)
        return out

    async def _ddg_via_browser(self, url: str) -> str | None:
        if self.ctx.browser is None or not is_http_url(url):
            return None
        try:
            async with self.ctx.browser.page(self.ctx.profile_id, "origin") as page:
                await self.ctx.browser.goto(page, url)
                html = await page.content()
                return html if "result__a" in html else None
        except Exception as e:
            log.debug("ddg browser failed: %s", e)
            return None

    async def _validate_candidate(self, p: JobPosting, url: str) -> Match | None:
        r = await self._get(url, gap=1.0)
        if r is None or r.status_code >= 400:
            return None
        html, final = r.text, clean_url(str(r.url))
        if is_aggregator(final):
            return None
        ld = jsonld_jobposting(html)
        tree = HTMLParser(html[:600_000])
        titles = [ld.get("title")] if ld else []
        titles += [(n.text() or "").strip() for n in tree.css("h1")[:2]]
        if t := tree.css_first("title"):
            titles.append((t.text() or "").strip())
        score = max((title_score(p.title, t) for t in titles if t), default=0)
        if score < 85:
            return None
        body = " ".join(titles) + " " + (ld.get("hiringOrganization", {}).get("name", "") if ld and isinstance(ld.get("hiringOrganization"), dict) else "") + " " + _host(final) + " " + html_to_text(html)[:3000]
        if not company_in_text(p.company, body):
            return None
        ats = ats_or_site(final, html)
        loc = None if p.remote == "full" else location_match(p.location, _ld_location(ld) if ld else None)
        if loc is False:
            return None
        conf = round(score_to_confidence(score, None if loc is None else loc) * (0.85 if ats != "careers_site" else 0.78), 3)
        return Match(Candidate(final, titles[0] or p.title, ats, posted_at=_parse_date(ld.get("datePosted")) if ld else None,
                               description=html_to_text(ld.get("description")) if ld else None), score, conf, "web_search")

    # ---- step 1 ----
    async def direct(self, p: JobPosting) -> tuple[str, str] | None:
        """Returns (final_url, via) when an apply/posting URL leads off-aggregator."""
        for u in (p.apply_url, p.url):
            if is_http_url(u) and not is_aggregator(u):
                if classify_ats(u) or await self._validate_candidate(p, u):
                    return clean_url(u), "direct"
        for u in (p.apply_url, p.url):
            if is_http_url(u) and (t := embedded_target(u)):
                if classify_ats(t) or await self._validate_candidate(p, t):
                    return clean_url(t), "direct_param"
        u = p.apply_url
        if is_http_url(u) and not (p.apply_method in ("easy_apply", "platform") and not is_redirector(u)):
            if final := await self.follow(u):
                if classify_ats(final) or await self._validate_candidate(p, final):
                    return clean_url(final), "direct_redirect"
        return None

    async def follow(self, url: str) -> str | None:
        if not is_http_url(url):
            return None
        r = await self._get(url, gap=1.5)
        if r is not None:
            for h in [*r.history, r]:
                if is_http_url(str(h.url)) and not is_aggregator(str(h.url)) and h.status_code < 400:
                    return str(h.url)
            body = r.text[:200_000]
            m = re.search(r"http-equiv=[\"']refresh[\"'][^>]*url=([^\"'>]+)", body, re.I) or re.search(r"location(?:\.href)?\s*=\s*[\"'](https?://[^\"']+)", body)
            if m and is_http_url(m.group(1)) and not is_aggregator(m.group(1)):
                return m.group(1)
            if r.status_code < 400 and "challenge" not in body[:5000].lower():
                return None
        if self.ctx.browser is None:
            return None
        try:
            async with self.ctx.browser.page(self.ctx.profile_id, "origin") as page:
                await self.ctx.browser.goto(page, url)
                for _ in range(16):
                    if not is_aggregator(page.url) and is_http_url(page.url):
                        return page.url
                    await asyncio.sleep(0.5)
        except Exception as e:
            log.info("browser follow failed for %s: %s", url, e)
        return None

    async def enrich_from_page(self, url: str) -> Candidate | None:
        """Cheap: fetch origin page for JSON-LD date/description (Workday: its public cxs detail JSON)."""
        if not is_http_url(url):
            return None
        if (ref := board_ref(url)) and ref.ats == "workday":
            m = re.match(rf"https://{re.escape(ref.host)}/{re.escape(ref.site)}(/job/.+)$", url.split("?")[0])
            if not m:
                return None
            d = await self._json(f"https://{ref.host}/wday/cxs/{ref.slug}/{ref.site}{m.group(1)}")
            info = (d or {}).get("jobPostingInfo") or {}
            if not info:
                return None
            return Candidate(url, info.get("title", ""), "workday", location=info.get("location"),
                             posted_at=_parse_date(info.get("startDate")), description=html_to_text(info.get("jobDescription")) or None)
        r = await self._get(url, gap=1.0)
        if r is None or r.status_code >= 400:
            return None
        ld = jsonld_jobposting(r.text)
        if not ld:
            return None
        return Candidate(url, ld.get("title", ""), ats_or_site(url, r.text), location=_ld_location(ld),
                         posted_at=_parse_date(ld.get("datePosted")), description=html_to_text(ld.get("description")) or None)


def _ats_urls(html: str, base: str) -> list[str]:
    urls = []
    try:
        tree = HTMLParser(html)
    except Exception:
        return urls
    for sel, attr in (("a[href]", "href"), ("iframe[src]", "src"), ("script[src]", "src"), ("link[href]", "href")):
        for n in tree.css(sel):
            v = n.attributes.get(attr)
            if v:
                u = urljoin(base, v)
                if classify_ats(u):
                    urls.append(u)
    for m in re.finditer(r"https?://[\w.-]+\.(?:greenhouse\.io|lever\.co|ashbyhq\.com|smartrecruiters\.com|workable\.com|recruitee\.com|teamtailor\.com|myworkdayjobs\.com)/[^\s\"'<>\\)]*", html):
        urls.append(m.group(0))
    return list(dict.fromkeys(urls))


def _anchor_candidates(html: str, base: str) -> list[Candidate]:
    out = []
    for a in HTMLParser(html).css("a[href]"):
        txt = re.sub(r"\s+", " ", a.text() or "").strip()
        if 8 <= len(txt) <= 160:
            u = clean_url(urljoin(base, a.attributes.get("href") or ""))
            if u.startswith("http") and not is_aggregator(u):
                out.append(Candidate(u, txt, ats_or_site(u)))
    return out


# --------------------------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------------------------

def _apply(p: JobPosting, url: str | None, ats: str | None, conf: float, extra: Candidate | None) -> JobPosting:
    upd: dict = {}
    if is_http_url(url) and not is_aggregator(url) and conf >= MIN_CONFIDENCE:
        upd.update(origin_url=url, origin_ats=ats, origin_confidence=conf)
    if extra:
        published = extra.posted_at.replace(tzinfo=extra.posted_at.tzinfo or timezone.utc).astimezone(timezone.utc) if extra.posted_at else None
        current = p.posted_at.replace(tzinfo=p.posted_at.tzinfo or timezone.utc).astimezone(timezone.utc) if p.posted_at else None
        if published and (current is None or published <= current):
            upd["posted_at"] = published
        if not p.description and extra.description:
            upd["description"] = extra.description
    return p.model_copy(update=upd)


async def _resolve_one(r: Resolver, p: JobPosting) -> JobPosting:
    if p.origin_url and (not is_http_url(p.origin_url) or is_aggregator(p.origin_url)):
        p = p.model_copy(update={"origin_url": None, "origin_ats": None, "origin_confidence": None})
    key = DiskCache.key(p.company, p.title, p.location, json.dumps([p.url, p.apply_url, p.company_website, p.remote]))
    if hit := r.cache.get(key):
        extra = Candidate(hit["origin_url"], p.title, hit["origin_ats"] or "", posted_at=_parse_date(hit.get("posted_at")),
                          description=hit.get("description")) if hit.get("origin_url") else None
        return _apply(p, hit.get("origin_url"), hit.get("origin_ats"), hit.get("origin_confidence") or 0.0, extra)

    url = ats = None
    conf, extra, via = 0.0, None, ""

    def take(u: str, a: str, c: float, e: Candidate | None, v: str):
        nonlocal url, ats, conf, extra, via
        if not is_http_url(u) or is_aggregator(u):
            return
        if e and p.remote != "full" and location_match(p.location, e.location) is False:
            return
        url, ats, conf, extra, via = u, a, c, e, v

    try:
        d = await r.direct(p)
        if d:  # steps 1-2
            u, v = d
            page = await r.enrich_from_page(u)
            take(u, ats_or_site(u, None), 1.0 if classify_ats(u) else 0.78, page, v)
        if url is None:  # step 3
            if (m := await r.probe_company(p)) and m.conf >= MIN_CONFIDENCE:
                take(m.cand.url, m.cand.ats, m.conf, m.cand, m.via)
        if url is None:  # step 4
            if (m := await r.discover_careers(p)) and m.conf >= MIN_CONFIDENCE:
                take(m.cand.url, m.cand.ats, m.conf, m.cand, m.via)
            elif m:
                log.info("origin guess below threshold for %r @ %r: %s (%.2f)", p.title, p.company, m.cand.url, m.conf)
        if url is None:  # step 5
            if (m := await r.web_search(p)) and m.conf >= MIN_CONFIDENCE:
                take(m.cand.url, m.cand.ats, m.conf, m.cand, m.via)
            elif m:
                log.info("origin guess below threshold for %r @ %r: %s (%.2f)", p.title, p.company, m.cand.url, m.conf)
        if url and not p.description and not (extra and extra.description):
            if page := await r.enrich_from_page(url):
                extra = Candidate(url, page.title, ats or "", page.location, (extra.posted_at if extra and extra.posted_at else page.posted_at), page.description)
        if extra and p.remote != "full" and location_match(p.location, extra.location) is False:
            url, ats, conf, extra = None, None, 0.0, None
    except Exception as e:  # one posting must never kill the batch
        log.warning("origin resolution failed for %s: %s", p.url, e, exc_info=log.isEnabledFor(logging.DEBUG))
        return p
    log.info("origin %s -> %s [%s %s %.2f]", p.url, url, ats, via, conf)
    out = _apply(p, url, ats, conf, extra)
    r.cache.put(key, {
        "origin_url": out.origin_url, "origin_ats": out.origin_ats, "origin_confidence": out.origin_confidence,
        "posted_at": extra.posted_at.isoformat() if extra and extra.posted_at else None,
        "description": (extra.description[:20000] if extra and extra.description and not p.description else None),
        "via": via,
    })
    return out


def is_wttj(url: str) -> bool:
    return "welcometothejungle.com" in url


async def resolve_origins(postings: list[JobPosting], ctx: SourceContext) -> list[JobPosting]:
    """Fill origin_url / origin_ats / origin_confidence (and posted_at / description when the origin has better).

    Returns new JobPosting objects in the same order; never raises for a single posting.
    """
    if not postings:
        return []
    r = Resolver(ctx)

    async def guarded(p: JobPosting) -> JobPosting:
        async with r.sem:
            return await _resolve_one(r, p)

    try:
        return list(await asyncio.gather(*(guarded(p) for p in postings)))
    finally:
        await r.aclose()
