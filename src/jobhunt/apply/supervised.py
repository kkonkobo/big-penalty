"""Attended fill-step state and a private same-user CLI bridge."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import re
import secrets
import stat
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Awaitable, Callable
from urllib.parse import urlsplit

from jobhunt.apply.adapters import Attempt, Match
from jobhunt.apply.destination import audit_url, clean_url, origin
from jobhunt.apply.errors import ApplyError, StepChanged
from jobhunt.apply.identity import canonical_identity
from jobhunt.apply.types import RunResult
from jobhunt.config import get_settings
from jobhunt.dedupe import normalize_title

UNAVAILABLE = "Aucune fenêtre supervisée active pour cette candidature."
WRONG_POSTING = "Le poste affiché ne correspond pas à la candidature réservée : fenêtre fermée."
CONFIRM_AGAIN = "La page a changé. Vérifiez le poste et confirmez de nouveau « Remplir cette étape »."

_VISIBLE_HEADING = """doc => Array.from(doc.querySelectorAll('h1')).filter(node => {
    const style = doc.defaultView.getComputedStyle(node);
    return node.getClientRects().length && style.visibility === 'visible';
}).map(node => node.innerText.trim())"""


def require_posting(match: Match, attempt: Attempt, *urls: str) -> None:
    expected = canonical_identity(attempt.tenant, attempt.requisition_id)
    for url in urls:
        vetted = clean_url(url, navigation=True)
        if not expected or not vetted or origin(vetted) not in {origin(attempt.url), *match.requisition.write_origins}:
            raise ApplyError(WRONG_POSTING)
        try:
            fragment = urlsplit(url).fragment
            candidate = match.adapter.resolve(vetted + (f"#{fragment}" if fragment else ""), frozenset())
        except Exception:
            raise ApplyError(WRONG_POSTING) from None
        if not candidate or canonical_identity(candidate.tenant, candidate.requisition_id) != expected:
            raise ApplyError(WRONG_POSTING)


def _digest(*values: str) -> str:
    return hashlib.sha256(json.dumps(values, ensure_ascii=False).encode()).hexdigest()


def _words(text: str) -> set[str]:
    return {word for word in re.findall(r"\w+", normalize_title(text)) if len(word) >= 3} - {
        "emploi", "jobs", "offre", "poste", "stage", "candidature", "apply", "careers", "pour", "avec", "chez", "the",
    }


@dataclass(frozen=True)
class FillPreview:
    token: str
    run_id: int
    application_id: int
    company: str
    title: str
    reserved_url: str
    page_url: str
    warning: bool
    requires_confirmation: bool


class Session:
    def __init__(self, *, attempt: Attempt, page, match: Match | None, company: str, title: str,
                 recheck: Callable[[], Awaitable[list[str]]], fill: Callable[[str | None], Awaitable[RunResult]]):
        self.attempt, self.page, self.match = attempt, page, match
        self.company, self.title, self.recheck, self.fill = company, title, recheck, fill
        self.lock = asyncio.Lock()
        self.closed = False
        self.pending: tuple[str, str] | None = None
        self.last: RunResult | None = None

    async def _close(self) -> None:
        self.closed = True
        self.pending = None
        for page in self.page.context.pages:
            with contextlib.suppress(Exception):
                await page.close()

    async def _check(self) -> None:
        if self.closed or self.page.is_closed():
            raise ApplyError(UNAVAILABLE)
        problems = await self.recheck()
        if problems:
            self.last = RunResult(status="blocked", message=" ; ".join(problems))
            await self._close()
            raise ApplyError(self.last.message)

    async def posting(self, target=None) -> None:
        try:
            top = self.page.url
            if target is None:
                if top != await self.page.evaluate("() => location.href"):
                    raise ApplyError(WRONG_POSTING)
                urls = (top,)
            else:
                urls = (top, *await target.evaluate("""node => {
                    const view = node.ownerDocument.defaultView;
                    return [view.location.href, node.form?.action || view.location.href];
                }"""))
            if top != self.page.url:
                raise ApplyError(WRONG_POSTING)
            require_posting(self.match, self.attempt, *urls)
        except Exception:
            raise ApplyError(WRONG_POSTING) from None

    async def fingerprint(self) -> tuple[str, str, str]:
        url, title, headings = await self.page.evaluate(
            f"() => [location.href, document.title, ({_VISIBLE_HEADING})(document)]")
        vetted = clean_url(url, navigation=True)
        if not vetted or origin(vetted) != origin(self.attempt.url):
            raise ApplyError(WRONG_POSTING)
        if url != self.page.url:
            raise StepChanged(CONFIRM_AGAIN)
        return _digest(self.attempt.nonce, vetted, urlsplit(url).fragment, _digest(title, *headings)), url, " ".join([title, *headings])

    async def before_mutation(self, target, expected: str | None) -> None:
        if self.match:
            await self.posting(target)
        else:
            current, _, _ = await self.fingerprint()
            if expected is None or not secrets.compare_digest(current, expected):
                raise StepChanged(CONFIRM_AGAIN)

    async def preview(self) -> FillPreview:
        async with self.lock:
            await self._check()
            try:
                current, url, headings = await self.fingerprint()
                if self.match:
                    await self.posting()
            except StepChanged:
                raise
            except ApplyError as exc:
                self.last = RunResult(status="blocked", message=str(exc))
                await self._close()
                raise
            token = secrets.token_urlsafe(32)
            self.pending = token, current
            return FillPreview(token, self.attempt.run_id, self.attempt.application_id, self.company, self.title,
                               audit_url(self.attempt.url), audit_url(url),
                               not bool(_words(headings) & _words(f"{self.company} {self.title}")), self.match is None)

    async def run(self, token: str | None, *, initial: bool = False) -> RunResult:
        async with self.lock:
            await self._check()
            pending, self.pending = self.pending, None
            if not (initial and self.match) and (not pending or not token or not secrets.compare_digest(pending[0], token)):
                raise ApplyError(CONFIRM_AGAIN)
            try:
                await self.before_mutation(None, pending[1] if pending else None)
                await self._check()
                self.last = await self.fill(pending[1] if pending else None)
            except StepChanged:
                raise
            except ApplyError as exc:
                self.last = RunResult(status="blocked", message=str(exc))
            except Exception as exc:
                self.last = RunResult(status="blocked", message=f"Saisie interrompue ({type(exc).__name__}).")
            if self.last.status in ("blocked", "failed"):
                await self._close()
            return self.last


def _socket_path(app_id: int) -> Path:
    namespace = _digest(str(get_settings().data_dir.resolve()))[:16]
    directory = Path("/tmp") / f"bp-fill-{os.getuid()}-{namespace}"
    directory.mkdir(mode=0o700, exist_ok=True)
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ApplyError("Canal local de saisie non sécurisé.")
    return directory / f"{app_id}.sock"


@contextlib.asynccontextmanager
async def local_bridge(session: Session):
    path = _socket_path(session.attempt.application_id)

    async def handle(reader, writer):
        try:
            command = json.loads(await asyncio.wait_for(reader.readline(), timeout=5))
            if command.get("op") == "preview":
                reply = asdict(await session.preview())
            elif command.get("op") == "fill" and isinstance(command.get("token"), str):
                result = await session.run(command["token"])
                reply = {"status": result.status}
            else:
                raise ApplyError(UNAVAILABLE)
        except ApplyError as exc:
            reply = {"error": str(exc)}
        except Exception:
            reply = {"error": UNAVAILABLE}
        try:
            writer.write(json.dumps(reply).encode() + b"\n")
            await writer.drain()
        finally:
            writer.close()

    path.unlink(missing_ok=True)
    server = await asyncio.start_unix_server(handle, path=str(path), limit=4096)
    try:
        yield
    finally:
        server.close()
        await server.wait_closed()
        path.unlink(missing_ok=True)


async def call_local(app_id: int, op: str, token: str | None = None) -> dict:
    try:
        reader, writer = await asyncio.open_unix_connection(str(_socket_path(app_id)), limit=8192)
        try:
            writer.write(json.dumps({"op": op, "token": token}).encode() + b"\n")
            await writer.drain()
            reply = json.loads(await reader.readline())
        finally:
            writer.close()
            await writer.wait_closed()
    except (OSError, ValueError):
        raise ApplyError(UNAVAILABLE) from None
    if error := reply.get("error"):
        raise ApplyError(error)
    return reply
