"""Origin resolver: find the company-hosted posting (careers site / ATS) behind an aggregator posting.

LinkedIn / Indeed / Glassdoor & co. are DISCOVERY ONLY: we always apply on the company's own page.

Ladder (stop at first confident hit), see `_resolve_one`:
  1. direct   apply_url points to a known ATS / an employer careers page corroborated by its HOST (company tokens in
              the registrable domain, or same domain as `company_website`) or by the fetched page; trackers /
              aggregators are followed (HEAD then GET, few hops) and the FINAL url is what gets classified
  2. classify the URL / page into a stable ATS id (see `classify_ats`)
  3. probe public ATS JSON endpoints; guessed tenants require employer evidence
  4. company careers-page discovery (embedded ATS links / matching anchors)              -> <= 0.8
  5. web search (Firecrawl if key, else DuckDuckGo HTML) + page validation               -> <= 0.75
Below MIN_CONFIDENCE origin_url stays None (best guess only goes to the log).
"""

from __future__ import annotations

import asyncio
import contextvars
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
FIRECRAWL_GAP_S = 1.5       # minimum spacing between two Firecrawl searches
FIRECRAWL_RETRIES = 3       # on a rate-limit answer
FIRECRAWL_BACKOFF_S = 20.0  # x attempt number
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
    # job boards / scrapers that showed up as "careers_site" origins: never apply there (milestone 2b)
    "bebee.com", "jobintree.com", "trabajo.org", "jobleads.com", "letudiant.fr", "stage.fr", "talents-handicap.com",
    "jobzmall.com", "iagora.com", "bordeaux-emplois.com", "bandana.com", "screeningpass.ai", "robopost.com",
    "versaillesgrandparc.fr", "stationf.co", "ycombinator.com",
    # click trackers / ad redirectors: never an origin, always followed to their final url
    "recruitics.com", "appcast.io", "doubleclick.net", "googleadservices.com", "googlesyndication.com",
    "dartsearch.net", "talroo.com", "joveo.com", "pandologic.com", "jobcase.com", "jobiak.ai",
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


_TRACKER_HOSTS = ("recruitics.com", "appcast.io", "doubleclick.net", "googleadservices.com", "dartsearch.net", "talroo.com",
                  "joveo.com", "pandologic.com", "jobcase.com")


def is_tracker_pattern(url: str | None) -> bool:
    """Redirect URLs hosted by an ATS / employer that merely bounce somewhere else (never the posting itself)."""
    if not url:
        return False
    try:
        p = urlparse(url)
    except ValueError:
        return False
    h = (p.hostname or "").lower()
    if h.endswith("smartrecruiters.com"):
        return "redirect" in p.path.lower() or (p.path.strip("/") == "" and "redirect" in p.query.lower())
    return False


def is_aggregator(url: str | None) -> bool:
    if not url:
        return False
    h = _host(url)
    return any(h == d or h.endswith("." + d) for d in aggregator_domains()) or is_tracker_pattern(url)


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


_REDIRECT_PARAMS = ("url", "u", "redirect", "redirect_url", "redirecturl", "redirect_uri", "dest", "destination", "target", "to", "link",
                    "applyurl", "ad", "r", "rx_url", "adurl", "dest_url", "desturl", "externalurl", "external_url", "click_url",
                    "clickurl", "landing", "landingurl", "jobapplyurl", "out", "go", "href", "new_url", "targeturl")


def embedded_target(url: str, _depth: int = 0) -> str | None:
    """Aggregator / tracker redirectors often carry the destination in a query param (LinkedIn externalApply, Recruitics
    `rx_url`, DoubleClick `adurl`...). Double-encoded values are decoded; a target that is itself a tracker is unwrapped."""
    try:
        qs = parse_qsl(urlparse(url).query, keep_blank_values=False)
    except ValueError:
        return None
    for k, v in qs:
        if k.lower() in _REDIRECT_PARAMS:
            for _ in range(2):
                if "%" in v and not v.startswith(("http://", "https://")):
                    v = unquote(v)
            if v.startswith(("http://", "https://")):
                if not is_aggregator(v):
                    return v
                if _depth < 3 and (inner := embedded_target(v, _depth + 1)):
                    return inner
    return None


def is_redirector(url: str) -> bool:
    """URL on an aggregator that is known to bounce to the employer."""
    u = url.lower()
    return any(
        s in u
        for s in (
            "/rc/clk", "/pagead/clk", "/applystart", "/viewjob", "/jobs/externalapply", "/externalapply/",
            "/partner/joblisting", "/job-listing/", "/jobs/view", "/redirect", "/clk?", "/apply/",
            "/emplois/", "/offres/", "/candidat/", "/job/", "/jobs/", "/fr/annonce", "/annonce/", "/track/", "/click",
        )
    ) or any(d in _host(url) for d in _TRACKER_HOSTS) or is_tracker_pattern(url)


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


def ats_of_host(host: str) -> str | None:
    """ATS id of a bare (canonical, lowercase) hostname, label-boundary safe: "jobs.lever.co" and "x.recruitee.com" match,
    "notdigitalrecruiters.com" does not. Same rules and data as `classify_ats` (evaluated on the path "/"), which is left as is."""
    host = (host or "").lower().rstrip(".")
    if not host:
        return None
    for ats, hre, pre in _ATS_RULES:
        m = hre.search(host)
        if m and (pre is None or pre.search("/")) and (m.start() == 0 or host[m.start()] == "." or host[m.start() - 1] == "."):
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


_REF = re.compile(
    r"[\(\[]?\s*\b(?:r[eé]f(?:[eé]rence)?|req(?:uisition)?|job\s*id|offre\s*n[°o]?)\b[\s.:#°n-]*[a-z0-9_/-]*\d[a-z0-9_/-]*\s*[\)\]]?|"
    r"#\s*\d{3,}|[\(\[]\s*[a-z]{0,4}[-_ ]?\d{4,}[a-z0-9_-]*\s*[\)\]]|\b[a-z]{1,4}[-_]\d{5,}\b",
    re.I,
)


def normalize_title(t: str) -> str:
    t = _strip_accents((t or "").lower().replace("’", "'"))
    t = _REF.sub(" ", t)
    t = _GENDER.sub(" ", t)
    t = re.sub(r"[^a-z0-9+#]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


_CORE_NOISE = re.compile(
    r"\b(?:stage|stagiaire|intern|internship|alternance|alternant|apprentissage|apprenti|apprenticeship|pfe|"
    r"de fin d etudes?|fin d etudes?|\d+\s*(?:mois|months?)|contrat|vie)\b"
)


def core_title(normalized: str) -> str:
    """Normalised title without the contract wording ("stage", "alternance", "6 mois", "PFE"...)."""
    return re.sub(r"\s+", " ", _CORE_NOISE.sub(" ", normalized)).strip()


def search_title(title: str, limit_words: int = 8) -> str:
    """Human-readable short title for search queries: no gender mark, reference number or leading contract wording."""
    t = _REF.sub(" ", title or "")
    t = _GENDER.sub(" ", t)
    t = re.sub(r"^\s*[\(\[]?\s*(?:stage|stagiaire|alternance|alternant|apprentissage|apprenti|internship|intern)\b"
               r"(?:\s+(?:de\s+)?(?:fin\s+d['’ ]?[eé]tudes?|pfe|\d+\s*mois))?\s*[\)\]]?\s*[:\-–—|/]*\s*", "", t, flags=re.I)
    words = re.sub(r"\s+", " ", re.sub(r"[\"“”]", " ", t)).strip(" -–—:|/()[]").split()
    return " ".join(words[:limit_words])


_JOB_REF = re.compile(r"(?:\br[eé]f(?:[eé]rence)?|\bjob\s*id|\breq(?:uisition)?|\boffre\s*n[°o]?)\s*[.:#°]*\s*([A-Za-z0-9][A-Za-z0-9_/-]{4,})", re.I)


def job_refs(p: JobPosting) -> list[str]:
    """Reference numbers quoted in the title / head of the description (a strong cross-site match key)."""
    text = f"{p.title or ''}\n{(p.description or '')[:3000]}"
    out = [m.group(1).strip("-_/").lower() for m in _JOB_REF.finditer(text)]
    out += [m.group(0).lower() for m in re.finditer(r"\b[a-z]{0,3}[-_]?\d{5,}\b", p.title or "", re.I)]
    return [r for r in dict.fromkeys(out) if sum(c.isdigit() for c in r) >= 4]


_CONTRACT = {
    "stage": "stage", "stagiaire": "stage", "intern": "stage", "internship": "stage",
    "alternance": "alt", "alternant": "alt", "apprentissage": "alt", "apprenti": "alt", "apprenticeship": "alt",
}
_SENIORITY = {"senior", "junior", "confirme", "lead", "principal", "staff", "head", "manager", "directeur", "responsable", "stagiaire", "graduate", "executive"}


def _raw_title_score(na: str, nb: str, ca: set, cb: set) -> float:
    ta, tb = set(na.split()), set(nb.split())
    score = (fuzz.token_set_ratio(na, nb) + fuzz.token_sort_ratio(na, nb)) / 2
    if ca != cb:
        score -= 10 if (ca and cb) else 5
    if (ta ^ tb) & _SENIORITY:
        score -= 8
    return score


def title_score(a: str, b: str) -> float:
    """0..100 similarity of two job titles (accent/gender/reference-insensitive, penalizes seniority/contract mismatch).

    "STAGE - Ingénieur systèmes (H/F)" vs "Ingénieur systèmes" is compared a second time without the contract wording
    (only when the contracts do not CONTRADICT each other: stage vs alternance still scores low)."""
    na, nb = normalize_title(a), normalize_title(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 100.0
    ca = {_CONTRACT[t] for t in na.split() if t in _CONTRACT}
    cb = {_CONTRACT[t] for t in nb.split() if t in _CONTRACT}
    score = _raw_title_score(na, nb, ca, cb)
    if not (ca and cb and ca != cb):
        ka, kb = core_title(na), core_title(nb)
        if ka and kb and (ka != na or kb != nb):
            score = max(score, _raw_title_score(ka, kb, set(), set()) - (2 if ca != cb else 0))
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


def main_company_name(company: str | None) -> str:
    """"Cea - Commissariat à l'énergie atomique" -> "Cea"; "Thales (Paris)" -> "Thales"; plain names unchanged."""
    c = re.sub(r"\(.*?\)", " ", company or "")
    c = re.split(r"\s+[-–—|:/]\s+|\s*[|:]\s+", c, maxsplit=1)[0]
    return re.sub(r"\s+", " ", c).strip() or (company or "").strip()


_SECTOR = {"ingenierie", "ingenierie", "engineering", "semiconductors", "semiconductor", "aerospace", "public", "security", "securite",
           "electronics", "electronic", "electronique", "defense", "defence", "systems", "systemes", "system", "technologies",
           "technologie", "technology", "industries", "industrie", "industrial", "solutions", "services", "service", "consulting",
           "conseil", "digital", "energy", "energies", "healthcare", "sante", "automotive", "aircraft", "engines", "landing",
           "helicopters", "missile", "infrared", "space", "spatial", "networks", "software", "logiciels", "lab", "labs", "research"}


def _brand_tokens(company: str | None) -> list[str]:
    """Distinctive tokens of the company's main name: legal forms, generic words and sector words removed
    ("Safran Electronics & Defense" -> safran, "NXP Semiconductors" -> nxp, "IDEMIA Public Security" -> idemia).
    Sector words are only dropped while a token of >= 3 letters remains ("GE HealthCare" stays ge+healthcare)."""
    toks = [t for t in _company_tokens(main_company_name(company)) if t not in _LEGAL and t not in _GENERIC and len(t) >= 2]
    core = [t for t in toks if t not in _SECTOR]
    return core if core and any(len(t) >= 3 for t in core) else toks


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
        toks = _company_tokens(main_company_name(company))
        core = [t for t in toks if t not in _LEGAL]
        brand = [t for t in core if t not in _GENERIC] or core
        if core:
            brand = [t for t in brand if t not in _SECTOR] if any(len(t) >= 3 for t in brand if t not in _SECTOR) else brand
            add("".join(brand))
            add("-".join(brand))
            add("".join(t for t in core if t not in _GENERIC))
            add("".join(core))  # keeps "group": boschgroup
            add("-".join(core))
            if len(brand) > 1 and len(brand[0]) >= 5:
                add(brand[0])  # "Thales SIX GTS France" -> thales
    return out[:limit]


_SKIP_WEB = re.compile(r"^(www|fr|en)$")


def company_in_text(company: str | None, text: str, loose: bool = False) -> bool:
    """Is the company named in `text`? Tries the whole name, then its main segment ("Cea - Commissariat..." -> "cea").
    `loose` (page checks on the company's own site, never employer checks on shared ATS boards) also accepts the
    first distinctive word of a longer name when it is specific enough ("Thales SIX GTS France" -> "thales")."""
    if not company:
        return False
    nt = _strip_accents(text.lower())
    words = set(re.findall(r"[a-z0-9]+", nt))

    def has(toks: list[str]) -> bool:
        return bool(toks) and ("".join(toks) in words or all(token in words for token in toks))

    full = [t for t in _company_tokens(company) if t not in _LEGAL and t not in _GENERIC and len(t) >= 2]
    if has(full):
        return True
    main = [t for t in _company_tokens(main_company_name(company)) if t not in _LEGAL and t not in _GENERIC and len(t) >= 2]
    if main != full and has(main):
        return True
    if not loose:
        return False
    brand = _brand_tokens(company)  # sector words dropped: "NXP Semiconductors" -> nxp, "Safran Electronics & Defense" -> safran
    if brand != main and has(brand):
        return True
    return bool(len(main) > 1 and len(main[0]) >= 5 and main[0] in words)


_SLD = {"co", "com", "org", "net", "gouv", "gov", "ac", "asso", "edu"}
_HOST_SUFFIXES = {"", "group", "groupe", "france", "fr", "jobs", "job", "careers", "career", "carrieres", "recrutement", "recrute",
                  "emploi", "emplois", "international", "global", "corp", "sa", "sas", "company", "online", "eu", "recrut"}


def registrable_domain(host: str) -> str:
    parts = [x for x in (host or "").lower().strip(".").split(".") if x]
    if len(parts) >= 3 and parts[-2] in _SLD and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def _company_keys(company: str | None) -> list[str]:
    main = _brand_tokens(company)
    full = [t for t in _company_tokens(company or "") if t not in _LEGAL and len(t) >= 2]
    keys = []
    if main:
        keys.append("".join(main))
        if len(main) > 1 and len(main[0]) >= 5:
            keys.append(main[0])
    if full:
        keys.append("".join(full))
    return [k for k in dict.fromkeys(keys) if len(k) >= 3]


def label_matches_company(label: str, company: str | None) -> bool:
    label = (label or "").lower().replace("-", "").replace("_", "")
    if len(label) < 3:
        return False
    return any(label.startswith(k) and label[len(k):] in _HOST_SUFFIXES for k in _company_keys(company))


def host_matches_company(host: str, company: str | None) -> bool:
    """Company name tokens make up the registrable domain: capgemini.com, thalesgroup.com, elsys-design.com, cea.fr."""
    if not company or not host:
        return False
    return label_matches_company(registrable_domain(host).split(".")[0], company)


_GENERIC_HOST_LABELS = {"www", "jobs", "job", "careers", "career", "carriere", "carrieres", "apply", "boards", "job-boards", "api", "app", "recrutement",
                        "emploi", "fr", "en", "eu", "candidat", "talent", "recruiting", "recruit"}
_PATH_TENANT_ATS = {"smartrecruiters", "lever", "greenhouse", "ashby", "workable", "join", "personio", "jobvite", "bamboohr"}


def ats_tenant_labels(url: str) -> list[str]:
    """Tenant names an ATS URL carries (subdomain, first path segment, `company=` / `for=` query): valeo.wd3.myworkdayjobs.com
    -> valeo; jobs.lever.co/acme/1 -> acme; career5.successfactors.eu/career?company=safran -> safran."""
    ats = classify_ats(url)
    if not ats:
        return []
    try:
        p = urlparse(url)
    except ValueError:
        return []
    labels: list[str] = []
    host_labels = (p.hostname or "").lower().split(".")
    first = host_labels[0] if host_labels else ""
    if first not in _GENERIC_HOST_LABELS and not re.fullmatch(r"career\d*|jobs\d*", first):
        labels.append(re.sub(r"^(careers?|jobs?|recrutement|emploi)[-_]", "", first))
    parts = [x for x in p.path.split("/") if x]
    if ats in _PATH_TENANT_ATS and parts and parts[0].lower() not in {"embed", "j", "company", "o", "jobs"}:
        labels.append(parts[0].lower())
    qs = {k.lower(): v for k, v in parse_qsl(p.query)}
    for k in ("company", "for", "tenant", "client", "organization"):
        if qs.get(k):
            labels.append(qs[k].lower())
    return [x for x in dict.fromkeys(label.replace("-", "").replace("_", "") for label in labels) if x]


def tenant_matches_company(url: str, company: str | None) -> bool:
    """The ATS tenant of `url` is this company's (name tokens in the tenant, or the curated Workday seeds)."""
    if not company or not url:
        return False
    if any(label_matches_company(label, company) for label in ats_tenant_labels(url)):
        return True
    host = _host(url)
    for slug in slug_candidates(company)[:4]:
        seed = WORKDAY_ALIASES.get(slug, slug)
        if seed in WORKDAY_SEEDS and WORKDAY_SEEDS[seed][0] == host:
            return True
    return False


def looks_like_posting(url: str) -> bool:
    """A path/query that points at ONE posting (never an ATS board root or a homepage)."""
    try:
        p = urlparse(url)
    except ValueError:
        return False
    segs = [x for x in p.path.split("/") if x]
    if classify_ats(url) == "workday":
        return "job" in [x.lower() for x in segs]
    return len(segs) >= 2 or any(c.isdigit() for c in p.path) or bool(p.query)


def host_matches_website(host: str, website: str | None) -> bool:
    if not website or is_aggregator(website if "//" in website else "https://" + website):
        return False
    site_host = _host(website if "//" in website else "//" + website)
    return bool(site_host and host and registrable_domain(host) == registrable_domain(site_host))


def host_corroborated(url: str | None, company: str | None, website: str | None = None) -> str | None:
    """"website" / "name" when the URL's HOST proves it is the employer's own site, else None. Aggregators, trackers
    and the bare homepage never qualify (a posting URL needs a path or a query)."""
    if not url or not is_http_url(url) or is_aggregator(url):
        return None
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    if not (parsed.path.strip("/") or parsed.query):
        return None
    host = _host(url)
    if host_matches_website(host, website):
        return "website"
    if host_matches_company(host, company):
        return "name"
    return None


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
    m = re.search(r"(\d+)(\+?)\s*days?", sl)
    if m:  # "30+ Days Ago" is a lower bound: use it as-is (older than any cutoff we apply)
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


@dataclass
class SearchHit:
    url: str
    title: str = ""
    snippet: str = ""
    provider: str = ""


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
        raw = "v3|" + "|".join([" ".join(t for t in _company_tokens(company or "") if t not in _LEGAL), normalize_title(title), "".join(loc), apply_url or ""])
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
# Diagnostics (why did a posting get no origin?)
# --------------------------------------------------------------------------------------------

REASON_LABELS = {
    "below_threshold": "meilleur candidat sous le seuil de confiance",
    "redirect_aggregator": "la redirection aboutit sur un site d'annonces",
    "employer_not_corroborated": "site non relié à l'entreprise (hôte et page)",
    "location_mismatch": "lieu de l'annonce d'origine différent",
    "search_none_validated": "résultats de recherche, aucun validé",
    "search_failed": "moteur de recherche en échec ou bloqué",
    "search_no_results": "recherche web sans aucun résultat",
    "careers_not_found": "site carrières de l'entreprise introuvable",
    "http_blocked": "site inaccessible en HTTP (bloqué ou sans réponse)",
    "redirect_failed": "redirection impossible à suivre",
    "no_candidate": "aucun candidat trouvé (ATS, page carrières, recherche web)",
    "no_apply_url": "aucun lien de candidature capturé (et rien trouvé)",
}
# the outcome of the search steps (3-5) always outranks the plain fact that no apply_url was captured
_REASON_ORDER = ("below_threshold", "redirect_aggregator", "employer_not_corroborated", "location_mismatch",
                 "search_none_validated", "search_failed", "search_no_results", "careers_not_found", "http_blocked",
                 "redirect_failed", "no_candidate", "no_apply_url")


@dataclass
class Trace:
    """What happened while resolving ONE posting; filled through `note` / `note_best` / `tlog` / `tcand` (a ContextVar, so
    the Resolver methods keep their signatures). Company names, titles and hosts only."""
    notes: list[tuple[str, str]] = field(default_factory=list)
    best_url: str | None = None
    best_conf: float = 0.0
    via: str = ""
    lines: list[str] = field(default_factory=list)
    cands: dict[str, list[tuple[float, str]]] = field(default_factory=dict)  # step -> [(rank, text)] (top 3 kept)

    def main_reason(self, p: JobPosting) -> tuple[str, str]:
        codes = {c: d for c, d in reversed(self.notes)}
        if self.best_url and "below_threshold" not in codes:
            codes["below_threshold"] = f"{_host(self.best_url)} ({self.best_conf:.2f})"
        if not p.apply_url:
            codes.setdefault("no_apply_url", "")
        for code in _REASON_ORDER:
            if code in codes:
                return code, codes[code]
        return "no_candidate", ""

    def render(self) -> list[str]:
        out = list(self.lines)
        for step, items in self.cands.items():
            out.append(f"  candidats [{step}] (3 meilleurs) :")
            out += [f"    - {text}" for _, text in sorted(items, key=lambda x: -x[0])[:3]]
        return out


_TRACE: contextvars.ContextVar[Trace | None] = contextvars.ContextVar("origin_trace", default=None)


def note(code: str, detail: str = "") -> None:
    if (t := _TRACE.get()) is not None:
        t.notes.append((code, detail))


def tlog(text: str) -> None:
    if (t := _TRACE.get()) is not None:
        t.lines.append(text)


def tcand(step: str, url: str, title: str | None, tscore: float | None, loc: bool | None, conf: float | None, verdict: str) -> None:
    """Record a candidate considered at a ladder step (only the 3 best by confidence/score are kept)."""
    if (t := _TRACE.get()) is None:
        return
    text = (f"{_host(url)} | titre « {(title or '')[:60]} » | score titre {'-' if tscore is None else f'{tscore:.0f}'}"
            f" | lieu {'?' if loc is None else ('ok' if loc else 'différent')} | confiance {'-' if conf is None else f'{conf:.2f}'} | {verdict}")
    rank = (conf or 0.0) * 1000 + (tscore or 0.0)
    items = t.cands.setdefault(step, [])
    items.append((rank, text))
    items.sort(key=lambda x: -x[0])
    del items[3:]


def note_best(url: str | None, conf: float) -> None:
    if (t := _TRACE.get()) is not None and url and conf > t.best_conf:
        t.best_url, t.best_conf = url, conf


@dataclass
class Resolution:
    posting: JobPosting  # the resolved posting (origin fields filled or cleared)
    via: str = ""  # ladder step that hit: direct, direct_host, direct_param, direct_redirect, ats_api, careers_*, web_search
    reason: str = ""  # REASON_LABELS key when there is no origin
    detail: str = ""  # host / candidate (company-level data only)
    best_url: str | None = None
    best_conf: float = 0.0
    cached: bool = False
    trace: list[str] = field(default_factory=list)  # human-readable ladder trace (company/title/host only)

    @property
    def found(self) -> bool:
        return bool(self.posting.origin_url)


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
        self.client = ctx.http(timeout=20, max_redirects=8, event_hooks={"request": [require_http_request]})
        self._boards: dict[str, asyncio.Future] = {}
        self.sem = asyncio.Semaphore(CONCURRENCY)
        self.ddg_lock = asyncio.Lock()
        self.ddg_blocked = False
        self.fc_lock = asyncio.Lock()
        self.fc_last = 0.0
        self._domains: dict[str, asyncio.Future] = {}

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

    async def _head(self, url: str, *, gap: float | None = None) -> httpx.Response | None:
        if not is_http_url(url):
            return None
        await self.pacer.wait(url, gap)
        try:
            return await self.client.head(url)
        except (httpx.HTTPError, ValueError) as e:
            log.debug("HEAD %s failed: %s", url, e)
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
                if sc >= 60:
                    tcand(via, c.url, c.title, sc, None, None, "titre trop différent")
                continue
            loc = None if p.remote == "full" else location_match(p.location, c.location)
            if loc is False:
                tcand(via, c.url, c.title, sc, False, None, "lieu différent")
                continue
            conf = round(score_to_confidence(sc, loc) * scale, 3)
            if via != "ats_api" and host_corroborated(c.url, p.company, p.company_website):
                # the candidate sits on the employer's own domain: the discount for "might be someone else's page" is moot
                conf = max(conf, round(min(0.62 + (sc - 85) * 0.02, 0.9) - (0.0 if loc is not None else 0.02), 3))
            if via == "ats_api" and not employer_verified(p.company, c):
                conf = min(conf, 0.59)
            tcand(via, c.url, c.title, sc, loc, conf, "accepté" if conf >= MIN_CONFIDENCE else "confiance sous le seuil"
                  + (" (employeur non vérifié)" if via == "ats_api" and not employer_verified(p.company, c) else ""))
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
            c = await self.search_workday(ref, search_title(p.title) or p.title)
            for cand in c:  # reached only through a curated seed derived from the company name: the tenant IS the employer
                cand.employer = cand.employer or p.company
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
            note("careers_not_found", "site de l'entreprise inconnu et introuvable par déduction")
            return None
        if "//" not in site:
            site = "https://" + site
        if not is_http_url(site):
            return None
        pages = await self._careers_pages(site, p.company, website=p.company_website)
        if not pages:
            note("careers_not_found", _host(site))
        tlog(f"  page carrières : {_host(site)} -> {len(pages)} page(s)")
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
            if is_aggregator(str(r.url)) or not (host_matches_company(_host(str(r.url)), p.company) or host_matches_website(_host(str(r.url)), p.company_website)
                                                  or company_in_text(p.company, _host(str(r.url)) + " " + html_to_text(html), loose=True)):
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
                    if title is not None and company_in_text(p.company, title.text() or "", loose=True):
                        return f"{r.url.scheme}://{r.url.host}"
        return None

    async def _careers_pages(self, site: str, company: str | None = None, website: str | None = None) -> list[str]:
        r = await self._get(site)
        if r is None or r.status_code >= 400 or is_aggregator(str(r.url)):
            if r is None or r.status_code in (401, 403, 429, 999):
                note("http_blocked", _host(site))
            return []
        if not (host_matches_company(_host(str(r.url)), company) or host_matches_website(_host(str(r.url)), website)
                or company_in_text(company, _host(str(r.url)) + " " + html_to_text(r.text), loose=True)):
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
    @staticmethod
    def _significant_words(title: str, n: int = 6) -> str:
        stop = {"de", "du", "la", "le", "les", "des", "et", "en", "d", "l", "pour", "au", "aux", "a", "un", "une", "of", "the", "and", "for"}
        words = [w for w in re.split(r"\s+", title) if w and re.sub(r"\W", "", w.lower()) not in stop]
        return " ".join(words[:n])

    def search_queries(self, p: JobPosting, domain: str | None = None) -> list[str]:
        """Most specific first: employer + short title (+ contract word), the employer's own domain (known or just found),
        known ATS hosts, a shortened unquoted variant (long quoted titles often return nothing), then the broad careers
        query. At most 5."""
        company = main_company_name(p.company)
        short = search_title(p.title)
        if not short:
            return []
        shorter = self._significant_words(short, 6)
        word = ""
        blob = f"{p.title} {p.contract_type.value if p.contract_type else ''}".lower()
        if "stage" in blob or "intern" in blob:
            word = "stage"
        elif "alternan" in blob or "apprenti" in blob:
            word = "alternance"
        queries: list[str] = []
        if company:
            queries.append(f'"{company}" "{short}"' + (f" {word}" if word else ""))
        if not domain and p.company_website and not is_aggregator(p.company_website):
            domain = registrable_domain(_host(p.company_website))
        if domain:
            queries.append(f'site:{domain} "{short}"')
            if shorter != short:
                queries.append(f"site:{domain} {shorter}")
        if company:
            queries.append(f'"{company}" "{short}" (site:myworkdayjobs.com OR site:smartrecruiters.com OR site:lever.co OR '
                           f'site:greenhouse.io OR site:welcomekit.co OR site:teamtailor.com OR site:recruitee.com OR site:successfactors.eu)')
            if shorter != short or not domain:
                queries.append(f"{company} {shorter}" + (f" {word}" if word else ""))
            queries.append(f'"{short}" "{company}" (carrieres OR careers OR jobs)')
        else:
            queries.append(f'"{short}" careers')
        return list(dict.fromkeys(queries))[:5]

    async def find_careers_domain(self, p: JobPosting) -> tuple[str | None, list[SearchHit]]:
        """Registrable domain of the employer's own site: `company_website` when known, else the first search result whose
        host is corroborated by the brand name."""
        if p.company_website and not is_aggregator(p.company_website):
            return registrable_domain(_host(p.company_website if "//" in p.company_website else "//" + p.company_website)), []
        brand = main_company_name(p.company)
        if not brand:
            return None, []
        if brand.lower() not in self._domains:
            self._domains[brand.lower()] = asyncio.ensure_future(self._find_domain(p, brand))
        return await self._domains[brand.lower()]

    async def _find_domain(self, p: JobPosting, brand: str) -> tuple[str | None, list[SearchHit]]:
        hits = await self._search_hits(f"{brand} carrières recrutement offres stage")
        for h in hits:
            host = _host(h.url)
            if host and not is_aggregator(h.url) and host_matches_company(host, p.company):
                return registrable_domain(host), hits
        return None, hits

    def evaluate_hit(self, p: JobPosting, hit: SearchHit) -> Match | None:
        """Accept a search result from its URL + title/snippet alone when the host is the employer's own domain or an ATS
        tenant that is the employer's, the URL points at one posting and the title matches. JS-only careers sites (Workday,
        SuccessFactors, Phenom...) return an empty shell to a plain fetch, so page validation can never work there."""
        url = clean_url(hit.url)
        step = "résultats web"
        if not is_http_url(url) or is_aggregator(url):
            return None
        ats = classify_ats(url)
        if ats and tenant_matches_company(url, p.company):
            kind, cap = "ats_tenant", 0.85
        elif host_corroborated(url, p.company, p.company_website):
            kind, cap = "host", 0.8
        else:
            tcand(step, url, hit.title, None, None, None, "hôte non relié à l'entreprise")
            return None
        if not looks_like_posting(url):
            tcand(step, url, hit.title, None, None, None, "pas une annonce précise")
            return None
        variants = [hit.title]
        variants += [x.strip() for x in re.split(r"\s+[|•·–—-]\s+|\s+:\s+", hit.title) if x.strip()]
        slug = [x for x in urlparse(url).path.split("/") if x]
        slug_text = re.sub(r"[-_]+", " ", re.sub(r"[_-](?:r|req|jr)?\d{3,}[a-z0-9-]*$", "", slug[-1], flags=re.I)) if slug else ""
        score = max((title_score(p.title, v) for v in variants if v), default=0.0)
        if slug_text:
            sl = core_title(normalize_title(slug_text))
            pt = core_title(normalize_title(p.title))
            if len(sl.split()) >= 3 and pt.startswith(sl):  # truncated slug of the same title
                score = max(score, 92.0)
            score = max(score, title_score(p.title, slug_text))
        if score < 85 and (refs := job_refs(p)):
            blob = f"{url} {hit.title} {hit.snippet}".lower()
            if any(ref in blob for ref in refs) and score >= 55:
                score = 90.0
        if score < 85:
            tcand(step, url, hit.title, score, None, None, "titre non concordant")
            return None
        loc_text = None
        if ats == "workday":
            segs = [x for x in urlparse(url).path.split("/") if x]
            if "job" in segs and segs.index("job") + 1 < len(segs) - 1:
                loc_text = re.sub(r"[-_]+", " ", segs[segs.index("job") + 1])
        loc = None if p.remote == "full" else location_match(p.location, loc_text)
        if loc is False:
            tcand(step, url, hit.title, score, False, None, "lieu différent")
            note("location_mismatch", _host(url))
            return None
        conf = round(min(0.62 + (score - 85) * 0.02, cap), 3)
        tcand(step, url, hit.title, score, loc, conf, f"accepté ({'tenant ATS' if kind == 'ats_tenant' else 'domaine employeur'})")
        return Match(Candidate(url, hit.title or p.title, ats or ats_or_site(url, None), location=loc_text), score, conf, "web_search")

    async def confirm_workday(self, p: JobPosting, m: Match) -> Match:
        """A Workday result: ask the tenant's public JSON API for the exact posting (canonical URL, posted date)."""
        ref = board_ref(m.cand.url)
        if not ref or ref.ats != "workday":
            return m
        try:
            cands = await self.search_workday(ref, search_title(p.title) or p.title)
        except Exception as e:  # noqa: BLE001
            tlog(f"  Workday API : erreur {type(e).__name__}")
            return m
        for c in cands:
            c.employer = p.company
        best = self.best_match(p, cands, "workday_api")
        tlog(f"  Workday API {ref.host}: {len(cands)} offre(s), " + (f"confirmé ({best.conf:.2f})" if best else "aucune concordance"))
        if best:
            return Match(best.cand, best.score, max(m.conf, best.conf), "web_search")
        return m

    async def web_search(self, p: JobPosting) -> Match | None:
        domain, discovery_hits = await self.find_careers_domain(p)
        if domain:
            tlog(f"  domaine de l'entreprise : {domain}")
        elif not p.company_website:
            note("careers_not_found", "aucun site de l'entreprise parmi les résultats de recherche")
        best: Match | None = None
        checked: set[str] = set()
        total_hits = 0
        validated_any = False

        async def consider(hit: SearchHit) -> Match | None:
            nonlocal best, validated_any
            u = clean_url(hit.url)
            if is_aggregator(u) or u in checked:
                return None
            checked.add(u)
            m = self.evaluate_hit(p, hit)
            if m:
                m = await self.confirm_workday(p, m)
            elif len(checked) <= 10:
                m = await self._validate_candidate(p, u)
                if m:
                    tcand("pages validées", u, m.cand.title, m.score, None, m.conf, "accepté" if m.conf >= MIN_CONFIDENCE else "confiance sous le seuil")
            if m:
                validated_any = True
                note_best(m.cand.url, m.conf)
                if best is None or m.conf > best.conf:
                    best = m
            return m

        for hit in discovery_hits:
            total_hits += 1
            await consider(hit)
            if best and best.conf >= 0.75:
                return best
        for q in self.search_queries(p, domain):
            hits = await self._search_hits(q)
            total_hits += len(hits)
            for hit in hits[:8]:
                await consider(hit)
                if best and best.conf >= 0.75:
                    return best
        if best is None or best.conf < MIN_CONFIDENCE:
            if total_hits == 0:
                if not any(c == "search_failed" for c, _ in (_TRACE.get().notes if _TRACE.get() else [])):
                    note("search_no_results", "aucun résultat sur toutes les requêtes")
            else:
                t = _TRACE.get()
                top = max((it for items in (t.cands.values() if t else []) for it in items), default=None, key=lambda x: x[0])
                note("search_none_validated", f"{total_hits} résultat(s)" + (f", meilleur : {top[1].split(' | ')[0]}" if top else ""))
        return best

    async def _search_urls(self, query: str) -> list[str]:
        return [h.url for h in await self._search_hits(query)]

    async def _search_hits(self, query: str) -> list[SearchHit]:
        """Firecrawl first (key configured), DuckDuckGo when Firecrawl errors OR comes back empty. The trace says which
        provider answered."""
        key = self.ctx.settings.firecrawl_api_key
        failures: list[str] = []
        answered = False
        if key:
            try:
                from firecrawl import Firecrawl

                def run():
                    res = Firecrawl(api_key=key).search(
                        query + " -site:linkedin.com -site:indeed.com -site:glassdoor.com",
                        limit=8, country="FR", exclude_domains=["linkedin.com", "indeed.com", "indeed.fr", "glassdoor.com", "glassdoor.fr"],
                    )
                    web = getattr(res, "web", None) or []
                    out = []
                    for w in web:
                        get = (lambda k, w=w: w.get(k) if isinstance(w, dict) else getattr(w, k, None))
                        if get("url"):
                            out.append(SearchHit(get("url"), get("title") or "", get("description") or get("snippet") or "", "firecrawl"))
                    return out

                # One Firecrawl search at a time, spaced out, and a rate-limit answer is waited out instead of giving up:
                # a burst of parallel postings used to exhaust the per-minute quota and lose the whole search step.
                hits = None
                for attempt in range(FIRECRAWL_RETRIES + 1):
                    async with self.fc_lock:
                        wait = FIRECRAWL_GAP_S - (time.monotonic() - self.fc_last)
                        if wait > 0:
                            await asyncio.sleep(wait)
                        try:
                            hits = await asyncio.to_thread(run)
                        except Exception as e:  # noqa: BLE001
                            if "ratelimit" not in type(e).__name__.lower() or attempt == FIRECRAWL_RETRIES:
                                raise
                            tlog(f"  recherche [firecrawl] limite de débit : nouvelle tentative dans {FIRECRAWL_BACKOFF_S * (attempt + 1):.0f} s")
                            self.fc_last = time.monotonic()
                            await asyncio.sleep(FIRECRAWL_BACKOFF_S * (attempt + 1))
                            continue
                        finally:
                            self.fc_last = time.monotonic()
                    break
                answered = True
                tlog(f"  recherche [firecrawl] {query[:90]!r} -> {len(hits)} résultat(s)")
                if hits:
                    return hits
                tlog("    firecrawl sans résultat : repli sur DuckDuckGo")
            except Exception as e:  # noqa: BLE001
                failures.append(f"firecrawl : {type(e).__name__}")
                tlog(f"  recherche [firecrawl] erreur {type(e).__name__} : repli sur DuckDuckGo")
                log.info("firecrawl search failed (%s); falling back to DuckDuckGo", e)
        html = None
        if self.ddg_blocked:
            failures.append("duckduckgo bloqué pour ce lancement")
        else:
            async with self.ddg_lock:  # one DDG query at a time, ~3s apart
                if self.ddg_blocked:
                    failures.append("duckduckgo bloqué pour ce lancement")
                else:
                    q = query + " -site:linkedin.com -site:indeed.com -site:glassdoor.com -site:glassdoor.fr -site:indeed.fr"
                    url = "https://html.duckduckgo.com/html/?" + urlencode({"q": q, "kl": "fr-fr"})
                    r = await self._get(url, gap=3.0)
                    html = r.text if r is not None and r.status_code == 200 else None
                    if html is None and r is not None and r.status_code in (202, 403, 429):
                        html = await self._ddg_via_browser(url)
                        if html is None:
                            log.info("DuckDuckGo is challenging us (HTTP %s); web-search fallback disabled for this run", r.status_code)
                            self.ddg_blocked = True
                            failures.append(f"duckduckgo bloqué (HTTP {r.status_code})")
                    elif html is None:
                        failures.append("duckduckgo sans réponse" if r is None else f"duckduckgo HTTP {r.status_code}")
        out: list[SearchHit] = []
        if html:
            answered = True
            tree = HTMLParser(html)
            blocks = tree.css("div.result") or tree.css("a.result__a")
            for blk in blocks:
                a = blk if blk.tag == "a" else blk.css_first("a.result__a")
                if a is None:
                    continue
                href = a.attributes.get("href") or ""
                if "uddg=" in href:
                    href = dict(parse_qsl(urlparse(href).query)).get("uddg", "")
                if href.startswith("http"):
                    sn = None if blk.tag == "a" else blk.css_first(".result__snippet")
                    out.append(SearchHit(href, (a.text() or "").strip(), (sn.text() or "").strip() if sn else "", "duckduckgo"))
            tlog(f"  recherche [duckduckgo] {query[:90]!r} -> {len(out)} résultat(s)")
        if not answered and failures:
            note("search_failed", "; ".join(failures))
            tlog("  recherche en échec : " + "; ".join(failures))
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
            note("http_blocked", _host(url))
            return None
        html, final = r.text, clean_url(str(r.url))
        if is_aggregator(final):
            note("redirect_aggregator", _host(final))
            return None
        ld = jsonld_jobposting(html)
        tree = HTMLParser(html[:600_000])
        titles = [ld.get("title")] if ld else []
        titles += [(n.text() or "").strip() for n in tree.css("h1")[:2]]
        if t := tree.css_first("title"):
            titles.append((t.text() or "").strip())
        score = max((title_score(p.title, t) for t in titles if t), default=0)
        page_text = html_to_text(html)
        if score < 85 and (refs := job_refs(p)):
            haystack = (final + " " + html[:300_000]).lower()
            if any(ref in haystack for ref in refs) and score >= 55:
                score = 90.0  # same job reference number on the page: the title only differs by wording
        if score < 85:
            return None
        body = " ".join(titles) + " " + (ld.get("hiringOrganization", {}).get("name", "") if ld and isinstance(ld.get("hiringOrganization"), dict) else "") + " " + _host(final) + " " + page_text[:3000]
        corroborated = host_corroborated(final, p.company, p.company_website)
        if not (corroborated or company_in_text(p.company, body, loose=True)):
            note("employer_not_corroborated", _host(final))
            return None
        ats = ats_or_site(final, html)
        loc = None if p.remote == "full" else location_match(p.location, _ld_location(ld) if ld else None)
        if loc is False:
            note("location_mismatch", _host(final))
            return None
        conf = round(score_to_confidence(score, None if loc is None else loc) * (0.85 if ats != "careers_site" else 0.78), 3)
        if corroborated:
            conf = max(conf, round(min(0.62 + (score - 85) * 0.02, 0.88), 3))
        return Match(Candidate(final, titles[0] or p.title, ats, posted_at=_parse_date(ld.get("datePosted")) if ld else None,
                               description=html_to_text(ld.get("description")) if ld else None), score, conf, "web_search")

    # ---- step 1 ----
    async def direct(self, p: JobPosting) -> tuple[str, str] | None:
        """Returns (final_url, via) when an apply/posting URL leads off-aggregator.

        An URL on the employer's own domain (same registrable domain as `company_website`, or company name tokens in
        the domain) is accepted by its HOST alone ("direct_host"): careers sites that block plain HTTP must not make us
        lose a perfectly good apply link. Other unknown hosts still need the page to name the employer.

        An apply URL that LinkedIn gave as the "apply on company website" target (`raw["apply_source"] == "linkedin_offsite"`)
        is the employer's own choice of destination: after following trackers it is taken as is ("linkedin_offsite"),
        whatever its host (ATS tenants rarely carry the company name). Aggregators, trackers that cannot be followed,
        LinkedIn itself and bare homepages are still refused."""
        if (p.raw or {}).get("apply_source") == "linkedin_offsite" and is_http_url(p.apply_url):
            final = p.apply_url
            if is_aggregator(final):
                final = embedded_target(final) or await self.follow(final)
                if final and is_aggregator(final):
                    final = None
            if final and is_http_url(final) and not is_aggregator(final):
                parsed = urlparse(final)
                if parsed.path.strip("/") or parsed.query:
                    return clean_url(final), "linkedin_offsite"
            note("redirect_aggregator", _host(p.apply_url))
        for u in (p.apply_url, p.url):
            if is_http_url(u) and not is_aggregator(u):
                if classify_ats(u):
                    return clean_url(u), "direct"
                if host_corroborated(u, p.company, p.company_website):
                    return clean_url(u), "direct_host"
                if await self._validate_candidate(p, u):
                    return clean_url(u), "direct"
                note("employer_not_corroborated", _host(u))
        for u in (p.apply_url, p.url):
            if is_http_url(u) and (t := embedded_target(u)):
                if classify_ats(t):
                    return clean_url(t), "direct_param"
                if host_corroborated(t, p.company, p.company_website):
                    return clean_url(t), "direct_host"
                if await self._validate_candidate(p, t):
                    return clean_url(t), "direct_param"
        u = p.apply_url
        if is_http_url(u) and not (p.apply_method in ("easy_apply", "platform") and not is_redirector(u)):
            if final := await self.follow(u):
                if classify_ats(final):
                    return clean_url(final), "direct_redirect"
                if host_corroborated(final, p.company, p.company_website):
                    return clean_url(final), "direct_host"
                if await self._validate_candidate(p, final):
                    return clean_url(final), "direct_redirect"
                note("employer_not_corroborated", _host(final))
        return None

    _BLOCKED = (401, 403, 405, 429, 999)
    _APPLY_ANCHOR = re.compile(r"postuler|apply|voir l.?offre|consulter l.?(?:offre|annonce)|continuer|acc[eé]der|site de l.?entreprise", re.I)

    async def follow(self, url: str) -> str | None:
        """Follow a redirector / tracker to the page it leads to (HEAD, then GET, then meta-refresh / JS / "Postuler" link,
        then a real browser) and return the FINAL url, or None when it still sits on an aggregator or a tracker."""
        if not is_http_url(url):
            return None
        need_browser = False
        for _hop in range(3):  # tracker -> interstitial -> tracker ... bounded
            nxt, need_browser = await self._follow_once(url)
            if nxt is None:
                break
            if not is_aggregator(nxt):
                return nxt
            if nxt == url:
                break
            url = nxt
        return await self._follow_browser(url) if need_browser else None

    async def _follow_once(self, url: str) -> tuple[str | None, bool]:
        """One HEAD/GET round: (final off-aggregator url | another aggregator/tracker url to try again | None,
        whether a real browser could still do better: blocked, challenged or no answer)."""
        blocked = False
        r = await self._head(url, gap=1.5)
        if r is not None:
            final = str(r.url)
            if is_http_url(final) and not is_aggregator(final) and (r.status_code < 400 or r.status_code in self._BLOCKED):
                return final, False
        r = await self._get(url, gap=1.5)
        if r is None:
            note("redirect_failed", _host(url))
            return None, True
        final = str(r.url)
        if is_http_url(final) and not is_aggregator(final) and (r.status_code < 400 or r.status_code in self._BLOCKED):
            return final, False
        blocked = r.status_code in self._BLOCKED
        body = r.text[:200_000]
        m = (re.search(r"http-equiv=[\"']refresh[\"'][^>]*url=([^\"'>]+)", body, re.I)
             or re.search(r"location(?:\.href)?\s*=\s*[\"'](https?://[^\"']+)", body)
             or re.search(r"location\.replace\(\s*[\"'](https?://[^\"']+)", body))
        if m and is_http_url(m.group(1).strip()):
            return m.group(1).strip(), False
        try:
            for a in HTMLParser(body).css("a[href]"):
                href = urljoin(final, a.attributes.get("href") or "")
                if is_http_url(href) and not is_aggregator(href) and self._APPLY_ANCHOR.search(a.text() or "") and _host(href) != _host(final):
                    return href, False
        except Exception:  # noqa: BLE001
            pass
        if is_aggregator(final):
            note("redirect_aggregator", _host(final))
        if blocked:
            note("http_blocked", _host(final))
        return None, blocked or r.status_code >= 400 or "challenge" in body[:5000].lower()

    async def _follow_browser(self, url: str) -> str | None:
        if self.ctx.browser is None or not is_http_url(url):
            return None
        try:
            async with self.ctx.browser.page(self.ctx.profile_id, "origin") as page:
                await self.ctx.browser.goto(page, url)
                for _ in range(16):
                    if not is_aggregator(page.url) and is_http_url(page.url):
                        return page.url
                    await asyncio.sleep(0.5)
        except Exception as e:  # noqa: BLE001
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
        if published and "origin_url" in upd:
            upd["origin_posted_at"] = published
        if not p.description and extra.description:
            upd["description"] = extra.description
    return p.model_copy(update=upd)


async def _resolve_one(r: Resolver, p: JobPosting, *, use_cache: bool = True) -> JobPosting:
    return (await _resolve_traced(r, p, use_cache=use_cache)).posting


async def _resolve_traced(r: Resolver, p: JobPosting, *, use_cache: bool = True) -> Resolution:
    if p.origin_url and (not is_http_url(p.origin_url) or is_aggregator(p.origin_url)):
        p = p.model_copy(update={"origin_url": None, "origin_ats": None, "origin_confidence": None})
    key = DiskCache.key(p.company, p.title, p.location, json.dumps([p.url, p.apply_url, p.company_website, p.remote]))
    if use_cache and (hit := r.cache.get(key)):
        extra = Candidate(hit["origin_url"], p.title, hit["origin_ats"] or "", posted_at=_parse_date(hit.get("posted_at")),
                          description=hit.get("description")) if hit.get("origin_url") else None
        out = _apply(p, hit.get("origin_url"), hit.get("origin_ats"), hit.get("origin_confidence") or 0.0, extra)
        res = Resolution(out, via=hit.get("via") or "", cached=True)
        if not out.origin_url:
            res.reason, res.detail = Trace().main_reason(p)
        return res

    trace = Trace()
    token = _TRACE.set(trace)
    url = ats = None
    conf, extra, via = 0.0, None, ""

    def take(u: str, a: str, c: float, e: Candidate | None, v: str):
        nonlocal url, ats, conf, extra, via
        if not is_http_url(u) or is_aggregator(u):
            return
        if e and p.remote != "full" and location_match(p.location, e.location) is False:
            note("location_mismatch", _host(u))
            return
        url, ats, conf, extra, via = u, a, c, e, v

    def below(m: Match) -> None:
        note_best(m.cand.url, m.conf)
        log.info("origin guess below threshold for %r @ %r: %s (%.2f)", p.title, p.company, m.cand.url, m.conf)

    tlog(f"{p.company or '?'} | {p.title[:70]} | apply_url : {_host(p.apply_url) if p.apply_url else 'aucun'}"
         f"{' (site entreprise connu : ' + _host(p.company_website) + ')' if p.company_website else ''}")
    try:
        d = await r.direct(p)
        tlog("étape 1-2 (lien direct) : " + (f"{_host(d[0])} [{d[1]}]" if d else "rien"))
        if d:  # steps 1-2
            u, v = d
            page = await r.enrich_from_page(u)
            take(u, ats_or_site(u, None), 0.9 if v == "linkedin_offsite" else (1.0 if classify_ats(u) else (0.8 if v == "direct_host" else 0.78)), page, v)
        if url is None:  # step 3
            m = await r.probe_company(p)
            tlog("étape 3 (sondes ATS, slugs " + ", ".join(slug_candidates(p.company, p.company_website)[:4]) + ") : "
                 + (f"{_host(m.cand.url)} {m.conf:.2f}" if m else "rien"))
            if m and m.conf >= MIN_CONFIDENCE:
                take(m.cand.url, m.cand.ats, m.conf, m.cand, m.via)
            elif m:
                below(m)
        if url is None:  # step 4
            m = await r.discover_careers(p)
            tlog("étape 4 (page carrières) : " + (f"{_host(m.cand.url)} {m.conf:.2f}" if m else "rien"))
            if m and m.conf >= MIN_CONFIDENCE:
                take(m.cand.url, m.cand.ats, m.conf, m.cand, m.via)
            elif m:
                below(m)
        if url is None:  # step 5
            m = await r.web_search(p)
            tlog("étape 5 (recherche web) : " + (f"{_host(m.cand.url)} {m.conf:.2f}" if m else "rien"))
            if m and m.conf >= MIN_CONFIDENCE:
                take(m.cand.url, m.cand.ats, m.conf, m.cand, m.via)
            elif m:
                below(m)
        if url and not p.description and not (extra and extra.description):
            if page := await r.enrich_from_page(url):
                extra = Candidate(url, page.title, ats or "", page.location, (extra.posted_at if extra and extra.posted_at else page.posted_at), page.description)
        if extra and p.remote != "full" and location_match(p.location, extra.location) is False:
            note("location_mismatch", _host(url or ""))
            url, ats, conf, extra = None, None, 0.0, None
    except Exception as e:  # one posting must never kill the batch
        log.warning("origin resolution failed for %s: %s", p.url, e, exc_info=log.isEnabledFor(logging.DEBUG))
        return Resolution(p, reason="redirect_failed", detail=type(e).__name__)
    finally:
        _TRACE.reset(token)
    log.info("origin %s -> %s [%s %s %.2f]", p.url, url, ats, via, conf)
    out = _apply(p, url, ats, conf, extra)
    r.cache.put(key, {
        "origin_url": out.origin_url, "origin_ats": out.origin_ats, "origin_confidence": out.origin_confidence,
        "posted_at": extra.posted_at.isoformat() if extra and extra.posted_at else None,
        "description": (extra.description[:20000] if extra and extra.description and not p.description else None),
        "via": via,
    })
    res = Resolution(out, via=via if out.origin_url else "", best_url=trace.best_url, best_conf=trace.best_conf, trace=trace.render())
    if not out.origin_url:
        res.reason, res.detail = trace.main_reason(p)
    return res


def is_wttj(url: str) -> bool:
    return "welcometothejungle.com" in url


async def resolve_origins_detailed(postings: list[JobPosting], ctx: SourceContext, *, use_cache: bool = True,
                                   on_done=None) -> list[Resolution]:
    """Like `resolve_origins` but returns, per posting, the ladder step that hit or the reason for the miss.
    `use_cache=False` ignores the disk cache (hits AND the 3-day miss back-off) and re-runs the whole ladder."""
    if not postings:
        return []
    r = Resolver(ctx)

    async def guarded(p: JobPosting) -> Resolution:
        async with r.sem:
            res = await _resolve_traced(r, p, use_cache=use_cache)
        if on_done is not None:
            on_done(res)
        return res

    try:
        return list(await asyncio.gather(*(guarded(p) for p in postings)))
    finally:
        await r.aclose()


async def resolve_origins(postings: list[JobPosting], ctx: SourceContext) -> list[JobPosting]:
    """Fill origin_url / origin_ats / origin_confidence (and posted_at / description when the origin has better).

    Returns new JobPosting objects in the same order; never raises for a single posting.
    """
    return [res.posting for res in await resolve_origins_detailed(postings, ctx)]
