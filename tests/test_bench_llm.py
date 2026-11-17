import json
from pathlib import Path
import runpy

import pytest


@pytest.fixture(scope="module")
def bench():
    return runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts" / "bench_llm.py"))


def test_benchmark_fixtures_cover_every_pair(bench):
    from jobhunt.models import JobPosting, Profile

    fixture_dir = bench["FIXTURES"]
    profiles = json.loads((fixture_dir / "profiles.json").read_text())
    jobs = json.loads((fixture_dir / "jobs.json").read_text())
    gold = json.loads((fixture_dir / "labels.json").read_text())
    assert len(profiles) == 3 and len(jobs) == 40
    assert len({item["posting"]["url"] for item in jobs}) == len(jobs)
    for profile in profiles:
        Profile.model_validate(profile["profile"])
    for job in jobs:
        JobPosting.model_validate(job["posting"])
        assert job["posting"]["description"] and job["collected_at"] == "2026-11-17"
    expected = {(profile["id"], job["id"]) for profile in profiles for job in jobs}
    assert len(gold["labels"]) == len(expected)
    assert {(label["profile"], label["job"]) for label in gold["labels"]} == expected
    assert "Model-generated" in gold["provenance"]
    assert all(label["reason"] and label["fit"] in {"good", "partial", "bad"} for label in gold["labels"])


def test_cache_pricing_uses_provider_token_semantics(bench):
    cost = bench["cost"]
    assert cost("claude-opus-5-5", "anthropic", {
        "input": 100, "output": 20, "cache_read": 50, "cache_write": 10}) == pytest.approx(.00086)
    assert cost("gpt-6.1-sol", "openai", {
        "input": 100, "output": 20, "cache_read": 30, "cache_write": 20,
        "cache_write_12h": 5}) == pytest.approx(.0003655)


def test_spearman_handles_ties_and_constant_inputs(bench):
    spearman = bench["spearman"]
    assert spearman([10, 10, 80], [0, 0, 2]) == pytest.approx(1)
    assert spearman([80, 80, 10], [0, 0, 2]) == pytest.approx(-1)
    assert spearman([10, 10], [0, 1]) is None


def test_accuracy_bands_and_high_ranked_bad_jobs(bench):
    calls = [{"task": "score", "profile": "p", "job_ids": ["one", "two", "three"], "output": {
        "scores": [{"ref": 0, "fit": 70}, {"ref": 1, "fit": 39}, {"ref": 2, "fit": 69}]}}]
    metrics = bench["score_metrics"](calls, {("p", "one"): "bad", ("p", "two"): "bad", ("p", "three"): "partial"})
    assert metrics["n"] == 3 and metrics["bad_high"] == 1
    assert metrics["accuracy"] == pytest.approx(2 / 3)
    assert metrics["balanced_accuracy"] == pytest.approx(.75)


def test_rome_validation_checks_membership_not_only_syntax(bench):
    metrics = bench["plan_metrics"]({"queries": [
        {"keywords": "systèmes embarqués C++", "language": "fr", "rome_codes": ["M1805", "M9999", "garbage"]}]}, 0)
    assert metrics["invalid_rome_syntax"] == 1
    assert metrics["invalid_rome_codes"] == 2
    assert len(bench["load"](bench["FIXTURES"] / "rome_codes.json")["codes"]) == 1923
