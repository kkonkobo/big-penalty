"""`jobhunt daily`: lock, headless, summary, log, notification. No network, no LLM, no real notification."""

import os
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from jobhunt import daily, db
from jobhunt.cli import app
from jobhunt.config import get_settings
from jobhunt.dedupe import MergedJob
from jobhunt.models import JobPosting, Profile
from jobhunt.pipeline import run_search

from pipeline_fakes import patch_registry
from test_pipeline import FakeLLM

CLOCK = datetime(2026, 11, 17, 7, 30, tzinfo=UTC)


def seed(profile_id, sid, score, title, *, hours_old=1):
    seen = datetime.now(UTC) - timedelta(hours=hours_old)
    j = MergedJob.from_posting(JobPosting(source="indeed", source_id=sid, url=f"https://x.test/{sid}", title=title,
                                          company="Acme", location="Toulouse", posted_at=seen))
    j.score = score
    db.save_jobs(profile_id, [j])


def fake_search(per_profile=None, *, fail=()):
    """Stand-in for pipeline.run_search: records calls, seeds jobs, may fail."""
    calls = []

    async def search(pid, **kw):
        calls.append((pid, kw.get("headless")))
        if pid in fail:
            return SimpleNamespace(run_id=99, raw_postings=0, stage="error", errors=["boom"])
        for i, (score, title) in enumerate((per_profile or {}).get(pid, [])):
            seed(pid, f"{pid}-{i}", score, title)
        return SimpleNamespace(run_id=pid * 10, raw_postings=7, stage="done", errors=[])

    search.calls = calls
    return search


def run(profile_ids=None, search=None, **kw):
    out, notes = [], []
    report = daily.run_daily(profile_ids, search=search, emit=out.append, now=lambda: CLOCK,
                             notify_fn=lambda t, b: notes.append((t, b)), **kw)
    return report, out, notes


def test_summary_counts_top5_log_headless_and_notification(profile):
    jobs = [(95, "Stage A"), (50, "Stage B"), (72, "Stage C"), (80, "Stage D"), (60, "Stage E"), (65, "Stage F"), (10, "Stage G")]
    search = fake_search({profile.id: jobs})
    report, out, notes = run(search=search)
    assert search.calls == [(profile.id, True)]  # headless passed explicitly to the pipeline
    assert "JOBHUNT_HEADLESS" not in os.environ and get_settings().headless is False  # nothing leaked
    s = report.profiles[0]
    assert (s.new, s.high) == (7, 3) and len(s.top) == 5
    assert [t.title for t in s.top][:2] == ["Stage A", "Stage D"]  # by rank (all equally fresh -> by score)
    assert out[0] == f"Profil {profile.id} (Léa) : 7 nouvelles offres aujourd'hui, 3 ≥ 70"
    assert out[1] == "  1. Stage A — Acme · fit 95 · aujourd'hui" and len(out) == 6
    assert notes == [("Big Penalty", "7 nouvelles offres, 3 ≥70")]
    log = (get_settings().data_dir / "logs" / "daily-2026-11-17.log").read_text().splitlines()
    assert len(log) == 1 and f"profil={profile.id}" in log[0] and "nouvelles=7" in log[0] and "statut=ok" in log[0]


def test_one_failing_profile_does_not_stop_the_others(profile):
    p2 = db.save_profile(Profile(name="Tom", headline="Étudiant"))
    search = fake_search({p2.id: [(90, "Stage Z")]}, fail={profile.id})
    report, out, notes = run(search=search)
    assert [c[0] for c in search.calls] == [profile.id, p2.id]
    assert report.failed and report.profiles[0].error == "boom" and report.profiles[1].new == 1
    assert "ERREUR — boom" in out[0] and notes[0][1].endswith("(erreur)")
    log = (get_settings().data_dir / "logs" / "daily-2026-11-17.log").read_text()
    assert "statut=ERREUR boom" in log and "nouvelles=1" in log


def test_exception_from_search_is_an_error_not_a_crash(profile):
    async def boom(pid, **kw):
        raise RuntimeError("réseau")

    report, out, _ = run(search=boom)
    assert report.failed and "RuntimeError: réseau" in out[0]


def test_selected_profiles_and_unknown_id(profile):
    p2 = db.save_profile(Profile(name="Tom", headline="Étudiant"))
    search = fake_search()
    run([p2.id], search=search)
    assert [c[0] for c in search.calls] == [p2.id]
    with pytest.raises(ValueError, match="999"):
        run([999], search=search)


def test_no_profiles_is_a_noop():
    search = fake_search()
    report, out, notes = run(search=search)
    assert search.calls == [] and notes == [] and not report.failed and "Aucun profil" in out[0]


def _hold_lock_in_subprocess(path):
    """A real other process holding the flock until its stdin closes."""
    code = ("import os,sys;from jobhunt.filelock import try_lock;fd=os.open(sys.argv[1],os.O_RDWR|os.O_CREAT);"
            "assert try_lock(fd);os.write(fd,b'123 other run\\n');print('ok',flush=True);sys.stdin.read()")
    proc = subprocess.Popen([sys.executable, "-c", code, str(path)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    assert proc.stdout.readline().strip() == "ok"
    return proc


def test_lock_held_by_another_process_blocks_overlap(profile):
    lock = get_settings().data_dir / daily.LOCK_NAME
    lock.parent.mkdir(parents=True, exist_ok=True)
    proc = _hold_lock_in_subprocess(lock)
    try:
        search = fake_search()
        report, out, notes = run(search=search)
        assert report.locked and search.calls == [] and notes == [] and not report.failed
        assert "ignoré" in (get_settings().data_dir / "logs" / "daily-2026-11-17.log").read_text()
    finally:
        proc.stdin.close()
        proc.wait(timeout=10)
    assert lock.exists()  # persistent file, never unlinked
    report, _, _ = run(search=fake_search())  # holder gone: kernel released the lock, no staleness delay
    assert not report.locked


def test_lock_file_is_never_unlinked_and_records_pid(tmp_path):
    with daily.daily_lock(tmp_path) as path:
        assert path.read_text().split()[0] == str(os.getpid())
        with pytest.raises(daily.DailyLocked):  # flock is per open file description: a second open conflicts
            with daily.daily_lock(tmp_path):
                pass
    assert path.exists()
    with daily.daily_lock(tmp_path):  # released on exit
        pass


def test_old_lock_file_without_holder_does_not_block(tmp_path):
    lock = tmp_path / daily.LOCK_NAME
    lock.write_text("123 crashed\n")
    old = time.time() - 30 * 3600
    os.utime(lock, (old, old))
    with daily.daily_lock(tmp_path):
        pass


def test_lock_released_even_when_run_blows_up(tmp_path):
    with pytest.raises(KeyError):
        with daily.daily_lock(tmp_path):
            raise KeyError("x")
    with daily.daily_lock(tmp_path):
        pass


def test_end_to_end_with_real_pipeline_and_fake_sources(monkeypatch, profile):
    patch_registry(monkeypatch, ["boarda", "boardb"])

    async def search(pid, **kw):
        return await run_search(pid, llm=FakeLLM(), **kw)

    report, out, notes = run(search=search)
    s = report.profiles[0]
    assert not report.failed and s.new > 0 and s.high > 0 and s.raw_postings > 0
    assert notes[0][1] == f"{s.new} nouvelles offres, {s.high} ≥70"


# ------------------------------------------------------------ notification
def test_notify_runs_osascript_on_darwin():
    calls = []
    ok = daily.notify("jobhunt", '3 "nouvelles" offres, 1 ≥70', platform="darwin", env={},
                      run=lambda cmd, **kw: calls.append(cmd))
    assert ok and calls == [["osascript", "-e",
                             'display notification "3 \\"nouvelles\\" offres, 1 ≥70" with title "jobhunt"']]


def test_notify_skipped_off_darwin_or_when_disabled():
    calls = []
    assert not daily.notify("t", "b", platform="linux", env={}, run=lambda *a, **k: calls.append(a))
    assert not daily.notify("t", "b", platform="darwin", env={"JOBHUNT_NO_NOTIFY": "1"}, run=lambda *a, **k: calls.append(a))
    assert calls == []


def test_notify_never_raises():
    def boom(*a, **k):
        raise FileNotFoundError("osascript")

    assert daily.notify("t", "b", platform="darwin", env={}, run=boom) is False


def test_notification_body_wording():
    assert daily.notification_body(12, 4) == "12 nouvelles offres, 4 ≥70"
    assert daily.notification_body(1, 0) == "1 nouvelle offre, 0 ≥70"
    assert daily.notification_body(0, 0, failed=True) == "0 nouvelle offre, 0 ≥70 (erreur)"


# ------------------------------------------------------------ CLI exit codes
@pytest.mark.parametrize("report,code", [
    (daily.DailyReport(profiles=[daily.ProfileSummary(1, "a")]), 0),
    (daily.DailyReport(locked=True), 0),
    (daily.DailyReport(profiles=[daily.ProfileSummary(1, "a", error="x")]), 1),
])
def test_cli_daily_exit_codes(monkeypatch, report, code):
    monkeypatch.setattr(daily, "run_daily", lambda ids, **kw: report)
    assert CliRunner().invoke(app, ["daily"]).exit_code == code


def test_cli_daily_unknown_profile(monkeypatch):
    monkeypatch.setenv("JOBHUNT_NO_NOTIFY", "1")
    assert CliRunner().invoke(app, ["daily", "999"]).exit_code == 2
