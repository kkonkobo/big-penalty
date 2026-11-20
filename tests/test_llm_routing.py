"""Per-task routing: subscription CLIs first (API as a backup) or the API only, chosen in the UI."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from jobhunt.config import get_settings
from jobhunt.llm import LLMClient, LLMError, llm_site
from jobhunt.llm_routing import TASK_IDS, load_routing, order_providers, set_claude_fallback, set_route, task_for


@pytest.fixture
def settings(tmp_path):
    return get_settings().model_copy(update={"data_dir": tmp_path, "llm_usage_log": False, "llm_route_default": "cli"})


def p(name, via_cli=False):
    return SimpleNamespace(name=name, via_cli=via_cli)


PROVIDERS = [p("codex-cli", True), p("claude-cli", True), p("anthropic"), p("openai")]


def names(providers):
    return [provider.name for provider in providers]


def test_task_of_each_call():
    assert task_for("apply.planner.inspect") == "apply.planner"
    assert task_for("search.score") == "search.score"
    assert task_for("", "AnswerVerdicts") == "apply.verify"
    assert task_for("", "LetterVerdict") == "cv.verify"
    assert task_for("", "TailorDraft") == "cv.write"


def test_cli_first_then_api_backup(settings):
    assert names(order_providers(PROVIDERS, "apply.planner", settings)) == ["codex-cli", "claude-cli", "anthropic", "openai"]
    # verifications read with another model family than the writer (Codex): Claude Code first
    assert names(order_providers(PROVIDERS, "cv.verify", settings)) == ["claude-cli", "codex-cli", "anthropic", "openai"]


def test_api_route_and_claude_toggle_persist(settings):
    set_route(settings, "search.score", "api")
    assert names(order_providers(PROVIDERS, "search.score", settings)) == ["anthropic", "openai"]
    assert names(order_providers(PROVIDERS, "search.plan", settings))[0] == "codex-cli"
    set_claude_fallback(settings, False)
    assert names(order_providers(PROVIDERS, "apply.planner", settings)) == ["codex-cli", "anthropic", "openai"]
    set_route(settings, None, "api")
    assert all(load_routing(settings).route(task, "cli") == "api" for task in TASK_IDS)
    with pytest.raises(ValueError):
        set_route(settings, "nope", "cli")


def test_default_route_from_settings(settings):
    api_default = settings.model_copy(update={"llm_route_default": "api"})
    assert names(order_providers(PROVIDERS, "cv.write", api_default)) == ["anthropic", "openai"]


def test_unreadable_routing_file_falls_back_to_defaults(settings):
    (settings.data_dir / "llm-routing.json").write_text("{not json")
    assert load_routing(settings).route("cv.write", "cli") == "cli"


class Fake:
    def __init__(self, name, via_cli=False, fail=False):
        self.name, self.via_cli, self.fail, self.enabled, self.calls = name, via_cli, fail, True, 0
        self.usage = {"calls": 0}

    def model_for(self, site):
        return (f"{self.name}-model", "low")

    async def structured(self, model_cls, **kwargs):
        self.calls += 1
        if self.fail:
            raise LLMError("down", code="rate_limit")
        return model_cls(answer=self.name)


class Answer(__import__("pydantic").BaseModel):
    answer: str


async def test_failover_follows_the_route_of_each_call(settings):
    codex, claude, api = Fake("codex-cli", True, fail=True), Fake("claude-cli", True), Fake("anthropic")
    llm = LLMClient(settings, providers=[codex, claude, api])
    with llm_site("apply.planner"):
        assert (await llm.structured(Answer, system_text="s", user_text="u")).answer == "claude-cli"
    set_route(settings, "apply.planner", "api")
    with llm_site("apply.planner"):
        assert (await llm.structured(Answer, system_text="s", user_text="u")).answer == "anthropic"
    assert codex.calls == 1  # the API route never touches a CLI
    assert llm.model_label("apply.planner") == "anthropic:anthropic-model"
