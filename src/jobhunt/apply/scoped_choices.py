"""Explicit ATS choices scoped to one application, revision, destination and field snapshot."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path

from jobhunt.apply.facts import answers_lock, answers_path
from jobhunt.apply.prompt_rules import decision_key
from jobhunt.apply.types import FormField, Question


def _digest(data: object) -> str:
    return hashlib.sha256(json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def field_choices(url: str, field: FormField) -> tuple[str, list[int]] | None:
    if field.kind not in {"select", "radio"} or field.multiple or not field.name or not field.form or not field.label:
        return None
    if not field.required and not (field.value and decision_key(field.label)):
        return None
    original = [(option.value, option.label) for option in field.options]
    by_value: dict[str, set[str]] = {}
    by_label: dict[str, set[str]] = {}
    for value, label in original:
        shown = " ".join(label.split())
        by_value.setdefault(value, set()).add(shown)
        if value and shown:
            by_label.setdefault(shown.casefold(), set()).add(value)
    unique = {}
    for index, (value, label) in enumerate(original):
        shown = " ".join(label.split())
        if value and shown and len(by_value[value]) == 1 and len(by_label[shown.casefold()]) == 1:
            unique.setdefault((value, shown), index)
    if not unique:
        return None
    binding = _digest([url, field.form, field.kind, field.name, field.label, field.group, original])
    return binding, list(unique.values())


def labels(question: Question) -> list[tuple[int, str]]:
    return [(index, " ".join(question.options[index].split())) for index in dict.fromkeys(question.choice_indices)
            if 0 <= index < len(question.options) and question.options[index].strip()]


def _path(profile_id: int, app_id: int, rev: int, origin: str) -> Path:
    return answers_path(profile_id).with_name(f"ats-choice-{_digest([app_id, rev, origin])}.json")


def load(profile_id: int, app_id: int, rev: int, origin: str) -> dict[str, int]:
    try:
        data = json.loads(_path(profile_id, app_id, rev, origin).read_text())
        return {binding: index for binding, index in data.items()
                if re.fullmatch(r"[0-9a-f]{64}", binding) and type(index) is int and index >= 0} if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save(profile_id: int, app_id: int, rev: int, origin: str, binding: str, index: int) -> None:
    path = _path(profile_id, app_id, rev, origin)
    with answers_lock(profile_id):
        data = load(profile_id, app_id, rev, origin)
        data[binding] = index
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, prefix="ats-choice-", delete=False) as temporary:
            try:
                json.dump(data, temporary)
                temporary.flush()
                os.fsync(temporary.fileno())
                os.replace(temporary.name, path)
            finally:
                Path(temporary.name).unlink(missing_ok=True)
