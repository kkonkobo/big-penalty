"""portal/snapshot.py: what leaves the Mac. Fictional data only."""

from __future__ import annotations

from datetime import timedelta

import pytest

from jobhunt import db
from jobhunt.apply import facts, store as apply_store
from jobhunt.apply.types import FilledField, Question
from jobhunt.cv import store as cvstore
from jobhunt.portal import protocol, snapshot
from jobhunt.portal.snapshot import build_snapshot, sanitize_url

from portal_fakes import NOW, fixture_ats, journal, make_outstanding, make_run, make_sent, master, new_app, set_pdf  # noqa: F401

pytestmark = pytest.mark.usefixtures("fixture_ats")


@pytest.fixture
def jp(journal, profile):
    journal.save_profile(profile.id, profile_key="a" * 32, stream_id="b" * 32, enabled=True)
    return journal


def build(j, profile, now=NOW):
    return build_snapshot(j, profile.id, now)


def test_schema_is_driven_by_the_engine_list(jp, profile, monkeypatch):
    extra = facts.AnswerField(path="shoe.size", label="Pointure", section=facts.ANSWER_SECTIONS[0], widget="select", options=("", "42", "43"))
    monkeypatch.setattr(facts, "ANSWER_FIELDS", (*facts.ANSWER_FIELDS, extra))
    snap, _ = build(jp, profile)
    paths = [f.path for f in snap.answer_schema.fields]
    assert paths == [f.path for f in facts.ANSWER_FIELDS] and "shoe.size" in snap.answers and set(snap.answers) == set(paths)
    spec = next(f for f in snap.answer_schema.fields if f.path == "shoe.size")
    assert spec.options == ["", "42", "43"] and spec.widget == "select"
    assert snap.answer_schema.sections == list(facts.ANSWER_SECTIONS) and snap.display_name == "Léa"


def test_write_only_and_masked_values_never_serialized(jp, profile):
    facts.save_answers(profile.id, {"disability": "Oui (RQTH)", "criminal_record": "Casier judiciaire vierge", "city": "Toulouse"})
    facts.save_custom_answers(profile.id, [("Avez-vous une maladie chronique ?", "diabète-secret"), ("Permis B ?", "Oui")])
    snap, _ = build(jp, profile)
    text = snap.model_copy(update={"answer_schema": protocol.AnswerSchema(sections=[], fields=[])}).model_dump_json()  # options are schema
    for secret in ("Oui (RQTH)", "Casier judiciaire vierge", "diabète-secret"):
        assert secret not in text
    assert snap.answers["disability"].value is None and snap.answers["disability"].is_set
    assert snap.answers["city"].value == "Toulouse"
    assert next(f for f in snap.answer_schema.fields if f.path == "disability").write_only
    c = {x.question: x for x in snap.custom}
    assert c["Avez-vous une maladie chronique ?"].masked and c["Avez-vous une maladie chronique ?"].answer == ""
    assert c["Permis B ?"].answer == "Oui" and not c["Permis B ?"].masked


def test_write_only_setting_is_read_at_call_time(jp, profile, monkeypatch):
    facts.save_answers(profile.id, {"city": "Lyon-secret", "disability": "Non"})
    monkeypatch.setenv("JOBHUNT_PORTAL_WRITE_ONLY", "city")
    snap, _ = build(jp, profile)
    assert "Lyon-secret" not in snap.model_dump_json() and snap.answers["disability"].value == "Non"


def test_dates_are_shown_as_iso_and_legacy_text_is_a_note(jp, profile):
    facts.save_answers(profile.id, {"stage.available_from": "2027-01-04"})
    data = facts.load_answers(profile.id)
    data["alternance"] = {"start": "janvier 2027"}
    facts._write_answers(profile.id, data)
    snap, _ = build(jp, profile)
    assert snap.answers["stage.available_from"].value == "2027-01-04" and snap.answers["stage.available_from"].legacy == ""
    a = snap.answers["alternance.start"]
    assert a.value == "" and a.legacy == "janvier 2027" and a.is_set
    assert not snap.answers["city"].is_set


def test_versions_are_stable_and_change_with_the_value(jp, profile):
    s1, _ = build(jp, profile)
    s2, _ = build(jp, profile, NOW + timedelta(minutes=5))
    assert {k: v.ver for k, v in s1.answers.items()} == {k: v.ver for k, v in s2.answers.items()}
    facts.save_answers(profile.id, {"city": "Lyon"})
    s3, _ = build(jp, profile)
    assert s3.answers["city"].ver != s1.answers["city"].ver
    assert s3.answers["postal_code"].ver == s1.answers["postal_code"].ver
    facts.save_answers(profile.id, {"city": "Lyon"})  # the same value again: same version
    assert build(jp, profile)[0].answers["city"].ver == s3.answers["city"].ver
    assert all(len(v.ver) == 32 for v in s3.answers.values())


def test_custom_versions(jp, profile):
    facts.save_custom_answers(profile.id, [("Permis B ?", "Oui")])
    a = build(jp, profile)[0].custom[0]
    assert a.custom_id == facts.custom_id("Permis B ?") and build(jp, profile)[0].custom[0].ver == a.ver
    facts.save_custom_answers(profile.id, [("Permis B ?", "Non")])
    assert build(jp, profile)[0].custom[0].ver != a.ver


def test_queue_buckets(jp, profile, master, tmp_path):
    _, draft = new_app(profile, master, tmp_path, 1, score=90)
    _, mail = new_app(profile, master, tmp_path, 2, approve=True)
    make_run(mail, tmp_path, status="needs_email", message="Cliquez le lien reçu")
    _, cap = new_app(profile, master, tmp_path, 3, approve=True)
    make_run(cap, tmp_path, status="needs_human")
    _, dry = new_app(profile, master, tmp_path, 4)
    make_run(dry, tmp_path, mode="dry_run", status="needs_input", questions=[Question(label="Q ?")])
    job5, unconf = new_app(profile, master, tmp_path, 5, approve=True)
    out = make_outstanding(unconf, job5)
    snap, docs = build(jp, profile)
    assert [a.company for a in snap.awaiting] == ["Société 1"] and snap.awaiting[0].host == "careers.exemple.test"
    assert snap.awaiting[0].job_url == "https://careers.exemple.test/jobs/1" and snap.awaiting[0].doc_id in docs
    assert not snap.email_pauses
    assert sorted(i.kind for i in snap.info) == ["captcha", "dry_run", "supervised"]
    u = snap.unconfirmed[0]
    assert u.company == "Société 5" and u.grant_sent != u.grant_not_sent
    g1, g2 = jp.get_grant(u.grant_sent), jp.get_grant(u.grant_not_sent)
    assert (g1.action, g2.action) == ("resolve_sent", "resolve_not_sent") and g1.run_id == g2.run_id == out.id
    ga = jp.get_grant(snap.awaiting[0].grant)
    assert (ga.action, ga.app_id, ga.rev) == ("send", draft.id, draft.current_rev)
    assert ga.pdf_sha256 == cvstore.sha256_file(draft.cv_path) and ga.origin_sha
    assert snap.counts.failed == 1


def test_missing_pdf_means_no_send_grant(jp, profile, master, tmp_path):
    _, a = new_app(profile, master, tmp_path, 1, with_pdf=False)
    _, b = new_app(profile, master, tmp_path, 2)
    snap, _ = build(jp, profile)
    assert [x.company for x in snap.awaiting] == ["Société 2"]
    assert len(jp.grants(profile.id)) == 1


def test_grants_reused_then_new_after_change_expiry_or_use(jp, profile, master, tmp_path):
    _, app = new_app(profile, master, tmp_path, 1)
    g1 = build(jp, profile)[0].awaiting[0].grant
    assert build(jp, profile, NOW + timedelta(days=1))[0].awaiting[0].grant == g1
    g2 = build(jp, profile, NOW + timedelta(days=8))[0].awaiting[0].grant  # expired: a new one, never revived
    assert g2 != g1 and jp.get_grant(g1).state == "offered"
    with jp._tx() as c:
        c.execute("UPDATE grants SET state='used' WHERE grant_id=?", (g2,))
    g3 = build(jp, profile, NOW + timedelta(days=8))[0].awaiting[0].grant
    assert g3 not in (g1, g2)
    set_pdf(cvstore.get_application(app.id), b"%PDF-1.4 a new render")  # the PDF changed: another binding, another grant
    assert build(jp, profile, NOW + timedelta(days=8))[0].awaiting[0].grant != g3


def test_grants_follow_stream_and_epoch(jp, profile, master, tmp_path):
    new_app(profile, master, tmp_path, 1)
    g1 = build(jp, profile)[0].awaiting[0].grant
    jp.save_profile(profile.id, epoch=1)
    assert build(jp, profile)[0].awaiting[0].grant != g1


def test_sent_items_omit_historical_inference_text(jp, profile, master, tmp_path):
    job1, a1 = new_app(profile, master, tmp_path, 1, approve=True)
    job2, a2 = new_app(profile, master, tmp_path, 2, approve=True)
    facts.save_custom_answers(profile.id, [("Avez-vous une maladie chronique ?", "Réponse privée")])
    make_sent(a1, job1, submitted_at=NOW - timedelta(days=2), inferred=[
        FilledField(label="Champ historique anodin", op="fill", value="Texte ancien", inferred=True, basis="Préfixe avec secret historique"),
        FilledField(label="Avez-vous un handicap ?", op="select", value="Non-secret", inferred=True, basis="déduit"),
        FilledField(label="Reconnaissance", op="select", fact="disability", value="Non-secret", inferred=True),
        FilledField(label="Champ libre", op="fill", value="Réponse privée", inferred=True),
        FilledField(label="Autre champ", op="fill", value="Oui", basis="Casier judiciaire", inferred=True),
        FilledField(label="Prénom", op="fill", fact="first_name", value="Léa")])
    cvstore.mark_submitted(a2.id, a2.current_rev)  # no recorded sending attempt
    snap, docs = build(jp, profile)
    by = {s.company: s for s in snap.sent}
    assert by["Société 2"].doc_id is None and by["Société 2"].url is None
    for text in ("inferred", "Champ historique anodin", "Texte ancien", "secret historique"):
        assert text not in snap.model_dump_json()
    assert "Non-secret" not in snap.model_dump_json()
    assert "Réponse privée" not in snap.model_dump_json()
    assert by["Société 1"].host == "careers.exemple.test" and by["Société 1"].url.startswith("https://careers.exemple.test/jobs/1")
    assert len({s.ref for s in snap.sent}) == 2 and snap.counts.sent_total == 2


def test_sent_url_is_not_reconstructed_if_original_changes(jp, profile, master, tmp_path):
    job, application = new_app(profile, master, tmp_path, 1, approve=True)
    make_sent(application, job)
    with db.connect() as conn:
        conn.execute("UPDATE jobs SET origin_url=? WHERE id=?", ("https://careers.exemple.test/jobs/new", job.id))
    assert build(jp, profile)[0].sent[0].url is None


def test_dead_run_with_recorded_click_has_no_portal_grant(jp, profile, master, tmp_path):
    _, application = new_app(profile, master, tmp_path, 1, approve=True)
    run = apply_store.start_run(application.id, application.current_rev, "submit", "user", lambda _: tmp_path)
    with db.connect() as conn:
        conn.execute("UPDATE application_runs SET pid=NULL, nav_clicked_at=? WHERE id=?", (db._iso(NOW), run.id))
    snap, _ = build(jp, profile)
    assert [item.kind for item in snap.info] == ["interrupted"]
    assert not snap.awaiting and not snap.email_pauses and not snap.unconfirmed


def test_sent_most_recent_first_and_prep(jp, profile, master, tmp_path):
    from jobhunt.cv.check import Flag

    prep = Flag(level="prep", code="x", location="summary", message="Mot embelli", hint="Prépare-toi")
    jobs = []
    for n, days in ((1, 5), (2, 1), (3, 9)):
        job, app = new_app(profile, master, tmp_path, n, approve=True, flags=[prep] if n == 2 else None)
        make_sent(app, job, submitted_at=NOW - timedelta(days=days))
        jobs.append(app)
    snap, _ = build(jp, profile)
    assert [s.company for s in snap.sent] == ["Société 2", "Société 1", "Société 3"]
    assert [(p.message, p.hint) for p in snap.sent[0].prep] == [("Mot embelli", "Prépare-toi")] and snap.sent[1].prep == []


def test_document_selection_caps_and_dedupe(jp, profile, master, tmp_path, monkeypatch):
    monkeypatch.setattr(protocol, "AWAITING_DOCUMENTS", 2)
    monkeypatch.setattr(protocol, "SENT_DOCUMENTS", 1)
    for n, score in ((1, 50), (2, 90), (3, 70), (4, 60)):
        new_app(profile, master, tmp_path, n, score=score)
    job, sent_new = new_app(profile, master, tmp_path, 5, approve=True)
    job6, sent_old = new_app(profile, master, tmp_path, 6, approve=True)
    make_sent(sent_new, job, submitted_at=NOW - timedelta(days=1))
    make_sent(sent_old, job6, submitted_at=NOW - timedelta(days=9))
    snap, docs = build(jp, profile)
    with_doc = [a.company for a in snap.awaiting if a.doc_id]
    assert sorted(with_doc) == ["Société 2", "Société 3"]  # the best scored
    assert [s.company for s in snap.sent if s.doc_id] == ["Société 5"]
    assert len(snap.documents) == len(docs) == 3
    again = build(jp, profile)
    assert [d.doc_id for d in again[0].documents] == [d.doc_id for d in snap.documents]  # deterministic
    for d in snap.documents:
        assert docs[d.doc_id].read_bytes()[:5] == b"%PDF-" and d.size == len(docs[d.doc_id].read_bytes())


def test_document_size_caps(jp, profile, master, tmp_path, monkeypatch):
    _, a = new_app(profile, master, tmp_path, 1, score=90)
    _, b = new_app(profile, master, tmp_path, 2, score=80)
    set_pdf(a, b"%PDF-" + b"0" * (protocol.MAX_DOCUMENT_BYTES + 1))
    snap, docs = build(jp, profile)
    assert [x.company for x in snap.awaiting if x.doc_id] == ["Société 2"]  # the oversized one still has its grant, but no document
    assert len(snap.awaiting) == 2
    monkeypatch.setattr(protocol, "MAX_DOCUMENTS_BYTES", 10)
    snap, docs = build(jp, profile)
    assert not snap.documents and all(x.doc_id is None for x in snap.awaiting)


def test_document_id_is_keyed_and_changes_with_the_bytes(jp, profile, master, tmp_path):
    _, a = new_app(profile, master, tmp_path, 1)
    d1 = build(jp, profile)[0].awaiting[0].doc_id
    set_pdf(a, b"%PDF-1.4 more")
    d2 = build(jp, profile)[0].awaiting[0].doc_id
    assert d1 != d2 and len(d1) == 32
    sha = cvstore.sha256_file(a.cv_path)
    assert sha[:32] != d2


@pytest.mark.parametrize("url,expected", [
    ("https://user:pw@Careers.Exemple.test/jobs/1?utm_source=x&id=3&gclid=zz#frag", "https://careers.exemple.test/jobs/1?id=3"),
    ("http://exemple.test:8080/a b", "http://exemple.test:8080/a%20b"),
    ("javascript:alert(1)", None), ("ftp://exemple.test/x", None), ("https:///nohost", None), ("", None), (None, None),
    ("https://exemple.test/" + "a" * 2100, None), ("https://[::1]:99999/x", None),
])
def test_url_sanitising(url, expected):
    assert sanitize_url(url) == expected


def test_oversized_and_odd_inputs_still_validate(jp, profile, master, tmp_path):
    job, app = new_app(profile, master, tmp_path, 1)
    from jobhunt.db import connect

    with connect() as c:
        c.execute("UPDATE jobs SET company=?, title=? WHERE id=?", ("S" * 1000 + "\u0000é", "T" * 900, job.id))
    with connect() as c:
        c.execute("UPDATE applications SET company=?, job_title=? WHERE id=?", ("S" * 1000, "T" * 900, app.id))
    facts.save_custom_answers(profile.id, [("Q" * 900, "A" * 900)])
    snap, _ = build(jp, profile)
    assert len(snap.awaiting[0].company) == 300 and len(snap.custom[0].question) == 500 and len(snap.custom[0].answer) == 500
    protocol.Snapshot.model_validate_json(snap.model_dump_json())


def test_questions_use_the_engine_labels_and_ids(jp, profile, master, tmp_path):
    _, app = new_app(profile, master, tmp_path, 1, approve=True)
    make_run(app, tmp_path, status="needs_input", questions=[Question(label="Permis B ?", options=["Oui", "Non"]), Question(label="Ville ?")])
    snap, _ = build(jp, profile)
    q = {x.label: x for x in snap.questions}
    assert q["Permis B ?"].qid == facts.custom_id("Permis B ?") and q["Permis B ?"].options == ["Oui", "Non"]
    assert q["Permis B ?"].company == "Société 1"
    assert snap.awaiting == []  # a paused application is not waiting for an OK


def test_flags_and_modes(jp, profile):
    jp.save_profile(profile.id, review_mode=True)
    jp.set_paused(True)
    snap, _ = build(jp, profile)
    assert snap.review_mode and snap.paused and snap.auto_send_on is False
