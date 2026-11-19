from datetime import timedelta

from typer.testing import CliRunner

from jobhunt import db
from jobhunt.apply.diagnostics import failure_breakdown
from jobhunt.cli import app


NO_ACTION = "Aucune action possible sur cette page."
PRIVATE = "PRIVATE-EXAMPLE person@example.invalid https://tenant.example.invalid/job/123"


def test_failure_breakdown_ranks_distinct_applications_and_keeps_handoffs_out(profile, monkeypatch):
    now = db.now()
    stamp = db._iso(now - timedelta(hours=1))
    with db.connect() as conn:
        app_ids = {}
        for job_id, (name, ats) in enumerate([
            ("workday_a", "workday"), ("workday_b", "workday"), ("lever_a", "lever"),
            ("lever_b", "lever"), ("smart_a", "smartrecruiters"), ("unknown_a", PRIVATE), ("unknown_b", "WORKDAY"),
        ], 1):
            app_ids[name] = conn.execute(
                "INSERT INTO applications(profile_id, job_id, ats, company, created_at, updated_at) VALUES (?,?,?,?,?,?)",
                (profile.id, job_id, ats, PRIVATE, stamp, stamp),
            ).lastrowid

        def record(name, *, status="failed", message=NO_ACTION, mode="submit", clicked=False, finished=stamp):
            conn.execute(
                "INSERT INTO application_runs(application_id, rev, mode, trigger, status, message, started_at, finished_at,"
                " submit_clicked_at, result_json, out_dir) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (app_ids[name], 1, mode, "auto", status, message, stamp, finished, stamp if clicked else None, PRIVATE, PRIVATE),
            )

        for name in ("workday_a", "workday_a", "workday_b"):
            record(name)
        for name in ("lever_a", "lever_b"):
            record(name, message=f"Clic impossible sur « {name} » ({PRIVATE}).")
        for _ in range(5):
            record("smart_a")
        for name in ("unknown_a", "unknown_b"):
            record(name)
        record("workday_a", mode="dry_run")
        record("workday_a", status="needs_input", message=f"Informations manquantes : {PRIVATE}")
        record("workday_a", status="needs_email", message=PRIVATE)
        record("workday_a", status="needs_human", message="Un CAPTCHA doit être résolu à la main.")
        record("workday_a", status="needs_human", message="Un défi anti-robot doit être résolu à la main.")
        record("workday_a", status="needs_human", message=PRIVATE)
        record("workday_a", status="blocked", message=f"Le formulaire est hébergé sur un site non reconnu : {PRIVATE}")
        record("workday_a", clicked=True)
        record("workday_a", message=f"Planificateur indisponible ({PRIVATE}).")
        record("workday_a", message=PRIVATE)
        record("workday_a", message="Trop d'étapes : limite atteinte.", finished=db._iso(now - timedelta(days=31)))
        record("workday_a", message="Trop d'étapes : limite atteinte.", finished=db._iso(now + timedelta(days=1)))
        record("workday_a", status="running", finished=None)
        record("workday_a", status="submitted", clicked=True)
        record("workday_a", status="ready", mode="dry_run")

    def no_writer():
        raise AssertionError("diagnostics must not use the migratory database connection")

    monkeypatch.setattr(db, "connect", no_writer)
    groups = failure_breakdown(at=now)
    assert [(g.ats, g.reason, g.applications, g.runs) for g in groups if g.rankable] == [
        ("workday", "no_action", 2, 3), ("lever", "click_failed", 2, 2),
    ]
    counts = {(g.mode, g.ats, g.category, g.reason): (g.applications, g.runs) for g in groups}
    assert counts == {
        ("submit", "workday", "ats_engine", "no_action"): (2, 3),
        ("submit", "lever", "ats_engine", "click_failed"): (2, 2),
        ("submit", "smartrecruiters", "ats_engine", "no_action"): (1, 5),
        ("submit", "unknown", "ats_engine", "no_action"): (2, 2),
        ("dry_run", "workday", "ats_engine", "no_action"): (1, 1),
        ("submit", "workday", "human_handoff", "missing_input"): (1, 1),
        ("submit", "workday", "human_handoff", "email_verification"): (1, 1),
        ("submit", "workday", "human_handoff", "captcha"): (1, 2),
        ("submit", "workday", "human_handoff", "human_review"): (1, 1),
        ("submit", "workday", "guard_or_origin", "untrusted_origin"): (1, 1),
        ("submit", "workday", "submission_review", "submission_unconfirmed"): (1, 1),
        ("submit", "workday", "technical", "planner_unavailable"): (1, 1),
        ("submit", "workday", "unclassified", "unclassified_failure"): (1, 1),
    }
    result = CliRunner().invoke(app, ["apply", "diagnostics", "--db", str(db.db_path())])
    assert result.exit_code == 0, result.exception
    assert "workday" in result.output and "no_action" in result.output and "dry_run" in result.output
    for secret in ("PRIVATE", "person@", "example.invalid", str(db.db_path())):
        assert secret not in result.output


def test_diagnostics_does_not_create_a_missing_database(tmp_path):
    missing = tmp_path / "PRIVATE.sqlite3"
    result = CliRunner().invoke(app, ["apply", "diagnostics", "--db", str(missing)])
    assert result.exit_code == 1
    assert "Diagnostic indisponible" in result.output
    assert "PRIVATE" not in result.output
    assert not missing.exists()


def test_diagnostics_reports_an_empty_history():
    with db.connect():
        pass
    result = CliRunner().invoke(app, ["apply", "diagnostics"])
    assert result.exit_code == 0
    assert "Aucun échec ni pause" in result.output
