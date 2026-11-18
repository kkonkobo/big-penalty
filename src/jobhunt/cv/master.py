"""Master CV: the single source of truth about the candidate. Stored as data/profiles/<pid>/master.yaml.

Every section item has a stable id so tailored CVs can reference the master (`source_ids`) and the fact check can verify them.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, field_validator

from jobhunt.config import get_settings


class Identity(BaseModel):
    name: str = ""
    default_title: str = ""  # e.g. "Élève ingénieur": first part of the headline
    email: str | None = None
    phone: str | None = None
    linkedin: str | None = None
    photo: str | None = None  # file name inside data/profiles/<pid>/ (photo.jpg)
    # Text in "Recherche de stage (<qualifier>) en ..."; empty = "Recherche de stage en ...". Set per candidate.
    stage_qualifier: str | None = None


class SkillGroup(BaseModel):
    id: str = ""
    label: str
    items: list[str] = Field(default_factory=list)


class Bullet(BaseModel):
    id: str = ""
    label: str = ""  # bold prefix shown as "Label :"
    text: str


class Project(BaseModel):
    id: str = ""
    title: str
    dates: str = ""
    context: str = ""
    bullets: list[Bullet] = Field(default_factory=list)


class Education(BaseModel):
    id: str = ""
    title: str
    school: str = ""
    dates: str = ""
    details: str = ""


class Language(BaseModel):
    id: str = ""
    name: str
    level: str = ""


class Master(BaseModel):
    identity: Identity = Field(default_factory=Identity)
    summary: str = ""
    skill_groups: list[SkillGroup] = Field(default_factory=list)
    projects: list[Project] = Field(default_factory=list)
    education: list[Education] = Field(default_factory=list)
    languages: list[Language] = Field(default_factory=list)
    # Confirmation by the user (UI button). `content_hash` = hash of everything except these two fields: editing the
    # master by hand (or on disk) invalidates it. Tailoring and auto-submit require a confirmed master.
    confirmed_at: str | None = None
    content_hash: str | None = None

    @field_validator("summary", mode="before")
    @classmethod
    def _summary(cls, v):
        return "" if v is None else v

    def ensure_ids(self) -> "Master":
        """Fill missing ids deterministically (sk1, p1, p1b1, e1, l1) without touching existing ones."""
        def fill(items, prefix):
            used = {i.id for i in items if i.id}
            n = 0
            for it in items:
                if not it.id:
                    n += 1
                    while f"{prefix}{n}" in used:
                        n += 1
                    it.id = f"{prefix}{n}"
                    used.add(it.id)

        fill(self.skill_groups, "sk")
        fill(self.projects, "p")
        fill(self.education, "e")
        fill(self.languages, "l")
        for p in self.projects:
            fill(p.bullets, f"{p.id}b")
        return self

    def compute_hash(self) -> str:
        data = self.model_dump(exclude={"confirmed_at", "content_hash"})
        return hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True).encode()).hexdigest()

    @property
    def is_confirmed(self) -> bool:
        return bool(self.confirmed_at and self.content_hash and self.content_hash == self.compute_hash())

    def bullet_index(self) -> dict[str, tuple[Project, Bullet]]:
        return {b.id: (p, b) for p in self.projects for b in p.bullets}

    def skill_items(self) -> list[str]:
        return [i for g in self.skill_groups for i in g.items]

    def all_text(self) -> str:
        """Every string of the master, for "is this term in the master?" checks."""
        parts: list[str] = []

        def walk(value) -> None:
            if isinstance(value, str):
                parts.append(value)
            elif isinstance(value, dict):
                for k, v in value.items():
                    if k not in ("id", "photo", "confirmed_at", "content_hash"):
                        walk(v)
            elif isinstance(value, list):
                for v in value:
                    walk(v)

        walk(self.model_dump())
        return "\n".join(parts)


class MasterError(Exception):
    pass


def profile_dir(profile_id: int) -> Path:
    return get_settings().profiles_dir / str(profile_id)


def master_path(profile_id: int) -> Path:
    return profile_dir(profile_id) / "master.yaml"


def pending_path(profile_id: int) -> Path:
    return profile_dir(profile_id) / "master.pending.yaml"


def report_path(profile_id: int) -> Path:
    return profile_dir(profile_id) / "master.pending.report.json"


PENDING_PHOTO = "pending-photo.jpg"
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}


def _inside(path: Path, root: Path) -> Path | None:
    """`path` resolved (symlinks followed) if it is a file inside `root` (resolved), else None."""
    try:
        real, base = path.resolve(strict=True), root.resolve(strict=True)
        real.relative_to(base)
    except (OSError, ValueError):
        return None
    return real if real.is_file() else None


def photo_path(profile_id: int, master: Master | None = None) -> Path | None:
    """The profile photo: resolved path, must be an image file contained in the profile dir (no symlink escape)."""
    pdir = profile_dir(profile_id)
    names = [Path(master.identity.photo).name] if master and master.identity.photo else [p.name for p in sorted(pdir.glob("photo.*"))]
    for name in names:
        real = _inside(pdir / name, pdir)
        if real is not None and real.suffix.lower() in IMAGE_SUFFIXES:
            return real
    return None


def parse_master(text: str) -> Master:
    try:
        data = yaml.safe_load(text) or {}
        if not isinstance(data, dict):
            raise MasterError("master.yaml doit être un dictionnaire")
        return Master.model_validate(data).ensure_ids()
    except yaml.YAMLError as e:
        raise MasterError(f"YAML invalide : {e}") from e
    except ValueError as e:
        raise MasterError(f"Profil maître invalide : {e}") from e


def dump_master(master: Master) -> str:
    return yaml.safe_dump(master.model_dump(), allow_unicode=True, sort_keys=False, width=100, default_flow_style=False)


def load_master(profile_id: int) -> Master | None:
    """The ACTIVE master (confirmed or not: see `Master.is_confirmed`)."""
    p = master_path(profile_id)
    if not p.is_file():
        return None
    return parse_master(p.read_text(encoding="utf-8"))


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def save_master(profile_id: int, master: Master) -> Path:
    """Save the active master. Any save UNCONFIRMS it (confirmation is an explicit UI act: `confirm_master`)."""
    master = master.model_copy(update={"confirmed_at": None, "content_hash": None}).ensure_ids()
    p = master_path(profile_id)
    _atomic_write(p, dump_master(master))
    return p


def load_pending(profile_id: int) -> Master | None:
    p = pending_path(profile_id)
    return parse_master(p.read_text(encoding="utf-8")) if p.is_file() else None


def load_report(profile_id: int) -> dict:
    try:
        data = json.loads(report_path(profile_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_pending(profile_id: int, master: Master, report: dict | None = None) -> Path:
    master = master.model_copy(update={"confirmed_at": None, "content_hash": None}).ensure_ids()
    _atomic_write(pending_path(profile_id), dump_master(master))
    if report is not None:
        _atomic_write(report_path(profile_id), json.dumps(report, ensure_ascii=False, indent=1))
    return pending_path(profile_id)


def discard_pending(profile_id: int) -> None:
    for p in (pending_path(profile_id), report_path(profile_id), profile_dir(profile_id) / PENDING_PHOTO):
        p.unlink(missing_ok=True)


def confirm_master(profile_id: int) -> Master:
    """The user's explicit confirmation. A pending import (if any) becomes the active master, otherwise the active master is
    confirmed in place. Writes `confirmed_at` + the content hash atomically."""
    pending = load_pending(profile_id)
    master = pending if pending is not None else load_master(profile_id)
    if master is None:
        raise MasterError("Aucun CV maître à confirmer")
    master = master.ensure_ids()
    master = master.model_copy(update={"confirmed_at": datetime.now(UTC).isoformat(timespec="seconds"),
                                       "content_hash": master.compute_hash()})
    pdir = profile_dir(profile_id)
    staged_photo = pdir / PENDING_PHOTO
    if pending is not None and staged_photo.is_file():
        for old in pdir.glob("photo.*"):
            old.unlink()
        os.replace(staged_photo, pdir / "photo.jpg")
    _atomic_write(master_path(profile_id), dump_master(master))
    if pending is not None:
        discard_pending(profile_id)
    return master


_WS = re.compile(r"\s+")


def norm_text(s: str) -> str:
    return _WS.sub(" ", s or "").strip()
