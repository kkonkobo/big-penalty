"""Stop reasons, overlays in the outline, and `apply report`. Synthetic data only."""

from __future__ import annotations

import json
from datetime import timedelta

from typer.testing import CliRunner

from apply_fakes import ScriptedPlanner, cv_pdf, fast, page, sample_facts, server  # noqa: F401
from jobhunt import db
from jobhunt.apply.engine import Uploads, run_application
from jobhunt.apply.page_outline import OUTLINE_FILE, RUN_FILE, SCREENSHOT_FILE, note_stop_reason, safe_reason, save_run_record
from jobhunt.cli import app

COOKIE_PAGE = """<title>Careers</title><h1>Jobs</h1><p>Hello</p>
<div id="cookie-banner" class="consent-wall" style="position:fixed;left:0;top:0;width:100%;height:100%;background:#fff">
We use cookies: please accept. <button>Accept</button></div>
<div id="chat-widget" style="position:fixed;right:0;bottom:0;width:60px;height:60px">chat</div>"""


class Boom:
    async def plan(self, *args, **kwargs):
        raise RuntimeError("boom for person@example.invalid code 987654")


async def test_exception_reason_and_overlays_are_recorded(page, server, fast, cv_pdf, tmp_path):
    server.html["/cookies"] = COOKIE_PAGE
    out = tmp_path / "run"
    res = await run_application(page, url=server.url("cookies"), facts=sample_facts(), planner=Boom(),
                                uploads=Uploads(cv_pdf=cv_pdf), mode="dry_run", out_dir=out, max_steps=3)
    assert res.status == "failed"
    record = json.loads((out / RUN_FILE).read_text(encoding="utf-8"))
    assert "RuntimeError: boom for <email>" in record["reason"]
    assert "person@" not in json.dumps(record) and "987654" not in json.dumps(record)
    assert (out / SCREENSHOT_FILE).is_file()
    overlays = json.loads((out / OUTLINE_FILE).read_text(encoding="utf-8"))["overlays"]
    banner = next(item for item in overlays if item["id"] == "cookie-banner")
    assert banner["covers_center"] is True and banner["text"].startswith("We use cookies") and len(banner["text"]) <= 80
    assert any(item["id"] == "chat-widget" and item["covers_center"] is False for item in overlays)


def test_run_record_merges_engine_reason_with_service_facts(tmp_path):
    save_run_record(tmp_path, status="failed", reason="entry: ApplyError: nope", step=2)
    note_stop_reason(tmp_path, "entry step: ApplyError: later note")
    save_run_record(tmp_path, status="failed", final_url="https://x.example.test/a?t=1", refused_request="GET https://y.example.test/",
                    reason="Aucune action possible.")
    record = json.loads((tmp_path / RUN_FILE).read_text(encoding="utf-8"))
    assert record["reason"] == "entry: ApplyError: nope | entry step: ApplyError: later note | Aucune action possible."
    assert record["step"] == 2 and record["final_url"] == "https://x.example.test/a" and record["refused_request"].startswith("GET ")


def test_safe_reason_masks_personal_shapes():
    text = safe_reason("fail p.q@example.invalid 123456 https://a.example.test/x?token=abc tok-1234567890123456789012345")
    assert "example.invalid" not in text and "123456" not in text and "token=" not in text and "tok-123" not in text


def test_apply_report_clusters_and_reads_run_folders(profile, tmp_path):
    now = db.now()
    folder = tmp_path / "runs" / "1"
    folder.mkdir(parents=True)
    (folder / RUN_FILE).write_text(json.dumps({"reason": "RuntimeError: boom", "refused_request": "POST https://w.example.test/api"}))
    (folder / OUTLINE_FILE).write_text(json.dumps({"overlays": [{"tag": "div", "id": "cookie-banner", "text": "We use cookies",
                                                               "covers_center": True}]}))
    (folder / SCREENSHOT_FILE).write_bytes(b"\x89PNG")
    with db.connect() as conn:
        def application(job_id, ats="lever"):
            stamp = db._iso(now)
            return conn.execute("INSERT INTO applications(profile_id, job_id, ats, company, job_title, created_at, updated_at)"
                                " VALUES (?,?,?,?,?,?,?)", (profile.id, job_id, ats, "Acme", "Intern", stamp, stamp)).lastrowid

        def run(app_id, status, code, url, started, out="", mode="submit"):
            payload = json.dumps({"status": status, "diagnostic_code": code, "final_url": url})
            conn.execute("INSERT INTO application_runs(application_id, rev, mode, trigger, status, message, result_json, out_dir,"
                         " started_at, finished_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                         (app_id, 1, mode, "auto", status, "msg", payload, out, db._iso(started), db._iso(started)))

        run(application(1), "needs_human", "apply_target_unverified", "https://jobs.lever.co/acme/1?x=1", now, str(folder))
        run(application(2), "needs_human", "apply_target_unverified", "https://jobs.lever.co/acme/2", now - timedelta(minutes=5))
        run(application(3), "failed", "form_actions_missing", "https://acme.example.test/jobs", now - timedelta(minutes=10))
        run(application(4), "submitted", "", "https://jobs.lever.co/acme/4", now - timedelta(minutes=12))
        run(application(5), "failed", "old", "https://jobs.lever.co/acme/5", now - timedelta(days=3))
        run(application(6), "failed", "dry", "https://jobs.lever.co/acme/6", now, mode="dry_run")
    result = CliRunner().invoke(app, ["apply", "report", "--last-batch"])
    assert result.exit_code == 0, result.output
    out = result.output
    assert "Unsent runs since" in out and ": 3" in out.splitlines()[0]
    assert "[2] apply_target_unverified  @ lever" in out and "[1] form_actions_missing  @ acme.example.test" in out
    assert "reason : RuntimeError: boom" in out and "refused: POST https://w.example.test/api" in out
    assert "div#cookie-banner [covers centre]" in out and str(folder / SCREENSHOT_FILE) in out
    assert "x=1" not in out and "failed/old" not in out
    since = CliRunner().invoke(app, ["apply", "report", "--since", db._iso(now - timedelta(days=4)), "--mode", "all"])
    assert ": 5" in since.output.splitlines()[0]


def test_apply_report_does_not_create_a_missing_database(tmp_path):
    missing = tmp_path / "nope.sqlite3"
    result = CliRunner().invoke(app, ["apply", "report", "--db", str(missing)])
    assert result.exit_code == 1 and not missing.exists()


def test_safe_reason_drops_the_browser_call_log_and_its_typed_value():
    error = ('TimeoutError: Locator.fill: Timeout 800ms exceeded.\nCall log:\n  - waiting for locator("#a")\n'
             '  - fill("Tr0ub4dor-zebra-Kite")\n  - locator resolved to <input id="a" value="Jeanne"/>')
    text = safe_reason(error)
    assert text.startswith("TimeoutError: Locator.fill: Timeout") and "zebra" not in text and "Jeanne" not in text
