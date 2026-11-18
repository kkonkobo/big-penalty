"""Small helpers shared by the French job-board connectors (wttj, apec, cadremploi, jobteaser, ...).

Underscore-prefixed on purpose: the source registry skips it.
"""

from __future__ import annotations

import html as _html
import re
from datetime import datetime, timezone

import httpx
from bs4 import BeautifulSoup

from jobhunt.models import ContractType

_BLOCK_TAGS = {"p", "div", "br", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "section"}


def html_to_text(raw: str | None) -> str | None:
    """HTML fragment (or already-plain text) -> readable plain text with line breaks."""
    if not raw:
        return None
    if "<" not in raw:
        text = _html.unescape(raw)
    else:
        soup = BeautifulSoup(raw, "html.parser")
        for n in soup(["script", "style"]):
            n.decompose()
        for n in soup.find_all("br"):
            n.replace_with("\n")
        for n in soup.find_all("li"):
            n.insert(0, "- ")
        for n in soup.find_all(["p", "div", "li", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "ul", "ol"]):
            n.append("\n")
        text = soup.get_text()
    text = text.replace("\xa0", " ").replace("\r", "")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" ?\n ?", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip() or None


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    v = value.strip().replace("Z", "+00:00")
    for fmt in (None, "%d/%m/%Y"):
        try:
            d = datetime.fromisoformat(v) if fmt is None else datetime.strptime(v, fmt)
            return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def guess_contract(text: str | None) -> ContractType:
    """Best-effort contract type from free text (French board labels)."""
    t = (text or "").lower()
    if not t:
        return ContractType.UNKNOWN
    if "alternance" in t or "apprentissage" in t or "professionnalisation" in t:
        return ContractType.ALTERNANCE
    if "stage" in t or "intern" in t:
        return ContractType.STAGE
    if "vie" == t.strip() or "v.i.e" in t or "volontariat international" in t:
        return ContractType.VIE
    if "intérim" in t or "interim" in t or "travail temporaire" in t or "mission d'intérim" in t:
        return ContractType.INTERIM
    if "freelance" in t or "indépendant" in t or "portage" in t:
        return ContractType.FREELANCE
    if "cdi" in t or "durée indéterminée" in t or "duree indeterminee" in t:
        return ContractType.CDI
    if "cdd" in t or "durée déterminée" in t or "duree determinee" in t or "temporaire" in t:
        return ContractType.CDD
    return ContractType.OTHER


_NUM = r"(\d{1,3}(?:[   .]\d{3})+|\d+(?:[.,]\d+)?)"


def _to_float(s: str) -> float:
    s = re.sub(r"[   ]", "", s)
    if re.fullmatch(r"\d{1,3}(\.\d{3})+", s):
        s = s.replace(".", "")
    return float(s.replace(",", "."))


def parse_salary_text(text: str | None) -> tuple[int | None, int | None]:
    """Parse '45 - 55 K€ brut annuel', '45000 à 55000 €', '3 500 € / mois' -> yearly gross EUR (min, max).

    Returns (None, None) when it can't be derived confidently (hourly/daily rates, no numbers...).
    """
    if not text:
        return None, None
    t = text.lower().replace(" ", " ").replace("\xa0", " ")
    nums = re.findall(_NUM + r"\s*(k)?", t)
    vals: list[float] = []
    for n, k in nums:
        try:
            v = _to_float(n)
        except ValueError:
            continue
        if k:
            v *= 1000
        vals.append(v)
    if not vals:
        return None, None
    has_k = "k€" in t or "k €" in t or re.search(r"\dk\b", t) is not None
    if has_k:
        vals = [v * 1000 if v < 1000 else v for v in vals]
    vals = vals[:2]
    monthly = any(w in t for w in ("mois", "mensuel", "/m", "par mois"))
    if "heure" in t or "/h" in t or "jour" in t or "journalier" in t or "tjm" in t:
        return None, None
    if monthly or all(v < 6000 for v in vals):
        if monthly or all(500 <= v < 6000 for v in vals):
            vals = [v * 12 for v in vals]
    if any(v < 8000 or v > 1_000_000 for v in vals):
        return None, None
    lo, hi = min(vals), max(vals)
    return int(lo), int(hi)


async def geocode_city(client: httpx.AsyncClient, location: str | None, postal_code: str | None = None):
    """(lat, lng, dept_code, label) via the public BAN API, or None. Used for radius searches."""
    q = postal_code or location
    if not q or q.strip().lower() in ("france", "remote", "télétravail"):
        return None
    try:
        r = await client.get(
            "https://api-adresse.data.gouv.fr/search/",
            params={"q": q, "type": "municipality", "limit": 1},
            headers={"Accept": "application/json"},
        )
        r.raise_for_status()
        feats = r.json().get("features") or []
        if not feats:
            return None
        f = feats[0]
        lng, lat = f["geometry"]["coordinates"]
        p = f["properties"]
        return lat, lng, p.get("citycode", "")[:2] or None, p.get("city") or p.get("name")
    except (httpx.HTTPError, ValueError, KeyError):
        return None


def dept_from_postal(pc: str | None) -> str | None:
    if not pc or not re.fullmatch(r"\d{5}", pc):
        return None
    return pc[:3] if pc.startswith(("97", "98")) else pc[:2]


# ---------------------------------------------------------------------------------------------
# Browser helpers (BrowserPool.goto matches loose markers such as "recaptcha/api" that legit pages
# embed, e.g. cadremploi.fr; so connectors that hit such pages use this stricter variant).
# ---------------------------------------------------------------------------------------------
_CHALLENGE_TITLES = ("just a moment", "attention required", "un instant", "access denied", "vérification", "verify you are human")
_CHALLENGE_BODY = ("captcha-delivery.com", "geo.captcha-delivery", "Verify you are human", "cf-turnstile", "px-captcha")


async def page_is_challenge(page) -> bool:
    try:
        title = (await page.title() or "").lower()
        if any(t in title for t in _CHALLENGE_TITLES):
            return True
        html = (await page.content())[:150_000]
    except Exception:
        return False
    return any(m in html for m in _CHALLENGE_BODY) and len(html) < 20_000


async def safe_goto(pool, page, url: str, *, wait_until: str = "domcontentloaded", challenge_timeout_s: float = 180) -> None:
    """Paced navigation; on a real bot challenge wait for a human (headful) or raise ChallengeRequired."""
    import asyncio
    import time

    from jobhunt.browser import human_pause
    from jobhunt.sources.base import ChallengeRequired

    await pool.pacer.wait(url)
    await page.goto(url, wait_until=wait_until, timeout=60_000)
    await human_pause(0.8, 1.8)
    if not await page_is_challenge(page):
        return
    if pool.headless:
        raise ChallengeRequired(f"bot challenge on {url}; rerun with JOBHUNT_HEADLESS=0 and solve it")
    deadline = time.monotonic() + challenge_timeout_s
    while time.monotonic() < deadline:
        await asyncio.sleep(2)
        if not await page_is_challenge(page):
            await human_pause(1, 2)
            return
    raise ChallengeRequired(f"challenge on {url} not solved within {challenge_timeout_s}s")
