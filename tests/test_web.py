import asyncio
import time

import pytest
from fastapi.testclient import TestClient

from jobhunt import db
from jobhunt.web.app import RUNS, app

from pipeline_fakes import patch_registry

FORM = {"name": "Tom", "headline": "Étudiant mécatronique", "interests": "robotique, systèmes embarqués",
        "contract_types": ["stage", "alternance"], "locations": "Toulouse, remote", "cv_text": "CV <b>texte</b>"}


@pytest.fixture
def client(monkeypatch):
    patch_registry(monkeypatch, ["boarda", "boardb", "broken", "needskey"])
    RUNS.clear()
    with TestClient(app) as c:
        yield c


def test_profile_crud(client):
    assert client.get("/").status_code == 200
    r = client.post("/profiles", data=FORM, follow_redirects=False)
    assert r.status_code == 303
    pid = int(r.headers["location"].rsplit("/", 1)[1])
    p = db.get_profile(pid)
    assert p.interests == ["robotique", "systèmes embarqués"] and [c.value for c in p.contract_types] == ["stage", "alternance"]
    assert client.get(f"/profiles/{pid}").status_code == 200
    page = client.get(f"/profiles/{pid}/edit").text
    assert 'value="stage"' in page and "checked" in page and "CV &lt;b&gt;texte" in page
    client.post(f"/profiles/{pid}", data={**FORM, "name": "Tom 2"})
    assert db.get_profile(pid).name == "Tom 2"
    assert client.get("/profiles/999").status_code == 404
    assert client.get("/profiles/new").status_code == 200


def test_search_shortlist_and_actions(client):
    pid = int(client.post("/profiles", data=FORM, follow_redirects=False).headers["location"].rsplit("/", 1)[1])
    r = client.post(f"/profiles/{pid}/search", data={"max_queries": "3"})
    assert r.status_code == 200
    for _ in range(100):
        body = client.get(f"/profiles/{pid}/progress").text
        if "Terminée" in body:
            break
        time.sleep(0.1)
    assert "Terminée" in body and "Board A" in body and "Plan de recherche" in body
    assert "identifiants manquants" in body

    page = client.get(f"/profiles/{pid}/shortlist")
    assert page.status_code == 200
    assert "Board A" in page.text and "Board B" in page.text and "il y a 2 j" in page.text
    assert "Approuver la sélection" in page.text
    jobs = db.load_jobs(pid)
    emb = next(j for j in jobs if "mbarqu" in j.title)
    r = client.post(f"/profiles/{pid}/jobs/action", data={"action": "approve", "job_ids": [emb.id]}, follow_redirects=False)
    assert r.status_code == 303
    assert db.get_job(emb.id).status == "approved"
    r = client.post(f"/profiles/{pid}/jobs/action", data={"action": "reject", "job_ids": [emb.id]}, headers={"HX-Request": "true"})
    assert r.status_code == 204 and r.headers["HX-Refresh"] == "true" and db.get_job(emb.id).status == "rejected"
    assert client.post(f"/profiles/{pid}/jobs/action", data={"action": "zzz"}).status_code == 400
    # filters
    assert "Approuvé" not in client.get(f"/profiles/{pid}/shortlist?status=approved").text.split("<tbody>")[1]
    assert client.get(f"/profiles/{pid}/shortlist?min_score=99&sort=freshness&source=boarda&contract_type=stage&max_age_days=7").status_code == 200
    assert client.get(f"/profiles/{pid}/shortlist?min_score=abc").status_code == 200


def test_sources_page(client):
    r = client.get("/sources")
    assert r.status_code == 200 and "Board A" in r.text and "france_travail_client_id" in r.text


def test_profile_edit_invalidates_scores_without_losing_decisions(client, profile):
    from jobhunt.dedupe import MergedJob
    from pipeline_fakes import posting

    job = MergedJob.from_posting(posting("boarda", "1"))
    job.score, job.score_json, job.status = 0, {"prefiltered": True}, "rejected"
    db.save_jobs(profile.id, [job])
    client.post(f"/profiles/{profile.id}", data={**FORM, "contract_types": ["cdi"], "experience_years": "5"})
    loaded = db.get_job(job.id)
    assert loaded.score is None and loaded.score_json == {} and loaded.status == "rejected"


# ---------------------------------------------------------------- planification
@pytest.fixture
def sched_client(client, monkeypatch, tmp_path):
    from jobhunt.web import app as webapp
    from test_schedule import Runner

    runner = Runner()
    monkeypatch.setattr(webapp, "SCHEDULE_KW", dict(repo=tmp_path, home=tmp_path, runner=runner, uid=501,
                                                      check_platform=False))
    client.runner = runner
    return client


def test_schedule_page_not_installed(sched_client):
    html = sched_client.get("/schedule").text
    assert "Activer (07:30)" in html and "Planification" in html
    assert "/schedule" in sched_client.get("/sources").text  # nav link
    r = sched_client.post("/schedule/install", follow_redirects=True)
    assert "Planification activée" in r.text and "07:30" in r.text and "Lancer maintenant" in r.text


def test_schedule_add_remove_run(sched_client):
    sched_client.post("/schedule/install")
    r = sched_client.post("/schedule/add", data={"time": "19:00"}, follow_redirects=True)
    assert "19:00" in r.text and "Heure 19:00 ajoutée" in r.text
    r = sched_client.post("/schedule/remove", data={"time": "07:30"}, follow_redirects=True)
    assert "retirée" in r.text
    r = sched_client.post("/schedule/remove", data={"time": "19:00"}, follow_redirects=True)
    assert "uninstall" in r.text  # last time refused
    r = sched_client.post("/schedule/add", data={"time": "99:99"}, follow_redirects=True)
    assert "heure invalide" in r.text
    r = sched_client.post("/schedule/run", follow_redirects=True)
    assert "Recherche lancée, résultats dans ~25 min." in r.text
    assert any(c[1] == "kickstart" for c in sched_client.runner.calls)
    r = sched_client.post("/schedule/uninstall", follow_redirects=True)
    assert "Activer (07:30)" in r.text


def test_schedule_run_when_not_installed_shows_error(sched_client):
    r = sched_client.post("/schedule/run", follow_redirects=True)
    assert "big-penalty daily" in r.text
