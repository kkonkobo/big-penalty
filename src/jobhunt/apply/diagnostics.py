"""Local aggregate of existing run outcomes, not a retry or submission signal."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from jobhunt import db
from jobhunt.origin import KNOWN_ATS_IDS


@dataclass(frozen=True)
class FailureGroup:
    mode: str
    ats: str
    category: str
    reason: str
    applications: int
    runs: int

    @property
    def rankable(self) -> bool:
        return self.category == "ats_engine" and self.ats in KNOWN_ATS_IDS and self.applications >= 2


def failure_breakdown(*, days: int = 30, path: Path | None = None, at: datetime | None = None) -> list[FailureGroup]:
    """Return only fixed categories and counts, ranked by distinct affected applications then runs.

    Dry runs and submits stay separate. ATS attribution is the saved application label, not a
    claim about the page that failed. Legacy messages are matched against known engine messages
    inside SQLite; raw messages, details, and IDs are never returned to Python.
    """
    if days < 1:
        raise ValueError("days must be positive")
    end = at or db.now()
    ats_ids = sorted(KNOWN_ATS_IDS | {"careers_site"})
    placeholders = ",".join("?" for _ in ats_ids)
    sql = f"""
        WITH normalized AS (
            SELECT r.application_id, r.mode,
                CASE WHEN a.ats COLLATE BINARY IN ({placeholders}) THEN a.ats ELSE 'unknown' END AS ats,
                CASE
                    WHEN r.status = 'needs_input' THEN 'missing_input'
                    WHEN r.status = 'needs_email' THEN 'email_verification'
                    WHEN r.status = 'needs_human' AND (instr(r.message, 'CAPTCHA') > 0
                        OR r.message = 'Un défi anti-robot doit être résolu à la main.') THEN 'captcha'
                    WHEN r.status = 'needs_human' THEN 'human_review'
                    WHEN r.status = 'submitted' THEN 'sent_not_recorded'
                    WHEN instr(r.message, 'Le site signale une erreur de saisie après le clic : ') = 1 THEN 'validation_after_click'
                    WHEN r.submit_clicked_at IS NOT NULL
                        OR (r.nav_clicked_at IS NOT NULL AND r.message = 'interrompu')
                        OR instr(r.message, 'Envoi non confirmé : ') = 1 THEN 'submission_unconfirmed'
                    WHEN r.status = 'blocked' AND instr(r.message, 'Le formulaire est hébergé sur un site non reconnu : ') = 1
                        THEN 'untrusted_origin'
                    WHEN r.status = 'blocked' AND instr(r.message, 'Lien de l''offre inexploitable : ') = 1 THEN 'bad_origin'
                    WHEN r.status = 'blocked' AND r.message = 'Offre clôturée' THEN 'posting_closed'
                    WHEN r.status = 'blocked' THEN 'blocked'
                    WHEN r.message = 'Aucune action possible sur cette page.' THEN 'no_action'
                    WHEN instr(r.message, 'Clic impossible sur « ') = 1 THEN 'click_failed'
                    WHEN r.message = 'Trop d''étapes : limite atteinte.' THEN 'step_limit'
                    WHEN r.message = 'Page de confirmation inattendue' THEN 'unexpected_confirmation'
                    WHEN instr(r.message, 'Planificateur indisponible (') = 1 THEN 'planner_unavailable'
                    WHEN instr(r.message, 'Erreur technique (') = 1 THEN 'technical_error'
                    WHEN r.message IN ('Interrompu.', 'interrompu') THEN 'interrupted'
                    ELSE 'unclassified_failure'
                END AS reason
            FROM application_runs r JOIN applications a ON a.id = r.application_id
            WHERE julianday(r.finished_at) BETWEEN julianday(?) AND julianday(?)
                AND r.mode IN ('dry_run', 'submit')
                AND (r.status IN ('needs_input', 'needs_email', 'needs_human', 'blocked', 'failed')
                    OR (r.status = 'submitted' AND instr(r.message, 'Envoyée sur le site, mais le statut n''a pas pu être mis à jour : ') = 1))
        )
        SELECT mode, ats,
            CASE
                WHEN reason IN ('missing_input', 'email_verification', 'captcha', 'human_review') THEN 'human_handoff'
                WHEN reason IN ('untrusted_origin', 'bad_origin', 'posting_closed', 'blocked') THEN 'guard_or_origin'
                WHEN reason IN ('validation_after_click', 'submission_unconfirmed', 'sent_not_recorded') THEN 'submission_review'
                WHEN reason IN ('no_action', 'click_failed', 'step_limit', 'unexpected_confirmation') THEN 'ats_engine'
                WHEN reason IN ('planner_unavailable', 'technical_error', 'interrupted') THEN 'technical'
                ELSE 'unclassified'
            END AS category,
            reason, COUNT(DISTINCT application_id), COUNT(*)
        FROM normalized
        GROUP BY mode, ats, reason
    """
    uri = (path if path is not None else db.db_path()).absolute().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as conn:
        groups = [FailureGroup(*row) for row in conn.execute(sql, (*ats_ids, db._iso(end - timedelta(days=days)), db._iso(end)))]
    return sorted(groups, key=lambda group: (not group.rankable, -group.applications, -group.runs,
                                             group.mode, group.ats, group.reason))
