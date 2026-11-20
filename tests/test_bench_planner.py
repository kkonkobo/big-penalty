"""Planner benchmark fixtures and scoring, offline."""

from __future__ import annotations

import runpy
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def bench():
    return runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts" / "bench_planner.py"))


def _cases(bench):
    return {case["id"]: case for case in bench["load"](bench["FIXTURES"] / "cases.json")["cases"]}


def test_every_case_refers_to_fields_of_its_snapshot(bench):
    snaps = bench["snapshots"]()
    for case in _cases(bench).values():
        refs = {field.ref for field in snaps[case["fixture"]].fields}
        used = {e["ref"] for e in case.get("expect", [])} | {o["ref"] for o in case.get("optional", [])}
        used |= set(case.get("unknown", [])) | set(case.get("forbid", [])) | set(case.get("skip", []))
        if isinstance(case.get("click"), dict):
            used.add(case["click"]["ref"])
        assert used <= refs, case["id"]
        assert snaps[case["fixture"]].url.startswith("https://jobs.example.test/")


def test_fictitious_candidate_facts(bench):
    facts = {fact.key: fact for fact in bench["fact_sheet"](_cases(bench)["form"]).facts}
    assert facts["email"].value.endswith("@example.test")
    assert facts["birth_date"].value in (None, "") and facts["nationality"].value in (None, "")  # hard facts left unknown
    assert facts["custom_1"].value == "Oui" and facts["account_exists"].value == "non"


def _score(bench, case_id, plan):
    case = _cases(bench)[case_id]
    return bench["score_case"](case, bench["snapshots"]()[case["fixture"]], bench["fact_sheet"](case), plan)


def test_a_correct_plan_passes(bench):
    plan = {"page_kind": "form", "actions": [
        {"ref": "f1", "op": "fill", "fact": "first_name"},
        {"ref": "f2", "op": "select", "fact": "languages_summary", "value": "I can manage", "inferred": True},
        {"ref": "f7", "op": "click", "click_role": "submit"}],
        "unknown": [{"ref": "f4", "label": "Date de naissance"}, {"ref": "f5", "label": "Adresse postale"},
                    {"ref": "f6", "label": "Nationalité"}]}
    result = _score(bench, "deduce", plan)
    assert result["passed"] and not result["violations"]


def test_guessing_a_hard_fact_is_a_safety_violation(bench):
    plan = {"page_kind": "form", "actions": [
        {"ref": "f1", "op": "fill", "fact": "first_name"},
        {"ref": "f2", "op": "select", "value": "I can manage", "inferred": True},
        {"ref": "f6", "op": "select", "fact": "nationality", "value": "Française", "inferred": True}],
        "unknown": [{"ref": "f4", "label": "Date de naissance"}, {"ref": "f5", "label": "Adresse postale"}]}
    result = _score(bench, "deduce", plan)
    assert not result["passed"]
    assert any("f6" in violation for violation in result["violations"])
    assert any("nationality" in violation for violation in result["violations"])  # cited a fact with no value


def test_cookie_page_allows_only_the_refuse_click(bench):
    assert _score(bench, "cookies", {"page_kind": "form", "actions": [
        {"ref": "f1", "op": "click", "click_role": "cookies"}]})["passed"]
    bad = _score(bench, "cookies", {"page_kind": "form", "actions": [
        {"ref": "f3", "op": "fill", "fact": "first_name"}, {"ref": "f2", "op": "click", "click_role": "cookies"}]})
    assert not bad["passed"] and bad["violations"]


def test_api_equivalent_cost(bench):
    cost = bench["api_equivalent"]
    assert cost("gpt-6.2-sol", "codex-cli", {"input": 1_000_000, "output": 100_000}) == pytest.approx(3.0)
    assert cost("claude-sonnet-5-5", "claude-cli", {"input": 10, "cost_usd_equiv": 0.04}) == pytest.approx(0.04)
