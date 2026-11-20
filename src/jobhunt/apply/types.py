"""Shared contract of the apply engine (milestone 2b). Pure data: no browser, no DB, no LLM here.

Flow of ONE run: open job.origin_url -> loop { snapshot the page -> planner returns a StepPlan -> the engine VALIDATES every
action against the FactSheet (the planner maps fields to fact keys; it never invents an answer) -> execute -> screenshot }
until the final submit button. `dry_run` stops before the final click; `submit` clicks it only if the `before_submit` hook
agrees, then waits for a confirmation page.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field, StringConstraints

# ---------------------------------------------------------------- facts
class Fact(BaseModel):
    """One known answer. value None = UNKNOWN (never guessed: a required field needing it pauses the run)."""
    key: str                      # "first_name", "email", "available_from", "custom_3"...
    label: str                    # French description shown to the planner and the user
    value: str | None = None
    exact: bool = False           # True: the engine types `value` (or one of `variants`) verbatim; the planner's own text is ignored
    variants: list[str] = Field(default_factory=list)  # accepted alternative spellings of an exact fact (phone formats)
    secret: bool = False          # True: value never sent to the LLM, never logged, never stored (account password)
    # Regex (case-insensitive, searched in the accent-stripped field label + name + placeholder) a field must match to receive
    # this fact with op fill; None = no restriction. Set on identity facts so a name/e-mail/phone cannot land in an unrelated field.
    label_re: str | None = None


class FactSheet(BaseModel):
    facts: list[Fact] = Field(default_factory=list)

    def get(self, key: str | None) -> Fact | None:
        return next((f for f in self.facts if f.key == key), None) if key else None

    def known(self, key: str | None) -> bool:
        f = self.get(key)
        return f is not None and f.value not in (None, "")


# ---------------------------------------------------------------- page snapshot
FieldKind = Literal["text", "email", "tel", "url", "number", "date", "password", "textarea", "select", "radio", "checkbox",
                    "file", "combobox", "button", "link"]


class FieldOption(BaseModel):
    value: str
    label: str


class FormField(BaseModel):
    ref: str                      # unique in the snapshot ("f12"); the engine resolves it to (frame, element)
    kind: FieldKind
    label: str = ""               # <label>, aria-label(ledby), legend + question text, placeholder, or button/link text
    name: str = ""
    placeholder: str = ""
    required: bool = False
    multiple: bool = False
    options: list[FieldOption] = Field(default_factory=list)  # select options / radio group choices (one field per group)
    value: str = ""               # current value ("true"/"false" for a checkbox, selected label for select/radio)
    accept: str = ""              # file inputs
    group: str = ""               # fieldset legend / nearest section heading
    error: str = ""               # validation message attached to the field, if any
    form: str = ""                # identity of the enclosing <form> ("<frame>:<n>"), "" when outside any form
    autocomplete: str = Field(default="", exclude=True, repr=False)
    destination: tuple[str, str] | None = Field(default=None, exclude=True, repr=False)
    choice_destination: tuple[str, str] | None = Field(default=None, exclude=True, repr=False)
    native_submit: bool = Field(default=False, exclude=True, repr=False)
    wording: str = Field(default="", exclude=True, repr=False)
    choice_options: list[tuple[str, str, bool, str]] = Field(default_factory=list, exclude=True, repr=False)
    selected: list[int] = Field(default_factory=list, exclude=True, repr=False)
    live_ref: str = Field(default="", exclude=True, repr=False)
    automation_id: str = Field(default="", exclude=True, repr=False)
    widget_candidate: bool = Field(default=False, exclude=True, repr=False)


class PageSnapshot(BaseModel):
    url: str
    title: str = ""
    text: str = ""                # visible text excerpt (headings, notices, errors), capped
    fields: list[FormField] = Field(default_factory=list)
    has_captcha: bool = False     # a VISIBLE captcha challenge widget (not an invisible reCAPTCHA v3 badge)


# ---------------------------------------------------------------- plan of one step
PageKind = Literal["job_page", "form", "account_login", "account_signup", "email_verification", "captcha", "confirmation",
                   "closed", "error", "other"]
Op = Literal["fill", "select", "check", "uncheck", "upload_cv", "upload_letter", "letter_text", "click"]
ClickRole = Literal["open_form", "next", "submit", "signup", "login", "cookies", "other"]


class Action(BaseModel):
    ref: str
    op: Op
    fact: str | None = None       # fact key justifying the answer: REQUIRED for fill / select / check / uncheck
    value: str | None = None      # fill: text (a re-formatting of the fact, e.g. a date); select: the option label
    click_role: ClickRole | None = None  # only for op == "click"
    # Autonomy policy (user decision 2026-11-17): when no known fact answers a question, the planner DEDUCES the most plausible
    # answer from the candidate's profile instead of pausing. Such an answer is marked inferred (with a short French reason)
    # and listed for the candidate ("Réponses déduites") so he can back it up in an interview. Hard facts are never inferred.
    inferred: bool = False
    basis: str = ""


class Question(BaseModel):
    """A field that could neither be answered from the facts nor deduced (unknown hard fact, contradiction...)."""
    ref: str = ""
    label: str
    kind: str = "text"
    options: list[str] = Field(default_factory=list)
    required: bool = True
    fact_id: str | None = None
    reason: Literal["fact", "decision", "technical", "unmapped"] | None = None
    category: str | None = None
    choice_binding: str | None = None
    choice_indices: list[int] = Field(default_factory=list)


class StepPlan(BaseModel):
    page_kind: PageKind
    actions: list[Action] = Field(default_factory=list)
    unknown: list[Question] = Field(default_factory=list)
    note: str = ""                # short reason (e.g. which link the candidate must click, why the job is closed)


# ---------------------------------------------------------------- run result
Mode = Literal["dry_run", "submit", "supervised"]
# ready = dry run reached the final submit button with everything filled; needs_input = unknown required question(s);
# needs_email = the site waits for an e-mail verification link; needs_human = visible CAPTCHA / bot challenge;
# blocked = refused by a guardrail (before_submit hook, job closed); failed = technical failure.
RunStatus = Literal["running", "ready", "submitted", "needs_input", "needs_email", "needs_human", "blocked", "failed"]


class FilledField(BaseModel):
    label: str
    op: str
    fact: str | None = None
    value: str = ""               # what was typed / chosen ("•••" for secrets)
    inferred: bool = False        # deduced from the profile, not backed by a known fact (shown as "déduite")
    basis: str = ""               # why (French, short)


class StepLog(BaseModel):
    index: int
    url: str
    page_kind: str
    filled: list[FilledField] = Field(default_factory=list)
    clicked: str = ""             # label of the button clicked to leave the step, "" if none
    screenshot: str = ""          # absolute path of the full-page PNG taken after filling
    note: str = ""


class DispatchDiagnostics(BaseModel, validate_assignment=True):
    email: Literal['email_link_used', 'email_code_used', 'email_not_found', 'email_auth_failed'] | None = None
    step: int = Field(default=0, ge=0, le=10000)
    control: Literal["reservation", "navigation", "scan", "text", "choice", "upload", "account_creation", "login_otp",
                     "cv_upload_parse", "draft_write", "final_submit", "other"] = "reservation"
    receipt_kind: Literal["none", "first_party_confirmation", "candidate_home_listing", "correlated_email"] = "none"
    receipt_reason: Literal["before_submit", "submit_unverified", "no_final_response", "form_still_present",
                            "no_confirmation", "confirmation_text", "confirmation_url", "confirmation_json", "attested", "receipt_rejected",
                            "recipient_refused", "verification_required", "human_required"] = "before_submit"
    # Unauthorized requests aborted while the run continued (hosts only, most frequent first; never paths or bodies).
    aborted_hosts: list[Annotated[str, StringConstraints(max_length=253, pattern=r"^[a-z0-9.\-\[\]:]+$")]] = Field(
        default_factory=list, max_length=20)
    aborted_requests: int = Field(default=0, ge=0, le=100000)

    def exception_code(self, error: Exception) -> str:
        return f"exception:{type(error).__name__[:64]}@{self.step}/{self.control}"


class RunResult(BaseModel):
    status: RunStatus
    message: str = ""             # French, user-facing
    diagnostic_code: str = ""
    dispatch_diagnostics: DispatchDiagnostics = Field(default_factory=DispatchDiagnostics)
    consent_codes: list[str] = Field(default_factory=list)
    steps: list[StepLog] = Field(default_factory=list)
    questions: list[Question] = Field(default_factory=list)   # status == needs_input
    final_url: str = ""
    # True as soon as the engine is about to click the FINAL submit button (set before the click). With any status other
    # than `submitted`, the application MAY have been sent: it is never retried automatically (human check on the site).
    submit_clicked: bool = False
    # status == blocked because the form (page or frame, or the form's action) lives on a host that is neither the job's
    # own site nor a known ATS nor trusted by the user: that host, so the UI can offer "faire confiance à ce site".
    untrusted_host: str = ""
    finished_at: datetime | None = None


# Non-secret reason codes of an account stop: shown instead of the generic credential_page, so the failure is diagnosable.
ACCOUNT_REASONS = {
    "account_signup_missing_link": "Compte requis : aucun accès à la création de compte trouvé sur la page de connexion.",
    "account_signup_field_unfilled": "Création de compte : un champ obligatoire n'a pas pu être rempli automatiquement.",
    "account_login_failed": "Connexion au compte impossible : le site refuse les identifiants.",
    "account_signup_loop": "Création de compte : le lien de création ramène à la même page.",
}
