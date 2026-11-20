"""Per-call LLM usage accounting: one JSON line per call (counts only), summed by `jobhunt llm-usage`."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from jobhunt.config import get_settings
from jobhunt.llm import (USAGE_FILE, AnthropicProvider, LLMClient, OpenAIProvider, Plan, PlannedQuery, estimate_cost, llm_site,
                         read_usage, record_usage, summarize_usage)
from jobhunt.models import Profile


class FakeMessages:
    def __init__(self, response):
        self.response, self.calls = response, []

    async def parse(self, **kw):
        self.calls.append(kw)
        return self.response


def usage_rows():
    return read_usage(get_settings().data_dir / "logs" / USAGE_FILE)


def test_cost_estimate_uses_cache_prices():
    # 1M uncached in + 1M out + 1M cache read + 1M cache write on Opus 5.5 = 4 + 20 + 0.2 + 5
    assert estimate_cost("claude-opus-5-5", 1_000_000, 1_000_000, 1_000_000, 1_000_000) == 29.2
    assert estimate_cost("claude-haiku-5-5", 1_000_000, 0, 0, 0) == 0.1
    assert estimate_cost("some-unknown-model", 10, 10, 0, 0) is None


async def test_anthropic_call_is_logged_with_site_model_tokens_and_no_text():
    parsed = Plan(queries=[PlannedQuery(keywords="firmware")])
    response = SimpleNamespace(parsed_output=parsed, stop_reason="end_turn", model="claude-opus-5-5",
                               usage=SimpleNamespace(input_tokens=1200, output_tokens=300, cache_read_input_tokens=2500,
                                                     cache_creation_input_tokens=0))
    messages = FakeMessages(response)
    provider = AnthropicProvider(get_settings(), SimpleNamespace(beta=SimpleNamespace(messages=messages)), retry_base_s=0)
    with llm_site("apply.planner"):
        await provider.structured(Plan, system_text="SYSTEM secret-free rules", user_text="Prénom Camille")
    [row] = usage_rows()
    assert row["site"] == "apply.planner" and row["provider"] == "anthropic" and row["model"] == "claude-opus-5-5"
    assert (row["input"], row["output"], row["cache_read"], row["cache_write"]) == (1200, 300, 2500, 0)
    assert row["cost_usd"] == estimate_cost("claude-opus-5-5", 1200, 300, 2500, 0) and len(row["sha"]) == 16
    raw = (get_settings().data_dir / "logs" / USAGE_FILE).read_text(encoding="utf-8")
    assert "Camille" not in raw and "SYSTEM" not in raw


async def test_search_calls_are_labelled_and_unlabelled_calls_fall_back_to_the_schema_name():
    parsed = Plan(queries=[PlannedQuery(keywords="firmware")])
    response = SimpleNamespace(parsed_output=parsed, stop_reason="end_turn",
                               usage=SimpleNamespace(input_tokens=10, output_tokens=5, cache_read_input_tokens=0,
                                                     cache_creation_input_tokens=0))
    client = SimpleNamespace(beta=SimpleNamespace(messages=FakeMessages(response)))
    llm = LLMClient(client=client)
    await llm.plan(Profile(name="T", headline="Développeur"), 3)
    await llm.providers[0].structured(Plan, system_text="s", user_text="u")
    assert [row["site"] for row in usage_rows()] == ["search.plan", "Plan"]


async def test_openai_cached_tokens_are_not_counted_as_uncached_input():
    parsed = Plan(queries=[PlannedQuery(keywords="firmware")])
    response = SimpleNamespace(
        status="completed", output=[], output_parsed=parsed,
        usage=SimpleNamespace(input_tokens=3000, output_tokens=200,
                              input_tokens_details=SimpleNamespace(cached_tokens=2048, cache_write_tokens=0)))

    class Responses:
        async def parse(self, **kw):
            return response

    provider = OpenAIProvider(get_settings(), SimpleNamespace(responses=Responses()), retry_base_s=0)
    with llm_site("AnswerVerdicts"):
        await provider.structured(Plan, system_text="s", user_text="u")
    [row] = usage_rows()
    assert (row["provider"], row["input"], row["cache_read"], row["output"]) == ("openai", 952, 2048, 200)
    # gpt-6-luna list prices: uncached input, output and cache reads priced separately.
    assert row["cost_usd"] == pytest.approx((952 * 0.10 + 200 * 0.50 + 2048 * 0.01) / 1e6, abs=1e-6)


def test_usage_log_can_be_disabled(monkeypatch):
    monkeypatch.setattr(get_settings(), "llm_usage_log", False)
    record_usage(get_settings(), provider="anthropic", model="claude-opus-5-5", site="x", input_tokens=1, output_tokens=1,
                 cache_read=0, cache_write=0)
    assert usage_rows() == []


def test_summary_groups_by_site_and_model_and_counts_repeated_prompts():
    rows = [
        {"site": "apply.planner", "model": "m", "input": 100, "output": 10, "cache_read": 0, "cache_write": 0, "cost_usd": 0.5, "sha": "a"},
        {"site": "apply.planner", "model": "m", "input": 100, "output": 10, "cache_read": 50, "cache_write": 0, "cost_usd": 0.4, "sha": "a"},
        {"site": "TailorDraft", "model": "m", "input": 10, "output": 1, "cache_read": 0, "cache_write": 0, "cost_usd": None, "sha": "b"},
    ]
    planner, tailor = summarize_usage(rows)
    assert planner["site"] == "apply.planner" and planner["calls"] == 2 and planner["repeats"] == 1
    assert planner["input"] == 200 and planner["cache_read"] == 50 and round(planner["cost_usd"], 2) == 0.9
    assert tailor["calls"] == 1 and tailor["cost_usd"] == 0


def test_cli_prints_the_summary():
    from typer.testing import CliRunner

    from jobhunt.cli import app

    record_usage(get_settings(), provider="anthropic", model="claude-opus-5-5", site="apply.planner", input_tokens=1000,
                 output_tokens=100, cache_read=0, cache_write=0, sha="abc")
    result = CliRunner().invoke(app, ["llm-usage", "--days", "0"])
    assert result.exit_code == 0 and "Total estimé : 0.01 $ sur 1 appels" in result.stdout


async def test_cache_break_gives_the_shared_rules_their_own_breakpoint_and_never_reaches_the_model():
    from jobhunt.llm import CACHE_BREAK

    parsed = Plan(queries=[PlannedQuery(keywords="firmware")])
    response = SimpleNamespace(parsed_output=parsed, stop_reason="end_turn",
                               usage=SimpleNamespace(input_tokens=1, output_tokens=1, cache_read_input_tokens=0,
                                                     cache_creation_input_tokens=0))
    messages = FakeMessages(response)
    provider = AnthropicProvider(get_settings(), SimpleNamespace(beta=SimpleNamespace(messages=messages)), retry_base_s=0)
    await provider.structured(Plan, system_text="RULES" + CACHE_BREAK + "FACTS", user_text="page")
    await provider.structured(Plan, system_text="RULES only", user_text="page")
    split, single = (call["system"] for call in messages.calls)
    assert split == [{"type": "text", "text": "RULES", "cache_control": {"type": "ephemeral"}},
                     {"type": "text", "text": "FACTS", "cache_control": {"type": "ephemeral"}}]
    assert single == [{"type": "text", "text": "RULES only", "cache_control": {"type": "ephemeral"}}]

    seen = {}

    class Responses:
        async def parse(self, **kw):
            seen.update(kw)
            return SimpleNamespace(status="completed", output=[], output_parsed=parsed, usage=None)

    await OpenAIProvider(get_settings(), SimpleNamespace(responses=Responses()), retry_base_s=0).structured(
        Plan, system_text="RULES" + CACHE_BREAK + "FACTS", user_text="page")
    assert seen["instructions"] == "RULESFACTS"


def response_for(parsed):
    return SimpleNamespace(parsed_output=parsed, stop_reason="end_turn",
                           usage=SimpleNamespace(input_tokens=1, output_tokens=1, cache_read_input_tokens=0,
                                                 cache_creation_input_tokens=0))


async def test_planner_and_cv_calls_default_to_sonnet_and_keep_their_own_settings(monkeypatch):
    from jobhunt.config import Settings

    assert Settings.model_fields["apply_planner_model"].default == "claude-sonnet-5-5"
    assert Settings.model_fields["cv_model"].default == "claude-sonnet-5-5"
    settings = get_settings()
    monkeypatch.setattr(settings, "apply_planner_model", "planner-model")
    monkeypatch.setattr(settings, "apply_planner_effort", "low")
    monkeypatch.setattr(settings, "cv_model", "cv-model")
    parsed = Plan(queries=[PlannedQuery(keywords="firmware")])
    messages = FakeMessages(response_for(parsed))
    provider = AnthropicProvider(settings, SimpleNamespace(beta=SimpleNamespace(messages=messages)), retry_base_s=0)
    for site in ("apply.planner", "apply.planner.inspect", "TailorDraft"):
        with llm_site(site):
            await provider.structured(Plan, system_text="s", user_text="u")
    assert [(c["model"], c["output_config"]) for c in messages.calls] == [
        ("planner-model", {"effort": "low"}), ("planner-model", {"effort": "low"}), ("cv-model", {"effort": "medium"})]
    llm = LLMClient(client=SimpleNamespace(beta=SimpleNamespace(messages=messages)))
    with llm_site("apply.planner"):
        info, _ = await llm.structured_ex(Plan, system_text="s", user_text="u")
    assert info == {"provider": "anthropic", "model": "planner-model"}


def test_openai_planner_calls_never_receive_a_claude_model_name():
    provider = OpenAIProvider(get_settings(), SimpleNamespace(responses=None))
    assert provider.model_for("apply.planner") == (get_settings().openai_planner_model, get_settings().openai_planner_effort)
