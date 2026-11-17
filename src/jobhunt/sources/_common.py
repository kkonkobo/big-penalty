"""Helpers shared by the aggregator connectors (linkedin, indeed, glassdoor, hellowork).

Private module (leading underscore): skipped by the source auto-discovery.
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
import threading
from datetime import date, datetime, timezone
from typing import Any

from bs4 import BeautifulSoup

from jobhunt.models import ContractType, JobPosting, Remote, SearchQuery
from jobhunt.sources.base import SourceError

log = logging.getLogger("jobhunt.sources.aggregators")

KM_PER_MILE = 1.609344
COUNTRY_WORDS = {"france", "fr", "frankreich"}
_JOBSPY_LOCKS: dict[str, Any] = {}

# ---------------------------------------------------------------- text


_BLOCK_TAGS = ("p", "div", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "section")


def html_to_text(html: str | None) -> str | None:
    """HTML (or text already) -> readable plain text, keeping paragraph/list line breaks."""
    if not html:
        return None
    if "<" not in html:
        text = html
    else:
        soup = BeautifulSoup(html, "html.parser")
        for br in soup.find_all("br"):
            br.replace_with("\n")
        for li in soup.find_all("li"):
            li.insert_before("- ")
        for tag in soup.find_all(_BLOCK_TAGS):
            tag.append("\n")
        text = soup.get_text()
    text = text.replace("\xa0", " ")
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r" ?\n ?", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip() or None


# ---------------------------------------------------------------- contract / remote

_RE_ALTERNANCE = re.compile(
    r"alternan|apprentissage|apprenti|contrat de professionnalisation|contrat pro\b|professionnalisation", re.I
)
_RE_STAGE = re.compile(r"\bstage\b|\bstages\b|stagiaire|internship|\bintern\b|\bstage de fin", re.I)
_RE_CDD = re.compile(r"\bCDD\b|contrat à durée déterminée|durée déterminée", re.I)
_RE_CDI = re.compile(r"\bCDI\b|contrat à durée indéterminée|durée indéterminée", re.I)
_RE_INTERIM = re.compile(r"intérim|interim|travail temporaire|mission temporaire", re.I)
_RE_FREELANCE = re.compile(r"freelance|free-lance|indépendant|independant|portage salarial|\bTJM\b|auto-?entrepreneur", re.I)
_RE_VIE = re.compile(r"\bV\.?I\.?E\b|volontariat international", re.I)
_RE_ALT_STRONG = re.compile(
    r"en alternance|contrat d'apprentissage|contrat d’apprentissage|contrat de professionnalisation|poste en alternance", re.I
)


def infer_contract(title: str | None, description: str | None, board_types: list[str] | None = None) -> ContractType:
    """Best-effort French contract type from title, board-provided types (jobspy values) and text."""
    title = title or ""
    desc = description or ""
    types = {t.strip().lower() for t in (board_types or []) if t}
    head = f"{title}\n{desc[:3000]}"

    if _RE_ALTERNANCE.search(title):
        return ContractType.ALTERNANCE
    if _RE_STAGE.search(title):
        return ContractType.STAGE
    if _RE_VIE.search(title):
        return ContractType.VIE
    if _RE_INTERIM.search(title):
        return ContractType.INTERIM
    if re.search(r"\bCDD\b", title):
        return ContractType.CDD
    if re.search(r"\bCDI\b", title):
        return ContractType.CDI
    if _RE_FREELANCE.search(title):
        return ContractType.FREELANCE

    if "internship" in types:
        return ContractType.ALTERNANCE if _RE_ALTERNANCE.search(head) else ContractType.STAGE
    if "temporary" in types:
        return ContractType.INTERIM if _RE_INTERIM.search(head) else ContractType.CDD
    if "contract" in types:
        if _RE_FREELANCE.search(head):
            return ContractType.FREELANCE
        if _RE_CDD.search(head):
            return ContractType.CDD
        return ContractType.OTHER

    has_cdi, has_cdd = bool(_RE_CDI.search(head)), bool(_RE_CDD.search(head))
    if has_cdi != has_cdd:
        return ContractType.CDI if has_cdi else ContractType.CDD
    if not has_cdi and _RE_ALT_STRONG.search(desc[:2500]):
        return ContractType.ALTERNANCE
    return ContractType.UNKNOWN


_RE_FULL_REMOTE = re.compile(
    r"full[ -]?remote|100 ?% ?(?:télétravail|remote|à distance)|télétravail (?:complet|total|intégral)|"
    r"(?:poste|travail) (?:en )?(?:full )?(?:télétravail|remote)|entièrement à distance|remote only|télétravail 5 ?j",
    re.I,
)
_RE_PARTIAL_REMOTE = re.compile(
    r"hybride|hybrid|télétravail (?:partiel|possible|occasionnel|hybride|\d)|\d ?(?:j|jours?) ?(?:/|par|de) ?(?:semaine|sem)? ?(?:de |en )?télétravail|"
    r"jours? de télétravail|télétravail (?:\d|jusqu)|télétravail partiel|possibilité de télétravail|télétravail",
    re.I,
)
_RE_NO_REMOTE = re.compile(r"pas de télétravail|sans télétravail|présence (?:sur site )?obligatoire", re.I)


def infer_remote(text: str | None, is_remote: bool | None = None) -> Remote:
    t = text or ""
    if is_remote:
        return "full"
    if _RE_FULL_REMOTE.search(t):
        return "full"
    if _RE_NO_REMOTE.search(t):
        return "none"
    if _RE_PARTIAL_REMOTE.search(t):
        return "partial"
    return "unknown"


# ---------------------------------------------------------------- salary

_HOURS_PER_YEAR = 1820  # 35h x 52
_DAYS_PER_YEAR = 218  # French standard working days
_FACTORS = {"yearly": 1, "monthly": 12, "weekly": 52, "daily": _DAYS_PER_YEAR, "hourly": _HOURS_PER_YEAR}
_LABELS = {"yearly": "an", "monthly": "mois", "weekly": "semaine", "daily": "jour", "hourly": "heure"}


def salary_from_parts(
    interval: str | None, lo: float | None, hi: float | None, currency: str | None
) -> tuple[str | None, int | None, int | None]:
    """(salary_text, yearly_min_eur, yearly_max_eur). Yearly values only when currency is EUR/unknown."""
    if not interval or interval not in _FACTORS or (lo is None and hi is None):
        return None, None, None
    lo = lo if lo is not None else hi
    hi = hi if hi is not None else lo
    assert lo is not None and hi is not None
    cur = (currency or "EUR").upper()
    sym = "€" if cur == "EUR" else cur

    def fmt(x: float) -> str:
        return f"{x:,.0f}".replace(",", " ") if x >= 100 else f"{x:g}"

    rng = fmt(lo) if lo == hi else f"{fmt(lo)} - {fmt(hi)}"
    text = f"{rng} {sym} / {_LABELS[interval]}"
    if cur != "EUR":
        return text, None, None
    f = _FACTORS[interval]
    ymin, ymax = int(round(lo * f)), int(round(hi * f))
    if not (4_000 <= ymin <= 1_000_000 and ymax >= ymin):
        return text, None, None  # implausible: keep the text only
    return text, ymin, ymax


# ---------------------------------------------------------------- location


def parse_location(raw: str | None) -> tuple[str | None, str | None, str | None]:
    """'Toulouse, O, France' / 'Toulouse, 31, France' / 'Lyon 3e - 69' -> (display, postal_code, department)."""
    if not raw:
        return None, None, None
    raw = raw.strip()
    m = re.fullmatch(r"(.+?) - (\d{2,3}|2[AB])", raw)
    parts = [m.group(1), m.group(2)] if m else [p.strip() for p in raw.split(",") if p.strip()]
    postal = dept = None
    keep: list[str] = []
    for p in parts:
        if p.lower() in COUNTRY_WORDS:
            continue
        if re.fullmatch(r"\d{5}", p):
            postal = p
            dept = dept or (p[:3] if p.startswith(("97", "98")) else p[:2])
        elif re.fullmatch(r"\d{2,3}|2[AB]", p):
            dept = p
        elif len(p) <= 2:
            continue  # opaque region codes ("O")
        else:
            keep.append(p)
    city = ", ".join(keep) or None
    if city and dept:
        display = f"{city} ({dept})"
    else:
        display = city or (dept and f"({dept})") or None
    return display, postal, dept


def search_location(query: SearchQuery) -> str:
    return query.location or query.postal_code or query.department or "France"


def km_to_miles(km: int | None) -> int | None:
    return None if km is None else max(1, round(km / KM_PER_MILE))


# ---------------------------------------------------------------- jobspy bridge


def _clean(v: Any) -> Any:
    if v is None:
        return None
    try:
        if isinstance(v, float) and math.isnan(v):
            return None
    except TypeError:
        pass
    if v.__class__.__name__ in ("NaTType", "NAType"):
        return None
    return v


def _to_dt(d: Any) -> datetime | None:
    d = _clean(d)
    if d is None:
        return None
    if isinstance(d, datetime):
        return d.astimezone(timezone.utc) if d.tzinfo else d.replace(tzinfo=timezone.utc)
    if isinstance(d, date):
        return datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(d))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def jobspy_job_types(query: SearchQuery) -> str | None:
    """Map query.contract_types to a single jobspy job_type (jobspy takes one), else None = no filter."""
    cts = set(query.contract_types)
    if not cts:
        return None
    mapping = {
        ContractType.STAGE: "internship",
        ContractType.ALTERNANCE: "internship",
        ContractType.CDD: "temporary",
        ContractType.INTERIM: "temporary",
        ContractType.CDI: "fulltime",
        ContractType.FREELANCE: "contract",
    }
    out = {mapping.get(c) for c in cts}
    return out.pop() if len(out) == 1 and None not in out else None


async def jobspy_scrape(site: str, query: SearchQuery, *, country: str = "france", **extra: Any) -> list[dict[str, Any]]:
    """Serialize each site's workers, even after caller cancellation.

    JobSpy's rotating sessions bound individual requests to 15 seconds by default;
    its public scrape_jobs API exposes no request-timeout parameter.
    """
    from jobspy import scrape_jobs

    kwargs: dict[str, Any] = dict(
        site_name=site,
        search_term=query.keywords,
        location=search_location(query),
        distance=km_to_miles(query.radius_km) or 30,  # jobspy distances are miles on every board
        is_remote=query.remote_only,
        results_wanted=max(1, query.max_results),
        country_indeed=country,
        description_format="html",
        verbose=0,
    )
    if query.posted_within_days:
        kwargs["hours_old"] = (query.posted_within_days + 1) * 24  # 1 day slack: upstream windows are inclusive/rounded; shortlist cuts at the exact limit
    jt = jobspy_job_types(query)
    if jt:
        kwargs["job_type"] = jt
    kwargs.update(extra)

    lock = _JOBSPY_LOCKS.setdefault(site, threading.Lock())

    def _run():
        with lock:
            return scrape_jobs(**kwargs)

    try:
        df = await asyncio.to_thread(_run)
    except Exception as e:  # jobspy raises ValueError for bad args; network libs for the rest
        raise SourceError(f"{site}: jobspy failed: {e}") from e
    errors = df.attrs.get("errors") or {}
    if errors and df.empty:
        raise SourceError(f"{site}: {'; '.join(str(v) for v in errors.values())}")
    if errors:
        log.warning("%s: partial failure: %s", site, errors)
    return [{k: _clean(v) for k, v in row.items()} for row in df.to_dict("records")]


def row_to_posting(row: dict[str, Any], *, source: str, id_prefix: str, estimated_salary: bool = False) -> JobPosting | None:
    """Map one jobspy row to a JobPosting (source-agnostic)."""
    url = row.get("job_url")
    title = row.get("title")
    raw_id = str(row.get("id") or "")
    if not (url and title and raw_id):
        return None
    source_id = raw_id[len(id_prefix):] if raw_id.startswith(id_prefix) else raw_id
    description = html_to_text(row.get("description"))
    board_types = [t for t in str(row.get("job_type") or "").split(",") if t.strip()]
    loc, postal, dept = parse_location(row.get("location"))
    stext, smin, smax = salary_from_parts(row.get("interval"), row.get("min_amount"), row.get("max_amount"), row.get("currency"))
    if stext and estimated_salary:
        stext += " (estimation)"
    return JobPosting(
        source=source,
        source_id=source_id,
        url=url,
        title=title.strip(),
        company=(row.get("company") or None),
        location=loc,
        postal_code=postal,
        department=dept,
        contract_type=infer_contract(title, description, board_types),
        remote=infer_remote(f"{title}\n{description or ''}", row.get("is_remote")),
        salary_text=stext,
        salary_min_eur=smin,
        salary_max_eur=smax,
        description=description,
        posted_at=_to_dt(row.get("date_posted")),
        company_website=row.get("company_url_direct") or None,
        experience_level=row.get("job_level") or row.get("experience_range") or None,
        raw={k: (str(v) if not isinstance(v, (str, int, float, bool, type(None))) else v) for k, v in row.items() if k != "description"},
    )
