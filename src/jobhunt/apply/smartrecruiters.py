"""Supervised SmartRecruiters guest flow. No ATS transport or browser handles."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal
from urllib.parse import unquote, urlsplit

from jobhunt.apply.destination import clean_url, origin
from jobhunt.apply.types import Action, FactSheet, PageSnapshot, Question, StepPlan
from jobhunt.apply.answer_check import fold
from jobhunt.apply.widgets import denied_widget

if TYPE_CHECKING:
    from jobhunt.apply.adapters import Attempt, Boundary, BoundaryPolicy, Requisition

POSTING = re.compile(r"/([A-Za-z0-9][A-Za-z0-9_-]{0,79})/([0-9]{5,24})(?:-[A-Za-z0-9_.~%-]+)?/?\Z")
PUBLICATION = re.compile(
    r"/oneclick-ui/company/([A-Za-z0-9][A-Za-z0-9_-]{0,79})/publication/"
    r"([a-fA-F0-9]{8}(?:-[a-fA-F0-9]{4}){3}-[a-fA-F0-9]{12})"
    r"(?:/(?:apply|application)(?:/(?:profile|experience|education|questions|attachments|review|submit))?"
    r"|/(?:profile|experience|education|questions|review))?/?\Z"
)
CONFIRMATION_SUFFIX = re.compile(r"/(?:confirmation|success|thank-you)/?\Z")
FORBIDDEN = re.compile(r"(?:linkedin|indeed|smart[-_]?profile|smartr|oauth|sign[-_ ]?in|log[-_ ]?in|"
                       r"sign[-_ ]?up|register|import|account[-_ ]?check|authentication|/auth(?:/|$))", re.I)
UPLOAD_ORIGIN = "https://api.smartrecruiters.com"
STATIC_ORIGIN = "https://c.smartrecruiters.com"
UPLOAD_PATH = "/oneclick-ui/company/{tenant}/publication/{requisition}/attachments"
SAME_JOB = "smartrecruiters:same-job:"
TEXT = {"text", "email", "tel", "url", "textarea", "number", "date"}
ALIASES = {
    "first name": "first_name", "firstname": "first_name", "given name": "first_name", "prenom": "first_name",
    "last name": "last_name", "lastname": "last_name", "family name": "last_name", "nom": "last_name",
    "email": "email", "e mail": "email", "email address": "email", "adresse e mail": "email", "courriel": "email",
    "phone": "phone", "phone number": "phone", "telephone": "phone", "numero de telephone": "phone",
    "location": "city", "city": "city", "ville": "city", "localisation": "city", "country": "country", "pays": "country",
    "linkedin": "linkedin", "linkedin profile": "linkedin", "linkedin url": "linkedin", "profil linkedin": "linkedin",
}
EDUCATION = {"school": "school", "institution": "school", "ecole": "school", "degree": "degree", "diplome": "degree"}
COVER = {"cover letter", "lettre de motivation", "message to hiring manager", "message to the hiring team",
         "message au recruteur", "message a l equipe de recrutement"}
CV = {"cv", "resume", "resume upload", "upload resume", "upload cv", "curriculum vitae", "joindre un cv"}
NOTICE = re.compile(r"\b(?:consent\w*|privacy|confidentialite|notice\w*|policy|terms|politiques?|conditions|"
                    r"rgpd|gdpr|marketing|talent pool|data processing|traitement des donnees)\b")
ACKNOWLEDGMENTS = {"your application has been submitted", "your application was successfully submitted",
                   "your application has been received", "votre candidature a bien ete envoyee",
                   "votre candidature a bien ete recue"}
LINKEDIN_FIELDS = {"linkedin", "linkedin profile", "linkedin url", "profil linkedin"}


def _words(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", fold(value).lower()))


def _first_party(url: str) -> bool:
    parsed = urlsplit(url)
    host = parsed.hostname or ""
    return (parsed.scheme == "https" and parsed.port is None and host.endswith(".smartrecruiters.com")
            and all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", label) for label in host.split(".")))


def _identity(url: str, *, confirmation: bool = False) -> tuple[str, str, str] | None:
    if "#" in url:
        return None
    url = clean_url(url, navigation=True)
    if not url or not _first_party(url):
        return None
    parsed = urlsplit(url)
    if confirmation and not CONFIRMATION_SUFFIX.search(parsed.path):
        return None
    path = CONFIRMATION_SUFFIX.sub("", parsed.path) if confirmation else parsed.path
    if parsed.query:
        return None
    if match := PUBLICATION.fullmatch(path):
        return match[1].lower(), match[2].lower(), "publication"
    if not confirmation and parsed.hostname == "jobs.smartrecruiters.com" and (match := POSTING.fullmatch(path)):
        return match[1].lower(), match[2], "posting"
    return None


def same_job_marker(source_url: str, apply_url: str) -> str:
    """For a trusted origin inspector: assert an application target belongs to the current posting, not a bare link."""
    return SAME_JOB + clean_url(source_url, navigation=True) + "\n" + clean_url(apply_url, navigation=True)


@dataclass(frozen=True)
class SameTabConfirmation:
    """Transient evidence supplied by a trusted observer of the active reserved browser tab, not scraped metadata.

    The observer must witness the guest form and the operator submitting it before reading the confirmation view.
    Tab identifiers and nonce are taken from that observation's browser/attempt binding, not the page or job row.
    """

    observed_nonce: str
    submitted_tab: str
    confirmation_tab: str
    submitted_url: str
    confirmation_url: str
    acknowledgment: str


class SmartRecruitersPlanner:
    def __init__(self, url: str, adapter=None):
        self.url = url
        self.adapter = adapter

    async def plan(self, snapshot: PageSnapshot, facts: FactSheet, **_context) -> StepPlan:
        if snapshot.has_captcha:
            return StepPlan(page_kind="captcha")
        if FORBIDDEN.search(urlsplit(snapshot.url).path) or any(field.kind == "password" for field in snapshot.fields):
            return StepPlan(page_kind="account_login")
        if (not (identity := _identity(self.url)) or identity[2] != "publication" or _identity(snapshot.url) != identity
                or origin(snapshot.url) != origin(self.url)):
            return StepPlan(page_kind="job_page")

        actions: list[Action] = []
        manual: list[Question] = []
        for field in snapshot.fields:
            label, name, group = map(_words, (field.label, field.name, field.group))
            if NOTICE.search(f"{label} {name} {group}"):
                if not self.adapter and field.kind not in {"button", "link"}:
                    manual.append(Question(ref=field.ref, label=field.label, kind=field.kind, required=field.required,
                                           reason="decision", category="consent"))
                continue
            if field.value or field.kind in {"checkbox", "radio", "button", "link", "password"}:
                continue
            if FORBIDDEN.search(f"{label} {name}") and label not in LINKEDIN_FIELDS:
                continue
            if field.kind == "file":
                if label in CV or name in CV:
                    actions.append(Action(ref=field.ref, op="upload_cv"))
                continue
            if field.kind not in TEXT | {"select", "combobox"}:
                continue
            if group in {"education", "formation", "education history"}:
                key = EDUCATION.get(label)
            else:
                key = ALIASES.get(label) or (ALIASES.get(name) if not label else None)
            labels = {f"{group} {label}".strip()} if group and group != label else {label}
            if label in COVER:
                labels |= COVER
            candidates = [fact for fact in facts.facts if fact.value and not fact.secret and (
                (key and fact.key == key) or (not key and fact.key.startswith("custom_") and (
                    _words(fact.label) in labels or (label and _words(fact.label) == label
                                                   and not re.search(r"\b(?:experience|education|formation)\b", group)))))]
            if len(candidates) != 1:
                continue
            fact = candidates[0]
            if field.kind == "select" or field.kind == "combobox" and not denied_widget(field):
                choices = [option.label for option in field.options if _words(option.label) == _words(fact.value or "")]
                if len(choices) == 1 and choices[0]:
                    actions.append(Action(ref=field.ref, op="select", fact=fact.key, value=choices[0]))
            elif field.kind in TEXT:
                actions.append(Action(ref=field.ref, op="fill", fact=fact.key, value=None if fact.exact else fact.value))
        by_ref = {field.ref: field for field in snapshot.fields}
        repeated_history_facts = {action.fact for action in actions
                                  if sum(other.fact == action.fact for other in actions) > 1
                                  and re.search(r"\b(?:education|experience|formation)\b", _words(by_ref[action.ref].group))}
        actions = [action for action in actions if action.fact not in repeated_history_facts]
        if self.adapter:
            controls = [(role, field) for field in snapshot.fields if field.kind in {'button', 'link'}
                        for role in ('next', 'submit') if self.adapter.browser_control(
                            snapshot.url, field.automation_id, field.label, role, role == 'submit')]
            if len(controls) == 1:
                role, field = controls[0]
                actions.append(Action(ref=field.ref, op='click', click_role=role))
        return StepPlan(page_kind="form", actions=actions, unknown=manual)


class SmartRecruitersAdapter:
    name = "smartrecruiters"
    capability: Literal["unattended"] = "unattended"
    retry_rule: Literal["never"] = "never"
    boundaries: dict[Boundary, BoundaryPolicy] = {
        "account_creation": "forbidden", "login_otp": "forbidden", "import_profile": "forbidden",
        "challenge": "pause_for_human", "cv_upload_parse": "auto", "field_entry": "auto", "draft_write": "auto",
        "final_submit": "auto",
    }

    def browser_enrollable(self, url):
        identity = _identity(url)
        return bool(identity and identity[2] == 'publication')

    def browser_planner(self, attempt):
        return SmartRecruitersPlanner(attempt.url, self)

    def browser_control(self, url, automation_id, label, role, final):
        if not self.browser_enrollable(url):
            return None
        marker, text = automation_id.casefold(), _words(label)
        if (final and role == 'submit' and marker in {'', 'submitbutton', 'submit-application'}
                and text in {'submit application', 'send application', 'envoyer ma candidature', 'submit'}):
            return 'final_submit'
        if (not final and role == 'next' and marker in {'', 'nextbutton', 'continue-button'}
                and text in {'next', 'continue', 'suivant', 'continuer'}):
            return 'draft_write'
        return None

    def browser_receipt_url(self, attempt, url):
        return _identity(url, confirmation=True) == (attempt.tenant, attempt.requisition_id, 'publication')

    def browser_continuation(self, url, previous_url):
        return url != previous_url and urlsplit(url).path.rstrip('/').endswith('/review')

    def browser_native_post(self, source, target, boundary):
        path = urlsplit(target).path.rstrip('/')
        if boundary == 'final_submit':
            return path.endswith(('/apply/submit', '/application/submit'))
        return boundary == 'draft_write' and path.endswith(('/questions', '/review')) and source != target

    async def browser_receipt(self, attempt, page, receipt_url, positive_text, posting):
        if positive_text and (posting or self.browser_receipt_url(attempt, receipt_url)):
            return 'first_party_confirmation'
        return 'none'

    def matches(self, url: str, dom_markers: frozenset[str]) -> float:
        return 1.0 if self.resolve(url, dom_markers) else 0.0

    def resolve(self, url: str, dom_markers: frozenset[str]) -> Requisition | None:
        from jobhunt.apply.adapters import Requisition

        if "#" in url:
            return None
        source = clean_url(url, navigation=True)
        if not origin(source) or not source.startswith("https://"):
            return None
        direct = _identity(source)
        prefix = SAME_JOB + source + "\n"
        targets = {target for marker in dom_markers if marker.startswith(prefix)
                   and (target := _identity(marker[len(prefix):])) and target[2] == "publication"}
        if len(targets) > 1 or (targets and direct and any(target[0] != direct[0] or
                (direct[2] == "publication" and target != direct) for target in targets)):
            return None
        if not direct and not targets:
            return None
        if not direct and _first_party(source):
            return None
        tenant, requisition, _ = next(iter(targets)) if targets else direct
        guest = direct and direct[2] == "publication"
        return Requisition(tenant, requisition, (UPLOAD_ORIGIN,) if guest else (),
                           endpoints=(*(("cv_upload_parse", host + UPLOAD_PATH)
                                        for host in dict.fromkeys((UPLOAD_ORIGIN, origin(source)))),
                                      ('draft_write', origin(source) + UPLOAD_PATH.removesuffix('/attachments') + '/draft'),
                                      ('final_submit', origin(source) + UPLOAD_PATH.removesuffix('/attachments') + '/submit')) if guest else (),
                           read_origins=(STATIC_ORIGIN,) if direct else ())

    async def supervise(self, request, fill):
        return await fill(SmartRecruitersPlanner(request.attempt.url))

    async def dispatch(self, request, gate):
        gate.stop("étape non autorisée")

    def browser_upload_route(self, attempt: Attempt, url: str) -> bool:
        return origin(url) == UPLOAD_ORIGIN or bool(re.search(
            r"/(?:attachments?|uploads?|resume(?:s|-parser)?|cv(?:-parser)?)(?:/|$)",
            unquote(urlsplit(url).path), re.I))

    def browser_request_allowed(self, attempt: Attempt, url: str, method: str, navigation: bool) -> bool:
        target_origin = origin(url)
        if not target_origin:
            return False
        parsed = urlsplit(url)
        if FORBIDDEN.search(unquote(parsed.path + "?" + parsed.query)):
            return False
        identity = _identity(attempt.url)
        if target_origin == STATIC_ORIGIN:
            return method in {"GET", "HEAD"} and not navigation and not parsed.query
        if target_origin == UPLOAD_ORIGIN:
            return bool(identity and identity[2] == "publication" and not navigation
                        and method in {"POST", "OPTIONS"} and not parsed.query
                        and parsed.path == UPLOAD_PATH.format(tenant=identity[0], requisition=identity[1]))
        if target_origin != origin(attempt.url):
            return False
        if not identity or identity[2] != "publication":
            return method in {"GET", "HEAD"} and (not navigation or clean_url(url, navigation=True) == attempt.url)
        if navigation:
            return (_identity(url) == identity or _identity(url, confirmation=True) == identity)
        return method in {"GET", "HEAD", "OPTIONS", "POST"}

    async def verify_receipt(self, attempt, *, evidence=None):
        from jobhunt.apply.adapters import Receipt

        if (attempt.adapter != self.name or not isinstance(evidence, SameTabConfirmation)
                or not evidence.submitted_tab or evidence.submitted_tab != evidence.confirmation_tab
                or not evidence.observed_nonce or evidence.observed_nonce != attempt.nonce
                or _words(evidence.acknowledgment) not in ACKNOWLEDGMENTS):
            return Receipt(attempt, "uncertain")
        form = _identity(evidence.submitted_url)
        confirmation = _identity(evidence.confirmation_url, confirmation=True)
        if (not form or form != confirmation or form != (attempt.tenant, attempt.requisition_id, "publication")
                or _identity(attempt.url) != form
                or origin(attempt.url) != origin(evidence.submitted_url)
                or origin(evidence.submitted_url) != origin(evidence.confirmation_url)):
            return Receipt(attempt, "uncertain")
        return Receipt(attempt, "confirmed", "first_party_confirmation", "same-tab-confirmation",
                       evidence.observed_nonce, confirmation[1])
