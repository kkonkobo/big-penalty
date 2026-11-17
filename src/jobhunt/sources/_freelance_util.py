"""Small helpers shared by the freelance-board connectors (not a Source; skipped by the registry)."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from html import unescape
from zoneinfo import ZoneInfo

from selectolax.lexbor import LexborHTMLParser

WORKING_DAYS_PER_YEAR = 218  # assumption used everywhere to convert a TJM (daily rate) to a yearly figure


def html_to_text(html: str | None) -> str:
    """Plain text from an HTML fragment, keeping paragraph / list / <br> structure as newlines."""
    if not html:
        return ""
    html = re.sub(r"(?i)<br\s*/?>", "\n", html)
    html = re.sub(r"(?i)</(p|div|h[1-6]|ul|ol|tr)>", "\n", html)
    html = re.sub(r"(?i)<li[^>]*>", "\n- ", html)
    tree = LexborHTMLParser(f"<body>{html}</body>")
    text = tree.body.text(separator="") if tree.body else ""
    text = unescape(text).replace("\xa0", " ")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def yearly_from_daily(daily: int | float | None) -> int | None:
    return int(round(daily * WORKING_DAYS_PER_YEAR)) if daily else None


def daily_text(lo: int | float | None, hi: int | float | None) -> str | None:
    """'450-550 €/jour' (or '450 €/jour')."""
    lo = int(lo) if lo else None
    hi = int(hi) if hi else None
    if lo and hi and lo != hi:
        return f"{lo}-{hi} €/jour"
    v = lo or hi
    return f"{v} €/jour" if v else None


_NUM = r"(\d[\d\s.,]*)"


def parse_daily_rate(text: str | None) -> tuple[int | None, int | None]:
    """Best-effort parse of free-text rates like '400 à 450 €/j', '450-530TJM', '410', '500€ - 550€ selon expérience'.

    Only meant for freelance-board fields that are known to hold a daily rate. Returns (min, max) in EUR/day.
    """
    if not text:
        return None, None
    nums = []
    for m in re.finditer(r"(\d{2,4})(?:[.,]\d+)?\s*(k)?", text.replace(" ", " ")):
        if m.group(2):  # '55k' -> yearly salary, not a TJM
            return None, None
        nums.append(int(m.group(1)))
    nums = [n for n in nums if 100 <= n <= 3000]  # plausible TJM range; drops "10 ans", years, etc.
    if not nums:
        return None, None
    return min(nums[:2]), max(nums[:2])


_REL = re.compile(r"il y a\s+(\d+|un|une)\s+(minute|heure|jour|semaine|mois|an)s?", re.I)


def parse_relative_fr(text: str | None, now: datetime | None = None) -> datetime | None:
    """'Il y a 11 jours' -> datetime (UTC). 'Aujourd'hui' / 'Hier' handled too."""
    if not text:
        return None
    now = now or datetime.now(timezone.utc)
    t = text.strip().lower()
    if t.startswith("aujourd"):
        return now
    if t.startswith("hier"):
        return now - timedelta(days=1)
    m = _REL.search(t)
    if not m:
        return None
    n = 1 if m.group(1) in ("un", "une") else int(m.group(1))
    unit = m.group(2).lower()
    delta = {
        "minute": timedelta(minutes=n),
        "heure": timedelta(hours=n),
        "jour": timedelta(days=n),
        "semaine": timedelta(weeks=n),
        "mois": timedelta(days=30 * n),
        "an": timedelta(days=365 * n),
    }[unit]
    return now - delta


def parse_iso(s: str | None) -> datetime | None:
    """ISO date/datetime -> timezone-aware datetime (naive values are taken as Europe/Paris)."""
    if not s:
        return None
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=ZoneInfo("Europe/Paris"))


GENERIC_WORDS = {"développeur", "developpeur", "ingénieur", "ingenieur", "consultant", "expert", "senior",
                 "junior", "confirmé", "freelance", "mission", "systèmes", "systemes", "système", "systeme"}


def keyword_attempts(keywords: str, max_words: int = 2) -> list[str]:
    """For boards whose search is a strict AND on every word (no stemming): the full phrase, then the most specific
    single words (generic ones like "développeur" last; singular first: "embarqués" -> "embarqué")."""
    attempts = [keywords]
    if len(keywords.split()) > 1:
        words = {w for w in re.findall(r"[\w+#.-]+", keywords.lower()) if len(w) >= 4 or re.search(r"[+#]", w)}
        specific = sorted((w for w in words if w not in GENERIC_WORDS), key=len, reverse=True)
        generic = sorted((w for w in words if w in GENERIC_WORDS), key=len, reverse=True)
        for w in (specific + generic)[:max_words]:
            if w.endswith(("s", "x")) and len(w) > 4:
                attempts.append(w[:-1])
            attempts.append(w)
    return attempts
