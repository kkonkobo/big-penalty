"""Application URLs and fail-closed routing for an attended browser context."""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
from collections.abc import Callable
from email import policy
from email.parser import BytesParser
from urllib.parse import parse_qsl, quote, urlencode, urljoin, urlsplit, urlunsplit

# jobseqno: Phenom career sites; extjobid: SAP SuccessFactors career-site previews.
_REQUISITION = {"gh_jid", "jobid", "job_id", "reqid", "requisitionid", "jobreqid", "career_job_req_id", "jobseqno", "extjobid"}
# Query keys a job link may carry that only attribute the visit or pick a display mode: dropped from a navigation URL
# (the posting is still reached from its path or requisition key). Any other key fails the link closed.
_TRACKING = {"source", "ref", "lang", "locale", "from", "gclid", "fbclid", "lcid", "partner", "trid", "rs", "feedid",
             "jobpipeline", "idorigine", "id_origin", "applicant_source_welcome", "in_iframe", "mode", "apply", "mapping",
             "step", "stepname"}  # step/stepname: Phenom's apply wizard writes them into the URL as it advances
_TRACKING_PREFIXES = ("utm_", "rx_", "__cf_chl")  # rx_*: Radancy job-ad attribution on Workday links; __cf_chl*: Cloudflare check tokens
_ID = re.compile(r"[A-Za-z0-9_.-]{1,80}\Z")
_URL = re.compile(r"https?://[^\s<>\"']+", re.I)
_CODE = re.compile(
    r"\b(?:otp|code(?:\s+(?:to\s+sign\s+in|de\s+(?:v[ée]rification|confirmation|s[ée]curit[ée])))?|pin|token|jeton|"
    r"(?:verifizierungs|bestätigungs|sicherheits)[ -]?code)(?:\s+(?:is|est|ist|lautet)\b)?\s*[:=–—-]?\s*"
    r"[A-Za-z0-9_.-]*\d[A-Za-z0-9_.-]*", re.I)
_JOB_PATH = re.compile(r"/(?:jobs?|job-postings?|requisitions?|positions?)/([0-9]{1,18})/?\Z", re.I)
_LOGIN_PATH = re.compile(r"(?:^|/)(?:(?:log-?in|sign-?in|register|createaccount)(?:\.aspx)?|account\.aspx)(?:/|$)", re.I)
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
            elif navigation and name not in _TRACKING and not name.startswith(_TRACKING_PREFIXES):
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


def posting_gone_redirect(source: str, target: str) -> bool:
    """The posting URL redirects, on the same host, to the site root or a bare one-segment path (careers index, language
    home): what an expired ad does. The source must itself look like a specific posting (a deep path or an identifier)."""
    a, b = urlsplit(source), urlsplit(target)
    if not a.hostname or (a.hostname or "").lower() != (b.hostname or "").lower() or a.scheme != b.scheme:
        return False
    deep = [seg for seg in a.path.split("/") if seg]
    specific = len(deep) >= 2 or bool(re.search(r"\d", a.path + a.query))
    target_segments = [seg for seg in b.path.split("/") if seg]
    bare = len(target_segments) <= 1 and not re.search(r"\d", b.path + b.query)
    return specific and bare and (b.path.rstrip("/"), b.query) != (a.path.rstrip("/"), a.query)


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


_VENDOR_ASSET_HOST = re.compile(r"(?:\A|\.)(?:phenompeople\.com|ashbyprd\.com|myworkdaycdn\.com|rmkcdn\.successfactors\.(?:com|eu))\Z")
_VENDOR_ASSET_PATH = re.compile(r"\.(?:m?js|css|woff2?|ttf|otf|eot|svg|png|jpe?g|gif|webp|ico)\Z", re.I)


def vendor_static_asset(url: str, *, data_entered: bool) -> bool:
    """A read of a static file (script, style, font, image) from an ATS vendor's own CDN or ATS host. Career sites built on
    Phenom or SuccessFactors render nothing without them. A cache-busting query is only tolerated before any candidate data."""
    from jobhunt.origin import ats_of_host

    try:
        parts = urlsplit(url or "")
        port = parts.port
    except ValueError:
        return False
    host = (parts.hostname or "").lower().rstrip(".")
    return (parts.scheme == "https" and port is None and not parts.username and not parts.password and not parts.fragment
            and (not parts.query or (not data_entered and len(parts.query) <= 64 and re.fullmatch(r"[\w.=&-]+", parts.query) is not None))
            and bool(host) and (_VENDOR_ASSET_HOST.search(host) is not None or ats_of_host(host) is not None)
            and _VENDOR_ASSET_PATH.search(parts.path) is not None and ".." not in parts.path and "%" not in parts.path)


def cloudflare_check(url: str) -> bool:
    """Cloudflare's own bot-check traffic on a protected site (its challenge scripts, beacons and answers)."""
    try:
        parts = urlsplit(url or "")
    except ValueError:
        return False
    return parts.scheme == "https" and parts.path.startswith("/cdn-cgi/challenge-platform/")


_CLOUDFLARE_PAGE = (b"cf-error-details", b"Attention Required! | Cloudflare", b"/cdn-cgi/challenge-platform/", b"cf_chl_opt")


async def cloudflare_edge_refusal(response) -> bool:
    """Cloudflare itself refused or challenged the request at its edge, so the site never received it: a `cf-mitigated`
    header, or Cloudflare's own block / check page. A site's own 403 merely relayed by Cloudflare is not one."""
    try:
        if response.status not in {403, 429, 503}:
            return False
        headers = {key.lower(): value for key, value in response.headers.items()}
        if headers.get("cf-mitigated"):
            return True
        if headers.get("server", "").lower() != "cloudflare" or not headers.get("cf-ray"):
            return False
        body = (await response.body())[:50_000]
    except Exception:
        return False
    return any(marker in body for marker in _CLOUDFLARE_PAGE)


_READ_SCHEMES = frozenset({"https"})
_READ_TYPES = frozenset({"script", "stylesheet", "font", "image", "fetch", "xhr", "media"})


def pre_data_read(url: str, method: str, resource_type: str, *, data_entered: bool) -> bool:
    """A page's own background read (script, style, font, image, fetch) of any public host, made before any candidate data
    was entered. Career sites build their page from CDN / API hosts that are not the employer's (job JSON in a cloud bucket, a
    vendor API); aborting them leaves a blank page. Nothing candidate-related exists yet to leak, and no navigation, frame or
    write is covered."""
    try:
        parts = urlsplit(url or "")
        port = parts.port
    except ValueError:
        return False
    host = (parts.hostname or "").lower().rstrip(".")
    return (not data_entered and method in {"GET", "HEAD"} and resource_type in _READ_TYPES
            and parts.scheme in _READ_SCHEMES and not parts.username and not parts.password and bool(host)
            and port in {None, 443} and "." in host and not host.replace(".", "").isdigit() and ":" not in host
            and not host.endswith((".local", ".internal", ".localhost")) and len(url) <= 2048)


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
        self.posting_gone = False  # the posting URL itself was redirected to its site's root / a bare path: the ad is gone
        self.refused_request = ""  # "METHOD url-without-query" of the first refused request (local failure record only)
        self.transmitted = False
        self.non_account_transmitted = False
        self.final_started = False
        self.final_response = False
        self._allow_write = allow_write
        self._initial_url = ""
        self._next_url = ""
        # Origins found behind Cloudflare: their plain reads go through the browser's own network stack, because Cloudflare
        # blocks the guard's re-sent copies (another TLS fingerprint than the Chrome the page runs in).
        self.browser_transport: set[str] = set()
        self.probe = None
        self.page = None
        # Autonomous (unattended) runs abort an unauthorized request and keep going instead of closing the window: an
        # aborted request can only prevent a leak. Dry inspection and supervised windows keep the fail-closed rule.
        self.lenient = audience is not None and not inspection_only
        self.aborted_hosts: dict[str, int] = {}

    def record_abort(self, url: str) -> None:
        with contextlib.suppress(ValueError):
            host = (urlsplit(url).hostname or "").lower()
            if not re.fullmatch(r"[a-z0-9.\-\[\]:]{1,253}", host):
                host = "invalid-host"
            if host in self.aborted_hosts or len(self.aborted_hosts) < 50:
                self.aborted_hosts[host] = self.aborted_hosts.get(host, 0) + 1

    def _top_level(self, request) -> bool:
        """A navigation of the bound tab's main frame (not an iframe, not another tab)."""
        try:
            frame = request.frame
            return frame.parent_frame is None and self.page is not None and frame == self.page.main_frame
        except Exception:
            return False

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

    async def _browser_final_response(self, request) -> None:
        """The final request was sent by the browser itself (Cloudflare): its own response stands in for the guard's."""
        try:
            response = await asyncio.wait_for(request.response(), 60)
        except Exception:
            return
        if response is None or not 200 <= response.status < 300 or self.refused:
            return  # a redirect or an error: the landing page decides, as for any unconfirmed send
        with contextlib.suppress(Exception):
            if request.resource_type in {"xhr", "fetch"}:
                await self.audience.inspect_receipt(request, response)
        self.final_response = True
        self.audience.record_response()

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

        async def drop(route, url):
            self.record_abort(url)
            await route.abort()

        def own_site(url: str) -> bool:
            """`url` is on the site being applied to (the page's own origin, or one the guard already admits)."""
            target = origin(url)
            with contextlib.suppress(Exception):
                return bool(target) and (target == origin(page.url) or self.allows(url)
                                         or bool(self.probe and self.probe.same_site(url))
                                         or bool(self.audience and self.audience.write_recipient(url)))
            return False

        async def guard(route, request):
            if self.refused:
                await route.abort()
                return
            if not request.is_navigation_request() and cloudflare_check(request.url) and own_site(request.url):
                # Cloudflare's bot check on the employer's own site: browser signals computed by Cloudflare's script, sent
                # to that same site. Aborting it (or re-sending it from outside the browser) turns the check into a block.
                await route.continue_()
                return
            navigation = request.is_navigation_request()
            read = request.method in {"GET", "HEAD"}
            write = not read
            lenient = self.lenient
            top = lenient and navigation and self._top_level(request)
            entry_post = False
            pre_data = False  # admitted only because no candidate data was entered yet: it cannot carry any
            if self.probe and navigation and read:
                with contextlib.suppress(Exception):
                    if request.frame.parent_frame is not None:
                        await (drop(route, request.url) if lenient else route.abort())
                        return
            if self.probe and write:
                try:
                    entry_post = self.probe.allow_post(request)
                except Exception:
                    pass
                if not entry_post:
                    if lenient and self.probe.same_site(request.url) and not self.audience.data_entered:
                        # Before any input, a same-site write cannot carry candidate data (rule 2): a top-level form post
                        # is followed like a navigation (its redirect is never replayed), a background write passes.
                        if navigation:
                            entry_post = top and self.probe.admit_navigation(request)
                            if not entry_post:
                                await drop(route, request.url)
                                return
                        pre_data = True
                    elif lenient:
                        await drop(route, request.url)
                        return
                    elif self.probe.same_site(request.url):
                        await route.abort()
                        return
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
            if not permitted and read and not navigation and (
                    _static_asset(request.url)
                    or vendor_static_asset(request.url, data_entered=bool(getattr(self.audience, "data_entered", True)))):
                permitted = True  # an ATS's own static code / fonts / UI strings: without them its pages render empty
            if (not permitted and read and not navigation and self.audience is not None
                    and not getattr(self.audience, "active", True)
                    and pre_data_read(request.url, request.method, request.resource_type,
                                      data_entered=bool(getattr(self.audience, "data_entered", True)))):
                permitted = True  # pre-data page build (see pre_data_read): it cannot carry candidate data
            if not permitted and lenient and not self.probe:
                if not top:
                    # A subresource, an iframe or another tab: aborting it cannot leak anything (rule 1).
                    if write and self.audience.pre_data_write(request.url):
                        permitted = pre_data = True
                    else:
                        await drop(route, request.url)
                        return
                else:
                    verdict = self.audience.navigation_verdict(request.url, write=write)
                    if verdict == "allow":
                        permitted = pre_data = True
                    elif verdict == "abort":
                        await drop(route, request.url)
                        return
            if not permitted:
                if lenient and not top:
                    await drop(route, request.url)
                elif write or navigation:
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
                if lenient and not top:
                    await drop(route, request.url)
                else:
                    await refuse(route)
                return
            response = None
            fetching = final_request = False
            try:
                if not read and self._allow_write and not await self._allow_write(request):
                    await refuse(route)
                    return
                if self.audience and (write or (self.audience.active and urlsplit(request.url).query)):
                    self.audience.recheck()
                    if not entry_post and not pre_data:
                        self.transmitted = True
                        if not getattr(self.audience, 'account_only_request', lambda _: False)(request):
                            self.non_account_transmitted = True
                final_request = self.final_started and write and self.audience.click_write is request
                fetching = True
                # Navigations only before any input: the browser follows their redirects without this guard vetting each hop.
                browser_sendable = read and not final_request and not (
                    navigation and bool(getattr(self.audience, "data_entered", False)))
                if browser_sendable and origin(request.url) in self.browser_transport:
                    await route.continue_()  # a Cloudflare-fronted site: the browser fetches it itself (see browser_transport)
                    return
                response = await route.fetch(max_redirects=0, max_retries=0)
                if ((browser_sendable or write) and (target_origin := origin(request.url))
                        and await cloudflare_edge_refusal(response)):
                    # Cloudflare refused, at its edge, the guard's copy of a request the guard had already admitted (the
                    # site never received it, so sending it once more cannot duplicate anything): the browser sends it
                    # itself, and from now on this site's reads too. Writes are still tried through the guard first.
                    self.browser_transport.add(target_origin)
                    await response.dispose()
                    response = None
                    await route.continue_()
                    if final_request:
                        await self._browser_final_response(request)
                    return
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
                    redirect = 300 <= response.status < 400 and bool(response.headers.get("location"))
                    initial = (redirect and request.method in {"GET", "HEAD"} and _navigation_matches(request.url, self._initial_url)
                               and request.is_navigation_request() and request.frame == page.main_frame)
                    if (read and not navigation and redirect and _static_asset(request.url) and _static_asset(target)):
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
                    elif (initial and redirect and self.audience and not self.audience.data_entered
                          and posting_gone_redirect(request.url, target)):
                        self.posting_gone = True
                        await refuse(route, "job_closed")
                    elif (initial
                            and (self.audience.initial_redirect(request.url, target) or
                                 (lenient and self.audience.navigation_verdict(target) == "allow")
                                 if self.audience else self.allows(target))
                            and (safe_target := clean_url(target, navigation=True))):
                        self._next_url = safe_target
                        await route.fulfill(status=200, content_type="text/plain", body="")
                    elif top and redirect and read and not initial and not self.probe and not self.audience.data_entered and (
                            verdict := self.audience.navigation_verdict(target)) != "refuse":
                        # Before any input, a top-level GET redirect to the same site or a recognized ATS is followed by
                        # the browser (the next hop goes through this guard again and is bound before any input).
                        if verdict == "allow":
                            await route.fulfill(response=response)
                        else:
                            await drop(route, target)
                    elif (top and not redirect and not self.audience.data_entered
                          and self.audience.navigation_verdict(request.url) != "refuse"):
                        # A refusal page (403/429) on the document before any input: show it, the challenge checks decide.
                        await route.fulfill(response=response)
                    elif lenient and not top and not final_request:
                        await drop(route, request.url)
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
                        if lenient and not top and fetching and not final_request:
                            await route.abort()  # a failed subresource fetch: the page sees a network error
                        else:
                            await refuse(route)
            finally:
                if response is not None:
                    with contextlib.suppress(Exception):
                        await response.dispose()

        await page.context.route("**/*", guard)
