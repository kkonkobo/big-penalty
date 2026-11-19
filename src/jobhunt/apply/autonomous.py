"""Live destination and policy checks for generic unattended applications."""

from __future__ import annotations

import json
import re
import secrets
import time
from urllib.parse import parse_qsl, urlsplit

import tldextract

from jobhunt.apply.destination import clean_url, origin
from jobhunt.apply.errors import ApplyError, ManualField
from jobhunt.apply.identity import ats_target
from jobhunt.apply.supervised import _FORM_TARGETS_JS, _generic_action_allowed, _posting_ids, _target_allowed, _url_posting_ids
from jobhunt.origin import ats_of_host


SUPERVISED_REQUIRED = "À envoyer en mode supervisé"
_PSL = tldextract.TLDExtract(suffix_list_urls=(), cache_dir=None, include_psl_private_domains=True)


def _domain(host):
    return _PSL(host or "").top_domain_under_public_suffix


def confirmation_url(url: str, posting_url: str) -> bool:
    """Recognize a same-origin confirmation path for this posting, not receipt correlation."""
    if not clean_url(url, navigation=True) or origin(url) != origin(posting_url):
        return False
    path = urlsplit(url).path.casefold().rstrip("/")
    ids = _posting_ids(path)
    expected = _posting_ids(urlsplit(posting_url).path)
    return (not ids or ids == expected) and path.rsplit("/", 1)[-1] in {
        "application-submitted", "application-received", "candidature-envoyee", "candidature-recue",
    }


def confirmation_json(body: bytes) -> bool:
    """Recognize a positive structured result, never a bare HTTP success or generic resource ID."""
    if len(body) > 65536:
        return False
    try:
        value = json.loads(body)
    except (ValueError, UnicodeError, RecursionError):
        return False
    if not isinstance(value, dict):
        return False
    status = value.get("status")
    status = status.casefold() if isinstance(status, str) else ""
    if (value.get("error") or value.get("errors") or value.get("success") is False
            or status in {"draft", "pending", "failed", "error", "rejected", "incomplete", "validation_error",
                      "in_progress", "inprogress", "started", "open", "saved", "created", "new", "partial"}):
        return False
    identifier = value.get("applicationId", value.get("application_id"))
    return (value.get("success") is True or status in {"submitted", "received"}
            or (type(identifier) is int and identifier > 0)
            or (isinstance(identifier, str) and bool(identifier.strip())))


class Audience:
    def __init__(self, reserved_url, verified_urls, recheck, *, pdf_bytes=None):
        self.reserved_url = reserved_url
        self.verified_urls = frozenset(verified_urls)
        self.document = reserved_url
        self.recheck = recheck
        self.host = urlsplit(reserved_url).hostname
        self.domain = _domain(self.host)
        self.active = False
        self.clicking = False
        self.endpoint = ""
        self.method = ""
        self.next_document = ""
        self.document_nonce = ""
        self.final_response = False
        self.final_response_at = 0.0
        self.final_started = False
        self.final_receipt_url = ""
        self.final_json = False
        self.click_write = None
        self.scriptable = False
        self.live_url = ""
        self.pdf_bytes = pdf_bytes
        self.final_actions = set()

    def write_recipient(self, url):
        recipient = origin(url)
        if not recipient:
            return False
        host = urlsplit(recipient).hostname
        return recipient == origin(self.reserved_url) or bool(self.domain and _domain(host) == self.domain)

    def initial_redirect(self, source, destination):
        target = clean_url(destination, navigation=True)
        if not target or not origin(target):
            return False
        original = urlsplit(self.reserved_url)
        live = urlsplit(target)
        original_ats, original_identity = ats_target(self.reserved_url)
        target_ats, target_identity = ats_target(target)
        if original_identity and any(value.casefold() != original_identity[1] for _, value in parse_qsl(live.query)):
            return False
        if target_ats and original_ats == target_ats and original_identity and target_identity:
            return original_identity == target_identity and self.write_recipient(target)
        expected = _posting_ids(original.path + "?" + original.query)
        live_ids = _posting_ids(live.path + "?" + live.query)
        if expected and expected != live_ids:
            return False
        if target in self.verified_urls and self.write_recipient(target):
            return True
        if not self.write_recipient(target):
            return False
        if origin(target) == origin(self.reserved_url) and (live.path.rstrip("/"), live.query) == (original.path.rstrip("/"), original.query):
            return True
        if live.hostname != original.hostname and (ats_of_host(original.hostname or "") or ats_of_host(live.hostname or "")):
            return False
        return bool(expected and live_ids == expected)

    def apply_navigation(self, target):
        return bool(target and target != self.document and self.initial_redirect(self.document, target))

    def request_allowed(self, request, page, *, initial):
        if initial and request.method in {"GET", "HEAD"} and request.is_navigation_request():
            return request.frame == page.main_frame and bool(origin(request.url))
        if not self.write_recipient(request.url):
            return False
        writing = request.method not in {"GET", "HEAD"}
        if request.is_navigation_request() and request.frame == page.main_frame:
            if (not writing and self.final_response and self.clicking and not self.next_document
                    and request.frame.url == self.live_url and self.receipt_url(request.url)):
                self.next_document = request.url
                return True
            if (not self.clicking or self.next_document or request.method != self.method or request.url != self.endpoint
                    or request.frame.url != self.live_url
                    or not _target_allowed(request.frame.url, request.url)):
                return False
            if writing and not self._admit_click_write(request):
                return False
            self.next_document = clean_url(request.url, navigation=True)
            return True
        if writing or self.active and urlsplit(request.url).query:
            if request.frame != page.main_frame or request.frame.url != self.live_url:
                return False
            if not _url_posting_ids(request.url) <= _url_posting_ids(self.document):
                return False
            if writing and self.clicking and self.scriptable and not request.is_navigation_request():
                return self._admit_click_write(request)
            target = urlsplit(request.url)
            ids = _posting_ids(target.path + "?" + urlsplit(clean_url(request.url)).query)
            expected = _posting_ids(urlsplit(self.document).path + "?" + urlsplit(self.document).query)
            if ids and ids != expected:
                return False
            live_params = dict(parse_qsl(urlsplit(self.document).query))
            if any(live_params.get(name) != value for name, value in
                   parse_qsl(urlsplit(clean_url(request.url)).query)):
                return False
            if request.method not in {"GET", "HEAD"}:
                return (self.active and not self.clicking and not self.final_started
                        and self._is_cv_upload(request))
        return request.method in {"GET", "HEAD"} or self.active

    def _admit_click_write(self, request):
        if self.click_write is not None:
            return False
        self.click_write = request
        return True

    async def inspect_receipt(self, request, response):
        mime = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if (mime == "application/json" or mime.endswith("+json")) and not self._is_cv_upload(request):
            self.final_json = confirmation_json(await response.body())

    def record_response(self):
        self.final_response = True
        self.final_response_at = time.monotonic()

    def receipt_settled(self):
        return self.final_response and time.monotonic() - self.final_response_at >= 0.2

    def _is_cv_upload(self, request):
        target = urlsplit(clean_url(request.url))
        key = (origin(request.url), target.path)
        if key in self.final_actions or not self.pdf_bytes or request.method not in {"POST", "PUT"}:
            return False
        if not re.search(r"(?:^|[/_-])(?:upload|uploads|attachments?|parse|resume|cv)(?:$|[/_.-])", target.path, re.I):
            return False
        return self.pdf_bytes in (request.post_data_buffer or b"")

    def confirmation_redirect(self, url):
        target = clean_url(url, navigation=True)
        if not target or origin(target) != origin(self.document):
            return ""
        if any(dict(parse_qsl(urlsplit(self.document).query)).get(name) != value
               for name, value in parse_qsl(urlsplit(target).query)):
            return ""
        if _generic_action_allowed(self.document, target, target) or self.receipt_url(target):
            return target
        return ""

    def receipt_url(self, url):
        return confirmation_url(url, self.document)

    async def bind_document(self, page):
        nonce = secrets.token_urlsafe(24)
        self.document_nonce = nonce
        self.live_url = await page.evaluate("value => { document.__bp_autonomous_document = value; return location.href; }",
                                            nonce, isolated_context=True)

    async def after_click(self, page, *, observing=False):
        if observing and self.scriptable and not self.next_document:
            return
        self.clicking = False
        if self.next_document and not self.final_response:
            new_url = clean_url(page.url, navigation=True)
            if self.next_document == new_url and _generic_action_allowed(self.document, new_url, new_url):
                self.document = new_url
                await self.bind_document(page)
        self.next_document = ""

    async def target(self, target, *, button=False, final=False):
        if target is None:
            raise ManualField()
        try:
            value = await target.evaluate(f"""(node, button) => {{
                const doc = node.ownerDocument, win = doc.defaultView;
                if (!node.isConnected || !win || win !== win.top || doc !== document) return null;
                if (button && node.matches('a[href]') && (!node.target || node.target === '_self'))
                    return [win.location.href, [node.getAttribute('href'), node.href, []],
                            node.href, doc.__bp_autonomous_document, 'GET'];
                const form = node.form;
                if (button && !['', '_self'].includes((node.getAttribute('formtarget') || form?.getAttribute('target') || '').toLowerCase())) return null;
                const targets = ({_FORM_TARGETS_JS})(node);
                const override = button ? node.getAttribute('formaction') : null;
                if (override !== null && override !== '') targets[2].push(node.formAction ?? new URL(override, doc.baseURI).href);
                const action = override !== null ? (override === '' ? doc.URL : node.formAction) : (targets[1] ?? doc.URL);
                const nativePost = form
                    && (node.getAttribute('formmethod') || form.getAttribute('method') || 'get').toLowerCase() === 'post';
                return [win.location.href, targets, action, doc.__bp_autonomous_document, nativePost ? 'POST' : ''];
            }}""", button, isolated_context=True)
        except Exception:
            raise ManualField() from None
        if not value or not _generic_action_allowed(value[0], *value[1]):
            raise ManualField()
        live = clean_url(value[0], navigation=True)
        if not live or live != self.document or value[0] != self.live_url or value[3] != self.document_nonce or not self.write_recipient(live):
            raise ApplyError(SUPERVISED_REQUIRED)
        if not button:
            for action in (value[2], *value[1][2]):
                self.final_actions.add((origin(action), urlsplit(action).path))
        if button:
            if final and value[4] == "GET":
                raise ApplyError(SUPERVISED_REQUIRED)
            endpoint = urlsplit(value[2] or live)._replace(fragment="").geturl()
            if not clean_url(endpoint, navigation=True) or not _target_allowed(value[0], endpoint):
                raise ApplyError(SUPERVISED_REQUIRED)
            self.endpoint = endpoint
            self.method = value[4]
        self.recheck()
        self.active = True

    def arm_click(self, *, scripted: bool):
        self.click_write = None
        self.scriptable = scripted and self.method != "GET"
        self.clicking = True


_OPT_OUT = re.compile(r"marketing|newsletter|job alerts?|alertes? (?:emploi|offres?)|talent pool|vivier|partners?|partenaires?|(?:third[ -]part|tiers)|publicit|prospection|transfert", re.I)
_APPLICATION = re.compile(r"candidat|recrut|application|recruit|employment process", re.I)
_PRIVACY = re.compile(r"privacy (?:policy|notice)|politique de confidentialite|protection (?:de|des|mes) donnees", re.I)
_DATA = re.compile(r"processing|retention|traitement|conservation", re.I)
_TERMS = re.compile(r"conditions|terms", re.I)
_ACCEPT = re.compile(r"accept|agree|acknowledge|consent|lu et|prise de connaissance", re.I)
_RACE = re.compile(r"\b(?:race|racial|ethnic\w*|ethniqu\w*|ethnisch\w*|ethnie)\b", re.I)
_OTHER_EEO = re.compile(r"disabilit|handicap|gender|genre|veteran|geschlecht|behinder", re.I)
_SELF_ID = re.compile(r"(?:self|auto)[ -]?identif", re.I)
_EEO = re.compile(r"\beeo\b|equal opportunit", re.I)
_PREFER_NOT = re.compile(r"(?:i )?prefer not to (?:say|answer|disclose)|decline to (?:answer|disclose|self identify)|"
                         r"(?:je )?ne souhaite pas (?:repondre|le preciser)|keine angabe", re.I)
_BLACK_LABELS = frozenset({"black", "black or african american", "black african american", "black african",
                           "black or african", "noir", "noire", "schwarz"})


def blocked_consent(text):
    return bool(_OPT_OUT.search(text))


def eeo_field(text):
    return any(pattern.search(text) for pattern in (_EEO, _RACE, _OTHER_EEO, _SELF_ID))


def _option_text(value, fold):
    return " ".join(re.findall(r"[a-z0-9]+", fold(value)))


def race_ethnicity_field(field, fold):
    label, group = fold(field.label), fold(field.group)
    if _OTHER_EEO.search(label) or (not _RACE.search(label) and _OTHER_EEO.search(group)):
        return False
    if _RACE.search(label):
        return True
    return bool(_SELF_ID.search(f"{label} {group}") and
                any(_option_text(option.label, fold) in _BLACK_LABELS for option in field.options))


def _live_option(field, index):
    option = field.options[index]
    return sum(candidate.value == option.value for candidate in field.options) == 1 and (
        not field.choice_options or
        [(label, disabled) for value, label, disabled, _ in field.choice_options
         if value == option.value] == [(option.label, False)])


def decision(field, fold, *, race_ethnicity=None):
    text = fold(f"{field.label} {field.group}")
    if field.kind == "checkbox" and field.required and not blocked_consent(text) and _ACCEPT.search(text):
        if _PRIVACY.search(text) or (_APPLICATION.search(text) and _DATA.search(text)):
            return "application_privacy", -1
        if _TERMS.search(text) and (_APPLICATION.search(text) or re.fullmatch(r"j['’ ]?accepte les conditions[ .]*", text.strip())):
            return "application_terms", -1
    if field.kind in {"select", "radio"} and not field.multiple and eeo_field(text):
        declared = _option_text(race_ethnicity or "", fold)
        if declared and race_ethnicity_field(field, fold):
            equivalent = _BLACK_LABELS if declared in _BLACK_LABELS else {declared}
            options = [index for index, option in enumerate(field.options)
                       if _option_text(option.label, fold) in equivalent]
            if len(options) == 1 and _live_option(field, options[0]):
                return "eeo_race_ethnicity", options[0]
        options = [index for index, option in enumerate(field.options)
                   if _PREFER_NOT.fullmatch(_option_text(option.label, fold))]
        if len(options) == 1 and _live_option(field, options[0]):
            return "eeo_opt_out", options[0]
    return None
