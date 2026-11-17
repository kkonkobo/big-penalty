"""Helpers shared by the French public-service connectors (France Travail, La Bonne Alternance,
Choisir le service public, VIE): text cleanup, salary/remote/contract parsing, geocoding."""

from __future__ import annotations

import html as _html
import logging
import re
import unicodedata
from datetime import date, datetime, timedelta, timezone
from typing import Any

import httpx
from bs4 import BeautifulSoup

from jobhunt.models import ContractType

log = logging.getLogger("jobhunt.sources.fr_public")

_ADRESSE_URL = "https://api-adresse.data.gouv.fr/search/"
_GEO_URL = "https://geo.api.gouv.fr"

HOURS_PER_YEAR = 1607  # legal annual working time in France
MONTHS = {
    "janvier": 1, "février": 2, "fevrier": 2, "mars": 3, "avril": 4, "mai": 5, "juin": 6, "juillet": 7,
    "août": 8, "aout": 8, "septembre": 9, "octobre": 10, "novembre": 11, "décembre": 12, "decembre": 12,
}


def strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")


def norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", strip_accents(s).lower()).strip()


def html_to_text(s: str | None) -> str | None:
    """HTML (or text with entities) -> plain text with sane line breaks."""
    if not s:
        return None
    if "<" in s and ">" in s:
        soup = BeautifulSoup(s, "html.parser")
        for br in soup.find_all("br"):
            br.replace_with("\n")
        for li in soup.find_all("li"):
            li.insert_before("\n- ")
        for p in soup.find_all(["p", "div", "h1", "h2", "h3", "h4", "ul", "ol"]):
            p.append("\n")
        s = soup.get_text()
    s = _html.unescape(s).replace("\xa0", " ").replace("\r", "")
    s = re.sub(r"[ \t]+\n", "\n", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip() or None


def clean(s: str | None) -> str | None:
    if s is None:
        return None
    s = re.sub(r"\s+", " ", _html.unescape(s)).strip()
    return s or None


# ---------------------------------------------------------------- dates

def parse_fr_date(s: str | None) -> datetime | None:
    """'13 novembre 2026', '09/11/2026', '2026-11-09T10:00:00Z', 'Publié il y a 3 jours', 'Publié hier'."""
    if not s:
        return None
    t = s.strip().lower()
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})(?:[t ](\d{2}):(\d{2}))?", t)
    if m:
        y, mo, d, hh, mm = m.groups()
        return datetime(int(y), int(mo), int(d), int(hh or 0), int(mm or 0), tzinfo=timezone.utc)
    m = re.search(r"(\d{1,2})\s+([a-zéûè]+)\s+(\d{4})", t)
    if m and m.group(2) in MONTHS:
        return datetime(int(m.group(3)), MONTHS[m.group(2)], int(m.group(1)), tzinfo=timezone.utc)
    m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", t)
    if m:
        return datetime(int(m.group(3)), int(m.group(2)), int(m.group(1)), tzinfo=timezone.utc)
    today = datetime.combine(date.today(), datetime.min.time(), tzinfo=timezone.utc)
    if "aujourd" in t or "à l'instant" in t:
        return today
    if "hier" in t:
        return today - timedelta(days=1)
    m = re.search(r"il y a (\d+)\+?\s*(minute|heure|jour|semaine|mois|an)", t)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        days = {"minute": 0, "heure": 0, "jour": n, "semaine": 7 * n, "mois": 30 * n, "an": 365 * n}[unit]
        return today - timedelta(days=days)
    return None


# ---------------------------------------------------------------- salary / remote / contract

_NUM = r"(\d[\d\s., ]*)"


def _to_float(s: str) -> float:
    s = s.replace(" ", "").replace(" ", "").replace("\xa0", "")
    if "," in s and "." in s:
        s = s.replace(",", "")
    else:
        s = s.replace(",", ".")
    return float(s)


def parse_salary(text: str | None) -> tuple[int | None, int | None]:
    """Yearly gross EUR (min, max) from France Travail style strings, or (None, None) when unclear.

    'Annuel de 40000.0 Euros à 55000.0 Euros sur 12.0 mois', 'Mensuel de 2500 Euros sur 13 mois',
    'Horaire de 12.5 Euros', '45 K€ - 55 K€ par an', '30000 - 35000 euros/an'."""
    if not text:
        return None, None
    t = text.lower().replace("\xa0", " ")
    nums = re.findall(_NUM + r"\s*(k)?\s*(?:€|euros?|eur)", t)
    rng = re.search(_NUM + r"\s*(k)?\s*(?:à|a|-|–|et)\s*" + _NUM + r"\s*(k)?\s*(?:€|euros?|eur)", t)
    if rng:  # "2 500 à 3 000 € brut mensuel": the first amount carries no currency sign
        nums = [(rng.group(1), rng.group(4) or rng.group(2)), (rng.group(3), rng.group(4))]
    if not nums:
        return None, None
    vals = []
    for n, k in nums:
        try:
            v = _to_float(n.strip())
        except ValueError:
            continue
        vals.append(v * 1000 if k else v)
    if not vals:
        return None, None
    # 'sur 12 mois' is a count of months only when it follows the amounts
    months = 12.0
    mm = re.search(r"sur\s+(\d+(?:[.,]\d+)?)\s*mois", t)
    if mm:
        months = _to_float(mm.group(1))
    if t.startswith("annuel") or "par an" in t or "/an" in t or "annuel" in t or "brut annuel" in t:
        mult = 1.0
    elif t.startswith("mensuel") or "par mois" in t or "/mois" in t or "mensuel" in t:
        mult = months
    elif t.startswith("horaire") or "de l'heure" in t or "/h" in t or "par heure" in t:
        mult = HOURS_PER_YEAR
    elif t.startswith("cachet"):
        return None, None
    else:
        # bare amounts: assume yearly only if clearly large
        mult = 1.0 if min(vals) >= 12000 else 0.0
    if not mult:
        return None, None
    lo, hi = int(round(min(vals) * mult)), int(round(max(vals) * mult))
    if lo < 5000 or hi > 1_000_000:  # sanity: implausible
        return None, None
    return lo, hi


_REMOTE_FULL = re.compile(r"(100\s*%\s*(?:en\s*)?(?:télétravail|remote)|full[\s-]*remote|télétravail\s+(?:total|complet|intégral)|poste\s+(?:en\s+)?(?:full\s+)?remote|entièrement\s+à\s+distance|100\s*%\s*à\s*distance)", re.I)
_REMOTE_PART = re.compile(r"(télétravail|teletravail|travail\s+à\s+distance|hybride|remote|home\s*office|jours?\s+de\s+télétravail)", re.I)
_REMOTE_NEG = re.compile(r"(pas\s+de\s+télétravail|sans\s+télétravail|télétravail\s*:\s*non|télétravail\s+non|aucun\s+télétravail|présentiel\s+uniquement)", re.I)


def detect_remote(*texts: str | None) -> str:
    blob = " ".join(t for t in texts if t)
    if not blob:
        return "unknown"
    if _REMOTE_NEG.search(blob):
        return "none"
    if _REMOTE_FULL.search(blob):
        return "full"
    if _REMOTE_PART.search(blob):
        return "partial"
    return "unknown"


_RE_ALT = re.compile(r"\b(alternan\w*|apprenti\w*|contrat\s+pro(?:fessionnalisation)?|professionnalisation)\b", re.I)
_RE_STAGE = re.compile(r"(?<![a-zé])(stage|stagiaire|internship|intern)(?![a-zé])", re.I)
_RE_VIE = re.compile(r"\bV\.?I\.?E\b|volontariat international", re.I)


def contract_from_title(title: str | None, default: ContractType = ContractType.UNKNOWN) -> ContractType:
    """Titles often reveal what the contract field hides (partner feeds label stages as CDI)."""
    if not title:
        return default
    if _RE_ALT.search(title):
        return ContractType.ALTERNANCE
    if _RE_STAGE.search(title):
        return ContractType.STAGE
    if _RE_VIE.search(title):
        return ContractType.VIE
    return default


def department_of(postal_or_code: str | None) -> str | None:
    if not postal_or_code:
        return None
    p = postal_or_code.strip()
    if p[:2] in ("97", "98") and len(p) >= 3:
        return p[:3]
    if p[:2] == "20" and len(p) == 5:  # Corsica 200xx/201xx -> 2A, 202xx+ -> 2B
        return "2A" if p[:3] in ("200", "201") else "2B"
    return p[:2] if p[:2].isdigit() or p[:2] in ("2A", "2B") else None


# ---------------------------------------------------------------- geocoding (free government APIs)

class Place(dict):
    """{'label','lat','lon','insee','postcode','dept','region'} (any may be missing)."""


async def geocode(client: httpx.AsyncClient, text: str | None, postal_code: str | None = None) -> Place | None:
    """Municipality lookup through api-adresse.data.gouv.fr (no key). None if not found."""
    q = (text or postal_code or "").strip()
    if not q or norm(q) in ("france", "toute la france", "remote"):
        return None
    params: dict[str, Any] = {"q": q, "limit": 1, "type": "municipality"}
    if postal_code and not text:
        params["postcode"] = postal_code
    try:
        r = await client.get(_ADRESSE_URL, params=params)
        r.raise_for_status()
        feats = r.json().get("features") or []
    except (httpx.HTTPError, ValueError) as e:
        log.debug("geocode failed for %r: %s", q, e)
        return None
    if not feats:
        return None
    f = feats[0]
    pr = f.get("properties", {})
    lon, lat = f["geometry"]["coordinates"]
    ctx = [c.strip() for c in (pr.get("context") or "").split(",")]
    return Place(
        label=pr.get("label") or q, lat=lat, lon=lon, insee=pr.get("citycode"), postcode=pr.get("postcode"),
        dept=pr.get("depcode") or (ctx[0] if ctx else None), region=ctx[-1] if ctx else None, score=pr.get("score"),
    )


async def find_region(client: httpx.AsyncClient, text: str | None) -> dict | None:
    """Region lookup ('Île-de-France', 'Occitanie') via geo.api.gouv.fr -> {'code','nom'}."""
    if not text:
        return None
    try:
        r = await client.get(f"{_GEO_URL}/regions", params={"nom": text})
        r.raise_for_status()
        data = r.json()
    except (httpx.HTTPError, ValueError):
        return None
    for d in data:
        if norm(d["nom"]) == norm(text):
            return d
    return None
