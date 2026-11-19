"""Application URLs and fail-closed routing for an attended browser context."""

from __future__ import annotations

import contextlib
import json
import re
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

_REQUISITION = {"gh_jid", "jobid", "job_id", "reqid", "requisitionid"}
_TRACKING = {"source", "ref", "lang", "locale", "from", "gclid", "fbclid"}
_ID = re.compile(r"[A-Za-z0-9_.-]{1,80}\Z")
_URL = re.compile(r"https?://[^\s<>\"']+", re.I)
_CODE = re.compile(
    r"\b(?:otp|code(?:\s+de\s+(?:v[ée]rification|confirmation|s[ée]curit[ée]))?|pin|token|jeton|"
    r"(?:verifizierungs|bestätigungs|sicherheits)[ -]?code)\s*(?:(?:is|est|ist|lautet)\s+|[:= -]\s*)"
    r"[A-Za-z0-9_.-]*\d[A-Za-z0-9_.-]*", re.I)
_JOB_PATH = re.compile(r"/(?:jobs?|job-postings?|requisitions?|positions?)/([0-9]{1,18})/?\Z", re.I)


def clean_url(url: str | None, *, navigation: bool = False) -> str:
    if not url or any(ord(char) < 33 or char == "\\" for char in url):
        return ""
    try:
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username is not None:
            return ""
        port = parsed.port
        host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
        netloc = f"{host}:{port}" if port is not None else host
        kept = {}
        for key, value in parse_qsl(parsed.query, keep_blank_values=True, max_num_fields=100):
            name = key.lower()
            if name in _REQUISITION and _ID.fullmatch(value) and name not in kept:
                kept[name] = (key, value)
            elif navigation and name not in _TRACKING and not name.startswith("utm_"):
                return ""
        return urlunsplit((parsed.scheme, netloc, parsed.path or "/", urlencode(sorted(kept.values())), ""))
    except ValueError:
        return ""


def origin(url: str) -> str:
    parsed = urlsplit(clean_url(url))
    if not parsed.hostname or (parsed.scheme != "https" and parsed.hostname not in {"127.0.0.1", "::1", "localhost"}):
        return ""
    return f"{parsed.scheme}://{parsed.netloc}"


def audit_url(url: str | None) -> str:
    """Never persist an opaque path as a diagnostic URL."""
    parsed = urlsplit(clean_url(url))
    if not parsed.netloc:
        return ""
    path = parsed.path if _JOB_PATH.fullmatch(parsed.path) else ""
    query = [(key, value) for key, value in parse_qsl(parsed.query) if re.fullmatch(r"[0-9]{1,18}", value)]
    return urlunsplit((parsed.scheme, parsed.netloc, path, urlencode(query), ""))


def clean_text(text: str, *, audit: bool = False) -> str:
    return _CODE.sub("[code masqué]", _URL.sub(lambda match: "[lien masqué]" if re.search(
        r"verif|confirm|activate|reset|magic|token|otp", match.group(), re.I) else
        (audit_url if audit else clean_url)(match.group()), text))


RECIPIENT_REFUSED = ("Destination ou transmission non autorisée : fenêtre fermée. "
                     "Si vous poursuivez depuis le lien de l’offre dans votre navigateur, vérifiez vous-même le destinataire.")


class RecipientGuard:
    def __init__(self, allowed: frozenset[str]):
        self.allowed = allowed
        self.refused = False
        self._initial_url = ""
        self._next_url = ""

    async def navigate(self, page, url, goto) -> None:
        """Follow initial redirects explicitly, preserving the actual document URL."""
        try:
            for hop in range(6):
                self._initial_url = url
                self._next_url = ""
                try:
                    await goto(page, url)
                except Exception:
                    if self.refused and not page.is_closed():
                        with contextlib.suppress(Exception):
                            await page.close()
                    if not self._next_url or self.refused:
                        raise
                if not self._next_url:
                    return
                url = self._next_url
            self.refused = True
            await page.close()
        finally:
            self._initial_url = self._next_url = ""

    def allows(self, url: str) -> bool:
        return not self.refused and origin(url) in self.allowed

    @staticmethod
    def refusal_status(status: int) -> bool:
        return status in {301, 302, 303, 307, 308, 403, 429}

    async def install(self, page) -> None:
        await page.context.add_init_script(script="""(() => {
            const allowed = new Set(%s);
            const inert = function () { throw new DOMException('Transport disabled', 'SecurityError'); };
            const lock = (key, value) => Object.defineProperty(globalThis, key, {value, writable: false, configurable: false});
            for (const key of ['WebSocket', 'WebTransport', 'RTCPeerConnection', 'webkitRTCPeerConnection',
                               'mozRTCPeerConnection', 'RTCDataChannel', 'Worker', 'SharedWorker']) lock(key, inert);
            const Native = globalThis.EventSource;
            if (Native) lock('EventSource', function (url, options) {
                const target = new URL(url, location.href);
                if (!allowed.has(target.origin)) return inert();
                return new Native(target.href, options);
            });
        })();""" % json.dumps(sorted(self.allowed)))

        async def refuse(route):
            self.refused = True
            await route.abort()
            for opened in page.context.pages:
                with contextlib.suppress(Exception):
                    await opened.close()

        async def guard(route, request):
            if self.refused:
                await route.abort()
                return
            if not self.allows(request.url):
                if request.method not in {"GET", "HEAD"} or request.is_navigation_request():
                    await refuse(route)
                else:
                    await route.abort()
                return
            if request.is_navigation_request() and not clean_url(request.url, navigation=True):
                await refuse(route)
                return
            response = None
            try:
                response = await route.fetch(max_redirects=0, max_retries=0)
                if self.refusal_status(response.status):
                    target = urljoin(request.url, response.headers.get("location", ""))
                    if (300 <= response.status < 400 and response.headers.get("location")
                            and request.method in {"GET", "HEAD"} and request.url == self._initial_url
                            and request.is_navigation_request() and request.frame == page.main_frame
                            and self.allows(target) and (safe_target := clean_url(target, navigation=True))):
                        self._next_url = safe_target
                        await route.fulfill(status=200, content_type="text/plain", body="")
                    else:
                        await refuse(route)
                elif self.refused:
                    await route.abort()
                else:
                    await route.fulfill(response=response)
            except Exception:
                if not self.refused:
                    with contextlib.suppress(Exception):
                        await refuse(route)
            finally:
                if response is not None:
                    with contextlib.suppress(Exception):
                        await response.dispose()

        await page.context.route("**/*", guard)
