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
from urllib.parse import parse_qsl, unquote, urlsplit

from jobhunt.apply.adapters import Attempt, Match
from jobhunt.apply.destination import audit_url, clean_url, origin
from jobhunt.apply.errors import ApplyError, ManualField, StepChanged
from jobhunt.apply.identity import canonical_identity
from jobhunt.apply.types import RunResult
from jobhunt.config import get_settings
from jobhunt.dedupe import normalize_title

UNAVAILABLE = "Aucune fenêtre supervisée active pour cette candidature."
NO_LOCAL_BRIDGE = "La commande fill-step n'est pas disponible sur ce système : utilisez « Remplir cette étape » dans l'application."
# The private CLI bridge needs Unix-domain sockets and per-user ownership checks. Elsewhere (Windows) supervised mode
# still works through the web UI, which talks to the session in-process.
HAS_LOCAL_BRIDGE = hasattr(os, "getuid") and hasattr(asyncio, "start_unix_server")
WRONG_POSTING = "Le poste affiché ne correspond pas à la candidature réservée : fenêtre fermée."
CONFIRM_AGAIN = "La page a changé. Vérifiez le poste et confirmez de nouveau « Remplir cette étape »."

_VISIBLE_HEADING = """doc => Array.from(doc.querySelectorAll('h1')).filter(node => {
    const style = doc.defaultView.getComputedStyle(node);
    return node.getClientRects().length && style.visibility === 'visible';
}).map(node => node.innerText.trim())"""

_FORM_TARGETS_JS = """node => {
    const form = node.form;
    if (!form) return [null, null, []];
    const action = Object.getOwnPropertyDescriptor(HTMLFormElement.prototype, 'action').get.call(form);
    const submitters = [...form.getRootNode().querySelectorAll('button[formaction],input[formaction]')].filter(control =>
        control.form === form && (control.type === 'submit' || control.type === 'image')
        && control.getAttribute('formaction') !== ''
    ).map(control => control.formAction);
    return [form.getAttribute('action'), action, submitters];
}"""

_POSTING_ID = re.compile(r"[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}|(?<![a-z0-9])(?:jr|req|r)[_-]?\d+|\d{3,}", re.I)


def _posting_ids(value: str) -> set[str]:
    return {identifier.casefold() for identifier in _POSTING_ID.findall(unquote(value))}


def _url_posting_ids(url: str) -> set[str]:
    parts = urlsplit(url)
    found = _posting_ids(parts.path)
    for _, value in parse_qsl(parts.query, keep_blank_values=True):
        found.update(_posting_ids(value))
    return found


def _target_allowed(document_url: str, target_url: str | None) -> bool:
    """A browser-resolved submission URL may receive this form only if it names no other posting than the page.

    Posting identifiers are read from the path AND the query of both URLs, so `/jobs/100?jobId=200` is refused on
    `/jobs/100`. The fragment is ignored. A different path is accepted only when it names exactly the same postings."""
    if not target_url or origin(target_url) != origin(document_url):
        return False
    expected, found = _url_posting_ids(document_url), _url_posting_ids(target_url)
    if not found <= expected:
        return False
    return urlsplit(target_url).path == urlsplit(document_url).path or found == expected


def _generic_action_allowed(document_url: str, raw_action: str | None, action_url: str | None,
                            submitter_urls: list[str] | tuple[str, ...] = ()) -> bool:
    """`action_url` and `submitter_urls` are resolved by the browser, so `<base href>` is taken into account
    (`action="#"` under `<base href="/jobs/200">` submits to job 200). An absent or empty action submits to the
    document URL itself. Submit controls carrying `formaction` override the form's action and are checked too."""
    targets = [] if raw_action is None or raw_action == '' else [action_url]
    targets.extend(submitter_urls or ())
    return all(_target_allowed(document_url, target) for target in targets)


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
        self._navigation = 0
        self._document_key = "__bp_" + secrets.token_hex(16)
        if self.match is None:
            self.page.on("framenavigated", self._on_navigation)

    def _on_navigation(self, frame) -> None:
        if frame == self.page.main_frame:
            self._navigation += 1
            self.pending = None

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

    async def fingerprint(self, *, issue: bool = False) -> tuple[str, str, str]:
        navigation = self._navigation
        try:
            url, title, headings, nonce = await self.page.evaluate(f"""([key, nonce]) => {{
                if (nonce !== null) document[key] = nonce;
                return [location.href, document.title, ({_VISIBLE_HEADING})(document), document[key] ?? ''];
            }}""", [self._document_key, secrets.token_urlsafe(32) if issue and not self.match else None], isolated_context=True)
        except Exception:
            raise StepChanged(CONFIRM_AGAIN) from None
        return self._read_fingerprint(navigation, url, title, headings, nonce)

    def _read_fingerprint(self, navigation: int, url: str, title: str, headings: list[str], nonce: str) -> tuple[str, str, str]:
        vetted = clean_url(url, navigation=True)
        if not vetted or origin(vetted) != origin(self.attempt.url):
            raise ApplyError(WRONG_POSTING)
        if url != self.page.url or navigation != self._navigation or (not self.match and not nonce):
            raise StepChanged(CONFIRM_AGAIN)
        return _digest(self.attempt.nonce, vetted, urlsplit(url).fragment, _digest(title, *headings),
                       str(navigation), nonce), url, " ".join([title, *headings])

    async def before_mutation(self, target, expected: str | None) -> None:
        if self.match:
            await self.posting(target)
        else:
            navigation = self._navigation
            live = None
            if target is not None:
                try:
                    live = await target.evaluate(f"""(node, key) => {{
                        const doc = node.ownerDocument, view = doc.defaultView;
                        if (!view || view !== view.top || doc !== document || !node.isConnected) return null;
                        return [[view.location.href, doc.title, ({_VISIBLE_HEADING})(doc), doc[key] ?? ''],
                                ...({_FORM_TARGETS_JS})(node)];
                    }}""", self._document_key, isolated_context=True)
                except Exception:
                    pass
            current, url, _ = self._read_fingerprint(navigation, *live[0]) if live else await self.fingerprint()
            if expected is None or not secrets.compare_digest(current, expected):
                raise StepChanged(CONFIRM_AGAIN)
            if target is not None and (not live or not _generic_action_allowed(url, *live[1:])):
                raise ManualField()

    async def preview(self) -> FillPreview:
        async with self.lock:
            await self._check()
            try:
                current, url, headings = await self.fingerprint(issue=True)
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
    if not HAS_LOCAL_BRIDGE:
        yield
        return
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
    if not HAS_LOCAL_BRIDGE:
        raise ApplyError(NO_LOCAL_BRIDGE)
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
