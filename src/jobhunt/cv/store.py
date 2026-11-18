"""`applications` + `application_revisions` access: ONE active application per (profile, job), immutable revisions,
approval bound to a revision. `applications.tailored_json/flags_json/cv_path` mirror the current revision."""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from pydantic import BaseModel, Field

from jobhunt.cv.check import Flag
from jobhunt.cv.errors import ApprovalError, CVError, StaleRevision, TransitionError
from jobhunt.cv.render import FitNotes
from jobhunt.cv.tailored import TailoredCV
from jobhunt.cv.verify import VerifierReport
from jobhunt.db import _dt, _iso, connect, now

APPLICATION_STATUSES = ("draft", "approved", "submitted", "failed", "withdrawn")
# draft -> approved; approved -> draft (edit); draft/approved -> withdrawn; withdrawn -> draft ("restaurer").
TRANSITIONS: dict[str, set[str]] = {
    "draft": {"approved", "withdrawn"},
    "approved": {"draft", "withdrawn", "submitted", "failed"},
    "failed": {"draft", "withdrawn"},
    "submitted": set(),
    "withdrawn": {"draft"},
}


def sha256_file(path: str | Path | None) -> str | None:
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest() if path else None
    except OSError:
        return None


class Application(BaseModel):
    id: int
    profile_id: int
    job_id: int
    job_title: str | None = None
    company: str | None = None
    status: str = "draft"
    current_rev: int = 0
    approved_rev: int | None = None
    approved_sha256: str | None = None
    approved_at: datetime | None = None
    approval_override: bool = False  # approved by hand despite blocking errors: never auto-eligible
    tailored: TailoredCV  # UNTRIMMED content of the current revision
    flags: list[Flag] = Field(default_factory=list)
    verifier: VerifierReport | None = None
    fit_notes: FitNotes = Field(default_factory=FitNotes)
    cv_path: str | None = None
    pdf_sha256: str | None = None
    master_hash: str | None = None
    cover_letter_path: str | None = None
    ats: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    notes: str | None = None

    @property
    def errors(self) -> list[Flag]:
        return [f for f in self.flags if f.level == "error"]

    @property
    def warnings(self) -> list[Flag]:
        return [f for f in self.flags if f.level == "warning"]

    @property
    def preps(self) -> list[Flag]:
        """Embellishments beyond the master: allowed, listed for interview preparation."""
        return [f for f in self.flags if f.level == "prep"]


_SELECT = (
    "SELECT a.*, r.tailored_json AS r_tailored, r.flags_json AS r_flags, r.verifier_json AS r_verifier,"
    " r.pdf_path AS r_pdf, r.pdf_sha256 AS r_sha, r.fit_notes_json AS r_fit, r.master_hash AS r_master"
    " FROM applications a LEFT JOIN application_revisions r ON r.application_id=a.id AND r.rev=a.current_rev"
)


def _from_row(r) -> Application:
    verifier = json.loads(r["r_verifier"]) if r["r_verifier"] else None
    return Application(
        id=r["id"], profile_id=r["profile_id"], job_id=r["job_id"], job_title=r["job_title"], company=r["company"],
        status=r["status"], current_rev=r["current_rev"], approved_rev=r["approved_rev"], approved_sha256=r["approved_sha256"],
        approved_at=_dt(r["approved_at"]), approval_override=bool(r["approval_override"]),
        tailored=TailoredCV.model_validate_json(r["r_tailored"] or r["tailored_json"]),
        flags=[Flag.model_validate(f) for f in json.loads(r["r_flags"] or r["flags_json"] or "[]")],
        verifier=VerifierReport.model_validate(verifier) if verifier else None,
        fit_notes=FitNotes.model_validate_json(r["r_fit"] or "{}"),
        cv_path=r["r_pdf"] if r["r_tailored"] else r["cv_path"], pdf_sha256=r["r_sha"], master_hash=r["r_master"],
        cover_letter_path=r["cover_letter_path"], ats=r["ats"], created_at=_dt(r["created_at"]),
        updated_at=_dt(r["updated_at"]), notes=r["notes"],
    )


def get_application(app_id: int) -> Application | None:
    with connect() as c:
        r = c.execute(_SELECT + " WHERE a.id=?", (app_id,)).fetchone()
    return _from_row(r) if r else None


def find_application(profile_id: int, job_id: int) -> Application | None:
    """The active application of this job, else the most recent withdrawn one (a new revision restores it)."""
    with connect() as c:
        r = c.execute(_SELECT + " WHERE a.profile_id=? AND a.job_id=? ORDER BY (a.status='withdrawn'), a.id DESC LIMIT 1",
                      (profile_id, job_id)).fetchone()
    return _from_row(r) if r else None


def list_applications(profile_id: int | None = None) -> list[Application]:
    with connect() as c:
        if profile_id is None:
            rows = c.execute(_SELECT + " ORDER BY a.id DESC").fetchall()
        else:
            rows = c.execute(_SELECT + " WHERE a.profile_id=? ORDER BY a.id DESC", (profile_id,)).fetchall()
    return [_from_row(r) for r in rows]


def applications_by_job(profile_id: int) -> dict[int, Application]:
    return {a.job_id: a for a in reversed(list_applications(profile_id)) if a.status != "withdrawn"}


def list_revisions(app_id: int) -> list[dict[str, Any]]:
    with connect() as c:
        rows = c.execute("SELECT rev, created_at, pdf_path, pdf_sha256, flags_json, verifier_json FROM application_revisions"
                         " WHERE application_id=? ORDER BY rev DESC", (app_id,)).fetchall()
    out = []
    for r in rows:
        flags = json.loads(r["flags_json"] or "[]")
        out.append({"rev": r["rev"], "created_at": _dt(r["created_at"]), "has_pdf": bool(r["pdf_path"]),
                    "errors": sum(1 for f in flags if f.get("level") == "error"), "verified": bool(r["verifier_json"])})
    return out


def _flags_json(flags: list[Flag]) -> str:
    return json.dumps([f.model_dump() for f in flags], ensure_ascii=False)


def add_revision(*, profile_id: int, job_id: int, job_title: str | None, company: str | None, ats: str | None,
                 tailored: TailoredCV, flags: list[Flag], verifier: VerifierReport | None, fit_notes: FitNotes,
                 master_hash: str | None, tmp_pdf: Path | None, pdf_target: Callable[[int], Path],
                 app_id: int | None = None, base_rev: int | None = None) -> Application:
    """ATOMIC: insert the revision (+ the application on first use), set current_rev, status=draft and clear the approval
    in one transaction. The PDF is moved (never overwriting) to `pdf_target(rev)` inside the transaction; on any failure
    nothing changes. `base_rev` (edits) must equal the current revision, else StaleRevision."""
    ts = _iso(now())
    dest: Path | None = None
    try:
        with connect() as c:
            c.execute("BEGIN IMMEDIATE")
            if app_id is not None:
                row = c.execute("SELECT * FROM applications WHERE id=?", (app_id,)).fetchone()
            else:
                row = c.execute("SELECT * FROM applications WHERE profile_id=? AND job_id=?"
                                " ORDER BY (status='withdrawn'), id DESC LIMIT 1", (profile_id, job_id)).fetchone()
            if app_id is not None and row is None:
                raise CVError("Candidature introuvable")
            if row is not None:
                if base_rev is not None and row["current_rev"] != base_rev:
                    raise StaleRevision("Le CV a changé entre-temps : rouvrir la page et recommencer")
                if "draft" not in TRANSITIONS[row["status"]] and row["status"] != "draft":
                    raise TransitionError("Candidature déjà envoyée : plus de modification possible")
                aid, rev = row["id"], row["current_rev"] + 1
            else:
                cur = c.execute(
                    "INSERT INTO applications(profile_id, job_id, job_title, company, status, tailored_json, flags_json, ats,"
                    " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (profile_id, job_id, job_title, company, "draft", "{}", "[]", ats, ts, ts))
                aid, rev = int(cur.lastrowid), 1
            pdf_path = sha = None
            if tmp_pdf is not None:
                while pdf_target(rev).parent.exists():  # never reuse (or overwrite) a revision directory
                    rev += 1
                dest = pdf_target(rev)
                dest.parent.mkdir(parents=True, exist_ok=False)
                shutil.move(str(tmp_pdf), str(dest))
                pdf_path, sha = str(dest), sha256_file(dest)
            c.execute(
                "INSERT INTO application_revisions(application_id, rev, tailored_json, flags_json, verifier_json, pdf_path,"
                " pdf_sha256, fit_notes_json, master_hash, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (aid, rev, tailored.model_dump_json(), _flags_json(flags), verifier.model_dump_json() if verifier else None,
                 pdf_path, sha, fit_notes.model_dump_json(), master_hash, ts))
            c.execute(
                "UPDATE applications SET current_rev=?, status='draft', approved_rev=NULL, approved_sha256=NULL,"
                " approved_at=NULL, approval_override=0, notes=NULL, tailored_json=?, flags_json=?, cv_path=?,"
                " job_title=COALESCE(?, job_title), company=COALESCE(?, company), ats=COALESCE(?, ats), updated_at=? WHERE id=?",
                (rev, tailored.model_dump_json(), _flags_json(flags), pdf_path, job_title, company, ats, ts, aid))
    except sqlite3.IntegrityError as e:
        if dest is not None:
            shutil.rmtree(dest.parent, ignore_errors=True)
        raise CVError("Une candidature active existe déjà pour cette offre") from e
    except BaseException:
        if dest is not None:  # the transaction rolled back: remove the file we moved
            shutil.rmtree(dest.parent, ignore_errors=True)
        raise
    return get_application(aid)  # type: ignore[return-value]


def set_status(app_id: int, new: str, *, base_rev: int | None = None) -> Application:
    """Enforce the status transitions. Leaving 'approved' clears the approval."""
    if new not in APPLICATION_STATUSES:
        raise ValueError(f"statut invalide : {new!r}")
    with connect() as c:
        c.execute("BEGIN IMMEDIATE")
        row = c.execute("SELECT status, current_rev FROM applications WHERE id=?", (app_id,)).fetchone()
        if row is None:
            raise CVError("Candidature introuvable")
        if base_rev is not None and row["current_rev"] != base_rev:
            raise StaleRevision("Le CV a changé entre-temps : rouvrir la page et recommencer")
        if new not in TRANSITIONS[row["status"]]:
            raise TransitionError(f"Passage « {row['status']} » → « {new} » impossible")
        try:
            c.execute("UPDATE applications SET status=?, updated_at=?,"
                      " approved_rev=CASE WHEN ?='approved' THEN approved_rev ELSE NULL END,"
                      " approved_sha256=CASE WHEN ?='approved' THEN approved_sha256 ELSE NULL END,"
                      " approval_override=CASE WHEN ?='approved' THEN approval_override ELSE 0 END WHERE id=?",
                      (new, _iso(now()), new, new, new, app_id))
        except sqlite3.IntegrityError as e:
            raise TransitionError("Une autre candidature active existe déjà pour cette offre") from e
    return get_application(app_id)  # type: ignore[return-value]


def approve_revision(app_id: int, rev: int, *, file_sha256: str | None, ignore_errors: bool = False) -> Application:
    """Approve EXACTLY `rev`: it must be the current revision, its PDF must exist and hash to the stored sha256."""
    with connect() as c:
        c.execute("BEGIN IMMEDIATE")
        a = c.execute("SELECT status, current_rev FROM applications WHERE id=?", (app_id,)).fetchone()
        if a is None:
            raise CVError("Candidature introuvable")
        if rev != a["current_rev"]:
            raise StaleRevision("Le CV a changé entre-temps : rouvrir la page et recommencer")
        if "approved" not in TRANSITIONS[a["status"]]:
            raise TransitionError("Seul un brouillon peut être approuvé")
        r = c.execute("SELECT pdf_path, pdf_sha256, flags_json FROM application_revisions WHERE application_id=? AND rev=?",
                      (app_id, rev)).fetchone()
        if r is None or not r["pdf_path"] or not r["pdf_sha256"] or file_sha256 != r["pdf_sha256"]:
            raise ApprovalError("PDF absent ou modifié : régénérer le CV avant d'approuver")
        errors = [f for f in json.loads(r["flags_json"] or "[]") if f.get("level") == "error"]
        if errors and not ignore_errors:
            raise ApprovalError(f"{len(errors)} erreur(s) à corriger avant d'approuver (ou cocher « Ignorer et approuver »)")
        note = None
        if errors:
            note = f"Approuvé à la main malgré {len(errors)} erreur(s) de vérification (jamais éligible à l'envoi automatique)."
        c.execute("UPDATE applications SET status='approved', approved_rev=?, approved_sha256=?, approved_at=?,"
                  " approval_override=?, notes=?, updated_at=? WHERE id=?",
                  (rev, r["pdf_sha256"], _iso(now()), 1 if errors else 0, note, _iso(now()), app_id))
    return get_application(app_id)  # type: ignore[return-value]
