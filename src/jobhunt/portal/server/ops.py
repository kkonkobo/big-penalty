"""What the pages do with the stored data (DESIGN §5.1, §6): read the effective view, append events to the inbox.

No HTTP object in here: plain arguments, dataclass results, explicit `now` (epoch seconds). Every function takes the
`Portal` container first (`portal.db`, `portal.cipher`). Reads (`load_view`, `open_document`) take no write lock; every
mutating function runs in ONE `BEGIN IMMEDIATE` transaction that re-reads the pending events, so the token rule and the
contiguous `seq` cannot race.

Tokens: the effective token of a field or custom answer is its latest pending `event_id` (including live field results),
else the snapshot's `ver`. A page puts `FieldView.token` in a hidden input and posts it back; a mismatch means the
page is stale and the edit is reported (`Problem.kind == "stale"`) and NOT stored."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Any, Literal

from pydantic import TypeAdapter

from jobhunt.portal import protocol
from jobhunt.portal.protocol import (AnswerEvent, CancelEvent, CustomDeleteEvent, CustomEditEvent, EventResult, FieldSpec, IntentEvent,
                                     QuestionAnswerEvent, Snapshot)

from .auth import Session, session_is_live
from .crypto import Cipher, Sealed

if TYPE_CHECKING:
    from .app import Portal

EVENT_ADAPTER: TypeAdapter = TypeAdapter(protocol.Event)
RESULTS_ADAPTER: TypeAdapter = TypeAdapter(list[EventResult])

MSG_STALE = "Cette réponse a changé entre-temps : rechargez la page."
MSG_CHANGED = "Cette candidature a changé : rechargez la page."
MSG_FULL = "Trop de demandes en attente : réessayez quand l'ordinateur aura synchronisé."
MSG_NO_SNAPSHOT = "Aucune donnée n'est encore synchronisée."
MSG_LOST = "Perdu lors d'une réinitialisation : à refaire."
MSG_REVOKED = "Annulée : les accès au portail ont changé."
CANCEL_RESERVE = 50
CANCELLABLE_STATUSES = ("received", "queued", "awaiting_review")
CHAINABLE_ANSWER_STATUSES = ("received", "awaiting_review")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# ---------------------------------------------------------------- results
@dataclass(frozen=True)
class Problem:
    kind: Literal["stale", "invalid", "refused"]
    key: str       # path, qid, custom id or grant id the message is about
    label: str
    message: str   # French, never echoes what he typed


@dataclass
class SubmitOutcome:
    """`stored` events were appended. `unchanged` edits needed nothing. Problems are per item. `inbox_full`: nothing was stored."""
    stored: int = 0
    unchanged: int = 0
    problems: list[Problem] = field(default_factory=list)
    inbox_full: bool = False
    no_snapshot: bool = False
    event_ids: list[str] = field(default_factory=list)

    def _of(self, kind: str) -> list[Problem]:
        return [p for p in self.problems if p.kind == kind]

    @property
    def stale(self) -> list[Problem]:
        return self._of("stale")

    @property
    def invalid(self) -> list[Problem]:
        return self._of("invalid")

    @property
    def refused(self) -> list[Problem]:
        return self._of("refused")

    @property
    def ok(self) -> bool:
        return not self.problems and not self.inbox_full and not self.no_snapshot

    def messages(self) -> list[str]:
        """French lines for the page, one per problem plus the global ones."""
        out = [MSG_NO_SNAPSHOT] if self.no_snapshot else []
        out += [MSG_FULL] if self.inbox_full else []
        return out + [f"{p.label} : {p.message}" if p.label else p.message for p in self.problems]


@dataclass(frozen=True)
class AnswerEdit:
    path: str
    new_value: str
    token: str
    clear: bool = False  # write-only field: an empty `new_value` means "no change" unless this is set


@dataclass(frozen=True)
class FieldView:
    spec: FieldSpec
    value: str | None   # None: write-only (unknown) -> show "renseigné" when `is_set`
    is_set: bool
    token: str
    pending: bool       # his latest edit is still pending, on the server or the Mac
    legacy: str = ""


@dataclass(frozen=True)
class CustomView:
    custom_id: str
    question: str
    answer: str         # "" when masked
    masked: bool
    token: str
    pending: bool
    deleted: bool       # a pending deletion


@dataclass(frozen=True)
class PendingAction:
    event: Any          # a protocol event model
    label: str


@dataclass
class PortalView:
    exists: bool
    has_snapshot: bool
    snapshot: Snapshot | None = None
    synced_at: datetime | None = None        # server clock: when the Mac last pushed
    synced_age_seconds: float | None = None
    results: list[EventResult] = field(default_factory=list)   # newest first
    pending: list[PendingAction] = field(default_factory=list)  # pending events, oldest first
    fields: dict[str, FieldView] = field(default_factory=dict)
    custom: list[CustomView] = field(default_factory=list)
    answered_questions: set[str] = field(default_factory=set)   # qids with a pending answer
    intent_events: dict[str, str] = field(default_factory=dict)  # grant -> event_id of a pending intent
    cancellable_intents: set[str] = field(default_factory=set)
    cancelling_intents: set[str] = field(default_factory=set)
    inbox_full: bool = False
    held: bool = False
    enabled: bool = True


# ---------------------------------------------------------------- loading
def _dt(epoch: float) -> datetime:
    return datetime.fromtimestamp(epoch, UTC)


def _sealed(row) -> Sealed:
    return Sealed(row["key_id"], bytes(row["nonce"]), bytes(row["ciphertext"]))


def _load_snapshot(conn, cipher: Cipher, profile_id: int) -> Snapshot | None:
    row = conn.execute("SELECT key_id, nonce, ciphertext FROM snapshots WHERE profile_id=? AND purpose='snapshot'", (profile_id,)).fetchone()
    if row is None:
        return None
    return Snapshot.model_validate_json(cipher.open("snapshot", profile_id, "0", _sealed(row)))


def load_results(conn, cipher: Cipher, profile_id: int) -> list[EventResult]:
    row = conn.execute("SELECT key_id, nonce, ciphertext FROM snapshots WHERE profile_id=? AND purpose='results'", (profile_id,)).fetchone()
    if row is None:
        return []
    return RESULTS_ADAPTER.validate_json(cipher.open("results", profile_id, "0", _sealed(row)))


def store_results(conn, cipher: Cipher, profile_id: int, results: list[EventResult]) -> None:
    sealed = cipher.seal("results", profile_id, "0", RESULTS_ADAPTER.dump_json(results))
    conn.execute("INSERT INTO snapshots(profile_id, purpose, key_id, nonce, ciphertext) VALUES (?, 'results', ?, ?, ?) "
                 "ON CONFLICT(profile_id, purpose) DO UPDATE SET key_id=excluded.key_id, nonce=excluded.nonce, "
                 "ciphertext=excluded.ciphertext", (profile_id, sealed.key_id, sealed.nonce, sealed.ciphertext))


def load_events(conn, cipher: Cipher, profile_id: int, after: int = 0, limit: int | None = None) -> list:
    sql = "SELECT seq, event_id, key_id, nonce, ciphertext FROM events WHERE profile_id=? AND seq>? ORDER BY seq"
    rows = conn.execute(sql + (" LIMIT ?" if limit else ""), (profile_id, after) + ((limit,) if limit else ())).fetchall()
    return [EVENT_ADAPTER.validate_json(cipher.open("event", profile_id, r["event_id"], _sealed(r))) for r in rows]


def event_label(event, snapshot: Snapshot | None) -> str:
    """What an event is about, for lists of his recent actions and for `lost` results (never a typed value)."""
    if snapshot is None:
        return ""
    if event.kind == "answer":
        spec = next((f for f in snapshot.answer_schema.fields if f.path == event.path), None)
        return spec.label if spec else event.path
    if event.kind == "question_answer":
        return next((q.label for q in snapshot.questions if q.qid == event.qid), "Question")
    if event.kind in ("custom_edit", "custom_delete"):
        return next((c.question for c in snapshot.custom if c.custom_id == event.custom_id), "Réponse enregistrée")
    if event.kind == "intent":
        for item in (*snapshot.awaiting, *snapshot.email_pauses):
            if item.grant == event.grant:
                return f"{item.company} — {item.title}".strip(" —")[:500]
        for u in snapshot.unconfirmed:
            if event.grant in (u.grant_sent, u.grant_not_sent):
                return f"{u.company} — {u.title}".strip(" —")[:500]
        return "Candidature"
    return "Annulation"


def _newest_results(results: list[EventResult]) -> list[EventResult]:
    return sorted(results, key=lambda result: result.at, reverse=True)


@dataclass(frozen=True)
class _IntentWork:
    event_id: str
    status: str
    cancelling: bool = False

    @property
    def can_cancel(self) -> bool:
        return not self.cancelling and self.status in CANCELLABLE_STATUSES


def effective_intents(events: list, results: list[EventResult]) -> dict[str, _IntentWork]:
    """Merge inbox intents and keyed results; a pending cancel blocks a repeat until the Mac resolves it."""
    targets = {event.target for event in events if event.kind == "cancel"}
    live = {}
    intent_results = {}
    live_cancels = {}
    for result in _newest_results(results):
        if result.kind == "intent" and result.event_id not in intent_results:
            intent_results[result.event_id] = result.status
            if result.status in protocol.LIVE_STATUSES and result.key.startswith("g:"):
                live.setdefault(result.key[2:], _IntentWork(result.event_id, result.status, result.event_id in targets))
        elif result.kind == "cancel" and result.status in protocol.LIVE_STATUSES and result.key.startswith("g:"):
            live_cancels.setdefault(result.key[2:], result)
    for event in events:
        if event.kind == "intent":
            status = intent_results.get(event.event_id, "received")
            if status in protocol.LIVE_STATUSES:
                live[event.grant] = _IntentWork(event.event_id, status, event.event_id in targets)
    for grant, result in live_cancels.items():
        work = live.get(grant)
        live[grant] = _IntentWork(work.event_id, work.status, True) if work else _IntentWork(result.event_id, result.status, True)
    return live


def _latest_answer_results(results: list[EventResult]) -> dict[str, EventResult]:
    latest = {}
    for result in _newest_results(results):
        if result.kind == "answer" and result.key.startswith("a:"):
            latest.setdefault(result.key[2:], result)
    return latest


@dataclass(frozen=True)
class _State:
    value: str | None
    is_set: bool
    token: str
    pending: bool
    after: str | None
    base_ver: str | None
    legacy: str = ""


def _answer_state(snapshot: Snapshot, events: list, spec: FieldSpec, result: EventResult | None) -> _State | None:
    av = snapshot.answers.get(spec.path)
    if av is None:
        return None
    last = None
    for ev in events:
        if ev.kind == "answer" and ev.path == spec.path:
            last = ev
    if last is not None:
        return _State(None if spec.write_only else last.value, last.value != "", last.event_id, True, last.event_id, av.ver)
    after = result.event_id if result and result.status in CHAINABLE_ANSWER_STATUSES else None
    return _State(None if spec.write_only else (av.value or ""), av.is_set, after or av.ver, after is not None, after, av.ver, av.legacy)


def _custom_state(snapshot: Snapshot, events: list, custom_id: str):
    base = next((c for c in snapshot.custom if c.custom_id == custom_id), None)
    if base is None:
        return None, None
    last = None
    for ev in events:
        if ev.kind in ("custom_edit", "custom_delete") and ev.custom_id == custom_id:
            last = ev
    if last is None:
        return base, _State(base.answer, True, base.ver, False, None, base.ver)
    answer = "" if (base.masked or last.kind == "custom_delete") else last.answer
    return base, _State(answer, last.kind != "custom_delete", last.event_id, True, last.event_id, base.ver,
                        "deleted" if last.kind == "custom_delete" else "")


def load_view(portal: Portal, profile_id: int, now: float) -> PortalView:
    c = portal.db.conn
    with c:
        c.execute("BEGIN")
        row = c.execute("SELECT enabled, held, snapshot_at, revoked_through_seq, last_seq, acked_seq FROM profiles WHERE id=?",
                        (profile_id,)).fetchone()
        if row is None:
            return PortalView(exists=False, has_snapshot=False)
        events = load_events(c, portal.cipher, profile_id, after=row["revoked_through_seq"])
        snapshot = _load_snapshot(c, portal.cipher, profile_id)
        results = load_results(c, portal.cipher, profile_id)
    live = effective_intents(events, results)
    view = PortalView(exists=True, has_snapshot=snapshot is not None, snapshot=snapshot, held=bool(row["held"]),
                      enabled=bool(row["enabled"]), inbox_full=row["last_seq"] - row["acked_seq"] >= protocol.MAX_PENDING_EVENTS,
                      results=_newest_results(results),
                      pending=[PendingAction(e, event_label(e, snapshot)) for e in events],
                      intent_events={grant: work.event_id for grant, work in live.items()},
                      cancellable_intents={work.event_id for work in live.values() if work.can_cancel},
                      cancelling_intents={work.event_id for work in live.values() if work.cancelling},
                      answered_questions={e.qid for e in events if e.kind == "question_answer"})
    if row["snapshot_at"] is not None:
        view.synced_at = _dt(row["snapshot_at"])
        view.synced_age_seconds = max(0.0, now - row["snapshot_at"])
    if snapshot is None:
        return view
    answer_results = _latest_answer_results(results)
    for spec in snapshot.answer_schema.fields:
        st = _answer_state(snapshot, events, spec, answer_results.get(spec.path))
        if st is not None:
            view.fields[spec.path] = FieldView(spec, st.value, st.is_set, st.token, st.pending, st.legacy if not st.pending else "")
    for item in snapshot.custom:
        _, st = _custom_state(snapshot, events, item.custom_id)
        view.custom.append(CustomView(item.custom_id, item.question, st.value or "", item.masked, st.token, st.pending, st.legacy == "deleted"))
    return view


def open_document(portal: Portal, profile_id: int | Session, doc_id: str) -> bytes | None:
    """The PDF, only when the current manifest lists it and the stored bytes are the listed ones."""
    with portal.db.transaction() as conn:
        if isinstance(profile_id, Session):
            if not session_is_live(conn, profile_id, portal.clock()):
                return None
            profile_id = profile_id.profile_id
        row = conn.execute("SELECT d.key_id, d.nonce, d.ciphertext FROM documents d JOIN manifest m ON m.profile_id=d.profile_id "
                           "AND m.doc_id=d.doc_id AND m.sha256=d.sha256 AND m.size=d.size WHERE d.profile_id=? AND d.doc_id=?",
                           (profile_id, doc_id)).fetchone()
    return portal.cipher.open("document", profile_id, doc_id, _sealed(row)) if row else None


# ---------------------------------------------------------------- appending
class _Ctx:
    def __init__(self, conn, cipher: Cipher, profile_id: int, now: float) -> None:
        self.conn, self.cipher, self.profile_id, self.now = conn, cipher, profile_id, now
        self.row = conn.execute("SELECT last_seq, acked_seq, revoked_through_seq FROM profiles WHERE id=?", (profile_id,)).fetchone()
        self.snapshot = _load_snapshot(conn, cipher, profile_id) if self.row else None
        self.events = load_events(conn, cipher, profile_id, after=self.row["revoked_through_seq"]) if self.row else []
        self.results = load_results(conn, cipher, profile_id) if self.row else []
        self.planned: list = []
        self.next_seq = (self.row["last_seq"] if self.row else 0) + 1

    def add(self, model, **fields):
        event = model(seq=self.next_seq + len(self.planned), event_id=protocol.new_id(), at=_dt(self.now), **fields)
        self.planned.append(event)
        self.events.append(event)
        return event


def _mutate(portal: Portal, profile_id: int | Session, now: float, plan, needs_snapshot: bool = True) -> SubmitOutcome:
    out = SubmitOutcome()
    with portal.db.transaction() as c:
        if isinstance(profile_id, Session):
            if not session_is_live(c, profile_id, portal.clock()):
                out.no_snapshot = True
                return out
            profile_id = profile_id.profile_id
        ctx = _Ctx(c, portal.cipher, profile_id, now)
        if ctx.row is None or (needs_snapshot and ctx.snapshot is None):
            out.no_snapshot = True
            return out
        pending_before = ctx.row["last_seq"] - ctx.row["acked_seq"]
        plan(ctx, out)
        if not ctx.planned:
            return out
        limit = protocol.MAX_PENDING_EVENTS
        if all(event.kind == "cancel" for event in ctx.planned):
            limit += CANCEL_RESERVE
        if pending_before + len(ctx.planned) > limit:
            out.inbox_full = True
            return out
        for ev in ctx.planned:
            sealed = portal.cipher.seal("event", profile_id, ev.event_id, ev.model_dump_json().encode())
            c.execute("INSERT INTO events(profile_id, seq, event_id, at, kind, key_id, nonce, ciphertext) VALUES (?,?,?,?,?,?,?,?)",
                      (profile_id, ev.seq, ev.event_id, int(now), ev.kind, sealed.key_id, sealed.nonce, sealed.ciphertext))
        c.execute("UPDATE profiles SET last_seq=? WHERE id=?", (ctx.planned[-1].seq, profile_id))
        out.stored = len(ctx.planned)
        out.event_ids = [e.event_id for e in ctx.planned]
    return out


# ---------------------------------------------------------------- validation
class _Invalid(Exception):
    pass


def _control_free(text: str, allow: str = "") -> bool:
    return all(unicodedata.category(ch) != "Cc" or ch in allow for ch in text)


def _clean(spec: FieldSpec, raw: str) -> str:
    if spec.widget == "textarea":
        value = raw.replace("\r\n", "\n").replace("\r", "\n").strip()
        ok = _control_free(value, "\n\t")
    else:
        value = " ".join(raw.split())
        ok = _control_free(value)
    if not ok:
        raise _Invalid("Caractères non autorisés.")
    if len(value) > spec.max_len:
        raise _Invalid(f"Trop long ({spec.max_len} caractères au maximum).")
    return value


def _check(spec: FieldSpec, value: str) -> None:
    if value == "":
        return
    if spec.widget == "select" and value not in spec.options:
        raise _Invalid("Choix non valide.")
    if spec.widget == "date":
        try:
            if not _DATE_RE.match(value):
                raise ValueError
            date.fromisoformat(value)
        except ValueError:
            raise _Invalid("Date non valide (AAAA-MM-JJ).") from None


def _line(raw: str, limit: int = 500) -> str:
    value = " ".join((raw or "").split())
    if not value:
        raise _Invalid("Réponse vide.")
    if len(value) > limit or not _control_free(value):
        raise _Invalid("Réponse non valide ou trop longue.")
    return value


# ---------------------------------------------------------------- public mutations
def submit_answers(portal: Portal, profile_id: int | Session, edits: list[AnswerEdit], now: float) -> SubmitOutcome:
    """Standing answers. Only edits whose value differs from what is displayed are looked at; each must carry the field's
    current token. Messages: `SubmitOutcome.messages()`."""
    def plan(ctx: _Ctx, out: SubmitOutcome) -> None:
        specs = {f.path: f for f in ctx.snapshot.answer_schema.fields}
        answer_results = _latest_answer_results(ctx.results)
        for e in edits:
            spec = specs.get(e.path)
            st = _answer_state(ctx.snapshot, ctx.events, spec, answer_results.get(e.path)) if spec else None
            if spec is None or st is None:
                out.problems.append(Problem("invalid", e.path[:40], "", "Champ inconnu."))
                continue
            label = spec.label
            if spec.write_only:
                if e.new_value == "" and not e.clear:
                    out.unchanged += 1
                    continue
            elif e.new_value == st.value:
                out.unchanged += 1
                continue
            try:
                value = _clean(spec, e.new_value)
                if not spec.write_only and value == st.value:
                    out.unchanged += 1
                    continue
                if spec.write_only and value == "" and not st.is_set:
                    out.unchanged += 1
                    continue
                _check(spec, value)
            except _Invalid as exc:
                out.problems.append(Problem("invalid", spec.path, label, str(exc)))
                continue
            if e.token != st.token:
                out.problems.append(Problem("stale", spec.path, label, MSG_STALE))
                continue
            ctx.add(AnswerEvent, path=spec.path, value=value, base_ver=st.base_ver, after=st.after)
    return _mutate(portal, profile_id, now, plan)


def answer_questions(portal: Portal, profile_id: int | Session, answers: list[tuple[str, str]], now: float) -> SubmitOutcome:
    """Answers to the questions the engine is waiting on (`snapshot.questions`)."""
    def plan(ctx: _Ctx, out: SubmitOutcome) -> None:
        questions = {q.qid: q for q in ctx.snapshot.questions}
        for qid, answer in answers:
            q = questions.get(qid)
            if q is None or any(e.kind == "question_answer" and e.qid == qid for e in ctx.events):
                out.problems.append(Problem("refused", qid[:16], "", "Cette question a changé : rechargez la page."))
                continue
            try:
                value = _line(answer)
                if q.options and value not in q.options:
                    raise _Invalid("Choisissez l'une des propositions.")
            except _Invalid as exc:
                out.problems.append(Problem("invalid", qid, q.label, str(exc)))
                continue
            ctx.add(QuestionAnswerEvent, qid=qid, answer=value)
    return _mutate(portal, profile_id, now, plan)


def edit_custom(portal: Portal, profile_id: int | Session, custom_id: str, answer: str, token: str, now: float) -> SubmitOutcome:
    """Replace a saved custom answer (masked ones too: their value is never shown, only replaced)."""
    def plan(ctx: _Ctx, out: SubmitOutcome) -> None:
        base, st = _custom_state(ctx.snapshot, ctx.events, custom_id)
        if base is None or st.legacy == "deleted":
            out.problems.append(Problem("refused", custom_id, "", MSG_STALE))
            return
        try:
            value = _line(answer)
        except _Invalid as exc:
            out.problems.append(Problem("invalid", custom_id, base.question, str(exc)))
            return
        if not base.masked and value == st.value:
            out.unchanged += 1
        elif token != st.token:
            out.problems.append(Problem("stale", custom_id, base.question, MSG_STALE))
        else:
            ctx.add(CustomEditEvent, custom_id=custom_id, answer=value, base_ver=st.base_ver, after=st.after)
    return _mutate(portal, profile_id, now, plan)


def delete_custom(portal: Portal, profile_id: int | Session, custom_id: str, token: str, now: float) -> SubmitOutcome:
    def plan(ctx: _Ctx, out: SubmitOutcome) -> None:
        base, st = _custom_state(ctx.snapshot, ctx.events, custom_id)
        if base is None or st.legacy == "deleted":
            out.problems.append(Problem("refused", custom_id, "", MSG_STALE))
        elif token != st.token:
            out.problems.append(Problem("stale", custom_id, base.question, MSG_STALE))
        else:
            ctx.add(CustomDeleteEvent, custom_id=custom_id, base_ver=st.base_ver, after=st.after)
    return _mutate(portal, profile_id, now, plan)


def request_intents(portal: Portal, profile_id: int | Session, grant_ids: list[str], now: float) -> SubmitOutcome:
    """He pressed a button for each grant id (as shown on the confirmation page). Refused when the grant is not in the
    CURRENT snapshot, already has a live pending intent, or (unconfirmed item) its sibling grant has one."""
    def plan(ctx: _Ctx, out: SubmitOutcome) -> None:
        snap = ctx.snapshot
        known = {a.grant for a in snap.awaiting} | {p.grant for p in snap.email_pauses}
        siblings: dict[str, str] = {}
        for u in snap.unconfirmed:
            known |= {u.grant_sent, u.grant_not_sent}
            siblings[u.grant_sent], siblings[u.grant_not_sent] = u.grant_not_sent, u.grant_sent
        live = effective_intents(ctx.events, ctx.results)
        for grant in grant_ids:
            if grant not in known or grant in live or siblings.get(grant) in live:
                out.problems.append(Problem("refused", grant, "", MSG_CHANGED))
            else:
                event = ctx.add(IntentEvent, grant=grant)
                live[grant] = _IntentWork(event.event_id, "received")
    return _mutate(portal, profile_id, now, plan)


def cancel_intent(portal: Portal, profile_id: int | Session, event_id: str, now: float) -> SubmitOutcome:
    """Ask the Mac to cancel an intent that has not started, whether still in the inbox or already in its custody."""
    def plan(ctx: _Ctx, out: SubmitOutcome) -> None:
        if any(work.event_id == event_id and work.can_cancel for work in effective_intents(ctx.events, ctx.results).values()):
            ctx.add(CancelEvent, target=event_id)
        else:
            out.problems.append(Problem("refused", event_id[:32], "", "Trop tard ou déjà annulée : rechargez la page."))
    return _mutate(portal, profile_id, now, plan, needs_snapshot=False)
