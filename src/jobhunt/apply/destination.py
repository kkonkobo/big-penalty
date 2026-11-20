"""Application URLs and fail-closed routing for an attended browser context."""

from __future__ import annotations

import contextlib
import json
import re
from collections.abc import Callable
from email import policy
from email.parser import BytesParser
from urllib.parse import parse_qsl, quote, urlencode, urljoin, urlsplit, urlunsplit

_REQUISITION = {"gh_jid", "jobid", "job_id", "reqid", "requisitionid", "jobreqid", "career_job_req_id"}
_TRACKING = {"source", "ref", "lang", "locale", "from", "gclid", "fbclid"}
_ID = re.compile(r"[A-Za-z0-9_.-]{1,80}\Z")
_URL = re.compile(r"https?://[^\s<>\"']+", re.I)
_CODE = re.compile(
    r"\b(?:otp|code(?:\s+(?:to\s+sign\s+in|de\s+(?:v[ée]rification|confirmation|s[ée]curit[ée])))?|pin|token|jeton|"
    r"(?:verifizierungs|bestätigungs|sicherheits)[ -]?code)(?:\s+(?:is|est|ist|lautet)\b)?\s*[:=–—-]?\s*"
    r"[A-Za-z0-9_.-]*\d[A-Za-z0-9_.-]*", re.I)
_JOB_PATH = re.compile(r"/(?:jobs?|job-postings?|requisitions?|positions?)/([0-9]{1,18})/?\Z", re.I)
_LOGIN_PATH = re.compile(r"(?:^|/)(?:(?:login|sign-?in|register|createaccount)(?:\.aspx)?|account\.aspx)(?:/|$)", re.I)
_LOGIN_REDIRECT = {"returnurl", "redirect"}


def login_return(url: str) -> str:
    try:
        parsed = urlsplit(url)
        if not _LOGIN_PATH.search(parsed.path):
            return ""
        values = [value for name, value in parse_qsl(parsed.query, max_num_fields=100) if name.lower() in _LOGIN_REDIRECT]
        if len(values) != 1 or re.search(r"[\\\x00-\x20]|%(?:2f|5c|00)", values[0], re.I):
            return ""
        target = urljoin(url, values[0])
        result = urlsplit(target)
        if (result.scheme, result.netloc) != (parsed.scheme, parsed.netloc) or result.fragment:
            return ""
        return clean_url(target, navigation=True, allow_login_redirect=False)
    except ValueError:
        return ""


def clean_url(url: str | None, *, navigation: bool = False, allow_login_redirect: bool = True) -> str:
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
            elif navigation and name in _LOGIN_REDIRECT and allow_login_redirect and login_return(url):
                continue
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


def _navigation_matches(actual: str, expected: str) -> bool:
    return bool(expected and actual == quote(expected, safe=":/?#[]@!$&'()*+,;=%"))


def clean_text(text: str, *, audit: bool = False) -> str:
    return _CODE.sub("[code masqué]", _URL.sub(lambda match: "[lien masqué]" if re.search(
        r"verif|confirm|activate|reset|magic|token|otp", match.group(), re.I) else
        (audit_url if audit else clean_url)(match.group()), text))


def contains_verification_code(text: str) -> bool:
    return _CODE.search(text) is not None


RECIPIENT_REFUSED = ("Destination ou transmission non autorisée : fenêtre fermée. "
                     "Si vous poursuivez depuis le lien de l’offre dans votre navigateur, vérifiez vous-même le destinataire.")


def is_bound_pdf(content_type: str, body: bytes | None, pdf: bytes) -> bool:
    """Accept just a PDF body or a single unencoded PDF file part, not draft data."""
    if not body or not content_type.isascii() or "\r" in content_type or "\n" in content_type:
        return False
    media = content_type.partition(";")[0].strip().lower()
    if media == "application/pdf":
        return body == pdf
    if media != "multipart/form-data" or len(body) > len(pdf) + 4096:
        return False
    message = BytesParser(policy=policy.default).parsebytes(b"Content-Type: " + content_type.encode() + b"\r\n\r\n" + body)
    parts = list(message.iter_parts())
    if message.defects or message.preamble or message.epilogue or len(parts) != 1:
        return False
    part = parts[0]
    return (not part.defects and part.get_content_type() == "application/pdf" and part.get_content_disposition() == "form-data"
            and part.get_filename() == "CV.pdf" and part.get("Content-Transfer-Encoding") is None
            and part.get_payload(decode=True) == pdf)


def _static_asset(url: str) -> bool:
    from jobhunt.apply.workday import static_asset  # (workday imports this module)

    return static_asset(url)


class RecipientGuard:
    def __init__(self, allowed: frozenset[str], *, audience=None, read_only: frozenset[str] = frozenset(),
                 inspection_only: bool = False,
                 request_allowed: Callable[[str, str, bool], bool] | None = None,
                 allow_write: Callable | None = None):
        self.allowed = allowed
        self.audience = audience
        self.read_only = read_only
        self.inspection_only = inspection_only
        self.request_allowed = request_allowed
        self.refused = False
        self.refusal_code = "recipient_refused_transport"
        self.refused_request = ""  # "METHOD url-without-query" of the first refused request (local failure record only)
        self.transmitted = False
        self.non_account_transmitted = False
        self.final_started = False
        self.final_response = False
        self._allow_write = allow_write
        self._initial_url = ""
        self._next_url = ""
        self.probe = None
        self.page = None

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

    @staticmethod
    async def _static_redirect(page, method: str, target: str):
        """A static asset moved to its versioned path: followed only while every hop is a static asset too."""
        for _ in range(3):
            try:
                response = await page.context.request.fetch(target, method=method, max_redirects=0, max_retries=0, timeout=15000)
            except Exception:
                return None
            location = response.headers.get("location", "")
            if 300 <= response.status < 400 and location and _static_asset(urljoin(target, location)):
                target = urljoin(target, location)
                await response.dispose()
                continue
            if 200 <= response.status < 300:
                return response
            await response.dispose()
            return None
        return None

    async def install(self, page) -> None:
        self.page = page
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

        async def refuse(route, code="recipient_refused_transport"):
            if not self.refused_request:
                with contextlib.suppress(Exception):
                    from jobhunt.apply.page_outline import bare_url

                    self.refused_request = f"{route.request.method} {bare_url(route.request.url)}"
            self.refused = True
            self.refusal_code = code
            await route.abort()
            for opened in page.context.pages:
                with contextlib.suppress(Exception):
                    await opened.close()

        async def guard(route, request):
            if self.refused:
                await route.abort()
                return
            navigation = request.is_navigation_request()
            read = request.method in {"GET", "HEAD"}
            write = not read
            entry_post = False
            if self.probe and navigation and read:
                with contextlib.suppress(Exception):
                    if request.frame.parent_frame is not None:
                        await route.abort()
                        return
            if self.probe and write:
                try:
                    entry_post = self.probe.allow_post(request)
                except Exception:
                    pass
                if not entry_post:
                    if self.probe.same_site(request.url):
                        await route.abort()
                    else:
                        await refuse(route, "apply_target_unverified")
                    return
            if self.inspection_only and write and not entry_post:
                await route.abort()
                return
            permitted = self.allows(request.url) or (read and not navigation and origin(request.url) in self.read_only)
            if self.probe:
                permitted = entry_post or self.probe.allows(request)
            elif self.audience:
                try:
                    permitted = self.audience.request_allowed(request, self.page, initial=_navigation_matches(request.url, self._initial_url))
                except Exception:
                    permitted = False
            if not permitted and read and not navigation and _static_asset(request.url):
                permitted = True  # an ATS's own static code / fonts / UI strings: without them its pages render empty
            if not permitted:
                if write or navigation:
                    from jobhunt.origin import classify_ats

                    category = ("same_site" if self.allows(request.url) or
                                (self.audience and self.audience.write_recipient(request.url)) else
                                "ats" if classify_ats(request.url) else "other")
                    await refuse(route, "apply_target_unverified" if self.probe else
                                 f"recipient_refused_{category}_{'write' if write else 'navigation'}")
                else:
                    await route.abort()
                return
            if self.request_allowed and not self.request_allowed(request.url, request.method, navigation):
                if not read or navigation:
                    await refuse(route)
                else:
                    await route.abort()
                return
            if navigation and not clean_url(request.url, navigation=True):
                await refuse(route)
                return
            response = None
            try:
                if not read and self._allow_write and not await self._allow_write(request):
                    await refuse(route)
                    return
                if self.audience and (write or (self.audience.active and urlsplit(request.url).query)):
                    self.audience.recheck()
                    if not entry_post:
                        self.transmitted = True
                        if not getattr(self.audience, 'account_only_request', lambda _: False)(request):
                            self.non_account_transmitted = True
                final_request = self.final_started and write and self.audience.click_write is request
                response = await route.fetch(max_redirects=0, max_retries=0)
                if final_request and request.is_navigation_request() and response.status in {301, 302, 303} and response.headers.get("location"):
                    target = self.audience.confirmation_redirect(urljoin(request.url, response.headers["location"]))
                    if not target:
                        await refuse(route, "recipient_refused_redirect")
                        return
                    await response.dispose()
                    response = None
                    response = await page.context.request.get(target, max_redirects=0, max_retries=0, timeout=10000)
                    if 200 <= response.status < 300 and self.audience.receipt_url(target):
                        self.audience.final_receipt_url = target
                if self.refusal_status(response.status):
                    target = urljoin(request.url, response.headers.get("location", ""))
                    if (read and not navigation and 300 <= response.status < 400 and response.headers.get("location")
                            and _static_asset(request.url) and _static_asset(target)):
                        await response.dispose()
                        response = await self._static_redirect(page, request.method, target)
                        if response is None:
                            await route.abort()
                        else:
                            await route.fulfill(response=response)
                    elif (self.probe and response.status in ({301, 302, 303} if entry_post else {301, 302, 303, 307, 308})
                            and response.headers.get("location")
                            and request.is_navigation_request() and self.probe.redirect(target)):
                        await route.fulfill(status=200, content_type="text/plain", body="")
                    elif (self.audience and self.audience.entry_binding and response.headers.get("location")
                            and response.status in ({301, 302, 303, 307, 308} if read else {301, 302, 303})
                            and request.is_navigation_request() and request.frame == self.page.main_frame
                            and self.audience.account_redirect(target)):
                        await route.fulfill(status=200, content_type="text/plain", body="")
                    elif (300 <= response.status < 400 and response.headers.get("location")
                            and request.method in {"GET", "HEAD"} and _navigation_matches(request.url, self._initial_url)
                            and request.is_navigation_request() and request.frame == page.main_frame
                            and (self.audience.initial_redirect(request.url, target) if self.audience else self.allows(target))
                            and (safe_target := clean_url(target, navigation=True))):
                        self._next_url = safe_target
                        await route.fulfill(status=200, content_type="text/plain", body="")
                    elif (self.audience and not self.audience.active and not write
                          and not request.is_navigation_request()):
                        await route.abort()
                    else:
                        await refuse(route, "recipient_refused_redirect" if 300 <= response.status < 400 else
                                     "challenge_or_rate_limited" if self.inspection_only and navigation else "recipient_refused_response")
                elif self.refused:
                    await route.abort()
                else:
                    positive_response = final_request and 200 <= response.status < 300
                    if positive_response:
                        if request.resource_type in {"xhr", "fetch"}:
                            await self.audience.inspect_receipt(request, response)
                    await route.fulfill(response=response)
                    if positive_response:
                        self.final_response = True
                        self.audience.record_response()
            except Exception:
                if not self.refused:
                    with contextlib.suppress(Exception):
                        await refuse(route)
            finally:
                if response is not None:
                    with contextlib.suppress(Exception):
                        await response.dispose()

        await page.context.route("**/*", guard)
