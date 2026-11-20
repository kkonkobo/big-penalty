"""Posting-bound audience for the service-owned unattended browser."""

from urllib.parse import urlsplit

from jobhunt.apply.autonomous import Audience
from jobhunt.apply.destination import clean_url, is_bound_pdf, origin
from jobhunt.apply.errors import ApplyError
from jobhunt.apply.supervised import require_posting


class AdapterAudience(Audience):
    def __init__(self, attempt, match, gate, recheck, *, pdf_bytes):
        super().__init__(attempt.url, (), recheck, pdf_bytes=pdf_bytes)
        self.attempt, self.match, self.gate = attempt, match, gate
        self.adapter = match.adapter
        self.upload_pending = False
        self.click_boundary = None

    def posting(self, *urls):
        try:
            require_posting(self.match, self.attempt, *urls)
        except ApplyError:
            return False
        return True

    def write_recipient(self, url):
        return bool(url and origin(url) == origin(self.reserved_url))

    def navigation_verdict(self, url, *, write=False):
        # The adapter's declared flow decides every top-level navigation; subresources are only aborted. The one extra
        # admission: before any candidate data, a plain GET to the same origin that still names the bound posting (the
        # ATS switching itself to the browser locale, as Workday does) - the landing is re-bound by continuity.
        if (not write and not self.data_entered and clean_url(url, navigation=True) == url
                and origin(url) == origin(self.reserved_url) and self.posting(url)):
            return "allow"
        return "refuse"

    def pre_data_write(self, url):
        return False

    def landing_allowed(self, target):
        return False

    def initial_redirect(self, source, destination):
        return self.write_recipient(destination) and self.posting(destination)

    def form_allowed(self, document, targets, endpoint):
        return all(self.write_recipient(url) and self.posting(url)
                   for url in (document, endpoint, *targets[2]))

    def click_endpoint_allowed(self, document, endpoint):
        return self.write_recipient(endpoint) and self.posting(endpoint)

    async def check_click(self, target, field, role, final):
        if self.core:  # the declared control boundary only labels the click (continuation proofs); it never stops the run
            try:
                await self._check_click(target, field, role, final)
            except ApplyError:
                self.click_boundary = None
            return
        await self._check_click(target, field, role, final)

    async def _check_click(self, target, field, role, final):
        live = await target.evaluate("node => [node.isConnected, node.getAttribute('data-automation-id') || '', "
                                     "(node.innerText || node.value || '').trim()]")
        boundary = self.adapter.browser_control(self.live_url, field.automation_id, field.label, role, final)
        if (not live[0] or live[1] != field.automation_id or not boundary
                or self.adapter.browser_control(self.live_url, live[1], live[2], role, final) != boundary
                or self.adapter.boundaries.get(boundary) != 'auto'):
            raise ApplyError("Commande de candidature non autorisée.")
        if not final and urlsplit(self.endpoint).path.rstrip('/').endswith('/submit'):
            raise ApplyError("Destination d’envoi hors étape finale.")
        if self.method == 'POST' and not self.adapter.browser_native_post(self.live_url, self.endpoint, boundary):
            raise ApplyError("Destination du formulaire non déclarée.")
        self.click_boundary = boundary

    def continuation(self, snapshot):
        return (self.click_boundary in {'account_creation', 'draft_write'} and self.click_write is not None
                and self.posting(snapshot.url) and self.adapter.browser_continuation(snapshot.url, self.click_source))

    def arm_click(self, *, scripted, account=False):
        self.click_source = self.live_url
        super().arm_click(scripted=scripted)

    def account_only_request(self, request):
        return (self.adapter.name == 'workday' and self.click_write is request and (
            self.click_boundary == 'account_creation' and not request.is_navigation_request()
            and self.gate.matches_endpoint('account_creation', request.url)
            or self.click_boundary == 'login_otp' and request.is_navigation_request() and request.url == self.live_url))

    def arm_email_code(self):
        if (self.adapter.name != 'workday' or not self.adapter.browser_native_post(self.live_url, self.endpoint, 'account_creation')
                or not self.posting(self.live_url, self.endpoint)):
            raise ApplyError('Formulaire de vérification non autorisé.')
        self.click_boundary = 'login_otp'
        self.arm_click(scripted=False)

    def request_allowed(self, request, page, *, initial):
        url, method = request.url, request.method
        navigation = request.is_navigation_request()
        reading = method in {'GET', 'HEAD'}
        if request.frame != page.main_frame:
            return False
        if initial:
            return reading and navigation and self.initial_redirect(self.reserved_url, url)
        if reading and not navigation and origin(url) in self.match.requisition.read_origins:
            return not self.active and not urlsplit(url).query
        upload = self.gate.matches_endpoint('cv_upload_parse', url)
        if not self.write_recipient(url) and not upload:
            return False
        if navigation and reading:
            if self.final_started:
                return self.final_response and self.receipt_url(url)
            if self.clicking and self.posting(request.frame.url, url):
                self.next_document = url
                return True
            return False
        if reading:
            return not urlsplit(url).query and (not self.active or self.posting(url))
        if not self.posting(page.url, request.frame.url) or request.frame.url != self.live_url:
            return False
        if upload and self.upload_pending and not navigation and not self.clicking:
            if method == 'OPTIONS':
                return True
            if method == 'POST' and is_bound_pdf(request.headers.get('content-type', ''), request.post_data_buffer,
                                                 self.pdf_bytes):
                self.upload_pending = False
                return True
            return False
        if method != 'POST' or not self.clicking or not self.click_boundary:
            return False
        if navigation:
            if self.method != 'POST' or url != self.endpoint or not self.posting(url):
                return False
            self.next_document = url
        elif not self.scriptable or not self.gate.matches_endpoint(self.click_boundary, url):
            return False
        return self._admit_click_write(request)

    async def after_click(self, page, *, observing=False):
        if self.core:
            return await Audience.after_click(self, page, observing=observing)
        if self.final_started:
            return
        live = page.url
        if not self.initial_redirect(self.document, live):
            await page.close()
            raise ApplyError("Identité du poste modifiée.")
        if live != self.live_url or self.next_document == live:
            self.document = clean_url(live, navigation=True)
            await self.bind_document(page)
        if not observing:
            self.clicking = False
            self.next_document = ''

    def confirmation_redirect(self, url):
        return url if self.receipt_url(url) else ''

    def receipt_url(self, url):
        return self.write_recipient(url) and self.adapter.browser_receipt_url(self.attempt, url)

    async def inspect_receipt(self, request, response):
        return None

    async def document_receipt(self, page, snapshot, *, positive_text):
        live = page.url
        if live != snapshot.url or not self.write_recipient(live):
            return 'none'
        native_final = self.method == 'POST' and live == self.endpoint
        receipt_url = self.final_receipt_url if native_final and self.final_receipt_url else live
        same_final_document = (live == self.click_source or native_final) and self.posting(live)
        if not self.receipt_url(receipt_url) and not same_final_document:
            return 'none'
        return await self.adapter.browser_receipt(self.attempt, page, receipt_url, positive_text, same_final_document)
