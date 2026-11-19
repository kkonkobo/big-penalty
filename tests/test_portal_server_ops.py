"""Portal operations used by the HTML pages: effective view, tokens, validation, inbox appends, intents, documents."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from portal_server_fakes import (CUSTOM_ID, CUSTOM_MASKED, DOC_A, DOC_B, GRANT_EMAIL, GRANT_NOT_SENT, GRANT_SEND_1, GRANT_SEND_2,
                                 GRANT_SENT, PDF_A, QID, T0, Harness, doc_ref, fast_scrypt, make_pdf, make_snapshot, ver)

from jobhunt.portal import protocol
from jobhunt.portal.server import auth, ops
from jobhunt.portal.server.ops import AnswerEdit


@pytest.fixture
def h(tmp_path, monkeypatch):
    fast_scrypt(monkeypatch)
    harness = Harness.create(tmp_path)
    yield harness
    harness.portal.db.close()


@pytest.fixture
def prof(h):
    return h.enable()


def view(h, prof):
    return ops.load_view(h.portal, prof.row_id, h.clock())


def events(h, prof):
    return ops.load_events(h.portal.db.conn, h.portal.cipher, prof.row_id)


def test_revoked_session_cannot_write_or_open_a_document(h, prof):
    login = h.login(prof)
    session = auth.resolve_session(h.portal, [login.session_id], h.clock())
    assert session and ops.open_document(h.portal, session, DOC_A) == PDF_A
    auth.delete_session(h.portal, session.id)
    assert ops.open_document(h.portal, session, DOC_A) is None
    assert ops.answer_questions(h.portal, session, [(QID, "Oui")], h.clock()).stored == 0
    assert events(h, prof) == []


def edit(h, prof, path, value, token=None, clear=False):
    v = view(h, prof)
    return ops.submit_answers(h.portal, prof.row_id, [AnswerEdit(path, value, token or v.fields[path].token, clear)], h.clock())


# ---------------------------------------------------------------- view
def test_view_without_snapshot(h):
    p = h.enable(with_snapshot=False)
    v = view(h, p)
    assert v.exists and not v.has_snapshot and v.snapshot is None and v.fields == {} and v.synced_at is None and v.synced_age_seconds is None
    out = ops.submit_answers(h.portal, p.row_id, [AnswerEdit("identity.city", "X", ver(1))], h.clock())
    assert out.no_snapshot and out.stored == 0 and not out.ok and out.messages() == [ops.MSG_NO_SNAPSHOT]
    assert not ops.load_view(h.portal, 99999, h.clock()).exists


def test_view_reflects_snapshot_and_age(h, prof):
    v = view(h, prof)
    assert v.has_snapshot and v.snapshot.display_name == "Léa" and v.fields["identity.city"].value == "Toulouse"
    assert v.fields["identity.city"].token == ver(2) and not v.fields["identity.city"].pending
    assert v.synced_age_seconds == 0
    h.clock.advance(125)
    assert view(h, prof).synced_age_seconds == 125 and view(h, prof).synced_at is not None
    assert not v.inbox_full and v.pending == [] and v.results == []


def test_effective_value_and_token_follow_pending_edits(h, prof):
    assert edit(h, prof, "identity.city", "Lyon").stored == 1
    f = view(h, prof).fields["identity.city"]
    ev = events(h, prof)[0]
    assert f.value == "Lyon" and f.pending and f.token == ev.event_id
    assert ev.kind == "answer" and ev.base_ver == ver(2) and ev.after is None and ev.value == "Lyon" and ev.seq == 1
    assert edit(h, prof, "identity.city", "Paris").stored == 1
    second = events(h, prof)[1]
    assert second.base_ver == ver(2) and second.after == ev.event_id and second.seq == 2
    assert view(h, prof).pending[0].label == "Ville"


@pytest.mark.parametrize("status", ["received", "awaiting_review"])
def test_answer_chains_to_latest_live_result_after_ack(h, prof, status):
    path = "identity.city"
    assert edit(h, prof, path, "Lyon").stored == 1
    old, = events(h, prof)
    result = protocol.EventResult(event_id=old.event_id, kind="answer", key=f"a:{path}", status=status, at=old.at)
    assert h.push_snapshot(h.pair(), prof, acked_seq=1, results=[result]).status_code == 200
    assert events(h, prof) == []
    field = view(h, prof).fields[path]
    assert field.pending and field.token == old.event_id and field.value == "Toulouse"
    assert edit(h, prof, path, "Nice", token=ver(2)).stale
    assert edit(h, prof, path, "Nice").stored == 1
    assert edit(h, prof, path, "Paris").stored == 1
    first, second = events(h, prof)
    assert first.after == old.event_id and first.base_ver == ver(2)
    assert second.after == first.event_id


def test_newest_answer_result_is_selected_before_filtering_by_status(h, prof):
    path = "identity.city"
    at = datetime.fromtimestamp(T0, UTC)
    older = protocol.EventResult(event_id=protocol.new_id(), kind="answer", key=f"a:{path}", status="received", at=at)
    latest = protocol.EventResult(event_id=protocol.new_id(), kind="answer", key=f"a:{path}", status="applied", at=at + timedelta(seconds=1))
    assert h.push_snapshot(h.pair(), prof, results=[older, latest]).status_code == 200
    field = view(h, prof).fields[path]
    assert not field.pending and field.token == ver(2)
    assert edit(h, prof, path, "Nice").stored == 1
    assert events(h, prof)[0].after is None


def test_unchanged_edits_store_nothing(h, prof):
    out = edit(h, prof, "identity.city", "Toulouse")
    assert out.stored == 0 and out.unchanged == 1 and out.ok and events(h, prof) == []
    assert edit(h, prof, "identity.city", "  Toulouse ", token="not-even-checked").unchanged == 1  # same after cleaning


def test_stale_token_is_reported_and_not_stored(h, prof):
    old = view(h, prof).fields["identity.city"].token
    assert edit(h, prof, "identity.city", "Lyon").stored == 1
    out = edit(h, prof, "identity.city", "Nice", token=old)
    assert out.stored == 0 and [p.kind for p in out.problems] == ["stale"] and out.stale[0].label == "Ville"
    assert out.messages() == ["Ville : " + ops.MSG_STALE] and len(events(h, prof)) == 1
    assert view(h, prof).fields["identity.city"].value == "Lyon"


def test_a_new_snapshot_makes_old_tokens_stale(h, prof):
    old = view(h, prof).fields["identity.city"].token
    snap = make_snapshot()
    snap.answers["identity.city"] = snap.answers["identity.city"].model_copy(update={"value": "Bordeaux", "ver": ver(50)})
    h.push_snapshot(h.pair(), prof, snap)
    assert edit(h, prof, "identity.city", "Nice", token=old).stale
    assert edit(h, prof, "identity.city", "Nice").stored == 1


@pytest.mark.parametrize("path,value,ok", [
    ("identity.city", "  a   b\t c ", "a b c"),
    ("identity.city", "x" * 61, None), ("identity.city", "bad\x00char", None), ("identity.city", "line1\nline2", "line1 line2"),
    ("situation.contract", "Stage", "Stage"), ("situation.contract", "", ""), ("situation.contract", "CDD", None),
    ("situation.start", "2027-02-28", "2027-02-28"), ("situation.start", "", ""), ("situation.start", "2027-02-30", None),
    ("situation.start", "28/02/2027", None), ("situation.start", "2027-2-3", None),
    ("misc.notes", "a\r\nb\rc\n  ", "a\nb\nc"), ("misc.notes", "z" * 201, None), ("misc.notes", "tab\tok", "tab\tok"),
    ("no.such.path", "x", None),
])
def test_validation_per_widget(h, prof, path, value, ok):
    token = view(h, prof).fields[path].token if path in view(h, prof).fields else ver(1)
    out = ops.submit_answers(h.portal, prof.row_id, [AnswerEdit(path, value, token)], h.clock())
    if ok is None:
        assert out.stored == 0 and len(out.invalid) == 1 and value not in " ".join(out.messages()) and not out.ok
    else:
        stored = [e.value for e in events(h, prof)]
        assert stored == [ok] or (out.unchanged == 1 and not stored)


def test_write_only_fields(h, prof):
    f = view(h, prof).fields["misc.health"]
    assert f.value is None and f.is_set and f.spec.write_only
    assert edit(h, prof, "misc.health", "").unchanged == 1  # empty = no change
    assert edit(h, prof, "misc.health", "Oui, RQTH").stored == 1
    f2 = view(h, prof).fields["misc.health"]
    assert f2.value is None and f2.pending and f2.is_set  # never displayed
    assert edit(h, prof, "misc.health", "", clear=True).stored == 1
    f3 = view(h, prof).fields["misc.health"]
    assert f3.value is None and not f3.is_set and [e.value for e in events(h, prof)] == ["Oui, RQTH", ""]
    assert edit(h, prof, "misc.health", "", clear=True).unchanged == 1  # nothing set any more
    assert edit(h, prof, "misc.health", "autre", token=ver(6)).stale


def test_several_edits_in_one_submission_and_per_field_messages(h, prof):
    v = view(h, prof)
    out = ops.submit_answers(h.portal, prof.row_id, [
        AnswerEdit("identity.city", "Lyon", v.fields["identity.city"].token),
        AnswerEdit("situation.contract", "CDD", v.fields["situation.contract"].token),
        AnswerEdit("identity.phone", "0600000000", "stale-token"),
        AnswerEdit("misc.notes", "Bonjour", v.fields["misc.notes"].token)], h.clock())
    assert out.stored == 2 and len(out.invalid) == 1 and len(out.stale) == 1
    assert [e.seq for e in events(h, prof)] == [1, 2]


def test_inbox_full_stores_nothing(h, prof, monkeypatch):
    monkeypatch.setattr(protocol, "MAX_PENDING_EVENTS", 2)
    v = view(h, prof)
    batch = [AnswerEdit("identity.city", "A", v.fields["identity.city"].token), AnswerEdit("identity.phone", "B", v.fields["identity.phone"].token),
             AnswerEdit("misc.notes", "C", v.fields["misc.notes"].token)]
    out = ops.submit_answers(h.portal, prof.row_id, batch, h.clock())
    assert out.inbox_full and out.stored == 0 and events(h, prof) == [] and not view(h, prof).inbox_full
    assert ops.MSG_FULL in out.messages()
    assert ops.submit_answers(h.portal, prof.row_id, batch[:2], h.clock()).stored == 2
    assert view(h, prof).inbox_full
    again = ops.answer_questions(h.portal, prof.row_id, [(QID, "Oui")], h.clock())
    assert again.inbox_full and again.stored == 0
    assert ops.request_intents(h.portal, prof.row_id, [GRANT_SEND_1], h.clock()).inbox_full
    assert len(events(h, prof)) == 2


def test_seq_is_contiguous_across_operations(h, prof):
    edit(h, prof, "identity.city", "A")
    ops.answer_questions(h.portal, prof.row_id, [(QID, "Oui")], h.clock())
    ops.request_intents(h.portal, prof.row_id, [GRANT_SEND_1, GRANT_SEND_2], h.clock())
    assert [e.seq for e in events(h, prof)] == [1, 2, 3, 4]
    assert h.portal.db.conn.execute("SELECT last_seq FROM profiles WHERE id=?", (prof.row_id,)).fetchone()[0] == 4


# ---------------------------------------------------------------- questions
def test_answer_questions(h, prof):
    out = ops.answer_questions(h.portal, prof.row_id, [(QID, "Oui")], h.clock())
    assert out.stored == 1 and out.ok
    e = events(h, prof)[0]
    assert e.kind == "question_answer" and e.qid == QID and e.answer == "Oui"
    assert QID in view(h, prof).answered_questions
    again = ops.answer_questions(h.portal, prof.row_id, [(QID, "Non")], h.clock())
    assert again.stored == 0 and again.refused


def test_answer_questions_refusals(h, prof):
    assert ops.answer_questions(h.portal, prof.row_id, [("00" * 8, "Oui")], h.clock()).refused
    assert ops.answer_questions(h.portal, prof.row_id, [(QID, "Peut-être")], h.clock()).invalid  # not one of the options
    assert ops.answer_questions(h.portal, prof.row_id, [(QID, "   ")], h.clock()).invalid
    assert ops.answer_questions(h.portal, prof.row_id, [(QID, "x" * 501)], h.clock()).invalid
    assert ops.answer_questions(h.portal, prof.row_id, [(QID, "Oui"), (QID, "Non")], h.clock()).stored == 1  # duplicate in a batch
    assert len(events(h, prof)) == 1


def test_free_text_question_accepts_any_short_answer(h):
    snap = make_snapshot(questions=[protocol.PendingQuestion(qid=QID, label="Pourquoi nous ?")])
    p = h.enable(snapshot=snap)
    assert ops.answer_questions(h.portal, p.row_id, [(QID, "  Parce que\n  oui ")], h.clock()).stored == 1
    assert events(h, p)[0].answer == "Parce que oui"


# ---------------------------------------------------------------- custom answers
def test_edit_and_delete_custom(h, prof):
    v = view(h, prof)
    c = next(c for c in v.custom if c.custom_id == CUSTOM_ID)
    assert c.answer == "Selon grille" and c.token == ver(7) and not c.masked
    assert ops.edit_custom(h.portal, prof.row_id, CUSTOM_ID, "Selon grille", c.token, h.clock()).unchanged == 1
    assert ops.edit_custom(h.portal, prof.row_id, CUSTOM_ID, "35 k€", "stale", h.clock()).stale
    assert ops.edit_custom(h.portal, prof.row_id, CUSTOM_ID, "35 k€", c.token, h.clock()).stored == 1
    c2 = next(c for c in view(h, prof).custom if c.custom_id == CUSTOM_ID)
    ev = events(h, prof)[0]
    assert c2.answer == "35 k€" and c2.pending and c2.token == ev.event_id and ev.base_ver == ver(7) and ev.after is None
    assert ops.delete_custom(h.portal, prof.row_id, CUSTOM_ID, c.token, h.clock()).stale
    assert ops.delete_custom(h.portal, prof.row_id, CUSTOM_ID, c2.token, h.clock()).stored == 1
    c3 = next(c for c in view(h, prof).custom if c.custom_id == CUSTOM_ID)
    assert c3.deleted and events(h, prof)[1].after == ev.event_id
    assert ops.edit_custom(h.portal, prof.row_id, CUSTOM_ID, "x", c3.token, h.clock()).refused  # already deleted
    assert ops.delete_custom(h.portal, prof.row_id, CUSTOM_ID, c3.token, h.clock()).refused
    assert ops.edit_custom(h.portal, prof.row_id, "ff" * 8, "x", ver(1), h.clock()).refused
    other = h.enable()
    assert ops.edit_custom(h.portal, other.row_id, CUSTOM_ID, "   ", ver(7), h.clock()).invalid
    assert ops.edit_custom(h.portal, other.row_id, CUSTOM_ID, "y" * 501, ver(7), h.clock()).invalid


def test_masked_custom_answer_is_replaceable_but_never_shown(h, prof):
    m = next(c for c in view(h, prof).custom if c.custom_id == CUSTOM_MASKED)
    assert m.masked and m.answer == ""
    assert ops.edit_custom(h.portal, prof.row_id, CUSTOM_MASKED, "Réponse privée", m.token, h.clock()).stored == 1
    m2 = next(c for c in view(h, prof).custom if c.custom_id == CUSTOM_MASKED)
    assert m2.masked and m2.answer == "" and m2.pending
    assert ops.delete_custom(h.portal, prof.row_id, CUSTOM_MASKED, m2.token, h.clock()).stored == 1


# ---------------------------------------------------------------- intents
def test_request_intents_and_refusals(h, prof):
    out = ops.request_intents(h.portal, prof.row_id, [GRANT_SEND_1, GRANT_SEND_2, "ee" * 16], h.clock())
    assert out.stored == 2 and len(out.refused) == 1 and out.messages() == [ops.MSG_CHANGED]
    assert {e.grant for e in events(h, prof) if e.kind == "intent"} == {GRANT_SEND_1, GRANT_SEND_2}
    assert ops.request_intents(h.portal, prof.row_id, [GRANT_SEND_1], h.clock()).refused  # already pending
    assert ops.request_intents(h.portal, prof.row_id, [GRANT_EMAIL], h.clock()).stored == 0
    assert set(view(h, prof).intent_events) == {GRANT_SEND_1, GRANT_SEND_2}


def test_unconfirmed_sibling_grant_is_refused(h, prof):
    assert ops.request_intents(h.portal, prof.row_id, [GRANT_SENT], h.clock(), verified=True).stored == 1
    assert ops.request_intents(h.portal, prof.row_id, [GRANT_NOT_SENT], h.clock(), verified=True).refused
    assert ops.request_intents(h.portal, prof.row_id, [GRANT_SENT], h.clock(), verified=True).refused
    both = ops.request_intents(h.portal, prof.row_id, [GRANT_NOT_SENT], h.clock(), verified=True)
    assert both.stored == 0
    p2 = h.enable()
    out = ops.request_intents(h.portal, p2.row_id, [GRANT_SENT, GRANT_NOT_SENT], h.clock(), verified=True)
    assert out.stored == 1 and len(out.refused) == 1  # a single batch cannot send both outcomes


def test_intent_refused_when_grant_left_the_snapshot(h, prof):
    assert h.push_snapshot(h.pair(), prof, make_snapshot(awaiting=[], documents=[make_snapshot().documents[1]])).status_code == 200
    assert ops.request_intents(h.portal, prof.row_id, [GRANT_SEND_1], h.clock()).refused


def test_cancel_intent(h, prof):
    ops.request_intents(h.portal, prof.row_id, [GRANT_SEND_1], h.clock())
    intent = events(h, prof)[0]
    assert ops.cancel_intent(h.portal, prof.row_id, "00" * 16, h.clock()).refused
    assert ops.cancel_intent(h.portal, prof.row_id, intent.event_id, h.clock()).stored == 1
    c = events(h, prof)[1]
    assert c.kind == "cancel" and c.target == intent.event_id
    assert ops.cancel_intent(h.portal, prof.row_id, intent.event_id, h.clock()).refused  # already withdrawn
    assert view(h, prof).cancelling_intents == {intent.event_id}
    assert ops.request_intents(h.portal, prof.row_id, [GRANT_SEND_1], h.clock()).refused
    other = h.enable()
    assert ops.cancel_intent(h.portal, other.row_id, intent.event_id, h.clock()).refused  # another profile's event


def test_cancel_refused_after_ack_without_a_live_result(h, prof):
    ops.request_intents(h.portal, prof.row_id, [GRANT_SEND_1], h.clock())
    intent = events(h, prof)[0]
    assert h.push_snapshot(h.pair(), prof, acked_seq=1).status_code == 200
    assert ops.cancel_intent(h.portal, prof.row_id, intent.event_id, h.clock()).refused
    assert events(h, prof) == []


@pytest.mark.parametrize("status", ["received", "awaiting_review", "queued", "running", "cancelled"])
def test_keyed_intent_result_controls_repeat_and_cancel_after_ack(h, prof, status):
    assert ops.request_intents(h.portal, prof.row_id, [GRANT_SENT], h.clock(), verified=True).stored == 1
    intent, = events(h, prof)
    result = protocol.EventResult(event_id=intent.event_id, kind="intent", key=f"g:{GRANT_SENT}", status=status, at=intent.at)
    assert h.push_snapshot(h.pair(), prof, acked_seq=1, results=[result]).status_code == 200
    assert events(h, prof) == []
    if status == "cancelled":
        assert ops.cancel_intent(h.portal, prof.row_id, intent.event_id, h.clock()).refused
        assert ops.request_intents(h.portal, prof.row_id, [GRANT_SENT], h.clock(), verified=True).stored == 1
        return
    assert view(h, prof).intent_events == {GRANT_SENT: intent.event_id}
    assert ops.request_intents(h.portal, prof.row_id, [GRANT_SENT, GRANT_NOT_SENT], h.clock(), verified=True).stored == 0
    out = ops.cancel_intent(h.portal, prof.row_id, intent.event_id, h.clock())
    if status == "running":
        assert out.refused and events(h, prof) == []
    else:
        assert out.stored == 1
        cancel, = events(h, prof)
        assert cancel.seq == 2 and cancel.kind == "cancel" and cancel.target == intent.event_id
        assert view(h, prof).cancelling_intents == {intent.event_id}
        assert ops.cancel_intent(h.portal, prof.row_id, intent.event_id, h.clock()).refused
        assert ops.request_intents(h.portal, prof.row_id, [GRANT_NOT_SENT], h.clock(), verified=True).refused


def test_only_cancels_can_use_fifty_reserved_slots(h, prof, monkeypatch):
    snap = make_snapshot()
    grants = [protocol.new_id() for _ in range(51)]
    snap.awaiting = [snap.awaiting[0].model_copy(update={"grant": grant}) for grant in grants]
    device = h.pair()
    assert h.push_snapshot(device, prof, snap).status_code == 200
    assert ops.request_intents(h.portal, prof.row_id, grants, h.clock()).stored == 51
    intents = events(h, prof)
    results = [protocol.EventResult(event_id=event.event_id, kind="intent", key=f"g:{event.grant}", status="queued", at=event.at)
               for event in intents]
    assert h.push_snapshot(device, prof, snap, acked_seq=51, results=results).status_code == 200
    monkeypatch.setattr(protocol, "MAX_PENDING_EVENTS", 1)
    assert edit(h, prof, "identity.city", "Lyon").stored == 1
    assert view(h, prof).inbox_full and edit(h, prof, "identity.city", "Nice").inbox_full
    for intent in intents[:50]:
        assert ops.cancel_intent(h.portal, prof.row_id, intent.event_id, h.clock()).stored == 1
    rejected = ops.cancel_intent(h.portal, prof.row_id, intents[-1].event_id, h.clock())
    assert rejected.inbox_full and rejected.stored == 0
    assert [event.seq for event in events(h, prof)] == list(range(52, 103))
    assert h.push_snapshot(device, prof, snap, acked_seq=52, results=results).status_code == 200
    assert ops.cancel_intent(h.portal, prof.row_id, intents[-1].event_id, h.clock()).stored == 1


def test_running_result_disallows_cancel_even_before_ack(h, prof):
    assert ops.request_intents(h.portal, prof.row_id, [GRANT_SEND_1], h.clock()).stored == 1
    intent, = events(h, prof)
    running = protocol.EventResult(event_id=intent.event_id, kind="intent", key=f"g:{GRANT_SEND_1}", status="running", at=intent.at)
    assert h.push_snapshot(h.pair(), prof, results=[running]).status_code == 200
    assert view(h, prof).cancellable_intents == set()
    assert ops.cancel_intent(h.portal, prof.row_id, intent.event_id, h.clock()).refused


def test_cancel_only_targets_intents(h, prof):
    edit(h, prof, "identity.city", "Lyon")
    assert ops.cancel_intent(h.portal, prof.row_id, events(h, prof)[0].event_id, h.clock()).refused


# ---------------------------------------------------------------- documents
def test_open_document(h, prof):
    assert ops.open_document(h.portal, prof.row_id, DOC_A) == PDF_A
    other = h.enable()
    assert ops.open_document(h.portal, other.row_id, DOC_A) == PDF_A  # same bytes, own copy
    never_uploaded = make_pdf("c")
    snap = make_snapshot(awaiting=[], sent=[], documents=[doc_ref(never_uploaded)])
    assert h.push_snapshot(h.pair(), prof, snap, upload=False).status_code == 200
    assert ops.open_document(h.portal, prof.row_id, DOC_A) is None  # no longer listed
    assert ops.open_document(h.portal, prof.row_id, DOC_B) is None  # dropped with the manifest
    assert ops.open_document(h.portal, prof.row_id, doc_ref(never_uploaded).doc_id) is None  # listed, never uploaded
    assert ops.open_document(h.portal, prof.row_id, "00" * 16) is None


def test_event_labels(h, prof):
    ops.request_intents(h.portal, prof.row_id, [GRANT_SEND_1, GRANT_SENT, GRANT_EMAIL], h.clock(), verified=True)
    ops.answer_questions(h.portal, prof.row_id, [(QID, "Oui")], h.clock())
    labels = [p.label for p in view(h, prof).pending]
    assert labels == ["Société Exemple — Technicien mécatronique", "Maison Dupont — Stagiaire", "Permis de conduire ?"]
