"""Application URLs and fail-closed routing for an attended browser context."""

from __future__ import annotations

import contextlib
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_REQUISITION = {"gh_jid", "jobid", "job_id", "reqid", "requisitionid"}
_TRACKING = {"source", "ref", "lang", "locale", "from", "gclid", "fbclid"}
_ID = re.compile(r"[A-Za-z0-9_.-]{1,80}\Z")
_URL = re.compile(r"https?://[^\s<>\"']+", re.I)
_CODE = re.compile(r"\b(?:otp|code|pin|token|jeton)\s*[:= -]\s*[A-Za-z0-9_.-]*\d[A-Za-z0-9_.-]*", re.I)


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


def clean_text(text: str) -> str:
    return _CODE.sub("[code masqué]", _URL.sub(lambda match: "[lien masqué]" if re.search(
        r"verif|confirm|activate|reset|magic|token|otp", match.group(), re.I) else clean_url(match.group()), text))


RECIPIENT_REFUSED = ("Destination ou transmission non autorisée : fenêtre fermée. "
                     "Si vous poursuivez depuis le lien de l’offre dans votre navigateur, vérifiez vous-même le destinataire.")


class RecipientGuard:
    def __init__(self, allowed: frozenset[str]):
        self.allowed = allowed
        self.refused = False

    def allows(self, url: str) -> bool:
        return not self.refused and origin(url) in self.allowed

    @staticmethod
    def refusal_status(status: int) -> bool:
        return status in {301, 302, 303, 307, 308, 403, 429}

    async def install(self, page) -> None:
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

        async def websocket(socket):
            self.refused = True
            await socket.close()
            for opened in page.context.pages:
                with contextlib.suppress(Exception):
                    await opened.close()

        await page.context.route_web_socket("**/*", websocket)
        await page.context.route("**/*", guard)
