"""Attended Workday planning and operator-attested Candidate Home receipts."""

from __future__ import annotations

import re
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING
from urllib.parse import unquote, urlsplit

from jobhunt.apply.answer_check import fold
from jobhunt.apply.destination import clean_url, origin
from jobhunt.apply.identity import canonical_identity
from jobhunt.apply.types import Action, Question, StepPlan
from jobhunt.apply.widgets import date_segment, date_segment_option, denied_widget
from jobhunt.origin import _identity as _origin_identity

if TYPE_CHECKING:
    from jobhunt.apply.adapters import Attempt, Receipt, Requisition

DOMAINS = ("myworkdayjobs.com", "myworkdaysite.com")
_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_HOST = re.compile(rf"{_LABEL}(?:\.{_LABEL})*\.(?:myworkdayjobs|myworkdaysite)\.com\Z")
_SITE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
_LOCALE = re.compile(r"[a-z]{2,3}-[a-z]{2}\Z", re.I)
_REQ = re.compile(r"(?:^|_)((?:JR|REQ|R)[-_]?\d[A-Za-z0-9-]{0,35})\Z", re.I)
_APPLY_STEPS = frozenset({"signin", "autofillwithresume", "applymanually", "usemylastapplication", "myinformation",
                          "myexperience", "applicationquestions", "voluntarydisclosures", "selfidentify", "review", "submit"})

_STEPS = {
    "my information": "information", "mes informations": "information",
    "my experience": "experience", "mon expérience": "experience", "mon experience": "experience",
    "application questions": "questions", "questions de candidature": "questions",
    "voluntary disclosures": "manual", "déclarations volontaires": "manual", "declarations volontaires": "manual",
    "self identify": "manual", "self-identify": "manual", "auto-identification": "manual",
    "review": "manual", "vérification": "manual",
}
_INFO = {
    "legalnamecountry-firstname": "first_name", "legalnamecountry-lastname": "last_name",
    "addresssection-addressline1": "address", "addresssection-city": "city",
    "addresssection-postalcode": "postal_code", "addresssection-country": "country",
    "countrydropdown": "country", "email": "email", "emailaddress": "email",
    "phone-number": "phone", "phone-devicetype": "phone_device_type",
    "sourceprompt": "job_source", "howdidyouhearaboutus": "job_source",
}
_EXPERIENCE = {"linkedin": "linkedin", "linkedinurl": "linkedin"}
_RESUME = {"file-upload-input-ref", "resumefileupload", "resumeupload"}
_SCREENING = {"availablefrom": "available_from", "graduationdate": "graduation_date"}
_AUTH_IDS = {"signin", "signinbutton", "createaccount", "createaccountbutton", "password",
             "verificationcode", "otp", "one-time-code"}
_MANUAL_IDS = {"voluntarydisclosures", "selfidentify", "selfidentification", "reviewpage"}
_OTP = re.compile(r"\b(?:verification code|one.time (?:code|password)|code (?:de verification|a usage unique)|otp)\b")
_EMAIL_CHALLENGE = re.compile(r"\b(?:check your (?:e-mail|email|inbox)|verify your (?:e-mail|email) address|"
                              r"verifiez votre (?:e-mail|email|boite de reception)|lien de verification)\b")
_CUSTOM_QUESTION = re.compile(r"(?:primary|secondary)?questionnaire(?:page[-_])?.+")


def _normalized(text: str) -> str:
    return " ".join(fold(text).casefold().strip().split()).rstrip(" *:")


def _posting(url: str) -> tuple[str, str, str] | None:
    cleaned = clean_url(url, navigation=True)
    if not cleaned or urlsplit(url).fragment:
        return None
    parsed = urlsplit(cleaned)
    host = parsed.hostname or ""
    if parsed.scheme != "https" or parsed.port is not None or parsed.query or not _HOST.fullmatch(host):
        return None
    parts = [unquote(segment) for segment in parsed.path.removeprefix("/").removesuffix("/").split("/")]
    if any(not segment or segment in {".", ".."} or any(ch in "/\\?#%" or ord(ch) < 32 or ord(ch) == 127 for ch in segment)
           for segment in parts):
        return None
    if host.endswith(".myworkdaysite.com") and parts[0] == "recruiting":
        if len(parts) < 4 or not _SITE.fullmatch(parts[1]):
            return None
        namespace = parts[1].casefold() + "/"
        parts = parts[2:]
    else:
        namespace = ""
    if parts and _LOCALE.fullmatch(parts[0]) and len(parts) > 1 and parts[1] != "job":
        parts = parts[1:]
    if namespace and len(parts) > 2 and _LOCALE.fullmatch(parts[1]) and parts[2] == "job":
        parts.pop(1)
    if len(parts) < 3 or not _SITE.fullmatch(parts[0]) or parts[1] != "job":
        return None
    for index in (3, 4):
        if len(parts) > index and parts[index].casefold() == "apply" and _REQ.search(parts[index - 1]):
            steps = parts[index + 1:]
            if len(steps) > 2 or any(step.casefold().replace("-", "") not in _APPLY_STEPS for step in steps):
                return None
            parts = parts[:index]
            break
    if len(parts) < 3 or len(parts) > 4:
        return None
    match = _REQ.search(parts[-1])
    if not match:
        return None
    requisition = match[1]
    tenant = f"{host}/{namespace}{parts[0].casefold()}"
    if not canonical_identity(tenant, requisition):
        return None
    return tenant, requisition.upper(), origin(cleaned)


@dataclass(frozen=True)
class CandidateHomeEvidence:
    """Local operator attestation scoped to a held attempt; never an ATS nonce or raw page."""

    attempt_nonce: str
    posting_url: str
    state: str


class WorkdayPlanner:
    def __init__(self, adapter=None):
        self.adapter = adapter

    async def plan(self, snapshot, facts, **_kwargs) -> StepPlan:
        ids = {field.automation_id.casefold() for field in snapshot.fields}
        if snapshot.has_captcha:
            return StepPlan(page_kind="captcha")
        if _OTP.search(_normalized(snapshot.text)) or _EMAIL_CHALLENGE.search(_normalized(snapshot.text)) or any(
            field.autocomplete == "one-time-code" or _OTP.search(_normalized(field.label)) for field in snapshot.fields
        ) or ids & {"verificationcode", "otp", "one-time-code"}:
            return StepPlan(page_kind="email_verification")
        if ids & _AUTH_IDS or any(field.kind == "password" for field in snapshot.fields):
            if self.adapter:
                actions = self.adapter.browser_actions(snapshot)
                login = any(action.click_role == "login" for action in actions)
                kind = "account_login" if login else "account_signup"
                for field in snapshot.fields:
                    if field.kind == "email" and not field.value:
                        actions.append(Action(ref=field.ref, op="fill", fact="account_email"))
                    if field.kind == "password" and not field.value:
                        actions.append(Action(ref=field.ref, op="fill", fact="account_password"))
                return StepPlan(page_kind=kind, actions=actions)
            return StepPlan(page_kind="account_signup" if ids & {"createaccount", "createaccountbutton"} else "account_login")

        headings = {_normalized(field.group) for field in snapshot.fields if field.kind not in {"button", "link"}}
        steps = {_STEPS[heading] for heading in headings if heading in _STEPS}
        if self.adapter and not steps:
            step = self.adapter._step(snapshot.url)
            steps = {kind for heading, kind in _STEPS.items() if heading.replace(' ', '').replace('-', '') == step}
        if not steps:
            first = next((_normalized(line) for line in snapshot.text.splitlines() if line.strip()), "")
            if first in _STEPS:
                steps = {_STEPS[first]}
        if len(steps) > 1 or (not self.adapter and ("manual" in steps or ids & _MANUAL_IDS)):
            return StepPlan(page_kind="other")
        if not steps:
            actions = self.adapter.browser_actions(snapshot) if self.adapter else []
            return StepPlan(page_kind="form" if actions and actions[0].click_role in {"submit", "next"} else "job_page",
                            actions=actions)

        step = steps.pop()
        actions, unknown = [], []
        for field in snapshot.fields:
            marker = field.automation_id.casefold()
            if field.value or field.kind in {"button", "link", "checkbox", "password"}:
                continue
            if step == "experience" and field.kind == "file" and marker in _RESUME:
                actions.append(Action(ref=field.ref, op="upload_cv"))
                continue
            key = {"information": _INFO, "experience": _EXPERIENCE, "questions": _SCREENING}.get(step, {}).get(marker)
            segment = date_segment(field) if step == "questions" else None
            if segment:
                key = segment[0]
            if key is None and step == "questions" and _CUSTOM_QUESTION.fullmatch(marker) and field.label:
                known = [fact.key for fact in facts.facts if fact.key.startswith("custom_")
                         and _normalized(fact.label) == _normalized(field.label)]
                key = known[0] if len(known) == 1 else None
            fact = facts.get(key) if key else None
            action = None
            if fact and fact.value and not fact.secret:
                if field.kind in {"select", "combobox", "radio"}:
                    labels = [option.label for option in field.options if option.label
                              and _normalized(option.label) == _normalized(fact.value)]
                    index = date_segment_option(field, fact.value) if segment and fact.exact else -1
                    if index >= 0:
                        labels = [field.options[index].label]
                    if len(labels) == 1 and (field.kind == "select" or field.kind == "combobox" and not denied_widget(field)) and not field.multiple:
                        action = Action(ref=field.ref, op="select", fact=fact.key, value=labels[0])
                elif field.kind == "date" and re.fullmatch(r"\d{4}-\d{2}-\d{2}", fact.value):
                    action = Action(ref=field.ref, op="fill", fact=fact.key, value=fact.value)
                elif field.kind in {"text", "email", "tel", "url", "textarea"}:
                    if marker not in _SCREENING:
                        action = Action(ref=field.ref, op="fill", fact=fact.key, value=None if fact.exact else fact.value)
            if action:
                actions.append(action)
            elif field.required and not (self.adapter and step == "manual"):
                unknown.append(Question(ref=field.ref, label=field.label, kind=field.kind,
                                        options=[option.label for option in field.options]))
        if self.adapter:
            actions.extend(self.adapter.browser_actions(snapshot))
        return StepPlan(page_kind="form", actions=actions, unknown=unknown)


class WorkdayAdapter:
    name = "workday"
    capability = "unattended"
    supervised_receipt_kind = "candidate_home_listing"
    retry_rule = "never"
    boundaries = MappingProxyType({"account_creation": "auto", "login_otp": "pause_for_human",
                                   "field_entry": "auto", "cv_upload_parse": "auto",
                                   "draft_write": "auto", "final_submit": "auto"})

    def browser_enrollable(self, url):
        return bool(_posting(url))

    def browser_planner(self, attempt):
        return WorkdayPlanner(self)

    @staticmethod
    def _step(url):
        path = urlsplit(url).path.casefold().rstrip('/')
        return path.rsplit('/', 1)[-1].replace('-', '') if '/apply/' in path else ''

    def browser_control(self, url, automation_id, label, role, final):
        marker, text, step = automation_id.casefold(), _normalized(label), self._step(url)
        if final:
            if (step == 'review' and role == 'submit' and marker in {'bottom-navigation-next-button', 'submitbutton'}
                    and text in {'submit', 'envoyer', 'soumettre'}):
                return 'final_submit'
        elif role == 'open_form' and not step:
            if ((marker == 'applymanually' and text in {'apply manually', 'postuler manuellement'}) or
                (marker in {'applybutton', 'jobpostingapplybutton'} and text in {'apply', 'postuler'})):
                return 'draft_write'
        elif role in {'login', 'signup'} and step == 'signin':
            if ((role == 'login' and marker == 'signinbutton' and text in {'sign in', 'se connecter'}) or
                (role == 'signup' and marker == 'createaccountbutton' and text in {'create account', 'creer un compte'})):
                return 'account_creation'
        elif (role == 'next' and step in _APPLY_STEPS - {'review', 'submit', 'signin', 'autofillwithresume', 'usemylastapplication'}
              and marker in {'bottom-navigation-next-button', 'pagefooternextbutton'}
              and text in {'next', 'continue', 'save and continue', 'suivant', 'continuer', 'enregistrer et continuer'}):
            return 'draft_write'
        return None

    def browser_actions(self, snapshot):
        allowed = [(role, field) for field in snapshot.fields if field.kind in {'button', 'link'}
                   for role in ('open_form', 'login', 'signup', 'next', 'submit')
                   if self.browser_control(snapshot.url, field.automation_id, field.label, role, role == 'submit')]
        manual = [(role, field) for role, field in allowed if field.automation_id.casefold() == 'applymanually']
        allowed = manual or allowed
        return [Action(ref=allowed[0][1].ref, op='click', click_role=allowed[0][0])] if len(allowed) == 1 else []

    def browser_continuation(self, url, previous_url):
        steps = ('signin', 'myinformation', 'myexperience', 'applicationquestions', 'voluntarydisclosures', 'selfidentify', 'review')
        before, after = self._step(previous_url), self._step(url)
        return before in steps and after in steps and steps.index(after) == steps.index(before) + 1

    def browser_native_post(self, source, target, boundary):
        if boundary == 'final_submit':
            return self._step(source) == 'review' and target == source.rstrip('/') + '/submit'
        if boundary == 'account_creation':
            return self._step(source) == 'signin' and target == source
        return boundary == 'draft_write' and self.browser_continuation(target, source)

    def browser_receipt_url(self, attempt, url):
        parts = urlsplit(url)
        if parts.query or parts.fragment:
            return False
        path = parts.path.rstrip('/')
        if path.endswith(('/application-submitted', '/confirmation')):
            target = self.resolve(parts._replace(path=path.rsplit('/', 1)[0]).geturl(), frozenset())
            return bool(target and (target.tenant, target.requisition_id) == (attempt.tenant, attempt.requisition_id))
        reserved_path = urlsplit(attempt.url).path.split('/job/', 1)[0]
        return path == reserved_path + '/candidateHome'

    async def browser_receipt(self, attempt, page, receipt_url, positive_text, posting):
        if self.browser_receipt_url(attempt, receipt_url) and urlsplit(receipt_url).path.endswith('/candidateHome'):
            rows = await page.locator('[data-automation-id="candidateHomeApplication"]').evaluate_all("""rows => rows
                .filter(row => row.getClientRects().length)
                .map(row => [row.querySelector('a[href]')?.href, row.querySelector('[data-automation-id="applicationStatus"]')?.innerText])""")
            matches = [status for link, status in rows if link and origin(link) == origin(attempt.url)
                       and (target := self.resolve(link, frozenset()))
                       and (target.tenant, target.requisition_id) == (attempt.tenant, attempt.requisition_id)]
            return 'candidate_home_listing' if len(matches) == 1 and _normalized(matches[0] or '') in {'submitted', 'envoyee'} else 'none'
        if positive_text and (posting or (self.browser_receipt_url(attempt, receipt_url)
                                          and not urlsplit(receipt_url).path.endswith('/candidateHome'))):
            return 'first_party_confirmation'
        return 'none'

    def matches(self, url: str, dom_markers: frozenset[str]) -> float:
        return 1.0 if _posting(url) else 0.0

    def dedupe_identity(self, url: str) -> tuple[str, str] | None:
        posting = _posting(url)
        if not posting:
            return None
        tenant, requisition, _ = _origin_identity(self.name, url)
        if requisition and requisition.casefold() == posting[1].casefold():
            return canonical_identity(tenant, requisition)
        return None

    def resolve(self, url: str, dom_markers: frozenset[str]) -> Requisition | None:
        from jobhunt.apply.adapters import Requisition

        posting = _posting(url)
        if not posting:
            return None
        tenant, requisition, recipient = posting
        return Requisition(tenant, requisition, (recipient,), endpoints=(
            ("cv_upload_parse", recipient + "/wday/cxs/{tenant}/job/{requisition}/attachments"),
            ("draft_write", recipient + "/wday/cxs/{tenant}/job/{requisition}/draft"),
            ("account_creation", recipient + "/wday/cxs/{tenant}/job/{requisition}/account"),
            ("final_submit", recipient + "/wday/cxs/{tenant}/job/{requisition}/submit"),
        ))

    async def dispatch(self, request, gate) -> object:
        gate.stop("final_submit")

    async def supervise(self, request, fill):
        return await fill(WorkdayPlanner())

    def browser_request_allowed(self, attempt: Attempt, url: str, method: str, navigation: bool) -> bool:
        if not navigation:
            return True
        if origin(url) != origin(attempt.url):
            return False
        target = _posting(url)
        if target:
            return target[:2] == (attempt.tenant, attempt.requisition_id)
        # On shared hosts, keep non-posting account/form screens within the tenant's career-site path.
        if _posting(attempt.url):
            path = unquote(urlsplit(url).path).strip("/").casefold().split("/")
            if path and _LOCALE.fullmatch(path[0]):
                path.pop(0)
            tenant = attempt.tenant.split("/", 1)[1].split("/")
            if len(tenant) > 1:
                tenant.insert(0, "recruiting")
            return path[:len(tenant)] == tenant and "job" not in path[len(tenant):]
        return False

    async def verify_receipt(self, attempt: Attempt, *, evidence: object = None) -> Receipt:
        from jobhunt.apply.adapters import Receipt

        if (attempt.adapter == self.name and attempt.receipt_kind == "candidate_home_listing"
                and isinstance(evidence, CandidateHomeEvidence) and evidence.state == "submitted"
                and evidence.attempt_nonce and evidence.attempt_nonce == attempt.nonce):
            posting = _posting(evidence.posting_url)
            if posting and posting[:2] == (attempt.tenant, attempt.requisition_id):
                return Receipt(attempt, "confirmed", "candidate_home_listing", posting[1],
                               evidence.attempt_nonce, posting[1])
        return Receipt(attempt, "uncertain")
