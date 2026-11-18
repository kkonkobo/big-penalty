"""Web UI of the sending queue (milestone 2b): fake apply service, no browser, no network."""

from __future__ import annotations

import re
import threading
import time
from urllib.parse import parse_qs, urlparse

import pytest
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient

from jobhunt.apply import service as apply_service
from jobhunt.apply import store as apply_store
from jobhunt.apply.errors import ApplyError
from jobhunt.apply.service import QueueItem
from jobhunt.apply.types import FilledField, Question, RunResult, StepLog
from jobhunt.config import get_settings
from jobhunt.cv import store as cvstore
from jobhunt.web import apply_routes
from jobhunt.web import app as webapp
from jobhunt.web.apply_routes import APPLYING, SENDING, router

from apply_cv_helpers import make_app, make_job
from cv_fakes import save_confirmed

NO_ORIGIN = "<no origin field>"
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 16
QUESTIONS = [Question(label="Permis B ?"), Question(label="Salaire"), Question(label="Ville")]


@pytest.fixture
def client():
    if not any(getattr(r, "path", None) == "/apply" for r in webapp.app.routes):
        webapp.app.include_router(router)
    SENDING.update(running=False, total=0, done=0, error=None, ids=[])
    APPLYING.clear()
    with TestClient(webapp.app) as c:
        post = c.post

        def post_with_origin(url, *a, data=None, **kw):
            """The single-application resume / answers / trust forms carry the destination digest the page showed (REQUIRED):
            added here from the job's current link unless the test sets `origin` itself (`NO_ORIGIN` = leave it out)."""
            m = re.fullmatch(r"/applications/([0-9]+)/apply/(resume|answers|trust)", url)
            if m is not None and isinstance(data, dict) and len(m.group(1)) <= 12:
                data = dict(data)
                if data.get("origin") == NO_ORIGIN:
                    del data["origin"]
                elif "origin" not in data:
                    app_ = cvstore.get_application(int(m.group(1)))
                    job = webapp.db.get_job(app_.job_id) if app_ else None
                    if job is not None:
                        data["origin"] = apply_service.origin_digest(job.origin_url)
            return post(url, *a, data=data, **kw)

        c.post = post_with_origin
        yield c
    SENDING.update(running=False)


@pytest.fixture
def master(profile):
    return save_confirmed(profile.id)


def mk_app(profile, master, tmp_path, n=1):
    job = make_job(profile, title=f"Stage firmware {n}", company=f"Société{n}", url=f"https://careers{n}.exemple.test/job")
    return make_app(profile, job, tmp_path, master_hash=master.content_hash)


def item(a, bucket, **kw):
    return QueueItem(app=a, bucket=bucket, origin_url="https://careers.exemple.test/job", origin_host="careers.exemple.test", **kw)


def revs(*apps):
    """The hidden `rev_<id>` and `origin_<id>` (sha256 of the job link) fields of the send form."""
    out = {f"rev_{a.id}": str(a.current_rev) for a in apps}
    out.update({f"origin_{a.id}": apply_service.origin_digest(webapp.db.get_job(a.job_id).origin_url) for a in apps})
    return out


def patch_queue(monkeypatch, items):
    monkeypatch.setattr(apply_service, "queue", lambda profile_id=None: items)


def soup(html):
    return BeautifulSoup(html, "html.parser")


def wait_idle(tries=200):
    for _ in range(tries):
        if not apply_routes._any_running():
            return
        time.sleep(0.01)
    raise AssertionError("background task still running")


def make_run(a, tmp_path, *, mode="dry_run", status="ready", shot=True, shot_path=None, questions=(), untrusted_host="", filled=()):
    out = get_settings().applications_dir / f"runs-{a.id}-{mode}-{status}"
    out.mkdir(parents=True, exist_ok=True)
    png = out / "step0.png"
    png.write_bytes(PNG)
    steps = [StepLog(index=0, url="https://careers.exemple.test/apply", page_kind="form",
                     filled=[FilledField(label="Prénom", op="fill", fact="first_name", value="Léa"),
                             FilledField(label="Mot de passe", op="fill", fact="account_password", value="•••"), *filled],
                     clicked="Suivant", screenshot=str(shot_path or png) if shot else "")]
    run = apply_store.start_run(a.id, a.current_rev, mode, "user", lambda rid: out)
    return apply_store.finish_run(run.id, RunResult(status=status, message="Message du run", steps=steps,
                                                       questions=list(questions), untrusted_host=untrusted_host)), out


# ------------------------------------------------------------------ queue page
def test_queue_page_renders_each_bucket(client, profile, master, tmp_path, monkeypatch):
    apps = [mk_app(profile, master, tmp_path, n) for n in range(1, 9)]
    a_sel, a_blocked_val, a_in, a_mail, a_cap, a_fail, a_blk, a_sent = apps
    failed_run, _ = make_run(a_fail, tmp_path, mode="submit", status="failed")
    sent_app = a_sent.model_copy(update={"status": "submitted"})
    items = [
        item(a_sel, "to_validate", score=82, age_days=2.0, auto=True, selectable=True),
        item(a_blocked_val, "to_validate", score=40, age_days=5.0, selectable=False, problems=["CV non approuvable"]),
        item(a_in, "paused", pause_reason="input", questions=[Question(label="Permis B ?", options=["Oui", "Non"]),
                                                            Question(label="Salaire souhaité")]),
        item(a_mail, "paused", pause_reason="email", last_run=failed_run.model_copy(update={"message": "Cliquer le lien reçu"})),
        item(a_cap, "paused", pause_reason="human"),
        item(a_fail, "failed", selectable=True, last_run=failed_run),
        item(a_blk, "blocked", problems=["Offre fermée"]),
        item(sent_app, "sent", last_run=failed_run),
    ]
    patch_queue(monkeypatch, items)
    html = client.get("/apply").text
    s = soup(html)
    assert not s.select("form form")
    boxes = s.select("#send-form input[name=app_id]")
    assert [b["value"] for b in boxes] == [str(a_sel.id)]
    assert "Envoi automatique : désactivé" in html and "60" in html
    assert s.select_one("#send-btn").has_attr("disabled")
    assert s.select_one(f'button[formaction="/applications/{a_sel.id}/apply/dry-run"]')
    assert not s.select_one(f'button[formaction="/applications/{a_blocked_val.id}/apply/dry-run"]')
    assert s.select_one(f'form[action="/applications/{a_in.id}/apply/answers"] select[name=a_0]')
    assert s.select_one(f'form[action="/applications/{a_in.id}/apply/answers"] input[name=a_1]')
    assert s.select_one(f'form[action="/applications/{a_in.id}/apply/answers"] input[name=q_0][value="Permis B ?"]')
    assert "Enregistrer et reprendre" in html and "Cliquer le lien reçu" in html and "Reprendre dans une fenêtre" in html
    assert len(s.select('form[action$="/apply/resume"]')) == 2
    assert "Offre fermée" in html and "Corriger le CV" in html
    assert "Réessayer" in html
    assert f"/apply/runs/{failed_run.id}" in html
    assert 'href="/applications/%d/pdf"' % a_sel.id in html
    assert 'href="https://careers.exemple.test/job"' in html


def test_failed_status_has_no_retry_and_shows_alert(client, profile, master, tmp_path, monkeypatch):
    a = mk_app(profile, master, tmp_path)
    patch_queue(monkeypatch, [item(a.model_copy(update={"status": "failed"}), "failed", selectable=True)])
    html = client.get("/apply").text
    assert "Envoi non confirmé : vérifier sur le site de l'entreprise" in html
    assert "Réessayer" not in html


def test_empty_queue_and_automatic_state(client, monkeypatch):
    patch_queue(monkeypatch, [])
    monkeypatch.setattr(get_settings(), "apply_auto", True)
    html = client.get("/apply").text
    assert "Envoi automatique : activé" in html and "Rien à valider" in html
    assert 'id="paused"' not in html and 'id="sent"' not in html


def test_unsafe_origin_url_is_not_linked(client, profile, master, tmp_path, monkeypatch):
    a = mk_app(profile, master, tmp_path)
    it = item(a, "to_validate", selectable=True).model_copy(update={"origin_url": "javascript:alert(1)"})
    patch_queue(monkeypatch, [it])
    assert "javascript:" not in client.get("/apply").text


def test_queue_failure_is_sanitised(client, monkeypatch):
    def boom(profile_id=None):
        raise RuntimeError("secret detail /etc/passwd")
    monkeypatch.setattr(apply_service, "queue", boom)
    r = client.get("/apply")
    assert r.status_code == 200 and "secret detail" not in r.text and "Erreur interne" in r.text


# ------------------------------------------------------------------ send
def test_send_starts_batch_with_selected_ids(client, profile, master, tmp_path, monkeypatch):
    a, b = mk_app(profile, master, tmp_path, 1), mk_app(profile, master, tmp_path, 2)
    calls = []

    async def fake_send(ids, *, headless=None, on_progress=None, **kw):
        calls.append(list(ids))
        for i in ids:
            on_progress(i, None)
        return []

    monkeypatch.setattr(apply_service, "send_selected", fake_send)
    patch_queue(monkeypatch, [])
    r = client.post("/apply/send", data={"app_id": [str(a.id), str(b.id), str(a.id)], **revs(a, b)}, follow_redirects=True)
    assert r.status_code == 200 and "Envoi lancé pour 2 candidatures" in r.text
    wait_idle()
    assert calls == [[a.id, b.id]] and SENDING["done"] == 2 and SENDING["total"] == 2 and not SENDING["running"]


def test_send_refuses_empty_unknown_and_concurrent(client, profile, master, tmp_path, monkeypatch):
    a = mk_app(profile, master, tmp_path)
    gate = threading.Event()
    calls = []

    async def slow_send(ids, *, headless=None, on_progress=None, **kw):
        calls.append(list(ids))
        while not gate.is_set():
            await __import__("asyncio").sleep(0.01)
        return []

    monkeypatch.setattr(apply_service, "send_selected", slow_send)
    patch_queue(monkeypatch, [])
    assert "Aucune candidature sélectionnée" in client.post("/apply/send", data={}, follow_redirects=True).text
    assert "Candidature introuvable" in client.post("/apply/send", data={"app_id": "9999", "rev_9999": "1"},
                                                    follow_redirects=True).text
    assert not calls
    client.post("/apply/send", data={"app_id": str(a.id), **revs(a)})
    for _ in range(200):
        if calls:
            break
        time.sleep(0.01)
    page = client.get("/apply").text
    assert 'hx-get="/apply/status"' in page and "Envoi en cours" in page
    assert "déjà en cours" in client.post("/apply/send", data={"app_id": str(a.id), **revs(a)}, follow_redirects=True).text
    assert len(calls) == 1
    frag = client.get("/apply/status")
    assert "Envoi en cours" in frag.text and "HX-Refresh" not in frag.headers
    gate.set()
    wait_idle()
    done = client.get("/apply/status", headers={"HX-Request": "true"})
    assert done.headers.get("HX-Refresh") == "true"


def test_batch_error_is_shown_once_and_sanitised(client, profile, master, tmp_path, monkeypatch):
    a = mk_app(profile, master, tmp_path)

    async def boom(ids, *, headless=None, on_progress=None, **kw):
        raise ApplyError("Navigateur indisponible")

    monkeypatch.setattr(apply_service, "send_selected", boom)
    patch_queue(monkeypatch, [])
    client.post("/apply/send", data={"app_id": str(a.id), **revs(a)}, follow_redirects=False)
    wait_idle()
    assert "Navigateur indisponible" in client.get("/apply").text
    assert "Navigateur indisponible" not in client.get("/apply").text

    async def boom2(ids, *, headless=None, on_progress=None, **kw):
        raise RuntimeError("clé API sk-secret")

    monkeypatch.setattr(apply_service, "send_selected", boom2)
    client.post("/apply/send", data={"app_id": str(a.id), **revs(a)}, follow_redirects=False)
    wait_idle()
    text = client.get("/apply").text
    assert "sk-secret" not in text and "Erreur interne" in text


# ------------------------------------------------------------------ dry run / resume / answers
def record_runs(monkeypatch):
    calls = []

    async def fake_run(app_id, *, mode, trigger, headless=None, **kw):
        calls.append({"app_id": app_id, "mode": mode, "trigger": trigger, "headless": headless, **kw})

    monkeypatch.setattr(apply_service, "run", fake_run)
    patch_queue(monkeypatch, [])
    return calls


def test_dry_run_route(client, profile, master, tmp_path, monkeypatch):
    a = mk_app(profile, master, tmp_path)
    calls = record_runs(monkeypatch)
    r = client.post(f"/applications/{a.id}/apply/dry-run", follow_redirects=True)
    assert "Essai à blanc lancé : rien ne sera envoyé" in r.text
    wait_idle()
    assert calls == [{"app_id": a.id, "mode": "dry_run", "trigger": "user", "headless": None}]  # nothing bound: nothing was reviewed
    assert client.post("/applications/9999/apply/dry-run").status_code == 404


def test_resume_uses_last_run_mode_and_headful_for_captcha(client, profile, master, tmp_path, monkeypatch):
    a, b, c = (mk_app(profile, master, tmp_path, n) for n in (1, 2, 3))
    calls = record_runs(monkeypatch)
    ra, _ = make_run(a, tmp_path, mode="submit", status="needs_email")
    rb, _ = make_run(b, tmp_path, mode="submit", status="needs_human")
    rc, _ = make_run(c, tmp_path, mode="dry_run", status="needs_human")
    for x, r_ in ((a, ra), (b, rb), (c, rc)):
        client.post(f"/applications/{x.id}/apply/resume", data={"run_id": str(r_.id), "mode": r_.mode, "rev": str(r_.rev)})
        wait_idle()
    by = {c_["app_id"]: c_ for c_ in calls}
    assert by[a.id]["mode"] == "submit" and by[a.id]["headless"] is None and by[a.id]["trigger"] == "user"
    assert by[b.id]["mode"] == "submit" and by[b.id]["headless"] is False
    assert by[c.id]["mode"] == "dry_run"
    assert all(by[x.id]["expected_rev"] == x.current_rev for x in (a, b, c))  # the revision the user was shown is bound
    assert client.post("/applications/9999/apply/resume", data={"run_id": "1", "mode": "dry_run"}).status_code == 404


def test_answers_are_stored_then_resumed(client, profile, master, tmp_path, monkeypatch):
    a = mk_app(profile, master, tmp_path)
    calls = record_runs(monkeypatch)
    stored = []
    monkeypatch.setattr(apply_service, "answer_questions", lambda app_id, answers: stored.append((app_id, answers)))
    run, _ = make_run(a, tmp_path, mode="submit", status="needs_input", questions=QUESTIONS)
    data = {"run_id": str(run.id), "q_0": "Permis B ?", "a_0": "Oui", "q_1": "Salaire", "a_1": "  ", "q_2": "Ville", "a_2": "Toulouse"}
    r = client.post(f"/applications/{a.id}/apply/answers", data=data, follow_redirects=True)
    assert "Réponses enregistrées" in r.text
    wait_idle()
    assert stored == [(a.id, [("Permis B ?", "Oui"), ("Ville", "Toulouse")])]
    assert calls[0]["mode"] == "submit"


def test_answers_refuse_empty_and_report_errors(client, profile, master, tmp_path, monkeypatch):
    a = mk_app(profile, master, tmp_path)
    calls = record_runs(monkeypatch)
    run, _ = make_run(a, tmp_path, mode="submit", status="needs_input", questions=QUESTIONS)

    def refuse(app_id, answers):
        raise ApplyError("Réponse refusée")

    monkeypatch.setattr(apply_service, "answer_questions", refuse)
    rid = str(run.id)
    assert "Aucune réponse saisie" in client.post(f"/applications/{a.id}/apply/answers",
                                                  data={"run_id": rid, "q_0": "Ville", "a_0": ""}, follow_redirects=True).text
    assert "Réponse refusée" in client.post(f"/applications/{a.id}/apply/answers",
                                            data={"run_id": rid, "q_0": "Ville", "a_0": "y"}, follow_redirects=True).text
    assert not calls


# ------------------------------------------------------------------ run page + screenshots
def test_run_page_shows_steps_fields_and_send_button(client, profile, master, tmp_path):
    a = mk_app(profile, master, tmp_path)
    run, _ = make_run(a, tmp_path)
    r = client.get(f"/apply/runs/{run.id}")
    s = soup(r.text)
    assert r.status_code == 200
    assert "Formulaire rempli, rien n'a été envoyé" in r.text
    assert "Formulaire de candidature" in r.text and "Essai à blanc" in r.text and "Message du run" in r.text
    assert "Prénom" in r.text and "Léa" in r.text and "first_name" in r.text and "•••" in r.text
    assert "Suivant" in r.text
    img = s.select_one("img[loading=lazy]")
    assert img and img["src"] == f"/apply/runs/{run.id}/shots/0"
    form = s.select_one('form[action="/apply/send"]')
    assert form and form.select_one(f'input[name=app_id][value="{a.id}"]') and "primary" in form.button["class"]
    assert form.select_one(f'input[name=rev_{a.id}][value="{run.rev}"]')


def test_run_page_without_send_button_when_submitted_or_not_ready(client, profile, master, tmp_path):
    a = mk_app(profile, master, tmp_path)
    run, _ = make_run(a, tmp_path, mode="submit", status="submitted", shot=False)
    r = client.get(f"/apply/runs/{run.id}")
    assert "Formulaire rempli" not in r.text and 'action="/apply/send"' not in r.text and "<img" not in r.text
    assert "Envoyée" in r.text
    assert client.get("/apply/runs/9999").status_code == 404


def test_screenshot_served_and_refused(client, profile, master, tmp_path):
    a = mk_app(profile, master, tmp_path)
    run, out = make_run(a, tmp_path)
    r = client.get(f"/apply/runs/{run.id}/shots/0")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png" and r.content == PNG
    assert client.get(f"/apply/runs/{run.id}/shots/1").status_code == 404
    assert client.get(f"/apply/runs/{run.id}/shots/-1").status_code == 404
    assert client.get("/apply/runs/9999/shots/0").status_code == 404

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "x.png").write_bytes(PNG)
    (outside / "x.txt").write_bytes(PNG)
    (out / "link.png").symlink_to(outside / "x.png")
    (out / "notpng.txt").write_bytes(PNG)
    (get_settings().applications_dir / "sibling.png").write_bytes(PNG)  # inside applications_dir but outside out_dir
    for bad in (outside / "x.png", outside / "x.txt", out / "link.png", out / "notpng.txt", out / "missing.png",
                get_settings().applications_dir / "sibling.png", out / ".." / ".." / "x.png"):
        b, _ = make_run(a, tmp_path, status="failed", shot_path=bad)
        assert client.get(f"/apply/runs/{b.id}/shots/0").status_code == 404, bad


def test_screenshot_refused_when_out_dir_escapes_applications_dir(client, profile, master, tmp_path):
    a = mk_app(profile, master, tmp_path)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    png = outside / "s.png"
    png.write_bytes(PNG)
    run = apply_store.start_run(a.id, a.current_rev, "dry_run", "user", lambda rid: outside)
    run = apply_store.finish_run(run.id, RunResult(status="ready", steps=[StepLog(index=0, url="https://x.test", page_kind="form",
                                                                                screenshot=str(png))]))
    assert client.get(f"/apply/runs/{run.id}/shots/0").status_code == 404


# ------------------------------------------------------------------ card + helper
def test_queue_item_helper_and_card(client, profile, master, tmp_path, monkeypatch):
    a = mk_app(profile, master, tmp_path)
    it = item(a, "to_validate", selectable=True, problems=["Offre ancienne"])
    patch_queue(monkeypatch, [it])
    assert apply_routes.queue_item(a.id).app.id == a.id
    assert apply_routes.queue_item(9999) is None
    monkeypatch.setattr(apply_service, "queue", lambda profile_id=None: (_ for _ in ()).throw(RuntimeError("x")))
    assert apply_routes.queue_item(a.id) is None

    apply_routes._register_globals(webapp.templates)
    tpl = webapp.templates.env.from_string('{% include "_apply_card.html" %}')
    html = tpl.render(item=it)
    assert "Envoi sur le site de l'entreprise" in html and "À valider" in html and "Offre ancienne" in html
    assert 'action="/apply/send"' in html and f"/applications/{a.id}/apply/dry-run" in html
    none_html = tpl.render(item=None)
    assert "Envoi sur le site de l'entreprise" in none_html and "/apply/send" not in none_html
    failed = tpl.render(item=item(a.model_copy(update={"status": "failed"}), "failed", selectable=True))
    assert "Envoi non confirmé" in failed and "/apply/send" not in failed


# ------------------------------------------------------------------ stale / forged forms, ids
def test_send_requires_the_revision_the_user_was_shown(client, profile, master, tmp_path, monkeypatch):
    a, b = mk_app(profile, master, tmp_path, 1), mk_app(profile, master, tmp_path, 2)
    calls = []

    async def fake_send(ids, *, headless=None, on_progress=None, expected_revs=None, **kw):
        calls.append((list(ids), expected_revs))
        return []

    monkeypatch.setattr(apply_service, "send_selected", fake_send)
    patch_queue(monkeypatch, [])
    og = revs(a, b)
    page = client.post("/apply/send", data={"app_id": [str(a.id), str(b.id)], f"rev_{a.id}": "1", f"origin_{a.id}": og[f"origin_{a.id}"],
                                            f"origin_{b.id}": og[f"origin_{b.id}"]}, follow_redirects=True).text
    assert "Page périmée" in page and not calls  # rev of b missing
    assert "Page périmée" in client.post("/apply/send", data={"app_id": str(a.id), f"rev_{a.id}": "x", **{k: v for k, v in og.items() if k == f"origin_{a.id}"}},
                                         follow_redirects=True).text
    assert "Page périmée" in client.post("/apply/send", data={"app_id": str(a.id), f"rev_{a.id}": "1"}, follow_redirects=True).text  # no origin
    assert "Page périmée" in client.post("/apply/send", data={"app_id": str(a.id), f"rev_{a.id}": "1", f"origin_{a.id}": "zz"},
                                         follow_redirects=True).text
    assert "Page périmée" in client.post("/apply/send", data={"app_id": str(a.id), f"rev_{a.id}": "١", f"origin_{a.id}": og[f"origin_{a.id}"]},
                                         follow_redirects=True).text
    assert not calls
    client.post("/apply/send", data={"app_id": [str(a.id), str(b.id)], f"rev_{a.id}": "1", f"rev_{b.id}": "7", **{k: v for k, v in og.items() if k.startswith("origin_")}})
    wait_idle()
    assert calls == [([a.id, b.id], {a.id: 1, b.id: 7})]  # a revision that is not the current one is refused by send_selected


def test_the_queue_forms_carry_run_id_revision_and_mode(client, profile, master, tmp_path, monkeypatch):
    a_sel, a_in, a_mail = (mk_app(profile, master, tmp_path, n) for n in (1, 2, 3))
    r_in, _ = make_run(a_in, tmp_path, mode="submit", status="needs_input", questions=QUESTIONS)
    r_mail, _ = make_run(a_mail, tmp_path, mode="submit", status="needs_email")
    patch_queue(monkeypatch, [item(a_sel, "to_validate", selectable=True),
                              item(a_in, "paused", pause_reason="input", last_run=r_in, questions=QUESTIONS),
                              item(a_mail, "paused", pause_reason="email", last_run=r_mail)])
    s = soup(client.get("/apply").text)
    assert s.select_one(f'#send-form input[name=rev_{a_sel.id}][value="{a_sel.current_rev}"]')
    assert s.select_one(f'form[action="/applications/{a_in.id}/apply/answers"] input[name=run_id][value="{r_in.id}"]')
    f = s.select_one(f'form[action="/applications/{a_mail.id}/apply/resume"]')
    assert f.select_one(f'input[name=run_id][value="{r_mail.id}"]') and f.select_one('input[name=mode][value="submit"]')


def test_stale_answers_form_is_refused_and_nothing_is_saved(client, profile, master, tmp_path, monkeypatch):
    a = mk_app(profile, master, tmp_path)
    calls = record_runs(monkeypatch)
    stored = []
    monkeypatch.setattr(apply_service, "answer_questions", lambda app_id, answers: stored.append(answers))
    old, _ = make_run(a, tmp_path, mode="submit", status="needs_input", questions=QUESTIONS)
    ok = {"q_0": "Ville", "a_0": "Toulouse"}

    def post(**data):
        return client.post(f"/applications/{a.id}/apply/answers", data=data, follow_redirects=True).text

    assert "Page périmée" in post(**ok)  # no run_id at all
    assert "Page périmée" in post(run_id="x", **ok)
    assert "Page périmée" in post(run_id=str(old.id), q_0="Question inventée", a_0="x")  # label not asked by that run
    assert "Page périmée" in post(run_id=str(old.id), q_0="Ville", a_0="x", q_1="Autre", a_1="y")  # one bad label refuses all
    newer, _ = make_run(a, tmp_path, mode="submit", status="failed")  # the form belongs to an older run
    assert "Page périmée" in post(run_id=str(old.id), **ok)
    assert "Page périmée" in post(run_id=str(newer.id), **ok)  # last run, but not waiting for answers
    again, _ = make_run(a, tmp_path, mode="submit", status="needs_input", questions=QUESTIONS)
    store_edit = cvstore_new_revision(a)
    assert store_edit.current_rev == a.current_rev + 1
    assert "Page périmée" in post(run_id=str(again.id), **ok)  # CV edited since: run.rev != current rev
    assert not stored and not calls


def cvstore_new_revision(a):
    from jobhunt.cv import store as cvstore
    return cvstore.add_revision(profile_id=a.profile_id, job_id=a.job_id, job_title=None, company=None, ats=None, tailored=a.tailored,
                                flags=[], verifier=a.verifier, fit_notes=a.fit_notes, master_hash=a.master_hash, tmp_pdf=None,
                                pdf_target=lambda r: __import__("pathlib").Path("/nonexistent"), app_id=a.id, base_rev=a.current_rev)


def test_answers_with_the_last_run_are_accepted_even_with_normalised_whitespace(client, profile, master, tmp_path, monkeypatch):
    a = mk_app(profile, master, tmp_path)
    record_runs(monkeypatch)
    stored = []
    monkeypatch.setattr(apply_service, "answer_questions", lambda app_id, answers: stored.append(answers))
    run, _ = make_run(a, tmp_path, mode="submit", status="needs_input", questions=[Question(label="Disponibilité\n  (début) ?")])
    r = client.post(f"/applications/{a.id}/apply/answers", data={"run_id": str(run.id), "q_0": "Disponibilité (début) ?", "a_0": "Janvier"},
                    follow_redirects=True)
    assert "Réponses enregistrées" in r.text and stored == [[("Disponibilité (début) ?", "Janvier")]]


def test_stale_resume_form_is_refused(client, profile, master, tmp_path, monkeypatch):
    a = mk_app(profile, master, tmp_path)
    calls = record_runs(monkeypatch)
    old, _ = make_run(a, tmp_path, mode="submit", status="needs_email")
    newer, _ = make_run(a, tmp_path, mode="submit", status="needs_human")

    def post(**data):
        data.setdefault("rev", "1")
        return client.post(f"/applications/{a.id}/apply/resume", data=data, follow_redirects=True).text

    assert "Page périmée" in post(run_id=str(old.id), mode="submit")  # not the last run any more
    assert "Page périmée" in post(mode="submit")
    assert "Page périmée" in post(run_id=str(newer.id), mode="dry_run")  # wrong mode
    assert "Page périmée" in post(run_id="9" * 13, mode="submit")
    assert "Page périmée" in post(run_id=str(newer.id), mode="submit", rev="2")  # not the revision that was shown
    assert "Page périmée" in post(run_id=str(newer.id), mode="submit", origin="nope")
    assert "Page périmée" in client.post(f"/applications/{a.id}/apply/resume", data={"run_id": str(newer.id), "mode": "submit"},
                                         follow_redirects=True).text  # no rev
    wait_idle()
    assert not calls
    assert "Reprise lancée" in post(run_id=str(newer.id), mode="submit")
    wait_idle()
    assert len(calls) == 1 and calls[0]["mode"] == "submit" and calls[0]["headless"] is False
    done, _ = make_run(a, tmp_path, mode="submit", status="submitted")
    assert "Page périmée" in post(run_id=str(done.id), mode="submit")  # last run, but nothing is waiting
    assert calls[0]["expected_rev"] == 1


@pytest.mark.parametrize("bad", ["١٢٣", "²", "1" * 13, "99999999999999999999", "-1", "0", "1.5", " 1", "٣"])
def test_unicode_and_huge_ids_are_never_accepted(client, profile, master, tmp_path, monkeypatch, bad):
    a = mk_app(profile, master, tmp_path)
    record_runs(monkeypatch)
    assert apply_routes.safe_id(bad) is None
    assert apply_routes.safe_id(str(a.id)) == a.id and apply_routes.safe_id("123456789012") == 123456789012
    for path in (f"/applications/{bad}/apply/dry-run", f"/applications/{bad}/apply/resume", f"/applications/{bad}/apply/answers",
                 f"/applications/{bad}/apply/resolve"):
        assert client.post(path, data={}).status_code == 404, path
    assert client.get(f"/apply/runs/{bad}").status_code == 404
    assert client.get(f"/apply/runs/1/shots/{bad}").status_code == 404
    r = client.post("/apply/send", data={"app_id": bad, f"rev_{bad}": "1"}, follow_redirects=True)
    assert "Aucune candidature sélectionnée" in r.text
    assert client.get(f"/apply?profile={bad}").status_code == 200


# ------------------------------------------------------------------ "was it sent?" reconciliation
def failed_app(profile, master, tmp_path, n=1):
    a = mk_app(profile, master, tmp_path, n)
    from jobhunt.cv import service as cvs
    cvs.approve(a.id, 1)
    run = apply_store.start_run(a.id, 1, "submit", "user", lambda rid: tmp_path / str(rid))
    apply_store.reserve_send(run.id, profile_id=profile.id, application_id=a.id, trigger="user", role_key=f"r{n}", url_key=f"u{n}", daily_cap=9)
    apply_store.finish_and_settle(run.id, RunResult(status="needs_email", submit_clicked=True))
    from jobhunt.cv import store as cvstore
    return cvstore.get_application(a.id)


def last_id(a):
    return str(apply_store.last_run(a.id).id)


def test_resolve_route_both_ways(client, profile, master, tmp_path):
    from jobhunt.cv import store as cvstore
    a, b = failed_app(profile, master, tmp_path, 1), failed_app(profile, master, tmp_path, 2)
    r = client.post(f"/applications/{a.id}/apply/resolve", data={"sent": "1", "run_id": last_id(a)}, follow_redirects=True)
    assert "marquée comme envoyée" in r.text and cvstore.get_application(a.id).status == "submitted"
    r = client.post(f"/applications/{b.id}/apply/resolve", data={"sent": "0", "run_id": last_id(b)}, follow_redirects=True)
    assert "remise en brouillon" in r.text and cvstore.get_application(b.id).status == "draft"
    assert not apply_store.has_unconfirmed_click(b.id) and apply_store.has_unconfirmed_click(a.id)
    assert "Réponse invalide" in client.post(f"/applications/{b.id}/apply/resolve", data={"sent": "2", "run_id": last_id(b)}, follow_redirects=True).text
    assert "pas en échec" in client.post(f"/applications/{b.id}/apply/resolve", data={"sent": "1", "run_id": last_id(b)}, follow_redirects=True).text
    assert client.post("/applications/9999/apply/resolve", data={"sent": "1", "run_id": "1"}).status_code == 404


def test_resolve_buttons_in_the_failed_section_and_the_card(client, profile, master, tmp_path, monkeypatch):
    a = failed_app(profile, master, tmp_path)
    it = item(a, "failed", selectable=False, last_run=apply_store.last_run(a.id), outstanding=apply_store.outstanding_attempt(a.id))
    patch_queue(monkeypatch, [it])
    s = soup(client.get("/apply").text)
    forms = s.select(f'#failed form[action="/applications/{a.id}/apply/resolve"]')
    assert [f.select_one("input[name=sent]")["value"] for f in forms] == ["1", "0"]
    assert [f.button.text.strip() for f in forms] == ["Elle a bien été envoyée", "Elle n'a pas été envoyée, réessayer plus tard"]
    assert all(f.select_one("input[name=run_id]")["value"] == last_id(a) for f in forms)  # the outstanding attempt
    assert all("confirm(" in f["onsubmit"] for f in forms) and not s.select('#failed form[action="/apply/send"]')
    apply_routes._register_globals(webapp.templates)
    card = soup(webapp.templates.env.from_string('{% include "_apply_card.html" %}').render(item=it))
    cforms = card.select(f'form[action="/applications/{a.id}/apply/resolve"]')
    assert [f.select_one("input[name=sent]")["value"] for f in cforms] == ["1", "0"] and all("confirm(" in f["onsubmit"] for f in cforms)


def test_run_page_send_button_carries_the_shown_revision(client, profile, master, tmp_path):
    a = mk_app(profile, master, tmp_path)
    run, _ = make_run(a, tmp_path)
    s = soup(client.get(f"/apply/runs/{run.id}").text)
    assert s.select_one(f'form[action="/apply/send"] input[name=rev_{a.id}]')["value"] == str(run.rev)


def test_resolve_route_refuses_a_stale_or_missing_run_id(client, profile, master, tmp_path):
    from jobhunt.cv import store as cvstore
    a = failed_app(profile, master, tmp_path)
    for data in ({"sent": "1"}, {"sent": "1", "run_id": str(int(last_id(a)) + 5)}, {"sent": "1", "run_id": "x"}):
        assert "Page périmée" in client.post(f"/applications/{a.id}/apply/resolve", data=data, follow_redirects=True).text
        assert cvstore.get_application(a.id).status == "failed"
    older = int(last_id(a))
    with webapp.db.connect() as c:  # a newer run exists: it does not hide the attempt that may have been sent
        c.execute("INSERT INTO application_runs(application_id, rev, mode, trigger, status, started_at) VALUES (?,?,?,?,?,?)",
                  (a.id, 1, "dry_run", "user", "ready", "2026-01-01T00:00:00+00:00"))
    newer = int(last_id(a))
    assert newer != older and "Page périmée" in client.post(f"/applications/{a.id}/apply/resolve", data={"sent": "1", "run_id": str(newer)},
                                                           follow_redirects=True).text  # a run that is not the outstanding one
    assert cvstore.get_application(a.id).status == "failed"
    assert "marquée comme envoyée" in client.post(f"/applications/{a.id}/apply/resolve", data={"sent": "1", "run_id": str(older)},
                                                   follow_redirects=True).text
    assert cvstore.get_application(a.id).status == "submitted"


# ------------------------------------------------------------------ untrusted host: "Faire confiance à ce site ?"
def host_blocked(profile, master, tmp_path, host="jobs.autre-site.test", mode="submit"):
    a = mk_app(profile, master, tmp_path)
    run, _ = make_run(a, tmp_path, mode=mode, status="blocked", untrusted_host=host)
    return a, run


def test_queue_puts_an_untrusted_host_run_in_paused_with_the_host(profile, master, tmp_path):
    a, run = host_blocked(profile, master, tmp_path)
    it = next(i for i in apply_service.queue(profile.id) if i.app.id == a.id)
    assert it.bucket == "paused" and it.pause_reason == "host" and it.untrusted_host == "jobs.autre-site.test"
    from jobhunt.apply import facts
    facts.add_trusted_host(profile.id, "jobs.autre-site.test")  # trusted since: back to the normal buckets
    it = next(i for i in apply_service.queue(profile.id) if i.app.id == a.id)
    assert it.bucket != "paused" and it.untrusted_host == ""


def test_queue_page_shows_the_trust_card(client, profile, master, tmp_path):
    a, run = host_blocked(profile, master, tmp_path)
    page = client.get("/apply").text
    s = soup(page)
    assert "est hébergé sur" in page and "jobs.autre-site.test" in page and "Faire confiance à ce site ?" in page
    f = s.select_one(f'#paused form[action="/applications/{a.id}/apply/trust"]')
    assert f["method"] == "post" and f.select_one("input[name=run_id]")["value"] == str(run.id)
    assert f.select_one("input[name=host]")["value"] == "jobs.autre-site.test"
    assert f.button.text.strip() == "Faire confiance à jobs.autre-site.test et reprendre"
    assert "Société1" in page


def test_trust_route_adds_the_host_and_resumes_in_the_runs_mode(client, profile, master, tmp_path, monkeypatch):
    from jobhunt.apply import facts
    a, run = host_blocked(profile, master, tmp_path)
    calls = record_runs(monkeypatch)
    r = client.post(f"/applications/{a.id}/apply/trust", data={"run_id": str(run.id), "host": "Jobs.Autre-Site.test"}, follow_redirects=True)
    wait_idle()
    assert "ajouté aux sites de confiance" in r.text and facts.trusted_hosts(profile.id) == ["jobs.autre-site.test"]
    assert calls == [{"app_id": a.id, "mode": "submit", "trigger": "user", "headless": None, "expected_rev": 1,
                      "expected_origin": apply_service.origin_digest(webapp.db.get_job(a.job_id).origin_url)}]


def test_trust_route_refuses_a_stale_run_a_host_mismatch_and_a_bad_host(client, profile, master, tmp_path, monkeypatch):
    from jobhunt.apply import facts
    a, run = host_blocked(profile, master, tmp_path)
    calls = record_runs(monkeypatch)

    def post(**data):
        return client.post(f"/applications/{a.id}/apply/trust", data=data, follow_redirects=True).text

    assert "Page périmée" in post(run_id=str(run.id), host="evil.example.test")  # not the host the run refused
    assert "Page périmée" in post(run_id=str(run.id + 9), host="jobs.autre-site.test")
    assert "Page périmée" in post(host="jobs.autre-site.test")
    newer, _ = make_run(a, tmp_path, status="ready")  # a later run: the card is stale
    assert "Page périmée" in post(run_id=str(run.id), host="jobs.autre-site.test")
    assert facts.trusted_hosts(profile.id) == [] and not calls
    assert client.post("/applications/abc/apply/trust", data={}).status_code == 404
    assert client.post("/applications/9999/apply/trust", data={}).status_code == 404


def test_trust_route_refuses_an_ip_host_even_when_the_run_names_it(client, profile, master, tmp_path, monkeypatch):
    from jobhunt.apply import facts
    a, run = host_blocked(profile, master, tmp_path, host="192.168.1.5")
    record_runs(monkeypatch)
    r = client.post(f"/applications/{a.id}/apply/trust", data={"run_id": str(run.id), "host": "192.168.1.5"}, follow_redirects=True)
    assert "Nom de site invalide" in r.text and facts.trusted_hosts(profile.id) == []


def test_run_page_shows_the_untrusted_host(client, profile, master, tmp_path):
    a, run = host_blocked(profile, master, tmp_path)
    assert "jobs.autre-site.test" in client.get(f"/apply/runs/{run.id}").text


# ------------------------------------------------------------------ deduced answers
DEDUCED = [FilledField(label="Niveau d'anglais", op="fill", fact="candidate_profile", value="B2", inferred=True,
                       basis="Le CV indique anglais B2"),
           FilledField(label="Niveau d'anglais", op="fill", fact="candidate_profile", value="B2", inferred=True, basis="doublon")]


def test_run_page_marks_deduced_values(client, profile, master, tmp_path):
    a = mk_app(profile, master, tmp_path)
    run, _ = make_run(a, tmp_path, filled=DEDUCED)
    s = soup(client.get(f"/apply/runs/{run.id}").text)
    rows = [tr for tr in s.select("#step-0 tbody tr")]
    deduced = [tr for tr in rows if "Niveau d'anglais" in tr.text]
    assert deduced and all(tr.select_one(".tag.warn").text.strip() == "déduite" for tr in deduced)
    assert "Le CV indique anglais B2" in deduced[0].text
    assert not [tr for tr in rows if "Prénom" in tr.text and tr.select(".tag.warn")]  # a known fact is not "déduite"


def test_inferred_answers_come_from_the_submitted_run_else_the_ready_one_of_the_current_revision(profile, master, tmp_path):
    a = mk_app(profile, master, tmp_path)
    assert apply_service.inferred_answers(a.id) == [] and apply_service.inferred_answers(9999) == []
    ready, _ = make_run(a, tmp_path, status="needs_input", filled=DEDUCED)
    assert apply_service.inferred_answers(a.id) == []  # only submitted / ready runs count
    ready, _ = make_run(a, tmp_path, status="ready", filled=DEDUCED[:1])
    got = apply_service.inferred_answers(a.id)
    assert [(f.label, f.value, f.basis) for f in got] == [("Niveau d'anglais", "B2", "Le CV indique anglais B2")]
    other = FilledField(label="Disponibilité", op="fill", value="janvier", inferred=True, basis="stage de fin d'études")
    make_run(a, tmp_path, status="ready", filled=[other])  # newer ready run
    assert [f.label for f in apply_service.inferred_answers(a.id)] == ["Disponibilité"]
    make_run(a, tmp_path, mode="submit", status="submitted", filled=DEDUCED)  # a submitted run wins and is deduplicated by label
    assert [f.label for f in apply_service.inferred_answers(a.id)] == ["Niveau d'anglais"]
    with webapp.db.connect() as c:  # a new revision: the old runs no longer describe what would be sent
        c.execute("UPDATE applications SET current_rev=current_rev+1 WHERE id=?", (a.id,))
    assert apply_service.inferred_answers(a.id) == []


def test_application_page_and_card_list_the_deduced_answers(client, profile, master, tmp_path):
    a = mk_app(profile, master, tmp_path)
    make_run(a, tmp_path, status="ready", filled=DEDUCED[:1])
    page = soup(client.get(f"/applications/{a.id}").text).get_text(" ")
    assert "Réponses déduites (à connaître avant l'entretien)" in page and "Niveau d'anglais" in page
    assert "B2" in page and "Le CV indique anglais B2" in page
    b = mk_app(profile, master, tmp_path, 2)
    assert "Réponses déduites" not in client.get(f"/applications/{b.id}").text


def test_sent_rows_show_the_count_of_deduced_answers(client, profile, master, tmp_path, monkeypatch):
    a = mk_app(profile, master, tmp_path)
    run, _ = make_run(a, tmp_path, mode="submit", status="submitted", filled=[
        FilledField(label=f"Q{i}", op="fill", value="x", inferred=True, basis="b") for i in range(3)])
    sent = a.model_copy(update={"status": "submitted"})
    patch_queue(monkeypatch, [item(sent, "sent", last_run=run)])
    s = soup(client.get("/apply").text)
    link = s.select_one("#sent a[href$='/apply/runs/%d'].small" % run.id)
    assert link is not None and link.text.strip() == "3 réponses déduites"


# ------------------------------------------------------------------ what the user reviewed is what runs
def test_resume_rechecks_the_paused_run_when_the_task_starts(client, profile, master, tmp_path, monkeypatch):
    """The mode / revision / run come from the validated form: a newer run between the click and the task start cancels it."""
    a = mk_app(profile, master, tmp_path)
    calls = record_runs(monkeypatch)
    run, _ = make_run(a, tmp_path, mode="submit", status="needs_email")
    real = apply_routes._single

    async def late(app_id, mode, headless, bound=None):
        make_run(a, tmp_path, mode="dry_run", status="ready")  # something happened in between
        await real(app_id, mode, headless, bound)

    monkeypatch.setattr(apply_routes, "_single", late)
    r = client.post(f"/applications/{a.id}/apply/resume", data={"run_id": str(run.id), "mode": "submit", "rev": "1"})
    assert "Reprise lancée" in r.text  # accepted at request time ...
    wait_idle()
    assert calls == []  # ... refused when the task starts
    assert "Page périmée" in r.text or "Page périmée" in client.get("/apply").text


def test_resume_passes_the_origin_digest_it_was_shown(client, profile, master, tmp_path, monkeypatch):
    a = mk_app(profile, master, tmp_path)
    calls = record_runs(monkeypatch)
    run, _ = make_run(a, tmp_path, mode="submit", status="needs_email")
    digest = apply_service.origin_digest(webapp.db.get_job(a.job_id).origin_url)
    client.post(f"/applications/{a.id}/apply/resume", data={"run_id": str(run.id), "mode": "submit", "rev": "1", "origin": digest})
    wait_idle()
    assert calls[0]["expected_origin"] == digest and calls[0]["expected_rev"] == 1


def test_send_forms_carry_the_origin_digest(client, profile, master, tmp_path, monkeypatch):
    a = mk_app(profile, master, tmp_path)
    digest = apply_service.origin_digest("https://careers.exemple.test/job")
    patch_queue(monkeypatch, [item(a, "to_validate", selectable=True, origin_sha=digest)])
    s = soup(client.get("/apply").text)
    assert s.select_one(f'#send-form input[name=origin_{a.id}]')["value"] == digest
    run, _ = make_run(a, tmp_path)
    s = soup(client.get(f"/apply/runs/{run.id}").text)
    assert s.select_one(f'form[action="/apply/send"] input[name=origin_{a.id}]')["value"] == apply_service.origin_digest(
        webapp.db.get_job(a.job_id).origin_url)


def test_failed_forms_use_the_outstanding_attempt_not_a_later_refusal(client, profile, master, tmp_path, monkeypatch):
    a = failed_app(profile, master, tmp_path)
    first = apply_store.last_run(a.id)
    later = apply_store.start_run(a.id, 1, "submit", "user", lambda rid: tmp_path / f"x{rid}")
    apply_store.finish_run(later.id, RunResult(status="blocked", message="refus"))
    s = soup(client.get("/apply").text)
    assert apply_store.last_run(a.id).id == later.id
    forms = s.select(f'#failed form[action="/applications/{a.id}/apply/resolve"]')
    assert [f.select_one("input[name=run_id]")["value"] for f in forms] == [str(first.id)] * 2
    r = client.post(f"/applications/{a.id}/apply/resolve", data={"sent": "1", "run_id": str(first.id)}, follow_redirects=True)
    assert "marquée comme envoyée" in r.text


# ------------------------------------------------------------------ "Réponses aux formulaires"
from jobhunt.apply import facts as apply_facts  # noqa: E402
from jobhunt.models import ContractType, Profile  # noqa: E402


def answers_url(profile):
    return f"/profiles/{profile.id}/answers"


def save(client, profile, data, **kw):
    """POST the main form the way the page would: every posted `ans_<path>` / `ca_<id>` field comes with the value the page
    displayed (`prev_…` / `caprev_…`) unless the test gives it."""
    ctx = apply_routes._answers_context(profile.id)
    shown = {r["name"]: r["value"] for sec in ctx["sections"] for r in sec["rows"]}
    data = dict(data)
    for key in list(data):
        if key.startswith("ans_") and "prev_" + key[4:] not in data:
            data["prev_" + key[4:]] = shown.get(key, "")
    for c in ctx["custom"]:
        if f"ca_{c['id']}" in data and f"caprev_{c['id']}" not in data:
            data[f"caprev_{c['id']}"] = c["answer"]
    return client.post(answers_url(profile), data=data, **kw)


def resume_form(client, profile):
    """The hidden fields of the "Candidatures en pause" form of the page, as a POST body."""
    s = soup(client.get(answers_url(profile)).text)
    data: dict = {"app_id": []}
    for i in s.select("#resumable input[type=hidden]"):
        if i["name"] == "app_id":
            data["app_id"].append(i["value"])
        else:
            data[i["name"]] = i["value"]
    return data


def other_profile():
    return webapp.db.save_profile(Profile(name="Zoé Autre", headline="Étudiante", contract_types=[ContractType.STAGE]))


def test_answers_page_renders_every_section_and_field(client, profile):
    s = soup(client.get(answers_url(profile)).text)
    assert not s.select("form form") and s.select_one("h1").text == "Réponses aux formulaires"
    assert [h.text.strip() for h in s.select("section.panel h2")][:5] == list(apply_facts.ANSWER_SECTIONS)
    for f in apply_facts.ANSWER_FIELDS:
        el = s.select_one(f'[name="ans_{f.path.replace(".", "__")}"]')
        assert el is not None, f.path
        assert el.name == {"select": "select", "textarea": "textarea"}.get(f.widget, "input")
        if f.widget == "date":
            assert el["type"] == "date"
    assert len(s.select("button.primary")) == 1 and s.select_one("button.primary").text.strip() == "Enregistrer les réponses"
    assert s.select_one("#pending") is None and "Suggestion : France" in client.get(answers_url(profile)).text
    assert 'name="ans_civility"' in client.get(answers_url(profile)).text
    assert [o.text for o in s.select('select[name="ans_civility"] option')] == ["Non renseigné", "M.", "Mme"]


def test_answers_page_shows_stored_values_and_never_another_profiles(client, profile):
    other = other_profile()
    apply_facts.save_answers(profile.id, {"birth_place": "Albi", "birth_date": "1999-03-04", "civility": "Mme"})
    apply_facts.save_answers(other.id, {"birth_place": "Quimper", "address": "7 quai secret"})
    apply_facts.save_custom_answers(other.id, [("Question de Zoé ?", "Réponse de Zoé")])
    apply_facts.add_trusted_host(other.id, "site-de-zoe.test")
    s = soup(client.get(answers_url(profile)).text)
    assert s.select_one('input[name="ans_birth_place"]')["value"] == "Albi" and s.select_one('input[name="ans_birth_date"]')["value"] == "1999-03-04"
    assert s.select_one('select[name="ans_civility"] option[selected]')["value"] == "Mme"
    html = client.get(answers_url(profile)).text
    assert "Quimper" not in html and "quai secret" not in html and "Zoé" not in html and "site-de-zoe" not in html
    assert "Quimper" in client.get(answers_url(other)).text


def test_answers_page_404s(client, profile):
    assert client.get("/profiles/9999/answers").status_code == 404
    assert client.get("/profiles/x/answers").status_code == 404
    assert client.post("/profiles/9999/answers", data={}).status_code == 404
    assert client.post("/profiles/9999/answers/resume").status_code == 404
    assert client.post("/profiles/9999/answers/custom/" + "a" * 16 + "/delete").status_code == 404
    assert client.post("/profiles/9999/answers/hosts/remove", data={"host": "x.test"}).status_code == 404
    assert client.post(f"/profiles/{profile.id}/answers/custom/x/delete").status_code == 404
    assert client.post(f"/profiles/{profile.id}/answers/custom/0/delete").status_code == 404


def test_answers_save_writes_only_what_changed_and_keeps_the_rest(client, profile):
    apply_facts.save_custom_answers(profile.id, [("Q ?", "R")])
    apply_facts.add_trusted_host(profile.id, "jobs.autre-site.test")
    r = save(client, profile, {"ans_birth_place": " Albi ", "ans_birth_date": "1999-03-04", "ans_civility": "Mme",
                                                "ans_stage__duration": "6 mois", "ans_city": ""})
    assert "Réponses enregistrées" in r.text
    data = apply_facts.load_answers(profile.id)
    assert data["birth_place"] == "Albi" and data["birth_date"] == "4 mars 1999" and data["stage"] == {"duration": "6 mois"}
    assert data["custom"] == [{"question": "Q ?", "answer": "R"}] and data["trusted_hosts"] == ["jobs.autre-site.test"]
    assert "city" not in data  # a field that was not posted / empty never creates a key
    r = save(client, profile, {"ans_birth_place": "Albi"})
    assert "Aucun changement" in r.text
    r = save(client, profile, {"ans_birth_place": ""})  # cleared
    assert "birth_place" not in apply_facts.load_answers(profile.id)


def test_answers_save_refuses_invalid_values_and_writes_nothing(client, profile):
    r = save(client, profile, {"ans_city": "Albi", "ans_civility": "Dr"})
    assert "non autorisée" in r.text and apply_facts.load_answers(profile.id) == {}
    r = save(client, profile, {"ans_birth_date": "pas une date"})
    assert "Date invalide" in r.text and apply_facts.load_answers(profile.id) == {}


def test_a_legacy_text_date_survives_a_save_of_the_page(client, profile):
    p = apply_facts.answers_path(profile.id)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("stage:\n  available_from: janvier 2027\ndriving_licence: Permis B depuis 2020\n", encoding="utf-8")
    html = client.get(answers_url(profile)).text
    s = soup(html)
    assert s.select_one('input[name="ans_stage__available_from"]')["value"] == "" and "janvier 2027" in html
    assert s.select_one('select[name="ans_driving_licence"] option[selected]')["value"] == "Permis B depuis 2020"  # kept as an extra option
    save(client, profile, {"ans_stage__available_from": "", "ans_driving_licence": "Permis B depuis 2020"})
    data = apply_facts.load_answers(profile.id)
    assert data["stage"]["available_from"] == "janvier 2027" and data["driving_licence"] == "Permis B depuis 2020"


def test_pending_questions_are_listed_deduplicated_and_saved_as_custom_answers(client, profile, master, tmp_path):
    a, b, c = (mk_app(profile, master, tmp_path, n) for n in (1, 2, 3))
    qs = [Question(label="Date de naissance ?"), Question(label="Permis B ?", options=["Oui", "Non"])]
    make_run(a, tmp_path, mode="submit", status="needs_input", questions=qs)
    make_run(b, tmp_path, mode="dry_run", status="needs_input", questions=[Question(label="permis b"), Question(label="Autre question")])
    make_run(c, tmp_path, mode="submit", status="needs_email")
    s = soup(client.get(answers_url(profile)).text)
    idx = {i["value"]: i["name"].removeprefix("pql_") for i in s.select("#pending input[name^=pql_]")}
    assert sorted(x.lower() for x in idx) == ["autre question", "date de naissance ?", "permis b"] or \
        sorted(x.lower() for x in idx) == ["autre question", "date de naissance ?", "permis b ?"]  # one "permis b", whichever asked first
    permis = next(k for k in idx if k.lower().startswith("permis b"))
    idx["Permis B ?"] = idx.pop(permis)
    assert s.select_one(f"#pending input[name=pqa_{idx['Date de naissance ?']}]") and s.select_one("#pending select, #pending input[name^=pqa_]")
    assert "Société1" in s.select_one("#pending").text and "Société2" in s.select_one("#pending").text
    data = {}
    for label, answer in (("Date de naissance ?", "4 mars 1999"), ("Permis B ?", "Oui"), ("Autre question", "")):
        shown = permis if label == "Permis B ?" else label
        data.update({f"pql_{idx[label]}": shown, f"pqa_{idx[label]}": answer})
    r = save(client, profile, data)
    assert "2 candidatures en pause peuvent reprendre" in r.text
    assert sorted((x["question"].lower().rstrip(" ?"), x["answer"]) for x in apply_facts.custom_answers(profile.id)) == [
        ("date de naissance", "4 mars 1999"), ("permis b", "Oui")]
    s = soup(client.get(answers_url(profile)).text)
    assert [i["value"] for i in s.select("#pending input[name^=pql_]")] == ["Autre question"]  # answered ones are gone


def test_a_question_that_nobody_asked_is_refused(client, profile, master, tmp_path):
    a = mk_app(profile, master, tmp_path)
    make_run(a, tmp_path, mode="submit", status="needs_input", questions=[Question(label="Ville ?")])
    r = save(client, profile, {"pql_0": "Question inventée", "pqa_0": "x", "ans_city": "Albi"})
    assert "Page périmée" in r.text and apply_facts.load_answers(profile.id) == {}


def test_custom_answers_can_be_edited_and_removed_by_stable_id(client, profile):
    apply_facts.save_custom_answers(profile.id, [("Q1 ?", "A1"), ("Q2 ?", "A2")])
    ids = [c["id"] for c in apply_facts.custom_answers(profile.id)]
    assert ids == [apply_facts.custom_id("Q1 ?"), apply_facts.custom_id("q1")] or ids[0] == apply_facts.custom_id("Q1 ?")
    s = soup(client.get(answers_url(profile)).text)
    assert [i["value"] for i in s.select("#custom input[name^=ca_]")] == ["A1", "A2"]
    assert [i["name"] for i in s.select("#custom input[name^=caprev_]")] == [f"caprev_{i}" for i in ids]
    save(client, profile, {f"ca_{ids[0]}": "A1 bis", f"ca_{ids[1]}": "A2"})
    assert [c["answer"] for c in apply_facts.custom_answers(profile.id)] == ["A1 bis", "A2"]
    r = client.post(f"{answers_url(profile)}/custom/{ids[0]}/delete", data={f"caprev_{ids[0]}": "A1 bis"})
    assert "Réponse retirée" in r.text
    assert [c["question"] for c in apply_facts.custom_answers(profile.id)] == ["Q2 ?"]
    assert client.post(f"{answers_url(profile)}/custom/{'0' * 16}/delete", data={f"caprev_{'0' * 16}": "x"}).status_code == 200
    assert client.post(f"{answers_url(profile)}/custom/5/delete").status_code == 404  # not an id


def test_a_stale_tab_never_edits_or_deletes_another_answer(client, profile):
    """Tab 1 shows Q1, Q2. Tab 2 deletes Q1. Tab 1 then edits / deletes: nothing lands on Q2 (it used to shift to index 0)."""
    apply_facts.save_custom_answers(profile.id, [("Q1 ?", "A1"), ("Q2 ?", "A2")])
    q1, q2 = (c["id"] for c in apply_facts.custom_answers(profile.id))
    page = {"ca_" + q1: "A1 modifiée", "caprev_" + q1: "A1", "ca_" + q2: "A2", "caprev_" + q2: "A2"}  # what tab 1 will post
    assert "Réponse retirée" in client.post(f"{answers_url(profile)}/custom/{q1}/delete", data={"caprev_" + q1: "A1"}).text  # tab 2
    r = client.post(answers_url(profile), data=page)
    assert "cette réponse a changé entre-temps" in r.text
    assert [(c["question"], c["answer"]) for c in apply_facts.custom_answers(profile.id)] == [("Q2 ?", "A2")]
    r = client.post(f"{answers_url(profile)}/custom/{q1}/delete", data={"caprev_" + q1: "A1"})
    assert "cette réponse a changé entre-temps" in r.text and len(apply_facts.custom_answers(profile.id)) == 1
    # the answer itself changed in another tab: the old edit does not overwrite it
    apply_facts.save_custom_answers(profile.id, [("Q2 ?", "A2 nouvelle")])
    r = client.post(answers_url(profile), data={"ca_" + q2: "A2 ancienne éditée", "caprev_" + q2: "A2"})
    assert "cette réponse a changé entre-temps" in r.text and apply_facts.custom_answers(profile.id)[0]["answer"] == "A2 nouvelle"
    r = client.post(f"{answers_url(profile)}/custom/{q2}/delete", data={"caprev_" + q2: "A2"})
    assert "changé entre-temps" in r.text and len(apply_facts.custom_answers(profile.id)) == 1
    assert "Page périmée" in client.post(f"{answers_url(profile)}/custom/{q2}/delete", data={}).text  # no previous value at all
    assert len(apply_facts.custom_answers(profile.id)) == 1
    r = client.post(answers_url(profile), data={"ca_" + q2: "sans précédente"})  # no caprev: never a blind write
    assert "changé entre-temps" in r.text and apply_facts.custom_answers(profile.id)[0]["answer"] == "A2 nouvelle"


def test_a_fixed_field_changed_in_another_tab_is_not_overwritten(client, profile):
    apply_facts.save_answers(profile.id, {"city": "Albi", "birth_place": "Tarbes"})
    stale_page = {"ans_city": "Toulouse", "prev_city": "Albi", "ans_birth_place": "Lourdes", "prev_birth_place": "Tarbes",
                  "ans_nationality": "Française", "prev_nationality": ""}
    apply_facts.save_answers(profile.id, {"city": "Nantes"})  # another tab
    r = client.post(answers_url(profile), data=stale_page)
    data = apply_facts.load_answers(profile.id)
    assert "Ville" in r.text and "cette réponse a changé entre-temps" in r.text
    assert data["city"] == "Nantes"  # not overwritten
    assert data["birth_place"] == "Lourdes" and data["nationality"] == "Française"  # the others are saved
    assert "Enregistré : 2" in r.text
    r = client.post(answers_url(profile), data={"ans_city": "Paris"})  # no prev_: never a blind overwrite
    assert "changé entre-temps" in r.text and apply_facts.load_answers(profile.id)["city"] == "Nantes"
    r = client.post(answers_url(profile), data={"ans_city": "Nantes"})  # nothing changed, nothing to say
    assert "Aucun changement" in r.text


def test_the_page_posts_the_displayed_values_back(client, profile):
    apply_facts.save_answers(profile.id, {"city": "Albi", "birth_date": "1999-03-04"})
    s = soup(client.get(answers_url(profile)).text)
    assert s.select_one('input[name="prev_city"]')["value"] == "Albi" and s.select_one('input[name="prev_birth_date"]')["value"] == "1999-03-04"
    assert s.select_one('input[name="prev_stage__available_from"]')["value"] == ""


def test_custom_answers_of_another_profile_are_not_reachable(client, profile):
    other = other_profile()
    apply_facts.save_custom_answers(other.id, [("Question de Zoé ?", "Réponse de Zoé")])
    zid = apply_facts.custom_answers(other.id)[0]["id"]
    assert "changé entre-temps" in client.post(f"{answers_url(profile)}/custom/{zid}/delete", data={f"caprev_{zid}": "Réponse de Zoé"}).text
    assert len(apply_facts.custom_answers(other.id)) == 1


def test_trusted_hosts_listed_and_removed(client, profile):
    apply_facts.add_trusted_host(profile.id, "jobs.autre-site.test")
    apply_facts.add_trusted_host(profile.id, "recrute.exemple.test")
    s = soup(client.get(answers_url(profile)).text)
    assert [b["value"] for b in s.select('#hosts button[name=host]')] == ["jobs.autre-site.test", "recrute.exemple.test"]
    assert "Site retiré" in client.post(f"{answers_url(profile)}/hosts/remove", data={"host": "jobs.autre-site.test"}).text
    assert apply_facts.trusted_hosts(profile.id) == ["recrute.exemple.test"]
    assert "introuvable" in client.post(f"{answers_url(profile)}/hosts/remove", data={"host": "jobs.autre-site.test"}).text
    assert "introuvable" in client.post(f"{answers_url(profile)}/hosts/remove", data={"host": ""}).text


def test_the_resume_button_relaunches_only_resumable_runs_with_their_mode(client, profile, master, tmp_path, monkeypatch):
    a, b, c, d = (mk_app(profile, master, tmp_path, n) for n in (1, 2, 3, 4))
    ra, _ = make_run(a, tmp_path, mode="submit", status="needs_input", questions=[Question(label="Q1 ?")])
    rb, _ = make_run(b, tmp_path, mode="dry_run", status="needs_input", questions=[Question(label="Q2 ?")])
    make_run(c, tmp_path, mode="submit", status="needs_email")
    make_run(d, tmp_path, mode="submit", status="submitted")
    calls = []

    async def fake_run(app_id, *, mode, trigger, headless=None, **kw):
        calls.append({"app_id": app_id, "mode": mode, "trigger": trigger, "headless": headless, **kw})

    monkeypatch.setattr(apply_service, "run", fake_run)
    html = client.get(answers_url(profile)).text
    assert "Reprendre les 2 candidatures en pause" in html
    form = resume_form(client, profile)
    assert sorted(form["app_id"]) == sorted([str(a.id), str(b.id)])
    assert form[f"mode_{a.id}"] == "submit" and form[f"mode_{b.id}"] == "dry_run" and form[f"run_{a.id}"] == str(ra.id)
    r = client.post(f"{answers_url(profile)}/resume", data=form)
    assert "Reprise lancée pour 2 candidatures" in r.text
    wait_idle()
    assert sorted((x["app_id"], x["mode"]) for x in calls) == sorted([(a.id, "submit"), (b.id, "dry_run")])
    assert all(x["trigger"] == "user" and x["expected_rev"] == 1 for x in calls)
    assert {x["app_id"]: x["expected_origin"] for x in calls} == {
        i.id: apply_service.origin_digest(webapp.db.get_job(i.job_id).origin_url) for i in (a, b)}


def test_the_resume_button_runs_the_applications_one_after_the_other(client, profile, master, tmp_path, monkeypatch):
    a, b = (mk_app(profile, master, tmp_path, n) for n in (1, 2))
    make_run(a, tmp_path, mode="submit", status="needs_input", questions=[Question(label="Q1 ?")])
    make_run(b, tmp_path, mode="submit", status="needs_input", questions=[Question(label="Q2 ?")])
    live, peak, order = [0], [0], []

    async def fake_run(app_id, *, mode, trigger, headless=None, **kw):
        live[0] += 1
        peak[0] = max(peak[0], live[0])
        order.append(app_id)
        await __import__("asyncio").sleep(0.02)
        live[0] -= 1

    monkeypatch.setattr(apply_service, "run", fake_run)
    client.post(f"{answers_url(profile)}/resume", data=resume_form(client, profile))
    wait_idle()
    assert sorted(order) == [a.id, b.id] and peak[0] == 1


def test_resume_with_nothing_paused_and_with_a_run_in_progress(client, profile, master, tmp_path, monkeypatch):
    assert "Aucune candidature à reprendre" in client.post(f"{answers_url(profile)}/resume").text
    a = mk_app(profile, master, tmp_path)
    make_run(a, tmp_path, mode="submit", status="needs_input", questions=[Question(label="Q ?")])
    APPLYING[a.id] = {"running": True, "error": None}
    assert "déjà en cours" in client.post(f"{answers_url(profile)}/resume", data=resume_form(client, profile)).text
    APPLYING.clear()


def test_resume_never_touches_another_profiles_applications(client, profile, master, tmp_path, monkeypatch):
    other = other_profile()
    job = make_job(other, title="Stage autre", company="AutreSoc", url="https://careers9.exemple.test/job")
    foreign = make_app(other, job, tmp_path, master_hash=save_confirmed(other.id).content_hash)
    make_run(foreign, tmp_path, mode="submit", status="needs_input", questions=[Question(label="Secret de Zoé ?")])
    calls = []

    async def fake_run(app_id, **kw):
        calls.append(app_id)

    monkeypatch.setattr(apply_service, "run", fake_run)
    assert "Secret de Zoé" not in client.get(answers_url(profile)).text
    forged = {"app_id": str(foreign.id), f"run_{foreign.id}": "1", f"rev_{foreign.id}": "1", f"mode_{foreign.id}": "submit",
              f"origin_{foreign.id}": apply_service.origin_digest(job.origin_url)}
    assert "introuvable" in client.post(f"{answers_url(profile)}/resume", data=forged).text
    assert calls == []


def test_the_answers_page_is_linked_from_the_queue_the_cards_and_the_profile(client, profile, master, tmp_path, monkeypatch):
    a = mk_app(profile, master, tmp_path)
    run, _ = make_run(a, tmp_path, mode="submit", status="needs_input", questions=[Question(label="Q ?")])
    patch_queue(monkeypatch, [item(a, "paused", pause_reason="input", last_run=run, questions=[Question(label="Q ?")])])
    href = answers_url(profile)
    s = soup(client.get("/apply").text)
    assert [x.text.strip() for x in s.select(f'.page-head a[href="{href}"]')] == ["Réponses aux formulaires"]
    assert [x.text.strip() for x in s.select(f'#paused a[href="{href}"]')] == ["Compléter mes réponses"]
    apply_routes._register_globals(webapp.templates)
    card = soup(webapp.templates.env.from_string('{% include "_apply_card.html" %}').render(
        item=item(a, "paused", pause_reason="input", last_run=run)))
    assert card.select_one(f'a[href="{href}"]').text.strip() == "Compléter mes réponses"
    card = soup(webapp.templates.env.from_string('{% include "_apply_card.html" %}').render(item=item(a, "to_validate")))
    assert not card.select(f'a[href="{href}"]')
    assert soup(client.get(f"/profiles/{profile.id}").text).select_one(f'.page-head a[href="{href}"]').text.strip() == "Réponses aux formulaires"


# ------------------------------------------------------------------ fix round 6 (web side)
def paused_pair(profile, master, tmp_path, monkeypatch):
    a, b = (mk_app(profile, master, tmp_path, n) for n in (1, 2))
    ra, _ = make_run(a, tmp_path, mode="submit", status="needs_input", questions=[Question(label="Q1 ?")])
    rb, _ = make_run(b, tmp_path, mode="dry_run", status="needs_input", questions=[Question(label="Q2 ?")])
    calls = []

    async def fake_run(app_id, *, mode, trigger, headless=None, **kw):
        calls.append({"app_id": app_id, "mode": mode, **kw})

    monkeypatch.setattr(apply_service, "run", fake_run)
    return a, b, ra, rb, calls


def test_the_answers_page_lists_each_resumable_application_with_its_mode_and_destination(client, profile, master, tmp_path, monkeypatch):
    a, b, ra, rb, _ = paused_pair(profile, master, tmp_path, monkeypatch)
    s = soup(client.get(answers_url(profile)).text)
    rows = {int(r.select_one("input[name=app_id]")["value"]): r for r in s.select("#resumable tbody tr")}
    assert set(rows) == {a.id, b.id}
    assert "Envoi" in rows[a.id].select("td")[1].text and "Essai à blanc" in rows[b.id].select("td")[1].text
    assert "careers1.exemple.test" in rows[a.id].text and "Société1" in rows[a.id].text
    for x, r_ in ((a, ra), (b, rb)):
        got = {i["name"]: i["value"] for i in rows[x.id].select("input[type=hidden]")}
        assert got == {"app_id": str(x.id), f"run_{x.id}": str(r_.id), f"rev_{x.id}": "1", f"mode_{x.id}": r_.mode,
                       f"origin_{x.id}": apply_service.origin_digest(webapp.db.get_job(x.job_id).origin_url)}


def test_bulk_resume_launches_only_the_rows_the_user_was_shown(client, profile, master, tmp_path, monkeypatch):
    a, b, ra, rb, calls = paused_pair(profile, master, tmp_path, monkeypatch)
    form = resume_form(client, profile)
    c = mk_app(profile, master, tmp_path, 3)  # pauses AFTER the page was rendered, in submit mode
    make_run(c, tmp_path, mode="submit", status="needs_input", questions=[Question(label="Q3 ?")])
    r = client.post(f"{answers_url(profile)}/resume", data=form)
    wait_idle()
    assert "Reprise lancée pour 2 candidatures" in r.text
    assert sorted(x["app_id"] for x in calls) == sorted([a.id, b.id]) and c.id not in {x["app_id"] for x in calls}


@pytest.mark.parametrize("what", ["run", "mode", "rev", "origin", "newer_run", "bad_origin", "missing_origin"])
def test_bulk_resume_skips_and_reports_a_row_that_changed(client, profile, master, tmp_path, monkeypatch, what):
    a, b, ra, rb, calls = paused_pair(profile, master, tmp_path, monkeypatch)
    form = resume_form(client, profile)
    if what == "run":
        form[f"run_{a.id}"] = str(ra.id + 50)
    elif what == "mode":
        form[f"mode_{a.id}"] = "dry_run"
    elif what == "rev":
        form[f"rev_{a.id}"] = "2"
    elif what == "origin":  # the job was re-resolved to another destination after the page was rendered
        with webapp.db.connect() as c_:
            c_.execute("UPDATE jobs SET origin_url=? WHERE id=?", ("https://evil.exemple.test/job", a.job_id))
    elif what == "newer_run":
        make_run(a, tmp_path, mode="submit", status="needs_input", questions=[Question(label="Autre ?")])
    elif what == "bad_origin":
        form[f"origin_{a.id}"] = "zz"
    else:
        del form[f"origin_{a.id}"]
    r = client.post(f"{answers_url(profile)}/resume", data=form)
    wait_idle()
    assert [x["app_id"] for x in calls] == [b.id]  # the untouched row still resumes
    assert "Reprise lancée pour 1 candidature" in r.text and "ignorée" in r.text and "Société1" in r.text


def test_bulk_resume_with_every_row_stale_launches_nothing(client, profile, master, tmp_path, monkeypatch):
    a, b, ra, rb, calls = paused_pair(profile, master, tmp_path, monkeypatch)
    form = resume_form(client, profile)
    with webapp.db.connect() as c_:
        c_.execute("UPDATE jobs SET origin_url=origin_url || '?x=1'")
    r = client.post(f"{answers_url(profile)}/resume", data=form)
    wait_idle()
    assert calls == [] and "Aucune reprise lancée" in r.text and "le lien de l'offre a changé" in __import__("html").unescape(r.text)


async def test_the_task_rechecks_the_destination_when_it_starts(profile, master, tmp_path, monkeypatch):
    """A job re-resolved between the click and the start: service.run refuses with a blocked run, nothing is opened."""
    from jobhunt.cv import service as cvs_

    a = mk_app(profile, master, tmp_path)
    cvs_.approve(a.id, 1)
    run_, _ = make_run(a, tmp_path, mode="submit", status="needs_input", questions=[Question(label="Q ?")])
    digest = apply_service.origin_digest(webapp.db.get_job(a.job_id).origin_url)
    with webapp.db.connect() as c_:
        c_.execute("UPDATE jobs SET origin_url='https://evil.exemple.test/job' WHERE id=?", (a.id,))
    APPLYING[a.id] = {"running": True, "error": None}
    await apply_routes._single(a.id, "submit", None, {"run_id": run_.id, "rev": 1, "origin": digest})
    last = apply_store.last_run(a.id)
    assert last.status == "blocked" and "le lien de l'offre a changé depuis l'affichage" in last.message
    APPLYING.clear()


@pytest.mark.parametrize("route, extra", [("resume", {"mode": "submit", "rev": "1"}), ("answers", {"q_0": "Ville ?", "a_0": "Albi"}),
                                          ("trust", {"host": "jobs.autre-site.test"})])
def test_single_resume_answers_and_trust_require_the_destination_digest(client, profile, master, tmp_path, monkeypatch, route, extra):
    a = mk_app(profile, master, tmp_path)
    if route == "trust":
        run_, _ = make_run(a, tmp_path, mode="submit", status="blocked", untrusted_host="jobs.autre-site.test")
    else:
        run_, _ = make_run(a, tmp_path, mode="submit", status="needs_input", questions=[Question(label="Ville ?")])
    calls = record_runs(monkeypatch)
    url = f"/applications/{a.id}/apply/{route}"
    data = {"run_id": str(run_.id), **extra}
    for origin in (NO_ORIGIN, "zz", "0" * 63):
        assert "Page périmée" in client.post(url, data={**data, "origin": origin}, follow_redirects=True).text
    wait_idle()
    assert calls == []
    good = client.post(url, data=data, follow_redirects=True).text  # the wrapper adds the right digest
    wait_idle()
    assert "Page périmée" not in good and len(calls) == 1
    assert calls[0]["expected_origin"] == apply_service.origin_digest(webapp.db.get_job(a.job_id).origin_url)


def test_a_wrong_digest_on_a_single_resume_is_refused_at_launch(client, profile, master, tmp_path, monkeypatch):
    a = mk_app(profile, master, tmp_path)
    run_, _ = make_run(a, tmp_path, mode="submit", status="needs_email")
    seen = []

    async def fake_run(app_id, *, mode, trigger, headless=None, expected_rev=None, expected_origin=None, **kw):
        seen.append(expected_origin)

    monkeypatch.setattr(apply_service, "run", fake_run)
    client.post(f"/applications/{a.id}/apply/resume", data={"run_id": str(run_.id), "mode": "submit", "rev": "1", "origin": "ab" * 32})
    wait_idle()
    assert seen == ["ab" * 32]  # handed to service.run, which compares it with the job's link


def test_the_paused_cards_forms_carry_the_digest(client, profile, master, tmp_path, monkeypatch):
    a_in, a_mail, a_host = (mk_app(profile, master, tmp_path, n) for n in (1, 2, 3))
    r_in, _ = make_run(a_in, tmp_path, mode="submit", status="needs_input", questions=QUESTIONS)
    r_mail, _ = make_run(a_mail, tmp_path, mode="submit", status="needs_email")
    r_host, _ = make_run(a_host, tmp_path, mode="submit", status="blocked", untrusted_host="jobs.autre-site.test")
    digest = "ab" * 32
    patch_queue(monkeypatch, [item(a_in, "paused", pause_reason="input", last_run=r_in, questions=QUESTIONS, origin_sha=digest),
                              item(a_mail, "paused", pause_reason="email", last_run=r_mail, origin_sha=digest),
                              item(a_host, "paused", pause_reason="host", last_run=r_host, untrusted_host="jobs.autre-site.test",
                                   origin_sha=digest)])
    s = soup(client.get("/apply").text)
    for aid, kind in ((a_in.id, "answers"), (a_mail.id, "resume"), (a_host.id, "trust")):
        assert s.select_one(f'form[action="/applications/{aid}/apply/{kind}"] input[name=origin][value="{digest}"]'), kind


def test_the_captcha_card_says_how_long_the_window_stays_open(client, profile, master, tmp_path, monkeypatch):
    a = mk_app(profile, master, tmp_path)
    run_, _ = make_run(a, tmp_path, mode="submit", status="needs_human")
    patch_queue(monkeypatch, [item(a, "paused", pause_reason="human", last_run=run_)])
    html = client.get("/apply").text
    assert ("Un CAPTCHA doit être résolu à la main. « Reprendre dans une fenêtre » rouvre le site : la fenêtre reste ouverte environ "
            "3 minutes, puis l'essai s'arrête s'il n'est pas résolu.") in html
    monkeypatch.setattr(get_settings(), "apply_captcha_wait_s", 60.0)
    assert "environ 1 minute, puis" in client.get("/apply").text


def test_the_failed_card_shows_the_message_and_both_resolve_buttons(client, profile, master, tmp_path, monkeypatch):
    a = failed_app(profile, master, tmp_path)
    run_ = apply_store.outstanding_attempt(a.id)
    assert run_.status == "needs_email"
    it = item(a, "failed", selectable=False, last_run=run_.model_copy(update={"message": "Erreur de validation du site"}),
              outstanding=run_.model_copy(update={"message": "Erreur de validation du site"}))
    patch_queue(monkeypatch, [it])
    s = soup(client.get("/apply").text)
    box = s.select_one("#failed")
    assert "Erreur de validation du site" in box.text and "Envoi non confirmé" in box.text
    assert [b.text.strip() for b in box.select(f'form[action="/applications/{a.id}/apply/resolve"] button')] == [
        "Elle a bien été envoyée", "Elle n'a pas été envoyée, réessayer plus tard"]


def test_queue_puts_blockers_first_with_a_count_line(client, profile, master, tmp_path, monkeypatch):
    apps = [mk_app(profile, master, tmp_path, n) for n in range(1, 7)]
    f1 = failed_app(profile, master, tmp_path, 7)
    run_in, _ = make_run(apps[0], tmp_path, mode="submit", status="needs_input", questions=QUESTIONS)
    items = [item(apps[1], "to_validate", selectable=True), item(apps[2], "to_validate", selectable=True),
             item(apps[0], "paused", pause_reason="input", last_run=run_in, questions=QUESTIONS),
             item(f1, "failed", last_run=apply_store.last_run(f1.id), outstanding=apply_store.outstanding_attempt(f1.id))]
    patch_queue(monkeypatch, items)
    s = soup(client.get("/apply").text)
    order = [sec["id"] for sec in s.select("section.panel")]
    assert order.index("failed") < order.index("paused") < order.index("to-validate")
    assert s.select_one("#queue-counts").text.strip() == "1 à vérifier · 1 en pause · 2 prêtes"
    patch_queue(monkeypatch, [item(apps[1], "to_validate", selectable=True)])
    s = soup(client.get("/apply").text)
    assert [sec["id"] for sec in s.select("section.panel")][0] == "to-validate" and s.select_one("#queue-counts").text.strip() == "1 prête"
    patch_queue(monkeypatch, [])
    assert soup(client.get("/apply").text).select_one("#queue-counts") is None


# ------------------------------------------------------------------ "Envoyées": counts, destination, cap
def sent_app(profile, master, tmp_path, n, *, when, url=None):
    a = mk_app(profile, master, tmp_path, n)
    with webapp.db.connect() as c:
        c.execute("UPDATE applications SET status='submitted', submitted_at=?, sent_origin_url=? WHERE id=?",
                  (webapp.db._iso(when), url, a.id))
    return cvstore.get_application(a.id)


def test_the_header_counts_sent_today_and_in_total_and_the_table_shows_destination_and_time(client, profile, master, tmp_path):
    from datetime import UTC, datetime, timedelta
    now = datetime.now(UTC)
    today = sent_app(profile, master, tmp_path, 1, when=now - timedelta(minutes=1), url="careers1.exemple.test/jobs/1")
    sent_app(profile, master, tmp_path, 2, when=now - timedelta(days=3), url="careers2.exemple.test/jobs/2")
    sent_app(profile, master, tmp_path, 3, when=now - timedelta(days=9), url=None)
    s = soup(client.get("/apply").text)
    assert s.select_one("#queue-counts").text.strip().endswith("1 envoyée aujourd'hui · 3 au total")
    rows = s.select("#sent tbody tr")
    assert len(rows) == 3 and [th.text for th in s.select("#sent th")][:3] == ["Offre", "Site de destination", "Envoyée le"]
    assert "careers1.exemple.test" in rows[0].text and "Société1" in rows[0].text  # newest first
    assert today.submitted_at.strftime("%d/%m/%Y %H:%M") in rows[0].text and "careers2.exemple.test" in rows[1].text
    assert "careers3.exemple.test" in rows[2].text  # no recorded destination: the job's link host


def test_the_sent_table_is_capped_at_the_200_most_recent_rows(client, profile, master, tmp_path, monkeypatch):
    from datetime import UTC, datetime, timedelta
    monkeypatch.setattr(apply_routes, "SENT_ROWS_MAX", 3)
    now = datetime.now(UTC)
    for n in range(1, 6):
        sent_app(profile, master, tmp_path, n, when=now - timedelta(hours=n))
    s = soup(client.get("/apply").text)
    assert len(s.select("#sent tbody tr")) == 3 and "3 plus récentes affichées" in s.select_one("#sent").text
    assert s.select_one("#sent .count").text == "5" and "5 au total" in s.select_one("#queue-counts").text
    assert [r.select_one("strong").text for r in s.select("#sent tbody tr")] == ["Société1", "Société2", "Société3"]
    assert apply_routes.SENT_ROWS_MAX == 3


def test_the_cap_constant_is_200():
    assert apply_routes.SENT_ROWS_MAX == 200
