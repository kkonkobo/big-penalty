"""Explicit ATS choices scoped to one application, revision, destination and field snapshot."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path

from jobhunt.apply.facts import answers_lock, answers_path
from jobhunt.apply.prompt_rules import decision_key, requires_site_consent, technical_key
from jobhunt.apply.types import FormField, Question


def _digest(data: object) -> str:
    return hashlib.sha256(json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def field_choices(url: str, field: FormField) -> tuple[str, list[int]] | None:
    if (field.kind not in {"select", "radio"} or field.multiple or not field.name or not field.form or not field.wording
            or not field.choice_destination or not field.choice_options
            or technical_key(field.label, field.kind, group=field.group, autocomplete=field.autocomplete)
            or (decision_key(field.label) or decision_key(field.group)) == "consent"
            or requires_site_consent(field.group)):
        return None
    if not field.required and not (field.selected and (decision_key(field.label) or decision_key(field.group))):
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
        live = [(option_label, disabled) for option_value, option_label, disabled, _ in field.choice_options if option_value == value]
        if (value and shown and len(by_value[value]) == 1 and len(by_label[shown.casefold()]) == 1
                and live and (field.kind != "radio" or len(live) == 1)
                and all(shown == " ".join(option_label.split()) and not disabled for option_label, disabled in live)):
            unique.setdefault((value, shown), index)
    if not unique:
        return None
    binding = _digest(["v2", url, field.form, field.choice_destination, field.kind, field.name, field.wording, field.group,
                       field.required, field.choice_options, original])
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


def save_once(profile_id: int, app_id: int, rev: int, origin: str, binding: str, index: int) -> bool:
    path = _path(profile_id, app_id, rev, origin)
    with answers_lock(profile_id):
        data = load(profile_id, app_id, rev, origin)
        if binding in data:
            return False
        data[binding] = index
        path.parent.mkdir(parents=True, exist_ok=True)
        # Close the temporary file before replacing: Windows refuses to rename a file that is still open.
        with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, prefix="ats-choice-", delete=False) as temporary:
            name = temporary.name
        try:
            with open(name, "w") as handle:
                json.dump(data, handle)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(name, path)
        finally:
            Path(name).unlink(missing_ok=True)
        return True
