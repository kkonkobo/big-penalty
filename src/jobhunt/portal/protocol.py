"""Wire contract between the Mac sync client and the portal server (portal/DESIGN.md §3-§5).

Imports: the standard library and pydantic only (the server image contains nothing else of `jobhunt`). Every model
forbids unknown keys and caps every string and list; both sides validate what they receive with these models."""

from __future__ import annotations

import hashlib
import json
import secrets
import unicodedata
from typing import Annotated, Literal, Union

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints

PROTOCOL_VERSION = 3
API_PREFIX = f"/api/v{PROTOCOL_VERSION}"

# ---------------------------------------------------------------- limits (enforced by both sides)
MAX_JSON_BODY = 64 * 1024            # every JSON request except the snapshot push
MAX_SNAPSHOT_BODY = 4 * 1024 * 1024  # PUT .../snapshot
MAX_DOCUMENT_BYTES = 1_500_000       # one PDF
MAX_DOCUMENTS = 150                  # per profile
MAX_DOCUMENTS_BYTES = 120 * 1024 * 1024  # per profile
MAX_PENDING_EVENTS = 500             # per profile, waiting for the Mac
CANCEL_RESERVE = 50
MAX_RESULTS = 100                    # outcomes reported with a snapshot
INBOX_PAGE = 200                     # events per GET inbox
MAX_PROFILES_PER_DEVICE = 10
SENT_MAX = 200
SENT_DOCUMENTS = 50                  # the most recent sent applications keep their PDF on the server
AWAITING_DOCUMENTS = 100             # the best-scored waiting applications get their PDF on the server

# ---------------------------------------------------------------- passcode verifier: ONE fixed scrypt tuple
SCRYPT_N, SCRYPT_R, SCRYPT_P, SCRYPT_DKLEN = 2 ** 15, 8, 1, 32
SCRYPT_SALT_BYTES = 16
SCRYPT_MAXMEM = 64 * 1024 * 1024

CODE_ALPHABET = "0123456789abcdefghjkmnpqrstvwxyz"  # 32 symbols, no i / l / o / u
HANDLE_LEN = 10          # 50 bits: the "identifiant" (looks up the profile; not a secret on its own)
PASSCODE_LEN = 12        # 60 bits: the generated "code"
PASSCODE_MIN_CHOSEN = 10  # an operator-chosen code, after normalisation
SECRET_MAX_INPUT = 128   # longest handle / code accepted from a form or the CLI


def normalize_secret(text: str) -> str:
    """How a handle or a code is compared on BOTH sides: NFKC, every whitespace and dash removed, casefolded."""
    t = unicodedata.normalize("NFKC", text or "")
    return "".join(ch for ch in t if not ch.isspace() and ch not in "-‐‑‒–—").casefold()


def _random_code(length: int) -> str:
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(length))


def new_handle() -> str:
    return _random_code(HANDLE_LEN)


def new_passcode() -> str:
    return _random_code(PASSCODE_LEN)


def grouped(code: str, size: int) -> str:
    """"abcdefghjkmn" -> "abcd-efgh-jkmn": how a generated handle (size 5) or code (size 4) is shown to a person."""
    return "-".join(code[i:i + size] for i in range(0, len(code), size))


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def handle_digest(handle: str) -> str:
    """What the server stores and looks up: sha256 of the normalised handle."""
    return sha256_hex(normalize_secret(handle))


def passcode_hash(passcode: str, salt: bytes) -> bytes:
    """scrypt of the normalised code with the fixed tuple. ~32 MiB and a few tens of ms: call it off the event loop."""
    return hashlib.scrypt(normalize_secret(passcode).encode("utf-8"), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P,
                          dklen=SCRYPT_DKLEN, maxmem=SCRYPT_MAXMEM)


def new_id() -> str:
    """128-bit random id (32 hex): profile keys, stream ids, grants, event ids, document ids, refs."""
    return secrets.token_hex(16)


def new_token() -> str:
    """256-bit random secret (device token, session id, known-device id), URL-safe."""
    return secrets.token_urlsafe(32)


# ---------------------------------------------------------------- field types
Hex16 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{16}$")]
Hex32 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{32}$")]
Hex64 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
Ver = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{32}$")]  # random version token of a field, issued by the Mac only
AnswerPath = Annotated[str, StringConstraints(pattern=r"^[a-z0-9_]{1,40}(\.[a-z0-9_]{1,40}){0,3}$")]
Host = Annotated[str, StringConstraints(max_length=255)]
Name = Annotated[str, StringConstraints(max_length=300)]      # company, job title
Line = Annotated[str, StringConstraints(max_length=500)]      # a label, a question, a short answer
Text = Annotated[str, StringConstraints(max_length=4000)]     # an answer value (textarea)
Url = Annotated[str, StringConstraints(max_length=2000, pattern=r"^https?://[^\s]+$")]
Message = Annotated[str, StringConstraints(max_length=300)]
Timestamp = AwareDatetime  # a naive datetime is refused: both sides compare and sort these
Number = Annotated[float, Field(allow_inf_nan=False)]  # NaN / infinity are refused (they do not survive JSON)


class Wire(BaseModel):
    model_config = ConfigDict(extra="forbid")


def body_digest(model: BaseModel) -> str:
    """sha256 of the CANONICAL JSON of a validated body (object keys sorted, lists kept in order, timestamps as pydantic
    serialises them): two bodies that validate to the same model have the same digest whatever the key order on the wire,
    so an identical retry of the same revision is accepted and a different body never is."""
    canonical = json.dumps(model.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- pairing and profile configuration
class PairRequest(Wire):
    pairing_code: Annotated[str, StringConstraints(min_length=1, max_length=200)]
    device_name: Annotated[str, StringConstraints(min_length=1, max_length=80)]
    token_sha256: Hex64


class PairResponse(Wire):
    device_id: Hex32


class PasscodeVerifier(Wire):
    salt: Hex32 = Field(repr=False)
    hash: Hex64 = Field(repr=False)


class ProfileConfig(Wire):
    """PUT /profiles/{profile_key}. `passcode=None` keeps the stored verifier (a first push must carry one)."""
    config_rev: int = Field(ge=1, le=2 ** 53)
    enabled: bool
    handle_sha256: Hex64
    passcode: PasscodeVerifier | None = None
    sessions_epoch: int = Field(ge=0, le=2 ** 53)
    stream_id: Hex32


class ProfileConfigResponse(Wire):
    config_rev: int


# ---------------------------------------------------------------- snapshot (Mac -> server)
class FieldSpec(Wire):
    """One entry of `facts.ANSWER_FIELDS` as the portal needs it."""
    path: AnswerPath
    label: Line
    help: Line = ""
    widget: Literal["text", "date", "select", "textarea"] = "text"
    options: list[Line] = Field(default_factory=list, max_length=60)
    section: Annotated[str, StringConstraints(max_length=100)]
    max_len: int = Field(default=500, ge=1, le=4000)
    placeholder: Line = ""
    write_only: bool = False  # the value is never sent to the server; the portal shows "renseigné" and lets him replace it


class AnswerSchema(Wire):
    sections: list[Annotated[str, StringConstraints(max_length=100)]] = Field(max_length=30)
    fields: list[FieldSpec] = Field(max_length=200)


class AnswerValue(Wire):
    value: Text | None = None  # the form's value (ISO for a date widget); None for a write-only field
    is_set: bool               # the Mac has a value for this path
    legacy: Line = ""          # date widget whose stored text is not a complete date ("janvier 2027"): shown as a note
    ver: Ver


class CustomAnswer(Wire):
    custom_id: Hex16
    question: Line
    answer: Line = ""      # "" when masked
    masked: bool = False   # the question looks like a write-only topic: the answer is not sent to the server
    ver: Ver


class PendingQuestion(Wire):
    qid: Hex16
    label: Line
    options: list[Line] = Field(default_factory=list, max_length=100)
    company: Name = ""
    title: Name = ""


class EmailPause(Wire):
    grant: Hex32      # action "resume": "j'ai cliqué sur le lien"
    company: Name = ""
    title: Name = ""
    host: Host = ""
    note: Message = ""


class Unconfirmed(Wire):
    grant_sent: Hex32      # action "resolve_sent": "elle a bien été envoyée"
    grant_not_sent: Hex32  # action "resolve_not_sent": "elle n'a pas été envoyée" (it goes back to the queue)
    company: Name = ""
    title: Name = ""
    host: Host = ""
    when: Timestamp | None = None


class InfoItem(Wire):
    kind: Literal["captcha", "host", "dry_run", "sending", "interrupted", "supervised", "approved", "email", "human"]
    company: Name = ""
    title: Name = ""
    host: Host = ""


class Awaiting(Wire):
    grant: Hex32      # action "send"
    company: Name = ""
    title: Name = ""
    score: Number | None = None
    age_days: Number | None = None
    host: Host = ""
    job_url: Url | None = None
    doc_id: Hex32 | None = None
    retry: bool = False  # the last attempt failed without any click that may have sent it: sending again is allowed


class Prep(Wire):
    message: Annotated[str, StringConstraints(max_length=1000)]
    hint: Annotated[str, StringConstraints(max_length=1000)] = ""


class SentItem(Wire):
    ref: Hex32
    company: Name = ""
    title: Name = ""
    submitted_at: Timestamp | None = None
    host: Host = ""
    url: Url | None = None
    doc_id: Hex32 | None = None
    prep: list[Prep] = Field(default_factory=list, max_length=40)


class DocumentRef(Wire):
    doc_id: Hex32
    sha256: Hex64
    size: int = Field(ge=5, le=MAX_DOCUMENT_BYTES)


class Counts(Wire):
    sending: int = Field(default=0, ge=0)
    failed: int = Field(default=0, ge=0)
    blocked: int = Field(default=0, ge=0)
    sent_total: int = Field(default=0, ge=0)


class Snapshot(Wire):
    protocol: Literal[3] = PROTOCOL_VERSION
    display_name: Annotated[str, StringConstraints(max_length=80)] = ""
    synced_at: Timestamp
    auto_send_on: bool = False
    review_mode: bool = False
    paused: bool = False
    answer_schema: AnswerSchema
    answers: dict[AnswerPath, AnswerValue] = Field(default_factory=dict, max_length=200)
    custom: list[CustomAnswer] = Field(default_factory=list, max_length=500)
    questions: list[PendingQuestion] = Field(default_factory=list, max_length=200)
    email_pauses: list[EmailPause] = Field(default_factory=list, max_length=200)
    unconfirmed: list[Unconfirmed] = Field(default_factory=list, max_length=200)
    info: list[InfoItem] = Field(default_factory=list, max_length=300)
    awaiting: list[Awaiting] = Field(default_factory=list, max_length=500)
    sent: list[SentItem] = Field(default_factory=list, max_length=SENT_MAX)
    documents: list[DocumentRef] = Field(default_factory=list, max_length=MAX_DOCUMENTS)
    counts: Counts = Field(default_factory=Counts)


# ---------------------------------------------------------------- events (server -> Mac)
class _Event(Wire):
    seq: int = Field(ge=1, le=2 ** 53)
    event_id: Hex32
    at: Timestamp  # server clock, UTC


class AnswerEvent(_Event):
    kind: Literal["answer"] = "answer"
    path: AnswerPath
    value: Text      # "" = remove the answer
    base_ver: Ver    # the snapshot's version of the field when he edited it
    after: Hex32 | None = None  # event_id of his previous pending edit of the same field, if any


class QuestionAnswerEvent(_Event):
    kind: Literal["question_answer"] = "question_answer"
    qid: Hex16
    answer: Annotated[str, StringConstraints(min_length=1, max_length=500)]


class CustomEditEvent(_Event):
    kind: Literal["custom_edit"] = "custom_edit"
    custom_id: Hex16
    answer: Annotated[str, StringConstraints(min_length=1, max_length=500)]
    base_ver: Ver
    after: Hex32 | None = None


class CustomDeleteEvent(_Event):
    kind: Literal["custom_delete"] = "custom_delete"
    custom_id: Hex16
    base_ver: Ver
    after: Hex32 | None = None


class IntentEvent(_Event):
    kind: Literal["intent"] = "intent"
    grant: Hex32  # a grant id of the current snapshot; the action is the grant's, known to the Mac only


class CancelEvent(_Event):
    kind: Literal["cancel"] = "cancel"
    target: Hex32  # event_id of a pending intent of the same profile


Event = Annotated[Union[AnswerEvent, QuestionAnswerEvent, CustomEditEvent, CustomDeleteEvent, IntentEvent, CancelEvent],
                  Field(discriminator="kind")]
EVENT_KINDS = ("answer", "question_answer", "custom_edit", "custom_delete", "intent", "cancel")


class Inbox(Wire):
    stream_id: Hex32
    last_seq: int = Field(ge=0, strict=True)
    acked_seq: int = Field(ge=0, strict=True)  # lower than the Mac's own record = restored server
    sessions_epoch: int = Field(ge=0, strict=True)
    revoked_through_seq: int = Field(ge=0, strict=True)
    config_rev: int = Field(ge=1, strict=True)
    enabled: bool = Field(strict=True)
    held: bool = Field(strict=True)
    events: list[Event] = Field(default_factory=list, max_length=INBOX_PAGE)


ResultStatus = Literal["received", "applied", "conflict", "rejected", "awaiting_review", "queued", "running", "done", "refused",
                       "failed", "cancelled", "interrupted", "lost"]
# "received": the Mac has the event but has not applied it yet (portal paused on the Mac).
# "lost" is only ever set by the server (stream change): the action never reached a final state and must be done again.
LIVE_STATUSES = ("received", "awaiting_review", "queued", "running")  # the Mac has it and it is not finished
FINAL_STATUSES = ("applied", "conflict", "rejected", "done", "refused", "failed", "cancelled", "interrupted", "lost")
GRANT_ACTIONS = ("send", "resume", "resolve_sent", "resolve_not_sent")


class EventResult(Wire):
    event_id: Hex32
    kind: Literal["answer", "question_answer", "custom_edit", "custom_delete", "intent", "cancel"]
    status: ResultStatus
    message: Message = ""   # French, for the job seeker: a fixed sentence, never a value and never free text of the engine
    label: Line = ""        # what it was about (field label, question, "Société — Poste")
    key: Annotated[str, StringConstraints(max_length=120)] = ""  # what it targets: "a:<path>", "c:<custom_id>", "q:<qid>", "g:<grant>"
    at: Timestamp


class SnapshotPush(Wire):
    stream_id: Hex32
    sessions_epoch: int = Field(ge=0, le=2 ** 53)
    revoked_through_seq: int = Field(ge=0, le=2 ** 53)
    rev: int = Field(ge=1, le=2 ** 53)
    acked_seq: int = Field(ge=0, le=2 ** 53)
    results: list[EventResult] = Field(default_factory=list, max_length=MAX_RESULTS)
    snapshot: Snapshot


class SnapshotPushResponse(Wire):
    rev: int
    missing_documents: list[Hex32] = Field(default_factory=list, max_length=MAX_DOCUMENTS)


class ProfileStatus(Wire):
    profile_key: Hex32
    enabled: bool
    held: bool = False
    config_rev: int
    snapshot_rev: int
    last_seq: int
    acked_seq: int
    sessions: int


class ServerStatus(Wire):
    protocol: Literal[3]
    version: Annotated[str, StringConstraints(max_length=80)] = ""
    profiles: list[ProfileStatus] = Field(default_factory=list, max_length=MAX_PROFILES_PER_DEVICE)


class ErrorBody(Wire):
    """Every API error: a fixed code, never an echo of the request. `current` = the stored revision for a 409."""
    error: Annotated[str, StringConstraints(max_length=40)]
    current: int | None = None
