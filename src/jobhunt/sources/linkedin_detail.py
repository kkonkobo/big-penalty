"""LinkedIn job detail through an external data command (default: the `treg` CLI, endpoint harvestapi.linkedin.job.get).

LinkedIn no longer shows guests where "Apply on company website" leads. A paid catalog endpoint returns the full job,
including `applyMethod.companyApplyUrl`. The command is configured by `Settings.linkedin_detail_cmd`
(JOBHUNT_LINKEDIN_DETAIL_CMD): "auto" (treg when on PATH, else disabled), "off", or a template with `{id}` that is split
with shlex and executed WITHOUT a shell (the id is validated as digits only). Nothing here ever raises to the caller.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shlex
import shutil
import weakref
from dataclasses import dataclass
from datetime import datetime
from typing import Awaitable, Callable
from urllib.parse import urlparse

log = logging.getLogger("jobhunt.linkedin_detail")

ID_RE = re.compile(r"^\d{6,15}$")
TREG_ARGS = ["call", "harvestapi.linkedin.job.get", "--query", "jobId={id}"]
TIMEOUT_S = 30.0
MAX_CONCURRENT = 3

Runner = Callable[[list[str], float], Awaitable["str | None"]]


@dataclass
class LinkedInDetail:
    job_id: str
    apply_url: str | None = None  # employer / ATS URL of "apply on company website", tracking parameters stripped
    easy_apply: bool = False
    company_website: str | None = None
    state: str = "unknown"  # listed | closed | unknown
    posted_at: datetime | None = None


@dataclass
class Usage:
    calls: int = 0
    cost: float = 0.0
    failures: int = 0


USAGE = Usage()  # per-run counters for the report


def reset_usage() -> None:
    USAGE.calls, USAGE.cost, USAGE.failures = 0, 0.0, 0


def command_template(setting: str | None) -> list[str] | None:
    """argv template (with a literal `{id}` token part) or None when the provider is disabled."""
    value = (setting or "auto").strip()
    if value.lower() == "off" or not value:
        return None
    if value.lower() == "auto":
        exe = shutil.which("treg")
        return [exe, *TREG_ARGS] if exe else None
    try:
        argv = shlex.split(value)
    except ValueError:
        return None
    return argv if argv and any("{id}" in a for a in argv) else None


def enabled(setting: str | None = None) -> bool:
    if setting is None:
        from jobhunt.config import get_settings
        setting = get_settings().linkedin_detail_cmd
    return command_template(setting) is not None


_sems: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore]" = weakref.WeakKeyDictionary()


def _sem() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    if loop not in _sems:
        _sems[loop] = asyncio.Semaphore(MAX_CONCURRENT)
    return _sems[loop]


async def _run_subprocess(argv: list[str], timeout: float) -> str | None:
    proc = await asyncio.create_subprocess_exec(*argv, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.DEVNULL)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        await proc.wait()
        raise
    return out.decode("utf-8", "replace")


def parse_stdout(text: str | None) -> dict | None:
    """The JSON object in stdout, tolerating log lines before/after it."""
    if not text:
        return None
    dec = json.JSONDecoder()
    first = None
    for m in re.finditer(r"\{", text):
        try:
            obj, _ = dec.raw_decode(text, m.start())
        except ValueError:
            continue
        if isinstance(obj, dict):
            if "element" in obj or "status" in obj:
                return obj
            first = first or obj
    return first


def _url(value) -> str | None:
    if not isinstance(value, str) or not value.strip().lower().startswith(("http://", "https://")):
        return None
    host = (urlparse(value.strip()).hostname or "").lower()
    return None if host == "linkedin.com" or host.endswith(".linkedin.com") else value.strip()


def parse_detail(data: dict, job_id: str) -> LinkedInDetail | None:
    from jobhunt.origin import _parse_date, clean_url

    el = data.get("element")
    if not isinstance(el, dict):
        return None
    am = el.get("applyMethod") if isinstance(el.get("applyMethod"), dict) else {}
    apply = _url(am.get("companyApplyUrl"))
    easy = bool(el.get("easyApplyUrl")) or "easy" in str(am.get("type") or "").lower()
    company = el.get("company") if isinstance(el.get("company"), dict) else {}
    site = _url(company.get("website"))
    state = str(el.get("jobState") or "").upper()
    if el.get("closedAt") or (state and state != "LISTED") or el.get("jobApplicationLimitReached"):
        status = "closed"
    elif state == "LISTED":
        status = "listed"
    else:
        status = "unknown"
    return LinkedInDetail(job_id=job_id, apply_url=clean_url(apply) if apply else None, easy_apply=easy and not apply,
                          company_website=site, state=status, posted_at=_parse_date(el.get("postedDate")))


async def fetch_job_detail(job_id: str, *, runner: Runner | None = None, setting: str | None = None) -> LinkedInDetail | None:
    """Detail of one LinkedIn job, or None (disabled, bad id, timeout, error, job missing). Never raises."""
    try:
        job_id = str(job_id).strip()
        if not ID_RE.match(job_id):
            return None
        if setting is None:
            from jobhunt.config import get_settings
            setting = get_settings().linkedin_detail_cmd
        template = command_template(setting)
        if template is None and runner is None:
            return None
        argv = [a.replace("{id}", job_id) for a in (template or ["provider", "{id}"])]
        async with _sem():
            USAGE.calls += 1
            try:
                out = await (runner or _run_subprocess)(argv, TIMEOUT_S)
            except asyncio.TimeoutError:
                USAGE.failures += 1
                log.info("linkedin detail %s: timeout", job_id)
                return None
        data = parse_stdout(out)
        if data is None:
            USAGE.failures += 1
            log.info("linkedin detail %s: no JSON in output (%d chars)", job_id, len(out or ""))
            return None
        cost = data.get("cost")
        if isinstance(cost, (int, float)):
            USAGE.cost += float(cost)
        status = data.get("status")
        if isinstance(status, int) and status != 200:
            USAGE.failures += 1
            return None
        return parse_detail(data, job_id)
    except Exception as e:  # noqa: BLE001 - the provider must never break a run
        USAGE.failures += 1
        log.info("linkedin detail %s failed: %s", job_id, type(e).__name__)
        return None
