"""portal/apply_events.py: every rule of DESIGN §5.2 step 3 and §5.4. Fictional data only."""

from __future__ import annotations

import contextlib
from datetime import timedelta

import pytest

from jobhunt.apply import facts, service
from jobhunt.apply import store as apply_store
from jobhunt.apply.errors import ApplyError
from jobhunt.apply.types import Question
from jobhunt.cv import service as cvs, store as cvstore
from jobhunt.portal import apply_events, protocol
from jobhunt.portal.apply_events import apply_pending, apply_reviewed
from jobhunt.portal.journal import NewEvent
from jobhunt.portal.snapshot import answer_key, custom_key, field_version, stored_answer

from portal_fakes import NOW, journal, make_outstanding, make_run, master, new_app  # noqa: F401

STREAM = "b" * 32


@pytest.fixture
def jp(journal, profile):
    journal.save_profile(profile.id, profile_key="a" * 32, stream_id=STREAM, enabled=True)
    return journal


class Feed:
    def __init__(self, j, pid):
        self.j, self.pid = j, pid

    def add(self, kind, **payload) -> str:
        p = self.j.get_profile(self.pid)
        seq = p.cursor_seq + 1
        eid = f"{seq:032x}"
        self.j.insert_page(self.pid, STREAM, [NewEvent(seq=seq, event_id=eid, kind=kind, payload=payload, at=NOW)], NOW)
        return eid

    def ver(self, path):
        return field_version(self.j, self.pid, answer_key(path), stored_answer(self.pid, path))

    def answer(self, path, value, after=None, base=None):
        return self.add("answer", path=path, value=value, base_ver=base or self.ver(path), after=after)

    def state(self, eid):
        return self.j.get_event(eid).state


@pytest.fixture
def feed(jp, profile):
    return Feed(jp, profile.id)


def run(jp, profile, now=NOW):
    return apply_pending(jp, profile.id, now)


def test_answer_applied_with_a_new_version(jp, profile, feed):
    base = feed.ver("city")
    e = feed.answer("city", "Lyon")
    rep = run(jp, profile)
    ev = jp.get_event(e)
    assert ev.state == "applied" and ev.ver_after and ev.ver_after != base and rep.applied_answers
    assert stored_answer(profile.id, "city") == "Lyon" and feed.ver("city") == ev.ver_after
    assert ev.label == "Ville" and ev.before_text == "" and ev.after_text == "Lyon"


def test_every_accepted_write_issues_a_new_version_even_for_the_same_value(jp, profile, feed):
    facts.save_answers(profile.id, {"city": "Lyon"})
    base = feed.ver("city")
    e = feed.answer("city", "Lyon")
    run(jp, profile)
    assert jp.get_event(e).state == "applied" and jp.get_event(e).ver_after != base


def test_value_removal_and_date_roundtrip(jp, profile, feed):
    facts.save_answers(profile.id, {"city": "Lyon"})
    e1 = feed.answer("city", "")
    e2 = feed.answer("stage.available_from", "2027-01-04")
    run(jp, profile)
    assert feed.state(e1) == feed.state(e2) == "applied"
    assert stored_answer(profile.id, "city") == "" and stored_answer(profile.id, "stage.available_from") == "4 janvier 2027"


def test_legacy_date_text_is_kept_when_cleared(jp, profile, feed):
    data = facts.load_answers(profile.id)
    data["alternance"] = {"start": "janvier 2027"}
    facts._write_answers(profile.id, data)
    e = feed.answer("alternance.start", "")
    run(jp, profile)
    assert feed.state(e) == "applied" and stored_answer(profile.id, "alternance.start") == "janvier 2027"


def test_conflict_when_the_mac_value_changed(jp, profile, feed):
    e = feed.answer("city", "Lyon-secret")
    facts.save_answers(profile.id, {"city": "Nantes"})  # edited on the Mac after the portal page
    rep = run(jp, profile)
    ev = jp.get_event(e)
    assert ev.state == "conflict" and "la valeur du Mac est conservée" in ev.message and not rep.applied_answers
    assert stored_answer(profile.id, "city") == "Nantes" and "Lyon-secret" not in ev.message and "Nantes" not in ev.message


def test_chained_edits_of_one_field_apply_in_order(jp, profile, feed):
    base = feed.ver("city")
    e1 = feed.answer("city", "Lyon", base=base)
    e2 = feed.answer("city", "Nice", after=e1, base=base)
    run(jp, profile)
    assert feed.state(e1) == feed.state(e2) == "applied" and stored_answer(profile.id, "city") == "Nice"


def test_chained_edit_conflicts_when_the_mac_changed_in_between(jp, profile, feed):
    base = feed.ver("city")
    e1 = feed.answer("city", "Lyon", base=base)
    run(jp, profile)
    facts.save_answers(profile.id, {"city": "Nantes"})
    e2 = feed.answer("city", "Nice", after=e1, base=base)
    run(jp, profile)
    assert feed.state(e2) == "conflict" and stored_answer(profile.id, "city") == "Nantes"


def test_edits_to_different_fields_never_conflict(jp, profile, feed):
    a, b = feed.answer("city", "Lyon"), feed.answer("postal_code", "69000")
    run(jp, profile)
    assert feed.state(a) == feed.state(b) == "applied"


@pytest.mark.parametrize("path,value", [("civility", "Peut-être"), ("stage.available_from", "2027-02-31"), ("stage.available_from", "demain"),
                                        ("unknown.field", "VALEUR-SECRETE")])
def test_engine_rejection_changes_nothing(jp, profile, feed, path, value):
    e = feed.add("answer", path=path, value=value, base_ver="1" * 32) if path == "unknown.field" else feed.answer(path, value)
    before = facts.load_answers(profile.id)
    run(jp, profile)
    ev = jp.get_event(e)
    assert ev.state == "rejected" and value not in ev.message and facts.load_answers(profile.id) == before


def test_replay_after_a_clean_apply_is_a_noop(jp, profile, feed, monkeypatch):
    writes = []
    real = facts.save_answers
    monkeypatch.setattr(facts, "save_answers", lambda pid, vals: (writes.append(vals), real(pid, vals))[1])
    e = feed.answer("city", "Lyon")
    run(jp, profile)
    first = jp.get_event(e)
    jp.update_event(e, NOW, state="received")  # simulated: the outcome was lost, the version was recorded
    run(jp, profile)
    again = jp.get_event(e)
    assert len(writes) == 1 and again.state == "applied" and again.ver_after == first.ver_after
    assert stored_answer(profile.id, "city") == "Lyon"


def test_crash_after_the_file_write_is_finished_once(jp, profile, feed, monkeypatch):
    writes = []
    real = facts.save_answers
    monkeypatch.setattr(facts, "save_answers", lambda pid, vals: (writes.append(vals), real(pid, vals))[1])
    e = feed.answer("city", "Lyon")

    def die(*a, **k):
        raise KeyboardInterrupt  # the process is killed between the write and its record

    with monkeypatch.context() as m:
        m.setattr(type(jp), "finalize_write", die)
        with pytest.raises(KeyboardInterrupt):
            run(jp, profile)
    assert stored_answer(profile.id, "city") == "Lyon" and feed.state(e) == "received" and jp.get_event(e).ver_after is None
    run(jp, profile)  # the next pass
    ev = jp.get_event(e)
    assert ev.state == "applied" and ev.ver_after and len(writes) == 1
    assert feed.ver("city") == ev.ver_after


def test_crash_before_the_file_write_is_applied_normally(jp, profile, feed, monkeypatch):
    e = feed.answer("city", "Lyon")
    real = facts.save_answers

    def die(*a, **k):
        raise KeyboardInterrupt

    monkeypatch.setattr(facts, "save_answers", die)
    with pytest.raises(KeyboardInterrupt):
        run(jp, profile)
    assert stored_answer(profile.id, "city") == "" and jp.get_event(e).write_ver  # planned, not written
    monkeypatch.setattr(facts, "save_answers", real)
    run(jp, profile)
    assert feed.state(e) == "applied" and stored_answer(profile.id, "city") == "Lyon"


@pytest.mark.parametrize("control", ["stream", "epoch"])
@pytest.mark.parametrize("edited, expected_state", [(False, "applied"), (True, "conflict")])
def test_control_change_reconciles_durable_answer_plan(jp, profile, feed, monkeypatch, edited, expected_state, control):
    event_id = feed.answer("city", "Lyon")
    with monkeypatch.context() as scoped:
        def die(*args, **kwargs):
            raise KeyboardInterrupt
        scoped.setattr(type(jp), "finalize_write", die)
        with pytest.raises(KeyboardInterrupt):
            run(jp, profile)
    if edited:
        facts.save_answers(profile.id, {"city": "Nantes"})
    if control == "stream":
        jp.reset_stream(profile.id, protocol.new_id(), NOW)
    else:
        jp.stage_config(profile.id, "revoke", NOW, epoch=jp.get_profile(profile.id).epoch + 1)
    assert feed.state(event_id) == expected_state
    assert stored_answer(profile.id, "city") == ("Nantes" if edited else "Lyon")


def test_readback_mismatch_retries_once_then_fails(jp, profile, feed, monkeypatch):
    calls = []
    monkeypatch.setattr(facts, "save_answers", lambda pid, vals: calls.append(vals))  # a writer that loses the value
    e = feed.answer("city", "Lyon")
    run(jp, profile)
    ev = jp.get_event(e)
    assert ev.state == "failed" and len(calls) == 2 and "Lyon" not in ev.message and ev.ver_after is None


def test_exception_in_one_event_does_not_stop_the_others(jp, profile, feed, monkeypatch):
    real = facts.save_answers

    def flaky(pid, vals):
        if "city" in vals:
            raise RuntimeError("disk exploded with Lyon-secret")
        real(pid, vals)

    monkeypatch.setattr(facts, "save_answers", flaky)
    a, b = feed.answer("city", "Lyon-secret"), feed.answer("postal_code", "69000")
    run(jp, profile)
    assert jp.get_event(a).state == "failed" and jp.get_event(a).message == "Erreur technique." and feed.state(b) == "applied"


# ------------------------------------------------------------------ custom answers
def cver(j, pid, cid):
    from jobhunt.portal.snapshot import custom_entry

    e = custom_entry(pid, cid)
    return field_version(j, pid, custom_key(cid), e["answer"] if e else None)


def test_custom_edit_and_delete(jp, profile, feed):
    facts.save_custom_answers(profile.id, [("Permis B ?", "Oui")])
    cid = facts.custom_id("Permis B ?")
    e1 = feed.add("custom_edit", custom_id=cid, answer="  Non  ", base_ver=cver(jp, profile.id, cid), after=None)
    run(jp, profile)
    assert feed.state(e1) == "applied" and facts.custom_answers(profile.id)[0]["answer"] == "Non"
    assert jp.get_event(e1).label == "Permis B ?" and cver(jp, profile.id, cid) == jp.get_event(e1).ver_after
    e2 = feed.add("custom_delete", custom_id=cid, base_ver=jp.get_event(e1).ver_after, after=e1)
    run(jp, profile)
    assert feed.state(e2) == "applied" and facts.custom_answers(profile.id) == []


def test_custom_conflicts(jp, profile, feed):
    facts.save_custom_answers(profile.id, [("Permis B ?", "Oui")])
    cid = facts.custom_id("Permis B ?")
    base = cver(jp, profile.id, cid)
    e1 = feed.add("custom_edit", custom_id=cid, answer="Non", base_ver=base)
    e2 = feed.add("custom_delete", custom_id=cid, base_ver=base)
    facts.save_custom_answers(profile.id, [("Permis B ?", "Peut-être")])
    run(jp, profile)
    assert feed.state(e1) == feed.state(e2) == "conflict" and facts.custom_answers(profile.id)[0]["answer"] == "Peut-être"


def test_custom_on_a_missing_entry_and_empty_answer(jp, profile, feed):
    e1 = feed.add("custom_edit", custom_id="f" * 16, answer="x", base_ver="1" * 32)
    facts.save_custom_answers(profile.id, [("Q ?", "a")])
    cid = facts.custom_id("Q ?")
    e2 = feed.add("custom_edit", custom_id=cid, answer="   ", base_ver=cver(jp, profile.id, cid))
    run(jp, profile)
    assert feed.state(e1) == "conflict" and feed.state(e2) == "rejected" and facts.custom_answers(profile.id)[0]["answer"] == "a"


def test_custom_replay_after_crash(jp, profile, feed, monkeypatch):
    facts.save_custom_answers(profile.id, [("Permis B ?", "Oui")])
    cid = facts.custom_id("Permis B ?")
    e = feed.add("custom_edit", custom_id=cid, answer="Non", base_ver=cver(jp, profile.id, cid))
    with monkeypatch.context() as m:
        m.setattr(type(jp), "finalize_write", lambda *a, **k: (_ for _ in ()).throw(KeyboardInterrupt()))
        with pytest.raises(KeyboardInterrupt):
            run(jp, profile)
    run(jp, profile)
    assert feed.state(e) == "applied" and jp.get_event(e).ver_after == cver(jp, profile.id, cid)


# ------------------------------------------------------------------ question answers
@pytest.fixture
def asked(jp, profile, master, tmp_path):
    _, app = new_app(profile, master, tmp_path, 1, approve=True)
    make_run(app, tmp_path, status="needs_input", questions=[Question(label="Permis B ?", options=["Oui", "Non"]), Question(label="Votre mobilité ?")])
    return app


def test_question_answer_uses_the_macs_label(jp, profile, feed, asked):
    e = feed.add("question_answer", qid=facts.custom_id("Votre mobilité ?"), answer="Lyon")
    rep = run(jp, profile)
    assert feed.state(e) == "applied" and rep.applied_answers
    assert [(c["question"], c["answer"]) for c in facts.custom_answers(profile.id)] == [("Votre mobilité ?", "Lyon")]
    assert [q.label for _a, _r, q in service.pending_questions(profile.id)] == ["Permis B ?"]


def test_question_answer_must_be_an_option(jp, profile, feed, asked):
    e = feed.add("question_answer", qid=facts.custom_id("Permis B ?"), answer="Peut-être")
    run(jp, profile)
    assert feed.state(e) == "rejected" and facts.custom_answers(profile.id) == []


def test_question_no_longer_pending_is_rejected_and_replay_is_applied(jp, profile, feed, asked):
    gone = feed.add("question_answer", qid=facts.custom_id("Autre ?"), answer="x")
    e = feed.add("question_answer", qid=facts.custom_id("Votre mobilité ?"), answer="Lyon")
    run(jp, profile)
    assert feed.state(gone) == "rejected" and "n'est plus en attente" in jp.get_event(gone).message
    jp.update_event(e, NOW, state="received")  # crash replay: the answer is saved, the question no longer pending
    run(jp, profile)
    assert feed.state(e) == "applied" and len(facts.custom_answers(profile.id)) == 1
    other = feed.add("question_answer", qid=facts.custom_id("Votre mobilité ?"), answer="Paris")
    run(jp, profile)
    assert feed.state(other) == "rejected" and facts.custom_answers(profile.id)[0]["answer"] == "Lyon"


# ------------------------------------------------------------------ intents
def grant(jp, profile, action, app, now=NOW, **kw):
    p = jp.get_profile(profile.id)
    return jp.find_or_create_grant(p, action, now, app_id=app.id, rev=app.current_rev, **kw)


def test_send_intent_is_queued_with_a_label(jp, profile, feed, master, tmp_path):
    _, app = new_app(profile, master, tmp_path, 1)
    g = grant(jp, profile, "send", app, pdf_sha256="p", origin_sha="o")
    e = feed.add("intent", grant=g)
    run(jp, profile)
    ev = jp.get_event(e)
    assert ev.state == "queued" and ev.label == "Société 1 — Stage numéro 1" and jp.get_grant(g).state == "offered"


@pytest.mark.parametrize("why", ["unknown", "expired", "void", "used", "other_stream", "other_epoch"])
def test_intent_with_an_invalid_grant_is_refused(jp, profile, feed, master, tmp_path, why):
    _, app = new_app(profile, master, tmp_path, 1)
    g = grant(jp, profile, "send", app)
    now = NOW
    if why == "unknown":
        g = "9" * 32
    elif why == "expired":
        now = NOW + timedelta(days=8)
    elif why in ("void", "used"):
        with jp._tx() as c:
            c.execute("UPDATE grants SET state=? WHERE grant_id=?", (why, g))
    elif why == "other_stream":
        with jp._tx() as c:
            c.execute("UPDATE grants SET stream_id=? WHERE grant_id=?", ("c" * 32, g))
    else:
        jp.save_profile(profile.id, epoch=3)
    e = feed.add("intent", grant=g)
    run(jp, profile, now)
    assert feed.state(e) == "refused" and "n'est plus valable" in jp.get_event(e).message


def test_grant_of_another_profile_is_refused(jp, profile, feed, master, tmp_path):
    _, app = new_app(profile, master, tmp_path, 1)
    jp.save_profile(99, profile_key="d" * 32, stream_id=STREAM, enabled=True)
    g = jp.find_or_create_grant(jp.get_profile(99), "send", NOW, app_id=app.id, rev=1)
    e = feed.add("intent", grant=g)
    run(jp, profile)
    assert feed.state(e) == "refused"


def unconfirmed(jp, profile, master, tmp_path):
    job, app = new_app(profile, master, tmp_path, 1, approve=True)
    out = make_outstanding(app, job)
    p = jp.get_profile(profile.id)
    binding = dict(app_id=app.id, rev=app.current_rev, run_id=out.id, mode="submit",
                   origin_sha=service.origin_digest(job.origin_url), pdf_sha256=cvstore.sha256_file(cvs.safe_pdf(app)))
    gs = jp.find_or_create_grant(p, "resolve_sent", NOW, **binding)
    gn = jp.find_or_create_grant(p, "resolve_not_sent", NOW, **binding)
    return app, out, gs, gn


def test_resolve_sent_marks_submitted_and_voids_the_sibling(jp, profile, feed, master, tmp_path):
    app, out, gs, gn = unconfirmed(jp, profile, master, tmp_path)
    e = feed.add("intent", grant=gs)
    run(jp, profile)
    assert feed.state(e) == "done" and jp.get_grant(gs).state == "used" and jp.get_grant(gn).state == "void"
    assert service.cvstore.get_application(app.id).status == "submitted"
    e2 = feed.add("intent", grant=gn)  # the other outcome can no longer be chosen
    run(jp, profile)
    assert feed.state(e2) == "refused"


def test_resolve_not_sent_goes_back_to_the_queue(jp, profile, feed, master, tmp_path):
    app, out, gs, gn = unconfirmed(jp, profile, master, tmp_path)
    e = feed.add("intent", grant=gn)
    run(jp, profile)
    assert feed.state(e) == "done" and jp.get_grant(gs).state == "void"
    assert service.cvstore.get_application(app.id).status != "failed" and apply_store.outstanding_attempt(app.id) is None


def test_resolve_with_a_stale_run_is_refused(jp, profile, feed, master, tmp_path):
    app, out, gs, gn = unconfirmed(jp, profile, master, tmp_path)
    with jp._tx() as c:
        c.execute("UPDATE grants SET run_id=? WHERE grant_id=?", (out.id + 50, gs))
    e = feed.add("intent", grant=gs)
    run(jp, profile)
    assert feed.state(e) == "refused" and service.cvstore.get_application(app.id).status == "failed"


def test_resolve_replay_if_attempt_is_no_longer_outstanding_is_interrupted(jp, profile, feed, master, tmp_path):
    app, out, gs, gn = unconfirmed(jp, profile, master, tmp_path)
    e = feed.add("intent", grant=gs)
    run(jp, profile)
    jp.update_event(e, NOW, state="received")
    other_event = feed.add("intent", grant=gs)
    run(jp, profile)
    assert feed.state(e) == "interrupted" and feed.state(other_event) == "refused"


def test_resolve_retries_only_its_own_event_after_crash(jp, profile, feed, master, tmp_path, monkeypatch):
    app, out, gs, gn = unconfirmed(jp, profile, master, tmp_path)
    event = feed.add("intent", grant=gs)
    with monkeypatch.context() as scoped:
        def crash(*args, **kwargs):
            raise KeyboardInterrupt
        scoped.setattr(service, "resolve_unconfirmed", crash)
        with pytest.raises(KeyboardInterrupt):
            run(jp, profile)
    assert jp.get_grant(gs).used_by == event and apply_store.outstanding_attempt(app.id).id == out.id
    run(jp, profile)
    assert feed.state(event) == "done" and cvstore.get_application(app.id).status == "submitted"
    assert apply_store.sent_run(app.id).id == out.id


# ------------------------------------------------------------------ review, pause, hold
def test_review_mode_holds_everything_until_the_operator_decides(jp, profile, feed):
    jp.save_profile(profile.id, review_mode=True)
    facts.save_answers(profile.id, {"city": "Nantes"})
    a = feed.answer("city", "Lyon")
    run(jp, profile)  # held: the base version is the one the portal saw
    ev = jp.get_event(a)
    assert ev.state == "awaiting_review" and ev.before_text == "Nantes" and ev.after_text == "Lyon" and ev.label == "Ville"
    assert stored_answer(profile.id, "city") == "Nantes"
    run(jp, profile)
    assert feed.state(a) == "awaiting_review"


def test_review_approve_applies_and_reject_does_not(jp, profile, feed):
    jp.save_profile(profile.id, review_mode=True)
    a, b = feed.answer("city", "Lyon"), feed.answer("postal_code", "69000")
    run(jp, profile)
    rep = apply_reviewed(jp, profile.id, [a], True, validated=jp.get_profile(profile.id))
    assert rep.applied_answers and feed.state(a) == "applied" and feed.state(b) == "awaiting_review"
    apply_reviewed(jp, profile.id, None, False)
    assert feed.state(b) == "rejected" and stored_answer(profile.id, "postal_code") == ""
    assert apply_reviewed(jp, profile.id, [a], True, validated=jp.get_profile(profile.id)).states == {}


def test_review_approved_answer_still_checks_the_version(jp, profile, feed):
    jp.save_profile(profile.id, review_mode=True)
    a = feed.answer("city", "Lyon")
    run(jp, profile)
    facts.save_answers(profile.id, {"city": "Nantes"})  # the Mac changed meanwhile
    apply_reviewed(jp, profile.id, None, True, validated=jp.get_profile(profile.id))
    assert feed.state(a) == "conflict" and stored_answer(profile.id, "city") == "Nantes"


def test_review_intent_is_requeued_not_executed(jp, profile, feed, master, tmp_path):
    jp.save_profile(profile.id, review_mode=True)
    _, app = new_app(profile, master, tmp_path, 1)
    e = feed.add("intent", grant=grant(jp, profile, "send", app))
    run(jp, profile)
    assert feed.state(e) == "awaiting_review" and jp.get_event(e).label.startswith("Société 1")
    # (at the fixture's own time: an agreement is valid for a week, and the real clock is long past the fixture's)
    apply_reviewed(jp, profile.id, None, True, now=NOW, validated=jp.get_profile(profile.id))
    assert feed.state(e) == "queued"


@pytest.mark.parametrize("how", ["paused", "held"])
def test_pause_and_hold_apply_nothing(jp, profile, feed, how):
    a = feed.answer("city", "Lyon")
    if how == "paused":
        jp.set_paused(True)
    else:
        jp.save_profile(profile.id, held=True)
    rep = run(jp, profile)
    assert feed.state(a) == "received" and stored_answer(profile.id, "city") == "" and rep.skipped_reason
    assert apply_reviewed(jp, profile.id, [a], True).skipped_reason is not None
    if how == "paused":
        jp.set_paused(False)
    else:
        jp.save_profile(profile.id, held=False)
    run(jp, profile)
    assert feed.state(a) == "applied"


def test_review_mode_pause_does_not_release_approvals(jp, profile, feed):
    jp.save_profile(profile.id, review_mode=True)
    a = feed.answer("city", "Lyon")
    run(jp, profile)
    jp.set_paused(True)
    assert apply_reviewed(jp, profile.id, None, True).skipped_reason and feed.state(a) == "awaiting_review"


def test_disable_between_review_selection_and_transaction_refuses_apply(jp, profile, feed, monkeypatch):
    from contextlib import contextmanager

    from jobhunt.portal import apply_events

    jp.save_profile(profile.id, review_mode=True)
    event = feed.answer("city", "Lyon")
    run(jp, profile)
    original_lock = apply_events.answers_lock

    @contextmanager
    def disable_before_lock(profile_id):
        jp.save_profile(profile_id, enabled=False)
        with original_lock(profile_id):
            yield

    monkeypatch.setattr(apply_events, "answers_lock", disable_before_lock)
    assert apply_reviewed(jp, profile.id, [event], True, validated=jp.get_profile(profile.id)).skipped_reason == "profil désactivé"
    assert not jp.get_event(event).approved and feed.state(event) == "awaiting_review"
    assert stored_answer(profile.id, "city") == ""


# ------------------------------------------------------------------ cancels
def test_cancel_in_the_same_pull_wins_over_its_target(jp, profile, feed, master, tmp_path):
    _, app = new_app(profile, master, tmp_path, 1)
    i = feed.add("intent", grant=grant(jp, profile, "send", app))
    c = feed.add("cancel", target=i)
    run(jp, profile)
    assert feed.state(i) == "cancelled" and feed.state(c) == "done"


def test_cancel_of_queued_and_awaiting_review(jp, profile, feed, master, tmp_path):
    _, app = new_app(profile, master, tmp_path, 1)
    i = feed.add("intent", grant=grant(jp, profile, "send", app))
    run(jp, profile)
    assert feed.state(i) == "queued"
    c = feed.add("cancel", target=i)
    run(jp, profile)
    assert feed.state(i) == "cancelled" and feed.state(c) == "done"
    jp.save_profile(profile.id, review_mode=True)
    j = feed.add("intent", grant=grant(jp, profile, "send", app))
    run(jp, profile)
    c2 = feed.add("cancel", target=j)
    run(jp, profile)
    assert feed.state(j) == feed.state(c2) == "awaiting_review"
    apply_reviewed(jp, profile.id, [c2], True, validated=jp.get_profile(profile.id))
    assert feed.state(j) == "cancelled" and feed.state(c2) == "done"


@pytest.mark.parametrize("target_state", ["running", "done", "failed", "applied", "interrupted"])
def test_cancel_of_a_started_or_final_target_is_refused(jp, profile, feed, target_state):
    i = feed.add("intent", grant="1" * 32)
    jp.update_event(i, NOW, state=target_state)
    c = feed.add("cancel", target=i)
    run(jp, profile)
    assert feed.state(c) == "refused" and jp.get_event(c).message == "Trop tard : l'action avait déjà commencé." and feed.state(i) == target_state


def test_cancel_of_unknown_or_cancel_target_is_refused(jp, profile, feed):
    c1 = feed.add("cancel", target="7" * 32)
    c2 = feed.add("cancel", target=c1)
    run(jp, profile)
    assert feed.state(c1) == feed.state(c2) == "refused"


def test_pause_voids_intent_and_defers_cancel(jp, profile, feed, master, tmp_path):
    _, app = new_app(profile, master, tmp_path, 1)
    i = feed.add("intent", grant=grant(jp, profile, "send", app))
    run(jp, profile)
    jp.set_paused(True)
    c = feed.add("cancel", target=i)
    run(jp, profile)
    assert feed.state(i) == "cancelled" and feed.state(c) == "received"
    jp.set_paused(False)
    run(jp, profile)
    assert feed.state(i) == "cancelled" and feed.state(c) == "done"


# ------------------------------------------------------------------ the answers lock
def test_own_lock_file_is_used_without_engine_lock(jp, profile, feed, monkeypatch):
    monkeypatch.setattr(apply_events, "_engine_lock", lambda: None)  # an engine without answers_lock
    e = feed.answer("city", "Lyon")
    run(jp, profile)
    assert feed.state(e) == "applied" and facts.answers_path(profile.id).with_name("answers.yaml.portal-lock").exists()


def test_engine_lock_is_used_when_present_and_busy_means_retry_next_pass(jp, profile, feed, monkeypatch):
    log = []

    @contextlib.contextmanager
    def lock(pid, *, timeout_s=10.0):
        log.append(("enter", pid))
        yield
        log.append(("exit", pid))

    monkeypatch.setattr(facts, "answers_lock", lock, raising=False)
    a = feed.answer("city", "Lyon")
    run(jp, profile)
    assert feed.state(a) == "applied" and log[0] == ("enter", profile.id) and log[-1] == ("exit", profile.id)
    assert log.count(("enter", profile.id)) == log.count(("exit", profile.id))
    assert not facts.answers_path(profile.id).with_name("answers.yaml.portal-lock").exists()

    @contextlib.contextmanager
    def busy(pid, *, timeout_s=10.0):
        raise ApplyError("verrou occupé")
        yield

    monkeypatch.setattr(facts, "answers_lock", busy, raising=False)
    b = feed.answer("postal_code", "69000")
    rep = run(jp, profile)
    assert feed.state(b) == "received" and rep.busy == 1 and stored_answer(profile.id, "postal_code") == ""
    monkeypatch.setattr(facts, "answers_lock", lock, raising=False)
    run(jp, profile)
    assert feed.state(b) == "applied"


def test_an_apply_error_inside_the_lock_is_not_a_busy_lock(jp, profile, feed, monkeypatch):
    @contextlib.contextmanager
    def lock(pid, *, timeout_s=10.0):
        yield

    monkeypatch.setattr(facts, "answers_lock", lock, raising=False)
    e = feed.answer("civility", "Peut-être")
    run(jp, profile)
    assert feed.state(e) == "rejected"


def test_no_message_ever_contains_a_value(jp, profile, feed):
    secrets_ = ["Zürich-secret", "Oui (RQTH)"]
    events = [feed.answer("city", secrets_[0]), feed.answer("disability", secrets_[1]), feed.answer("civility", "Zürich-secret"),
              feed.add("question_answer", qid="e" * 16, answer=secrets_[0]),
              feed.add("custom_edit", custom_id="e" * 16, answer=secrets_[0], base_ver="1" * 32)]
    facts.save_answers(profile.id, {"city": "Nantes"})
    run(jp, profile)
    for e in events:
        ev = jp.get_event(e)
        assert not any(s in ev.message or s in ev.label for s in secrets_), (ev.kind, ev.message)
    assert all(not any(s in r.message for s in secrets_) for r in jp.latest_results(profile.id))
