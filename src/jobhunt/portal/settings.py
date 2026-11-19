"""Portal settings of the Mac side, read from the environment at call time (never cached at import)."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import timedelta

from jobhunt.apply import facts

GRANT_LIFETIME = timedelta(days=7)
MAX_SEND_MINUTES = 20

DEFAULT_WRITE_ONLY = ("disability", "criminal_record")

# A custom question on one of these topics keeps its answer on the Mac (applied to facts.norm_question(question):
# lowercase, accents and punctuation removed).
MASKED_QUESTION_RE = re.compile(r"handicap|rqth|invalidite|casier|judiciaire|condamnation|sante|medical|maladie")


def is_masked_question(text: str | None) -> bool:
    return bool(MASKED_QUESTION_RE.search(facts.norm_question(text or "")))


def _int(name: str, default: int, minimum: int = 0) -> int:
    try:
        return max(minimum, int(os.environ.get(name, "").strip()))
    except ValueError:
        return default


@dataclass(frozen=True)
class PortalSettings:
    write_only: frozenset[str]
    send_every_min: int
    max_sends_per_day: int
    auto_resume: bool

    @classmethod
    def from_env(cls) -> "PortalSettings":
        raw = os.environ.get("JOBHUNT_PORTAL_WRITE_ONLY")
        paths = DEFAULT_WRITE_ONLY if raw is None else tuple(p.strip() for p in raw.split(","))
        resume = os.environ.get("JOBHUNT_PORTAL_AUTO_RESUME", "1").strip().lower() not in ("0", "false", "no", "off")
        return cls(write_only=frozenset(p for p in paths if p), send_every_min=_int("JOBHUNT_PORTAL_SEND_EVERY_MIN", 15),
                   max_sends_per_day=_int("JOBHUNT_PORTAL_MAX_SENDS_PER_DAY", 60), auto_resume=resume)


def get_portal_settings() -> PortalSettings:
    return PortalSettings.from_env()
