import asyncio
import time
from threading import Event

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
def sched_client(monkeypatch):
    from datetime import datetime

    from jobhunt import schedule

    launches = []
    monkeypatch.setattr(schedule, "_launch_daily", launches.append)
    scheduler = schedule.Scheduler
    monkeypatch.setattr(schedule, "Scheduler", lambda: scheduler(clock=lambda: datetime(2026, 11, 19, 12).timestamp()))
    with TestClient(app) as client:
        client.launches = launches
        yield client


def test_schedule_page_defaults(sched_client):
    html = sched_client.get("/schedule").text
    assert "Planification activée" in html and "07:30" in html and "Lancer maintenant" in html
    assert "Prochain lancement" in html
    assert "Le lancement automatique n’a lieu que si l’application est ouverte à l’heure prévue." in html
    assert "/schedule" in sched_client.get("/sources").text


@pytest.mark.parametrize("has_snapshot", [False, True], ids=["first-use", "existing-schedule"])
def test_schedule_page_renders_while_scheduler_holds_lock(monkeypatch, lock_backend, has_snapshot):
    from datetime import datetime

    from jobhunt import schedule
    from jobhunt.config import get_settings

    try_lock, unlock = lock_backend
    monkeypatch.setattr(schedule, "try_lock", try_lock)
    monkeypatch.setattr(schedule, "unlock", unlock)
    monkeypatch.setattr(schedule, "LOCK_TIMEOUT", 0.05)
    path = get_settings().data_dir / "schedule.json"
    if has_snapshot:
        schedule.set_times(["08:15"])
    held, release = Event(), Event()
    read_schedule = schedule._read_schedule

    def delayed_read(*args):
        held.set()
        assert release.wait(5)
        return read_schedule(*args)

    monkeypatch.setattr(schedule, "_read_schedule", delayed_read)
    scheduler = schedule.Scheduler
    monkeypatch.setattr(schedule, "Scheduler", lambda: scheduler(clock=lambda: datetime(2026, 11, 19, 12).timestamp()))
    with TestClient(app) as client:
        try:
            assert held.wait(2)
            html = client.get("/schedule").text
            assert "Planification activée" in html and "Prochain lancement" in html
            assert ("08:15" if has_snapshot else "07:30") in html
            assert path.exists() is has_snapshot
        finally:
            release.set()
    assert path.exists()


def test_schedule_add_remove_run(sched_client):
    r = sched_client.post("/schedule/add", data={"time": "19:00"}, follow_redirects=True)
    assert "19:00" in r.text and "Heure 19:00 ajoutée" in r.text
    r = sched_client.post("/schedule/remove", data={"time": "07:30"}, follow_redirects=True)
    assert "retirée" in r.text
    r = sched_client.post("/schedule/remove", data={"time": "19:00"}, follow_redirects=True)
    assert "Aucune heure prévue" in r.text
    r = sched_client.post("/schedule/add", data={"time": "99:99"}, follow_redirects=True)
    assert "heure invalide" in r.text
    sched_client.post("/schedule/add", data={"time": "19:00"})
    r = sched_client.post("/schedule/enabled", data={"enabled": "false"}, follow_redirects=True)
    assert "Planification désactivée" in r.text and "Prochain lancement : aucun" in r.text and "19:00" in r.text
    r = sched_client.post("/schedule/run", follow_redirects=True)
    assert "Recherche lancée" in r.text and len(sched_client.launches) == 1
    r = sched_client.post("/schedule/enabled", data={"enabled": "true"}, follow_redirects=True)
    assert "Planification activée" in r.text and "19:00" in r.text


def test_schedule_writes_require_same_origin(sched_client, monkeypatch):
    from jobhunt.web import app as webapp

    monkeypatch.setattr(webapp, "REQUIRE_ORIGIN_EVIDENCE", True)
    for action in ("add", "remove", "enabled", "run"):
        assert sched_client.post(f"/schedule/{action}").status_code == 403
        assert sched_client.post(f"/schedule/{action}", headers={"Origin": "https://example.test"}).status_code == 403
    assert not sched_client.launches
    assert sched_client.post("/schedule/run", headers={"Origin": "http://testserver"}, follow_redirects=False).status_code == 303


def test_single_row_triage_returns_only_that_row(client):
    pid = int(client.post("/profiles", data=FORM, follow_redirects=False).headers["location"].rsplit("/", 1)[1])
    client.post(f"/profiles/{pid}/search", data={"max_queries": "3"})
    for _ in range(100):
        if "Terminée" in client.get(f"/profiles/{pid}/progress").text:
            break
        time.sleep(0.1)
    a, b = db.load_jobs(pid)[:2]
    # A stray checked box in the bulk form must not ride along with a one-row action.
    r = client.post(f"/profiles/{pid}/jobs/action", data={"action": "approve", "job_id": str(a.id), "job_ids": [b.id]},
                    headers={"HX-Request": "true"})
    assert r.status_code == 200 and f'id="job-{a.id}"' in r.text and "Approuvé" in r.text and r.text.count("<tr") == 1
    assert db.get_job(a.id).status == "approved" and db.get_job(b.id).status == "new"
    r = client.post(f"/profiles/{pid}/jobs/action", data={"action": "reset", "job_id": str(a.id)}, headers={"HX-Request": "true"})
    assert db.get_job(a.id).status == "new" and "Approuver" in r.text
    other = int(client.post("/profiles", data=FORM, follow_redirects=False).headers["location"].rsplit("/", 1)[1])
    r = client.post(f"/profiles/{other}/jobs/action", data={"action": "reject", "job_id": str(a.id)}, headers={"HX-Request": "true"})
    assert r.status_code == 404 and db.get_job(a.id).status == "new"


def test_today_page_summarises_each_profile(client):
    assert "Commencez par un profil" in client.get("/").text
    pid = int(client.post("/profiles", data=FORM, follow_redirects=False).headers["location"].rsplit("/", 1)[1])
    page = client.get("/").text
    assert "Aujourd'hui" in page and f'href="/profiles/{pid}/shortlist' in page and "CV à valider" in page


# ------------------------------------------------------------------ CSRF / origin guard (app-wide middleware)
class TestOriginGuard:
    """The strict production rules: conftest relaxes them for the other tests, this class turns them back on."""
    URL = "/profiles"

    @pytest.fixture(autouse=True)
    def strict(self, monkeypatch):
        from jobhunt.web import app as webapp

        monkeypatch.setattr(webapp, "REQUIRE_ORIGIN_EVIDENCE", True)

    def post(self, client, headers=None, **kw):
        return client.post(self.URL, data=FORM, headers=headers or {}, follow_redirects=False, **kw)

    def test_a_write_without_origin_evidence_is_refused(self, client):
        r = self.post(client)  # curl / a script: neither Origin nor Sec-Fetch-Site
        assert r.status_code == 403 and "refusée" in r.text and db.list_profiles() == []

    def test_same_origin_write_is_accepted(self, client):
        assert self.post(client, {"Origin": "http://testserver"}).status_code == 303
        assert self.post(client, {"Sec-Fetch-Site": "same-origin"}).status_code == 303

    @pytest.mark.parametrize("host", ["127.0.0.1:8765", "localhost:8765", "localhost", "[::1]:8765", "[::1]", "127.0.0.1"])
    def test_loopback_hosts_are_accepted_for_reads_and_writes(self, client, host):
        assert client.get("/", headers={"Host": host}).status_code == 200
        assert self.post(client, {"Host": host, "Origin": f"http://{host}"}).status_code == 303

    @pytest.mark.parametrize("host", ["evil.example.test", "evil.example.test:8765", "127.0.0.1.evil.test", "192.168.1.20:8765", ""])
    def test_foreign_host_is_refused_on_get_and_post(self, client, host):
        r = client.get("/", headers={"Host": host})  # DNS rebinding reads (CV PDFs, screenshots) too
        assert r.status_code == 403 and "refusée" in r.text
        r = self.post(client, {"Host": host, "Origin": f"http://{host}", "Sec-Fetch-Site": "same-origin"})
        assert r.status_code == 403 and db.list_profiles() == []

    def test_testserver_is_not_allowed_without_the_test_hook(self, client, monkeypatch):
        from jobhunt.web import app as webapp

        monkeypatch.setattr(webapp, "EXTRA_ALLOWED_HOSTS", set())
        assert client.get("/").status_code == 403
        assert client.get("/", headers={"Host": "127.0.0.1:8765"}).status_code == 200

    def test_cross_origin_post_is_refused(self, client):
        host = {"Host": "127.0.0.1:8765"}
        for origin in ("http://evil.example.test", "http://127.0.0.1:9999", "https://127.0.0.1:8765", "http://localhost:8765", "null", ""):
            assert self.post(client, {**host, "Origin": origin}).status_code == 403, origin
        assert db.list_profiles() == []
        assert self.post(client, {**host, "Origin": "http://127.0.0.1:8765"}).status_code == 303

    @pytest.mark.parametrize("site, code", [("same-origin", 303), ("none", 403), ("cross-site", 403), ("same-site", 403)])
    def test_sec_fetch_site(self, client, site, code):
        assert self.post(client, {"Sec-Fetch-Site": site}).status_code == code

    def test_a_cross_site_signal_beats_a_matching_origin(self, client):
        assert self.post(client, {"Origin": "http://testserver", "Sec-Fetch-Site": "cross-site"}).status_code == 403

    def test_safe_methods_only_need_a_good_host(self, client):
        assert client.get("/", headers={"Origin": "http://evil.example.test", "Sec-Fetch-Site": "cross-site"}).status_code == 200
        assert client.head("/", headers={"Sec-Fetch-Site": "cross-site"}).status_code in (200, 405)
        assert client.options("/", headers={"Host": "evil.example.test"}).status_code == 403

    def test_other_methods_are_filtered_too(self, client):
        assert client.delete("/profiles/1", headers={"Origin": "http://evil.example.test"}).status_code == 403
        assert client.put("/profiles/1", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
        assert client.delete("/profiles/1").status_code == 403  # no evidence at all

    def test_extra_hosts_from_the_environment(self, client, monkeypatch):
        assert self.post(client, {"Host": "mac.lan:8765", "Origin": "http://mac.lan:8765"}).status_code == 403
        monkeypatch.setenv("JOBHUNT_WEB_ALLOWED_HOSTS", "mac.lan, Other.Example.test")
        assert client.get("/", headers={"Host": "mac.lan:8765"}).status_code == 200
        assert self.post(client, {"Host": "mac.lan:8765", "Origin": "http://mac.lan:8765"}).status_code == 303
        assert self.post(client, {"Host": "other.example.test", "Sec-Fetch-Site": "same-origin"}).status_code == 303
        assert self.post(client, {"Host": "mac.lan:8765", "Origin": "http://evil.example.test"}).status_code == 403
