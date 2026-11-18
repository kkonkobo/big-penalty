"""application_runs (apply/store.py) + migration 4 + the JOBHUNT_APPLY_* settings."""

from __future__ import annotations

import os
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from jobhunt import db
from jobhunt.apply import store as astore
from jobhunt.apply.errors import ApplyError
from jobhunt.apply.types import RunResult
from jobhunt.config import Settings, get_settings
from jobhunt.cv import service as cvs
from jobhunt.cv import store
from jobhunt.freshness import start_of_today_paris

from apply_cv_helpers import make_app, make_job
from cv_fakes import save_confirmed


@pytest.fixture
def app(profile, tmp_path):
    m = save_confirmed(profile.id)
    return make_app(profile, make_job(profile), tmp_path, master_hash=m.content_hash)


def out_dir(run_id):
    return get_settings().data_dir / "runs" / str(run_id)


# ------------------------------------------------------------------ migration
def test_v3_database_migrates_to_v4_keeping_data(tmp_path, monkeypatch):
    path = tmp_path / "v3.sqlite3"
    conn = sqlite3.connect(path)
    for stmt in db._statements("".join(db.MIGRATIONS[:3])):
        conn.execute(stmt)
    conn.execute("PRAGMA user_version=3")
    ts = "2026-01-01T00:00:00+00:00"
    conn.execute("INSERT INTO profiles(id, name, data, created_at, updated_at) VALUES (1,'p','{}',?,?)", (ts, ts))
    conn.execute("INSERT INTO applications(profile_id, job_id, job_title, company, status, tailored_json, flags_json, current_rev,"
                 " created_at, updated_at) VALUES (1,5,'T','C','approved','{\"headline\":\"h\",\"summary\":\"s\"}','[]',1,?,?)", (ts, ts))
    conn.execute("INSERT INTO application_revisions(application_id, rev, tailored_json, created_at) VALUES (1,1,'{\"headline\":\"h\",\"summary\":\"s\"}',?)", (ts,))
    conn.commit()
    conn.close()
    monkeypatch.setattr(db, "DB_PATH_OVERRIDE", path)
    from jobhunt.cv import store
    a = store.get_application(1)
    assert a.job_title == "T" and a.status == "approved" and a.approved_by is None and a.submitted_at is None
    with db.connect() as c:
        assert c.execute("PRAGMA user_version").fetchone()[0] == len(db.MIGRATIONS) >= 4
        cols = {r["name"] for r in c.execute("PRAGMA table_info(application_runs)")}
        assert {"application_id", "rev", "mode", "trigger", "status", "message", "result_json", "out_dir", "started_at",
                "finished_at"} <= cols
        assert c.execute("SELECT name FROM sqlite_master WHERE name='idx_runs_app'").fetchone()
    run = astore.start_run(1, 1, "dry_run", "user", out_dir)
    assert run.status == "running"
    with db.connect() as c:  # migration 5
        assert "submit_clicked_at" in {r["name"] for r in c.execute("PRAGMA table_info(application_runs)")}
        assert "sent_origin_url" in {r["name"] for r in c.execute("PRAGMA table_info(applications)")}
        assert {r["name"] for r in c.execute("PRAGMA table_info(apply_reservations)")} == {
            "id", "profile_id", "application_id", "run_id", "trigger", "role_key", "url_key", "created_at", "released_at", "pending"}
        assert {"nav_clicked_at", "pid"} <= {r["name"] for r in c.execute("PRAGMA table_info(application_runs)")}  # migration 6
    assert a.sent_origin_url is None and run.submit_clicked_at is None and run.nav_clicked_at is None and run.pid == os.getpid()


def test_v5_database_migrates_to_v6_additively(tmp_path, monkeypatch):
    """Migrations 4 and 5 are already applied on the developer's database: migration 6 only adds columns."""
    path = tmp_path / "v5.sqlite3"
    conn = sqlite3.connect(path)
    for stmt in db._statements("".join(db.MIGRATIONS[:5])):
        conn.execute(stmt)
    conn.execute("PRAGMA user_version=5")
    ts = "2026-01-01T00:00:00+00:00"
    conn.execute("INSERT INTO apply_reservations(profile_id, application_id, run_id, trigger, role_key, url_key, created_at)"
                 " VALUES (1,2,3,'user','r','u',?)", (ts,))
    conn.commit()
    conn.close()
    monkeypatch.setattr(db, "DB_PATH_OVERRIDE", path)
    (r,) = astore.active_reservations(1)
    assert r["application_id"] == 2 and r["pending"] == 0  # an old reservation is firm


# ------------------------------------------------------------------ settings
def test_apply_settings_defaults():
    s = Settings(data_dir=get_settings().data_dir)
    assert (s.apply_auto, s.apply_auto_min_score, s.apply_daily_cap) == (False, 60, 10)
    assert (s.apply_prepare_min_score, s.apply_prepare_daily_cap, s.apply_max_steps) == (60, 15, 12)


def test_apply_settings_from_env(monkeypatch, tmp_path):
    monkeypatch.setenv("JOBHUNT_DATA_DIR", str(tmp_path / "envdata"))
    monkeypatch.setenv("JOBHUNT_APPLY_AUTO", "1")
    monkeypatch.setenv("JOBHUNT_APPLY_DAILY_CAP", "3")
    monkeypatch.setenv("JOBHUNT_APPLY_AUTO_MIN_SCORE", "72.5")
    s = get_settings.__wrapped__()  # bypass the lru_cache: the shared settings object stays untouched
    assert s.apply_auto is True and s.apply_daily_cap == 3 and s.apply_auto_min_score == 72.5


# ------------------------------------------------------------------ runs
def test_run_lifecycle(app):
    run = astore.start_run(app.id, 1, "dry_run", "user", out_dir)
    assert run.status == "running" and run.out_dir == str(out_dir(run.id)) and run.finished_at is None and run.result is None
    done = astore.finish_run(run.id, RunResult(status="ready", message="Prêt à envoyer", final_url="https://x.test/apply"))
    assert done.status == "ready" and done.message == "Prêt à envoyer" and done.finished_at is not None
    assert done.result.final_url == "https://x.test/apply" and done.mode == "dry_run" and done.trigger == "user"
    assert astore.get_run(run.id) == done and astore.last_run(app.id) == done
    assert astore.get_run(999) is None and astore.last_run(999) is None
    with pytest.raises(ApplyError):
        astore.finish_run(999, RunResult(status="failed"))


def test_only_one_running_run_per_application(app):
    first = astore.start_run(app.id, 1, "submit", "auto", out_dir)
    with pytest.raises(ApplyError, match="Un envoi est déjà en cours pour cette candidature"):
        astore.start_run(app.id, 1, "dry_run", "user", out_dir)
    assert len(astore.list_runs(app.id)) == 1
    astore.finish_run(first.id, RunResult(status="failed", message="x"))
    assert astore.start_run(app.id, 1, "dry_run", "user", out_dir).id != first.id  # finished -> a new run is allowed


def test_stale_running_run_is_marked_interrupted(app, monkeypatch):
    old = astore.start_run(app.id, 1, "submit", "auto", out_dir)
    real = datetime.now(UTC)
    monkeypatch.setattr(astore, "pid_alive", lambda pid: False)
    with db.connect() as c:
        c.execute("UPDATE application_runs SET pid=NULL")  # owner unknown: only the clock tells
    monkeypatch.setattr(astore, "now", lambda: real + timedelta(minutes=44))
    with pytest.raises(ApplyError):
        astore.start_run(app.id, 1, "submit", "auto", out_dir)  # 44 min: still considered alive
    monkeypatch.setattr(astore, "now", lambda: real + timedelta(minutes=46))
    new = astore.start_run(app.id, 1, "submit", "auto", out_dir)
    stale = astore.get_run(old.id)
    assert stale.status == "failed" and stale.message == "interrompu" and stale.finished_at is not None
    assert new.status == "running" and [r.id for r in astore.list_runs(app.id)] == [new.id, old.id]  # newest first


def test_latest_runs_per_application_and_profile(profile, app, tmp_path):
    other = make_app(profile, make_job(profile, title="Autre", company="Autre SA", url="https://autre.test/1"), tmp_path,
                     master_hash=app.master_hash)
    a1 = astore.start_run(app.id, 1, "dry_run", "user", out_dir)
    astore.finish_run(a1.id, RunResult(status="ready"))
    a2 = astore.start_run(app.id, 1, "submit", "user", out_dir)
    assert set(astore.latest_runs(profile.id)) == {app.id}
    assert astore.latest_runs(profile.id)[app.id].id == a2.id
    b1 = astore.start_run(other.id, 1, "dry_run", "user", out_dir)
    assert {k: v.id for k, v in astore.latest_runs(profile.id).items()} == {app.id: a2.id, other.id: b1.id}
    assert astore.latest_runs(profile.id + 1) == {}


def reserve(app, run, *, trigger="auto", role="r", url="u", cap=10, at=None, profile_id=None):
    astore.reserve_send(run.id, profile_id=profile_id or app.profile_id, application_id=app.id, trigger=trigger, role_key=role,
                        url_key=url, daily_cap=cap, at=at)


def fake_reservation(profile_id, n, at, *, trigger="auto"):
    """A reservation of some other application (no FK): quota / duplicate tests."""
    with db.connect() as c:
        c.execute("INSERT INTO apply_reservations(profile_id, application_id, run_id, trigger, role_key, url_key, created_at)"
                  " VALUES (?,?,?,?,?,?,?)", (profile_id, 10_000 + n, 10_000 + n, trigger, f"role{n}", f"url{n}", db._iso(at)))


def test_count_auto_submitted_counts_reservations_across_paris_midnight(profile):
    now = datetime(2026, 7, 10, 22, 30, tzinfo=UTC)  # 00:30 on 11 July in Paris (UTC+2)
    midnight = start_of_today_paris(now)
    assert midnight == datetime(2026, 7, 10, 22, 0, tzinfo=UTC)
    fake_reservation(profile.id, 1, midnight - timedelta(minutes=1))  # 23:59 Paris the day before: yesterday
    fake_reservation(profile.id, 2, midnight)  # exactly midnight: today
    fake_reservation(profile.id, 3, midnight + timedelta(hours=5))
    fake_reservation(profile.id, 4, midnight + timedelta(hours=1), trigger="user")  # manual: not counted
    assert astore.count_auto_submitted(profile.id, midnight) == 2
    assert astore.count_auto_submitted(profile.id, midnight + timedelta(hours=6)) == 0
    assert astore.count_auto_submitted(profile.id, midnight - timedelta(days=1)) == 3
    assert astore.count_auto_submitted(profile.id + 1, midnight) == 0
    astore.release_reservation(10_002)  # a human said "not sent": no longer counted
    assert astore.count_auto_submitted(profile.id, midnight) == 1


def test_click_before_midnight_counts_for_that_day_only(profile, app):
    before = datetime(2026, 7, 10, 21, 59, tzinfo=UTC)  # 23:59 Paris, 10 July
    for n in range(2):
        fake_reservation(profile.id, n, before)
    run = astore.start_run(app.id, 1, "submit", "auto", out_dir)
    with pytest.raises(ApplyError, match="Plafond de 2"):  # same Paris day: full
        reserve(app, run, cap=2, at=before + timedelta(seconds=30))
    reserve(app, run, cap=2, at=before + timedelta(minutes=3))  # 00:02 on 11 July: the counter restarted


def test_reserve_send_sets_the_click_time_and_blocks_a_second_attempt(profile, app):
    run = astore.start_run(app.id, 1, "submit", "user", out_dir)
    assert run.submit_clicked_at is None and not astore.has_unconfirmed_click(app.id)
    reserve(app, run, trigger="user")
    assert astore.get_run(run.id).submit_clicked_at is not None and astore.has_unconfirmed_click(app.id)
    astore.finish_run(run.id, RunResult(status="failed"))
    again = astore.start_run(app.id, 1, "submit", "user", out_dir)
    with pytest.raises(ApplyError, match="déjà été tenté"):
        reserve(app, again, role="other", url="other")


def test_two_concurrent_sends_with_nine_reserved_only_the_first_gets_the_last_slot(profile, app, tmp_path):
    for n in range(9):
        fake_reservation(profile.id, n, datetime.now(UTC))
    (tmp_path / "x").mkdir()
    other = make_app(profile, make_job(profile, title="Autre", company="Autre SA", url="https://autre.test/1"), tmp_path / "x",
                     master_hash=app.master_hash)
    r1, r2 = astore.start_run(app.id, 1, "submit", "auto", out_dir), astore.start_run(other.id, 1, "submit", "auto", out_dir)
    reserve(app, r1, role="a", url="ua", cap=10)
    with pytest.raises(ApplyError, match="Plafond de 10"):
        reserve(other, r2, role="b", url="ub", cap=10)
    assert astore.get_run(r2.id).submit_clicked_at is None and astore.count_auto_submitted(profile.id, datetime(2000, 1, 1, tzinfo=UTC)) == 10
    reserve(other, r2, trigger="user", role="b", url="ub", cap=10)  # a manual send ignores the cap


def test_reservation_blocks_same_role_or_same_url_of_another_application(profile, app, tmp_path):
    fake_reservation(profile.id, 1, datetime.now(UTC))
    run = astore.start_run(app.id, 1, "submit", "user", out_dir)
    with pytest.raises(ApplyError, match="même offre"):
        reserve(app, run, trigger="user", role="role1", url="free")
    with pytest.raises(ApplyError, match="même offre"):
        reserve(app, run, trigger="user", role="free", url="url1")
    reserve(app, run, trigger="user", role="|", url="")  # empty role / URL never collide
    fake_reservation(profile.id + 1, 1, datetime.now(UTC))  # another profile does not interfere
    assert [r["application_id"] for r in astore.active_reservations(profile.id)] == [10_001, app.id]


def test_release_reservation_frees_quota_duplicates_and_the_unconfirmed_click(profile, app):
    run = astore.start_run(app.id, 1, "submit", "auto", out_dir)
    reserve(app, run, role="r1", url="u1")
    astore.finish_run(run.id, RunResult(status="failed"))
    assert astore.has_unconfirmed_click(app.id) and astore.count_auto_submitted(profile.id, datetime(2000, 1, 1, tzinfo=UTC)) == 1
    assert astore.release_reservation(app.id) == 1 and astore.release_reservation(app.id) == 0
    assert not astore.has_unconfirmed_click(app.id) and astore.active_reservations(profile.id) == []
    assert astore.get_run(run.id).submit_clicked_at is not None  # the history is kept


def test_stale_run_that_reached_the_click_fails_the_application_and_refuses_the_new_run(profile, app, monkeypatch):
    a = cvs.approve(app.id, 1)
    old = astore.start_run(a.id, 1, "submit", "user", out_dir)
    reserve(a, old, trigger="user")
    real = datetime.now(UTC)
    monkeypatch.setattr(astore, "now", lambda: real + timedelta(minutes=46))
    with pytest.raises(ApplyError, match="déjà en cours"):
        astore.start_run(a.id, 1, "submit", "user", out_dir)  # 46 min but its owner (this process) is alive: never taken over
    monkeypatch.setattr(astore, "pid_alive", lambda pid: False)
    with pytest.raises(ApplyError, match="Un envoi précédent a peut-être abouti : vérifier sur le site de l'entreprise"):
        astore.start_run(a.id, 1, "submit", "user", out_dir)
    stale = astore.get_run(old.id)
    assert stale.status == "failed" and stale.message == "interrompu" and stale.submit_clicked_at is not None
    cur = store.get_application(a.id)
    assert cur.status == "failed" and cur.approved_rev is None and "Envoi non confirmé" in cur.notes
    assert [r.id for r in astore.list_runs(a.id)] == [old.id]  # no new run row was created
    assert astore.has_unconfirmed_click(a.id)


def test_stale_run_without_click_does_not_touch_the_application(profile, app, monkeypatch):
    a = cvs.approve(app.id, 1)
    astore.start_run(a.id, 1, "submit", "user", out_dir)
    real = datetime.now(UTC)
    monkeypatch.setattr(astore, "now", lambda: real + timedelta(minutes=50))
    monkeypatch.setattr(astore, "pid_alive", lambda pid: False)
    assert astore.start_run(a.id, 1, "submit", "user", out_dir).status == "running"
    assert store.get_application(a.id).status == "approved"


# ------------------------------------------------------------------ pending reservations (navigation clicks)
def reserve_nav(app, run, **kw):
    kw.setdefault("trigger", "user")
    kw.setdefault("role", "r")
    kw.setdefault("url", "u")
    astore.reserve_send(run.id, profile_id=app.profile_id, application_id=app.id, trigger=kw["trigger"], role_key=kw["role"],
                        url_key=kw["url"], daily_cap=kw.get("cap", 10), pending=True)


def only_reservation(profile_id):
    with db.connect() as c:
        rows = c.execute("SELECT * FROM apply_reservations WHERE profile_id=?", (profile_id,)).fetchall()
    assert len(rows) == 1
    return rows[0]


def test_pending_reservation_is_released_when_the_run_ends_without_submit_clicked(profile, app):
    a = cvs.approve(app.id, 1)
    run = astore.start_run(a.id, 1, "submit", "user", out_dir)
    reserve_nav(a, run)
    got = astore.get_run(run.id)
    assert got.nav_clicked_at is not None and got.submit_clicked_at is None
    assert astore.has_unconfirmed_click(a.id) and len(astore.active_reservations(profile.id)) == 1
    done = astore.finish_and_settle(run.id, RunResult(status="needs_input", message="question"))
    assert done.status == "needs_input" and only_reservation(profile.id)["released_at"] is not None
    assert astore.active_reservations(profile.id) == [] and not astore.has_unconfirmed_click(a.id)
    assert store.get_application(a.id).status == "approved"  # nothing was sent: the application is untouched


def test_pending_reservation_is_kept_and_firm_when_submit_clicked_is_true(profile, app):
    a = cvs.approve(app.id, 1)
    run = astore.start_run(a.id, 1, "submit", "user", out_dir)
    reserve_nav(a, run)
    astore.finish_and_settle(run.id, RunResult(status="failed", message="plantage", submit_clicked=True))
    r = only_reservation(profile.id)
    assert r["released_at"] is None and r["pending"] == 0
    cur = store.get_application(a.id)
    assert cur.status == "failed" and "Envoi non confirmé" in cur.notes
    assert astore.get_run(run.id).submit_clicked_at is not None and astore.has_unconfirmed_click(a.id)


def test_a_final_click_reservation_is_firm_even_if_the_result_says_not_clicked(profile, app):
    a = cvs.approve(app.id, 1)
    run = astore.start_run(a.id, 1, "submit", "user", out_dir)
    reserve(a, run, trigger="user")
    astore.finish_and_settle(run.id, RunResult(status="blocked"))
    assert only_reservation(profile.id)["released_at"] is None and store.get_application(a.id).status == "failed"


def test_submitted_run_marks_application_and_job_and_keeps_the_reservation(profile, app):
    a = cvs.approve(app.id, 1)
    run = astore.start_run(a.id, 1, "submit", "user", out_dir)
    reserve_nav(a, run)
    reserve(a, run, trigger="user")
    astore.finish_and_settle(run.id, RunResult(status="submitted", message="ok"), sent_origin_url="https://careers.exemple.test/jobs/42")
    cur = store.get_application(a.id)
    assert cur.status == "submitted" and cur.sent_origin_url == "https://careers.exemple.test/jobs/42"
    assert db.get_job(a.job_id).status == "applied"
    assert only_reservation(profile.id)["released_at"] is None


def test_submitted_run_whose_application_changed_is_flagged_not_resendable(profile, app):
    a = cvs.approve(app.id, 1)
    run = astore.start_run(a.id, 1, "submit", "user", out_dir)
    reserve(a, run, trigger="user")
    store.set_status(a.id, "draft")  # edited meanwhile: cannot become submitted
    done = astore.finish_and_settle(run.id, RunResult(status="submitted", message="ok"))
    assert "ne pas la renvoyer" in done.message and done.status == "submitted"
    assert store.get_application(a.id).status == "failed"


def test_reserve_send_is_idempotent_per_run_and_upgrades_pending_to_firm(profile, app):
    run = astore.start_run(app.id, 1, "submit", "auto", out_dir)
    reserve_nav(app, run, trigger="auto", cap=1)
    first = only_reservation(profile.id)
    reserve_nav(app, run, trigger="auto", cap=1)  # same run again: no error even with a full cap, no second row
    assert only_reservation(profile.id)["id"] == first["id"] and first["pending"] == 1
    assert astore.get_run(run.id).submit_clicked_at is None
    reserve(app, run, trigger="auto", cap=1)  # final click: upgrade
    up = only_reservation(profile.id)
    assert up["id"] == first["id"] and up["pending"] == 0 and astore.get_run(run.id).submit_clicked_at is not None
    reserve(app, run, trigger="auto", cap=1)
    reserve_nav(app, run, trigger="auto", cap=1)
    assert only_reservation(profile.id)["pending"] == 0  # a late navigation call never downgrades


def test_a_pending_reservation_blocks_a_second_application_with_the_same_role_or_url(profile, app, tmp_path):
    (tmp_path / "x").mkdir()
    other = make_app(profile, make_job(profile, title="Autre", company="Autre SA", url="https://autre.test/1"), tmp_path / "x",
                     master_hash=app.master_hash)
    r1 = astore.start_run(app.id, 1, "submit", "user", out_dir)
    r2 = astore.start_run(other.id, 1, "submit", "user", out_dir)
    reserve_nav(app, r1, role="same-role", url="same-url")
    with pytest.raises(ApplyError, match="même offre"):
        reserve_nav(other, r2, role="same-role", url="x")
    with pytest.raises(ApplyError, match="même offre"):
        reserve(other, r2, trigger="user", role="y", url="same-url")
    assert astore.get_run(r2.id).nav_clicked_at is None
    astore.finish_and_settle(r1.id, RunResult(status="needs_input"))  # released: the role is free again
    reserve_nav(other, r2, role="same-role", url="same-url")


def test_an_earlier_unreleased_reservation_of_the_same_application_blocks_but_never_the_same_run(profile, app):
    r1 = astore.start_run(app.id, 1, "submit", "user", out_dir)
    reserve(app, r1, trigger="user")
    astore.finish_run(r1.id, RunResult(status="failed"))
    r2 = astore.start_run(app.id, 1, "submit", "user", out_dir)
    with pytest.raises(ApplyError, match="déjà été tenté"):
        reserve_nav(app, r2)


def test_reserve_is_refused_when_the_run_is_not_running_or_not_the_newest(profile, app):
    r1 = astore.start_run(app.id, 1, "submit", "user", out_dir)
    astore.finish_run(r1.id, RunResult(status="failed"))
    with pytest.raises(ApplyError, match="plus actif"):
        reserve_nav(app, r1)
    r2 = astore.start_run(app.id, 1, "submit", "user", out_dir)
    with db.connect() as c:  # a newer run exists (e.g. a takeover): r2 is no longer the newest
        c.execute("INSERT INTO application_runs(application_id, rev, mode, trigger, status, started_at) VALUES (?,?,?,?,?,?)",
                  (app.id, 1, "dry_run", "user", "running", db._iso(datetime.now(UTC))))
    with pytest.raises(ApplyError, match="plus actif"):
        reserve_nav(app, r2)
    with pytest.raises(ApplyError, match="plus actif"):
        astore.reserve_send(9999, profile_id=profile.id, application_id=app.id, trigger="user", role_key="r", url_key="u", daily_cap=1)
    assert astore.active_reservations(profile.id) == []


def test_finish_and_settle_is_one_transaction(profile, app, monkeypatch):
    a = cvs.approve(app.id, 1)
    run = astore.start_run(a.id, 1, "submit", "user", out_dir)
    reserve_nav(a, run)

    def boom(c, app_id, note):
        raise RuntimeError("disque plein")

    monkeypatch.setattr(store, "tx_force_failed", boom)
    with pytest.raises(RuntimeError):
        astore.finish_and_settle(run.id, RunResult(status="failed", submit_clicked=True))
    cur = astore.get_run(run.id)  # nothing half-written: the run is still running, the reservation still pending, the app approved
    assert cur.status == "running" and cur.finished_at is None and cur.result is None
    assert only_reservation(profile.id)["pending"] == 1 and store.get_application(a.id).status == "approved"


def test_finish_and_settle_and_finish_run_only_act_on_a_running_run(profile, app):
    run = astore.start_run(app.id, 1, "submit", "user", out_dir)
    astore.finish_and_settle(run.id, RunResult(status="failed"))
    with pytest.raises(ApplyError, match="plus actif"):
        astore.finish_and_settle(run.id, RunResult(status="submitted"))
    with pytest.raises(ApplyError, match="plus actif"):
        astore.finish_run(run.id, RunResult(status="ready"))
    assert astore.get_run(run.id).status == "failed" and store.get_application(app.id).status == "draft"


def test_policy_ignores_the_asking_runs_own_reservation(profile, app):
    a = cvs.approve(app.id, 1)
    run = astore.start_run(a.id, 1, "submit", "auto", out_dir)
    reserve_nav(a, run, trigger="auto")
    day = datetime(2000, 1, 1, tzinfo=UTC)
    assert astore.count_auto_submitted(profile.id, day) == 1 and astore.count_auto_submitted(profile.id, day, except_run_id=run.id) == 0
    assert astore.has_unconfirmed_click(a.id) and not astore.has_unconfirmed_click(a.id, except_run_id=run.id)


# ------------------------------------------------------------------ stale takeover and the owner's pid
def test_a_running_row_with_a_live_owner_is_never_taken_over_even_when_old(profile, app, monkeypatch):
    run = astore.start_run(app.id, 1, "submit", "user", out_dir)
    monkeypatch.setattr(astore, "now", lambda: datetime.now(UTC) + timedelta(hours=5))
    for _ in range(2):
        with pytest.raises(ApplyError, match="Un envoi est déjà en cours pour cette candidature"):
            astore.start_run(app.id, 1, "submit", "user", out_dir)
    assert astore.get_run(run.id).status == "running" and len(astore.list_runs(app.id)) == 1


def test_a_young_run_whose_owner_is_dead_is_taken_over_at_once(profile, app, monkeypatch):
    old = astore.start_run(app.id, 1, "dry_run", "user", out_dir)
    monkeypatch.setattr(astore, "pid_alive", lambda pid: False)
    new = astore.start_run(app.id, 1, "dry_run", "user", out_dir)
    assert astore.get_run(old.id).message == "interrompu" and new.id != old.id


def test_dead_owner_with_a_navigation_click_fails_the_application_and_makes_the_reservation_firm(profile, app, monkeypatch):
    a = cvs.approve(app.id, 1)
    old = astore.start_run(a.id, 1, "submit", "user", out_dir)
    reserve_nav(a, old)
    monkeypatch.setattr(astore, "pid_alive", lambda pid: False)
    with pytest.raises(ApplyError, match="Un envoi précédent a peut-être abouti"):
        astore.start_run(a.id, 1, "submit", "user", out_dir)
    assert store.get_application(a.id).status == "failed" and [r.id for r in astore.list_runs(a.id)] == [old.id]
    r = only_reservation(profile.id)
    assert r["released_at"] is None and r["pending"] == 0 and astore.get_run(old.id).status == "failed"


def test_pid_alive_is_true_for_this_process_and_false_for_nothing():
    assert astore.pid_alive(os.getpid()) and not astore.pid_alive(None) and not astore.pid_alive(0)


# ------------------------------------------------------------------ resolution bound to one attempt
def failed_after_click(a, *, nav=False):
    run = astore.start_run(a.id, 1, "submit", "user", out_dir)
    (reserve_nav if nav else reserve)(a, run, **({} if nav else {"trigger": "user"}))
    astore.finish_and_settle(run.id, RunResult(status="failed", submit_clicked=True))
    return run


def test_resolve_attempt_not_sent_releases_and_drafts_in_one_go(profile, app):
    a = cvs.approve(app.id, 1)
    run = failed_after_click(a, nav=True)
    astore.resolve_attempt(a.id, sent=False, run_id=run.id)
    assert store.get_application(a.id).status == "draft" and astore.active_reservations(profile.id) == []


def test_resolve_attempt_sent_marks_submitted_job_applied_and_keeps_the_reservation(profile, app):
    a = cvs.approve(app.id, 1)
    run = failed_after_click(a)
    astore.resolve_attempt(a.id, sent=True, run_id=run.id)
    assert store.get_application(a.id).status == "submitted" and db.get_job(a.job_id).status == "applied"
    assert len(astore.active_reservations(profile.id)) == 1


def test_resolve_attempt_refuses_a_stale_run_and_an_application_that_never_clicked(profile, app):
    a = cvs.approve(app.id, 1)
    old = failed_after_click(a)
    with pytest.raises(ApplyError, match="périmée"):
        astore.resolve_attempt(a.id, sent=False, run_id=old.id + 1)
    assert store.get_application(a.id).status == "failed" and len(astore.active_reservations(profile.id)) == 1
    with pytest.raises(ApplyError, match="introuvable"):
        astore.resolve_attempt(app.id + 99, sent=True)
    # a failed application whose last run never clicked, with nothing reserved
    with db.connect() as c:
        c.execute("UPDATE apply_reservations SET released_at='2026-01-01T00:00:00+00:00'")
        c.execute("UPDATE application_runs SET submit_clicked_at=NULL, nav_clicked_at=NULL")
    with pytest.raises(ApplyError, match="jamais atteint"):
        astore.resolve_attempt(a.id, sent=True)
    with pytest.raises(ApplyError, match="jamais atteint"):
        astore.resolve_attempt(a.id, sent=False, run_id=old.id)
    assert store.get_application(a.id).status == "failed"


def test_resolve_attempt_is_atomic(profile, app, monkeypatch):
    a = cvs.approve(app.id, 1)
    run = failed_after_click(a)

    def boom(c, app_id, new, **kw):
        raise RuntimeError("x")

    monkeypatch.setattr(store, "tx_set_status", boom)
    with pytest.raises(RuntimeError):
        astore.resolve_attempt(a.id, sent=False, run_id=run.id)
    assert len(astore.active_reservations(profile.id)) == 1 and store.get_application(a.id).status == "failed"
