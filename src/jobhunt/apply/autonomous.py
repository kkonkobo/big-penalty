"""Live destination and policy checks for generic unattended applications."""

from __future__ import annotations

import re
import secrets
from urllib.parse import parse_qsl, urlsplit

import tldextract

from jobhunt.apply.destination import clean_url, origin
from jobhunt.apply.errors import ApplyError, ManualField
from jobhunt.apply.supervised import _generic_action_allowed, _posting_ids
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
        self.final_started = False
        self.final_receipt_url = ""
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
        expected = _posting_ids(original.path + "?" + original.query)
        live_ids = _posting_ids(live.path + "?" + live.query)
        if expected and expected != live_ids:
            return False
        if target in self.verified_urls:
            return True
        if not self.write_recipient(target) and not ats_of_host(live.hostname or ""):
            return False
        if origin(target) == origin(self.reserved_url) and (live.path.rstrip("/"), live.query) == (original.path.rstrip("/"), original.query):
            return True
        if live.hostname != original.hostname and (ats_of_host(original.hostname or "") or ats_of_host(live.hostname or "")):
            return False
        return bool(expected and live_ids == expected)

    def request_allowed(self, request, page, *, initial):
        if initial and request.method in {"GET", "HEAD"} and request.is_navigation_request():
            return request.frame == page.main_frame and bool(origin(request.url))
        if not self.write_recipient(request.url):
            return False
        if request.is_navigation_request() and request.frame == page.main_frame:
            if (not self.clicking or self.next_document or request.method != self.method or request.url != self.endpoint
                    or clean_url(request.frame.url, navigation=True) != self.document):
                return False
            self.next_document = clean_url(request.url, navigation=True)
            return True
        if request.method not in {"GET", "HEAD"} or self.active and urlsplit(request.url).query:
            if request.frame != page.main_frame or clean_url(request.frame.url, navigation=True) != self.document:
                return False
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
        await page.evaluate("value => { document.__bp_autonomous_document = value; }", nonce, isolated_context=True)

    async def after_click(self, page):
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
            value = await target.evaluate("""(node, button) => {
                const doc = node.ownerDocument, win = doc.defaultView;
                if (!node.isConnected || !win || win !== win.top || doc !== document) return null;
                if (button && node.matches('a[href]') && (!node.target || node.target === '_self'))
                    return [win.location.href, node.getAttribute('href'), node.href, doc.__bp_autonomous_document, 'GET'];
                if (button && (!node.form || !['submit', 'image'].includes(node.type))) return null;
                const form = node.form;
                if (button && ((node.getAttribute('formmethod') || form.getAttribute('method') || 'get').toLowerCase() !== 'post'
                    || !['', '_self'].includes((node.getAttribute('formtarget') || form.getAttribute('target') || '').toLowerCase()))) return null;
                const override = button && node.hasAttribute('formaction');
                const raw = override ? node.getAttribute('formaction') : form?.getAttribute('action');
                const action = override ? node.formAction : (form && Object.getOwnPropertyDescriptor(HTMLFormElement.prototype, 'action').get.call(form));
                return [win.location.href, raw ?? null, action, doc.__bp_autonomous_document, 'POST'];
            }""", button, isolated_context=True)
        except Exception:
            raise ManualField() from None
        if not value or not _generic_action_allowed(*value[:3]):
            raise ManualField()
        live = clean_url(value[0], navigation=True)
        if not live or live != self.document or value[3] != self.document_nonce or not self.write_recipient(live):
            raise ApplyError(SUPERVISED_REQUIRED)
        if not button:
            action = value[2] or live
            self.final_actions.add((origin(action), urlsplit(action).path))
        if button:
            if final and value[4] != "POST":
                raise ApplyError(SUPERVISED_REQUIRED)
            endpoint = urlsplit(value[2] or live)._replace(fragment="").geturl()
            if not clean_url(endpoint, navigation=True) or origin(endpoint) != origin(live):
                raise ApplyError(SUPERVISED_REQUIRED)
            ids = _posting_ids(urlsplit(clean_url(endpoint)).query)
            if ids and ids != _posting_ids(urlsplit(live).path + "?" + urlsplit(live).query):
                raise ApplyError(SUPERVISED_REQUIRED)
            self.endpoint = endpoint
            self.method = value[4]
            self.clicking = True
        self.recheck()
        self.active = True


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
