"""Build the snapshot of one profile (DESIGN §4.1): everything that leaves the Mac, driven by the engine's own lists."""

from __future__ import annotations

import hashlib
import hmac
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qsl, quote, urlencode, urlsplit

from pydantic import ValidationError

from jobhunt import db
from jobhunt.apply import facts, service
from jobhunt.apply import store as apply_store
from jobhunt.cv import service as cvs
from jobhunt.cv import store as cvstore
from jobhunt.dedupe import TRACKING_PARAMS
from jobhunt.portal import protocol
from jobhunt.portal.journal import Journal, ProfileRow
from jobhunt.portal.settings import get_portal_settings, is_masked_question

_PATH_RE = re.compile(r"^[a-z0-9_]{1,40}(\.[a-z0-9_]{1,40}){0,3}$")
_HEX16 = re.compile(r"^[0-9a-f]{16}$")
EMAIL_PAUSE_NOTE = "En pause : un e-mail de confirmation est à valider."


def _t(text, n: int) -> str:
    return str(text if text is not None else "")[:n]


# ---------------------------------------------------------------- values and versions (shared with apply_events)
def answer_key(path: str) -> str:
    return f"a:{path}"


def custom_key(custom_id: str) -> str:
    return f"c:{custom_id}"


def stored_answer(profile_id: int, path: str) -> str:
    return facts.get_answer(facts.load_answers(profile_id), path) or ""


def displayed(f: facts.AnswerField, stored: str | None) -> str:
    """What the local page shows: a date widget shows ISO (nothing for a legacy free text), the others the stored text."""
    if f.widget == "date":
        return facts.from_french_date(stored) or ""
    return stored or ""


def value_hash(journal: Journal, text: str | None) -> str:
    """HMAC-SHA256 (local secret) of a stored text; None (absent) differs from the empty text."""
    return hmac.new(journal.local_secret(), b"v:" + (b"\x00" if text is None else text.encode("utf-8")), hashlib.sha256).hexdigest()


def field_version(journal: Journal, profile_id: int, key: str, text: str | None) -> str:
    return journal.version_for(profile_id, key, value_hash(journal, text))


def custom_entry(profile_id: int, custom_id: str) -> dict | None:
    return next((c for c in facts.custom_answers(profile_id) if c["id"] == custom_id), None)


# ---------------------------------------------------------------- URLs
def sanitize_url(url: str | None) -> str | None:
    """http(s) only; no userinfo, no fragment, tracking parameters removed (dedupe.canonical_url rules), scheme kept."""
    if not url or not isinstance(url, str):
        return None
    try:
        u = urlsplit(url.strip())
        host, port = u.hostname, u.port
    except ValueError:
        return None
    if u.scheme.lower() not in ("http", "https") or not host:
        return None
    query = [(k, v) for k, v in parse_qsl(u.query, keep_blank_values=True)
             if k.lower() not in TRACKING_PARAMS and not k.lower().startswith("utm_")]
    host_part = f"[{host}]" if ":" in host else host.lower()
    out = f"{u.scheme.lower()}://{host_part}" + (f":{port}" if port else "") + quote(u.path, safe="/%:@!$&'()*+,;=~-._")
    if query:
        out += "?" + urlencode(query)
    if len(out) > 2000 or re.search(r"\s", out):
        return None
    return out


def _sent_url(item: service.QueueItem) -> str | None:
    recorded = (item.app.sent_origin_url or "").strip()
    if recorded:
        if "://" in recorded:
            return sanitize_url(recorded)
        if item.origin_url and service.canonical_url(item.origin_url) == recorded:
            return sanitize_url(item.origin_url)
    return None


def _sent_host(item: service.QueueItem) -> str:
    recorded = (item.app.sent_origin_url or "").strip()
    if recorded:
        host = recorded.split("://", 1)[-1].split("/", 1)[0].split("?", 1)[0].split("@")[-1].split(":")[0]
        if host:
            return host.lower()
    return item.origin_host


def interrupted_after_click(run: apply_store.ApplyRun | None) -> bool:
    return bool(run and run.status == "running" and run.mode == "submit"
                and (run.submit_clicked_at or run.nav_clicked_at or (run.result and run.result.submit_clicked))
                and not apply_store.pid_alive(run.pid))


# ---------------------------------------------------------------- the schema and the answers
def _schema(write_only: frozenset[str]) -> tuple[protocol.AnswerSchema, list[facts.AnswerField]]:
    fields, specs = [], []
    for f in facts.ANSWER_FIELDS:
        if not _PATH_RE.match(f.path) or len(specs) >= 200:
            continue
        fields.append(f)
        specs.append(protocol.FieldSpec(
            path=f.path, label=_t(f.label, 500), help=_t(f.help, 500), widget=f.widget, options=[_t(o, 500) for o in f.options[:60]],
            section=_t(f.section, 100), max_len=min(max(int(f.max_len), 1), 4000), placeholder=_t(f.placeholder, 500),
            write_only=f.path in write_only))
    sections = [_t(s, 100) for s in facts.ANSWER_SECTIONS][:30]
    return protocol.AnswerSchema(sections=sections, fields=specs), fields


def _answers(journal: Journal, profile_id: int, fields: list[facts.AnswerField], write_only: frozenset[str]) -> dict[str, protocol.AnswerValue]:
    data = facts.load_answers(profile_id)
    out = {}
    for f in fields:
        stored = facts.get_answer(data, f.path)
        ver = field_version(journal, profile_id, answer_key(f.path), stored or "")
        if f.path in write_only:
            out[f.path] = protocol.AnswerValue(value=None, is_set=bool(stored), ver=ver)
            continue
        shown = displayed(f, stored)
        out[f.path] = protocol.AnswerValue(value=_t(shown, 4000), is_set=bool(stored), ver=ver,
                                           legacy=_t(stored, 500) if (f.widget == "date" and stored and not shown) else "")
    return out


def _custom(journal: Journal, profile_id: int) -> list[protocol.CustomAnswer]:
    out, seen = [], set()
    for c in facts.custom_answers(profile_id):
        if not c["question"].strip() or c["id"] in seen or len(out) >= 500:
            continue
        seen.add(c["id"])
        masked = is_masked_question(c["question"])
        out.append(protocol.CustomAnswer(custom_id=c["id"], question=_t(c["question"], 500), answer="" if masked else _t(c["answer"], 500),
                                         masked=masked, ver=field_version(journal, profile_id, custom_key(c["id"]), c["answer"])))
    return out


def question_id(label: str) -> str:
    return facts.custom_id(label)


# ---------------------------------------------------------------- documents
def _pdf_info(app) -> tuple[Path, str, int, bool] | None:
    p = cvs.safe_pdf(app)
    if p is None:
        return None
    try:
        data = p.read_bytes()
    except OSError:
        return None
    ok = data.startswith(b"%PDF-") and 5 <= len(data) <= protocol.MAX_DOCUMENT_BYTES
    return p, hashlib.sha256(data).hexdigest(), len(data), ok


def _doc_id(journal: Journal, profile_key: str, sha: str) -> str:
    return hmac.new(journal.local_secret(), f"{profile_key}:{sha}".encode(), hashlib.sha256).hexdigest()[:32]


def build_snapshot(journal: Journal, profile_id: int, now: datetime) -> tuple[protocol.Snapshot, dict[str, Path]]:
    profile = journal.get_profile(profile_id)
    if profile is None:
        raise ValueError("profil du portail inconnu")
    ps = get_portal_settings()
    schema, fields = _schema(ps.write_only)
    answers = facts.load_answers(profile_id)
    private_values = {facts.norm_question(facts.get_answer(answers, path) or "") for path in ps.write_only}
    private_values.update(facts.norm_question(entry["answer"]) for entry in facts.custom_answers(profile_id)
                          if is_masked_question(entry["question"]))
    private_values.discard("")
    local = db.get_profile(profile_id)
    items = service.queue(profile_id)
    grant = lambda action, **b: journal.find_or_create_grant(profile, action, now, **b)  # noqa: E731

    # --- awaiting / email pauses / unconfirmed / info
    awaiting_src: list[tuple[service.QueueItem, str, str]] = []  # (item, grant, sha)
    email_pauses, unconfirmed, info = [], [], []
    pdfs: dict[int, tuple[Path, str, int, bool] | None] = {}
    for it in items:
        a = it.app
        if it.bucket != "sent" and interrupted_after_click(it.last_run):
            info.append(protocol.InfoItem(kind="interrupted", company=_t(a.company, 300), title=_t(a.job_title, 300),
                                          host=_t(it.origin_host, 255)))
        elif it.bucket == "to_validate" and it.selectable:
            if apply_store.requires_manual_retry(a.id) or apply_store.has_site_handoff(a.id):
                continue
            pdfs[a.id] = pdf = _pdf_info(a)
            if pdf is None:
                continue
            awaiting_src.append((it, grant("send", app_id=a.id, rev=a.current_rev, pdf_sha256=pdf[1], origin_sha=it.origin_sha), pdf[1]))
        elif it.bucket == "paused":
            last = it.last_run
            if it.pause_reason == "email" and it.selectable and last is not None and last.mode == "submit":
                pdfs[a.id] = pdf = _pdf_info(a)
                if pdf is None:
                    continue
                gid = grant("resume", app_id=a.id, rev=a.current_rev, pdf_sha256=pdf[1], origin_sha=it.origin_sha, run_id=last.id, mode=last.mode)
                email_pauses.append(protocol.EmailPause(grant=gid, company=_t(a.company, 300), title=_t(a.job_title, 300),
                                                        host=_t(it.origin_host, 255), note=EMAIL_PAUSE_NOTE))
            elif it.pause_reason == "human":
                info.append(protocol.InfoItem(kind="captcha", company=_t(a.company, 300), title=_t(a.job_title, 300), host=_t(it.origin_host, 255)))
            elif it.pause_reason == "host":
                info.append(protocol.InfoItem(kind="host", company=_t(a.company, 300), title=_t(a.job_title, 300),
                                              host=_t(it.untrusted_host or it.origin_host, 255)))
            elif last is not None and last.mode == "dry_run":
                info.append(protocol.InfoItem(kind="dry_run", company=_t(a.company, 300), title=_t(a.job_title, 300), host=_t(it.origin_host, 255)))
        elif it.bucket == "sending":
            info.append(protocol.InfoItem(kind="sending", company=_t(a.company, 300), title=_t(a.job_title, 300), host=_t(it.origin_host, 255)))
        elif it.bucket == "needs_verification" and it.outstanding is not None:
            o = it.outstanding
            binding = dict(app_id=a.id, rev=a.current_rev, run_id=o.id, mode=o.mode, origin_sha=it.origin_sha,
                           pdf_sha256=cvstore.sha256_file(cvs.safe_pdf(a)) or "")
            gs = grant("resolve_sent", **binding)
            gn = grant("resolve_not_sent", **binding)
            unconfirmed.append(protocol.Unconfirmed(
                grant_sent=gs, grant_not_sent=gn, company=_t(a.company, 300), title=_t(a.job_title, 300), host=_t(it.origin_host, 255),
                when=o.submit_clicked_at or o.nav_clicked_at or o.finished_at or o.started_at))

    # --- documents: the 100 best-scored awaiting, the 50 most recent sent
    sent_items = sorted((i for i in items if i.bucket == "sent"),
                        key=lambda i: (i.app.submitted_at.timestamp() if i.app.submitted_at else 0.0, i.app.id),
                        reverse=True)[:protocol.SENT_MAX]
    awaiting_src.sort(key=lambda t: (-(t[0].score if t[0].score is not None else -1.0), t[0].app.id))
    awaiting_src = awaiting_src[:500]
    documents: dict[str, Path] = {}
    refs: list[protocol.DocumentRef] = []
    total = 0

    def doc_for(app_id: int, app) -> str | None:
        nonlocal total
        pdf = pdfs.get(app_id) or _pdf_info(app)
        if pdf is None or not pdf[3]:
            return None
        path, sha, size, _ = pdf
        did = _doc_id(journal, profile.profile_key, sha)
        if did in documents:
            return did
        if len(documents) >= protocol.MAX_DOCUMENTS or total + size > protocol.MAX_DOCUMENTS_BYTES:
            return None
        documents[did] = path
        refs.append(protocol.DocumentRef(doc_id=did, sha256=sha, size=size))
        total += size
        return did

    awaiting = []
    for n, (it, gid, _sha) in enumerate(awaiting_src):
        a = it.app
        did = doc_for(a.id, a) if n < protocol.AWAITING_DOCUMENTS else None
        awaiting.append(protocol.Awaiting(grant=gid, company=_t(a.company, 300), title=_t(a.job_title, 300), score=it.score,
                                          age_days=it.age_days, host=_t(it.origin_host, 255), job_url=sanitize_url(it.origin_url), doc_id=did))
    sent = []
    for n, it in enumerate(sent_items):
        a = it.app
        run = apply_store.sent_run(a.id)
        did = doc_for(a.id, a) if n < protocol.SENT_DOCUMENTS and run is not None and run.rev == a.current_rev else None
        inferred = None
        if run is not None:
            inferred = []
            seen: set[str] = set()
            for field in (field for step in (run.result.steps if run.result else []) for field in step.filled):
                if not field.inferred or field.label in seen or len(inferred) >= 60:
                    continue
                if field.fact in ps.write_only or any(is_masked_question(text) or facts.norm_question(text or "") in private_values
                                                      for text in (field.label, field.value, field.basis)):
                    continue
                seen.add(field.label)
                inferred.append(protocol.Inferred(label=_t(field.label, 500), value=_t(field.value, 1000), basis=_t(field.basis, 500)))
        sent.append(protocol.SentItem(
            ref=journal.ref_for(profile_id, "sent", a.id), company=_t(a.company, 300), title=_t(a.job_title, 300), submitted_at=a.submitted_at,
            host=_t(_sent_host(it), 255), url=_sent_url(it), doc_id=did, inferred=inferred,
            prep=[protocol.Prep(message=_t(p.message, 1000), hint=_t(p.hint, 1000)) for p in a.preps[:40]]))

    # --- questions
    questions, seen = [], set()
    for app, _run, q in service.pending_questions(profile_id):
        qid = question_id(q.label)
        if qid in seen or not q.label.strip() or len(questions) >= 200:
            continue
        seen.add(qid)
        questions.append(protocol.PendingQuestion(qid=qid, label=_t(q.label, 500), options=[_t(o, 500) for o in q.options[:100]],
                                                  company=_t(app.company, 300), title=_t(app.job_title, 300)))

    counts = protocol.Counts(sending=sum(1 for i in items if i.bucket == "sending"),
                             failed=sum(1 for i in items if i.bucket in ("failed", "needs_verification")),
                             blocked=sum(1 for i in items if i.bucket == "blocked"), sent_total=sum(1 for i in items if i.bucket == "sent"))
    name = ((local.name if local else "") or "").split()
    snap = protocol.Snapshot(
        display_name=_t(name[0] if name else "", 80), synced_at=now, auto_send_on=bool(service.get_settings().apply_auto),
        review_mode=profile.review_mode, paused=journal.paused, answer_schema=schema,
        answers=_answers(journal, profile_id, fields, ps.write_only), custom=_custom(journal, profile_id), questions=questions,
        email_pauses=email_pauses[:200], unconfirmed=unconfirmed[:200], info=info[:300], awaiting=awaiting, sent=sent, documents=refs,
        counts=counts)
    try:
        protocol.Snapshot.model_validate(snap.model_dump())
    except ValidationError as e:  # a bug here must never push something the server refuses
        raise ValueError("instantané invalide") from e
    return snap, documents
