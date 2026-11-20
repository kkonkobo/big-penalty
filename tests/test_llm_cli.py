"""Subscription CLI providers, offline: fake `claude` / `codex` executables stand in for the real CLIs."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from pydantic import BaseModel

from jobhunt.config import get_settings
from jobhunt.llm import LLMError
from jobhunt.llm_cli import ClaudeCLIProvider, CodexCLIProvider, output_schema


class Answer(BaseModel):
    answer: str
    key_seen: bool


# The fake CLIs are POSIX scripts with a shebang; Windows cannot execute them (the CLIs are off by default there).
posix_only = pytest.mark.skipif(sys.platform == "win32", reason="fake CLIs are POSIX shebang scripts")


def fake_cli(tmp_path: Path, body: str) -> str:
    path = tmp_path / "fake-cli"
    path.write_text(f"#!{sys.executable}\nimport json, os, sys\nargv = sys.argv[1:]\nprompt = sys.stdin.read()\n{body}\n")
    path.chmod(0o755)
    return str(path)


CLAUDE_OK = """
print(json.dumps({"is_error": False, "subtype": "success", "total_cost_usd": 0.0123,
                  "usage": {"input_tokens": 5, "output_tokens": 7, "cache_read_input_tokens": 100,
                            "cache_creation_input_tokens": 50},
                  "structured_output": {"answer": prompt + "|" + argv[argv.index("--system-prompt") + 1],
                                        "key_seen": "ANTHROPIC_API_KEY" in os.environ}}))
"""

CODEX_OK = """
out = argv[argv.index("-o") + 1]
instructions = [a for a in argv if a.startswith("model_instructions_file=")][0].split("=", 1)[1]
system = open(json.loads(instructions)).read().split("\\n\\n")[0]
json.dump({"answer": prompt + "|" + system, "key_seen": "OPENAI_API_KEY" in os.environ}, open(out, "w"))
print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 1000, "cached_input_tokens": 400,
                                                      "output_tokens": 10, "reasoning_output_tokens": 5}}))
"""


@pytest.fixture
def settings(tmp_path):
    return get_settings().model_copy(update={"llm_usage_log": False, "data_dir": tmp_path})


@posix_only
async def test_claude_cli_parses_structured_output_without_api_key(tmp_path, settings, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-reach-the-cli")
    provider = ClaudeCLIProvider(settings, binary=fake_cli(tmp_path, CLAUDE_OK))
    out = await provider._parse(Answer, model="claude-sonnet-5-5", system_text="rules\x00cache-breakpoint\x00facts",
                                user_text="page", effort="low", max_tokens=100)
    assert out.answer == "page|rulesfacts"  # system prompt sent whole, cache marker removed
    assert out.key_seen is False
    assert provider.usage["calls"] == 1 and provider.usage["cache_read"] == 100 and provider.usage["cache_write"] == 50
    assert provider.usage["cost_usd_equiv"] == pytest.approx(0.0123)


def test_claude_cli_argv_turns_tools_settings_and_mcp_off(settings):
    argv = ClaudeCLIProvider(settings, binary="/bin/claude").argv(Answer, model="m", system_text="s", effort="low")
    assert argv[argv.index("--tools") + 1] == "" and argv[argv.index("--setting-sources") + 1] == ""
    assert "--strict-mcp-config" in argv and "--no-session-persistence" in argv
    assert json.loads(argv[argv.index("--json-schema") + 1]) == output_schema(Answer)


@posix_only
async def test_claude_cli_limit_is_a_rate_limit_error(tmp_path, settings):
    body = 'print(json.dumps({"is_error": True, "subtype": "error", "result": "Claude AI usage limit reached|1767225600"}))'
    provider = ClaudeCLIProvider(settings, binary=fake_cli(tmp_path, body))
    with pytest.raises(LLMError) as caught:
        await provider._parse(Answer, model="m", system_text="s", user_text="u", effort="low", max_tokens=10)
    assert caught.value.code == "rate_limit" and "usage limit" not in str(caught.value)


@posix_only
async def test_claude_cli_invalid_output_never_quotes_it(tmp_path, settings):
    body = 'print(json.dumps({"is_error": False, "structured_output": {"answer": "SECRET-OUTPUT"}}))'
    provider = ClaudeCLIProvider(settings, binary=fake_cli(tmp_path, body))
    with pytest.raises(LLMError) as caught:
        await provider._parse(Answer, model="m", system_text="s", user_text="u", effort="low", max_tokens=10)
    assert caught.value.code == "invalid_response" and "SECRET" not in str(caught.value)


@posix_only
async def test_codex_cli_reads_output_file_and_usage(tmp_path, settings, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-should-not-reach-the-cli")
    provider = CodexCLIProvider(settings, binary=fake_cli(tmp_path, CODEX_OK))
    out = await provider._parse(Answer, model="gpt-6.2-sol", system_text="rules", user_text="page", effort="low",
                                max_tokens=100)
    assert out.answer == "page|rules" and out.key_seen is False
    # OpenAI counts cached tokens inside input_tokens; reasoning is billed as output.
    assert provider.usage["input"] == 600 and provider.usage["cache_read"] == 400 and provider.usage["output"] == 15


def test_codex_cli_ignores_user_config(settings, tmp_path):
    argv = CodexCLIProvider(settings, binary="/bin/codex").argv(model="m", effort="low", workdir=tmp_path)
    assert "--ignore-user-config" in argv and "--ephemeral" in argv and argv[argv.index("-s") + 1] == "read-only"


@posix_only
async def test_codex_cli_failure_without_output(tmp_path, settings):
    body = 'print(json.dumps({"type": "turn.failed", "error": {"message": "You\'ve hit your usage limit"}})); sys.exit(1)'
    provider = CodexCLIProvider(settings, binary=fake_cli(tmp_path, body))
    with pytest.raises(LLMError) as caught:
        await provider._parse(Answer, model="m", system_text="s", user_text="u", effort="low", max_tokens=10)
    assert caught.value.code == "rate_limit"


def test_missing_binary_disables_provider(settings, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: None)
    assert ClaudeCLIProvider(settings).enabled is False


# ------------------------------------------------------------------ memory off, retry, cooldown
ECHO_ENV = """
out = argv[argv.index("-o") + 1] if "-o" in argv else None
seen = {"auto_memory_off": os.environ.get("CLAUDE_CODE_DISABLE_AUTO_MEMORY") == "1", "codex_home": os.environ.get("CODEX_HOME", "")}
if out:
    json.dump({"answer": json.dumps(seen), "key_seen": False}, open(out, "w"))
    print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}}))
else:
    print(json.dumps({"is_error": False, "usage": {"input_tokens": 1}, "structured_output": {"answer": json.dumps(seen), "key_seen": False}}))
"""


def test_claude_cli_memory_and_sessions_off(settings):
    argv = ClaudeCLIProvider(settings, binary="/bin/claude").argv(Answer, model="m", system_text="s", effort="low")
    assert "--no-session-persistence" in argv
    assert json.loads(argv[argv.index("--settings") + 1]) == {"autoMemoryEnabled": False}


@posix_only
async def test_claude_cli_child_env_disables_auto_memory(tmp_path, settings):
    provider = ClaudeCLIProvider(settings, binary=fake_cli(tmp_path, ECHO_ENV))
    out = await provider._parse(Answer, model="m", system_text="s", user_text="u", effort="low", max_tokens=10)
    assert json.loads(out.answer)["auto_memory_off"] is True


def test_codex_cli_memory_history_and_sessions_off(settings, tmp_path):
    argv = CodexCLIProvider(settings, binary="/bin/codex").argv(model="m", effort="low", workdir=tmp_path)
    assert "--ephemeral" in argv and "--ignore-user-config" in argv
    joined = " ".join(argv)
    assert "memories.generate_memories=false" in joined and "memories.use_memories=false" in joined
    assert 'history.persistence="none"' in joined


@posix_only
async def test_codex_cli_uses_a_dedicated_home_when_configured(tmp_path, settings, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", "/personal/codex")  # the user's own home never leaks into the child
    provider = CodexCLIProvider(settings, binary=fake_cli(tmp_path, ECHO_ENV))
    out = await provider._parse(Answer, model="m", system_text="s", user_text="u", effort="low", max_tokens=10)
    assert json.loads(out.answer)["codex_home"] == ""
    dedicated = CodexCLIProvider(settings.model_copy(update={"codex_home": tmp_path / "codex-home"}),
                                 binary=fake_cli(tmp_path, ECHO_ENV))
    out = await dedicated._parse(Answer, model="m", system_text="s", user_text="u", effort="low", max_tokens=10)
    assert json.loads(out.answer)["codex_home"] == str(tmp_path / "codex-home")


def test_codex_provider_uses_sol_for_every_task(settings):
    provider = CodexCLIProvider(settings.model_copy(update={"codex_cli_model": "gpt-6.2-sol"}), binary="/bin/codex")
    assert provider.model_for("apply.planner") == ("gpt-6.2-sol", "low")
    assert provider.model_for("") == ("gpt-6.2-sol", "medium")  # CV writing
    assert provider.settings.scoring_model == "gpt-6.2-sol" and provider.settings.scoring_effort == "low"


@posix_only
async def test_transient_cli_failure_is_retried_once(tmp_path, settings):
    counter = tmp_path / "count"
    body = f"""
path = {str(counter)!r}
n = int(open(path).read()) + 1 if os.path.exists(path) else 1
open(path, "w").write(str(n))
if n == 1:
    sys.exit(3)
print(json.dumps({{"is_error": False, "usage": {{"input_tokens": 1}}, "structured_output": {{"answer": "ok", "key_seen": False}}}}))
"""
    provider = ClaudeCLIProvider(settings, binary=fake_cli(tmp_path, body), retry_base_s=0)
    out = await provider._parse(Answer, model="m", system_text="s", user_text="u", effort="low", max_tokens=10)
    assert out.answer == "ok" and counter.read_text() == "2"


@posix_only
async def test_usage_limit_puts_the_cli_on_cooldown(tmp_path, settings):
    from jobhunt.llm_cli import cooldowns, set_cooldown

    calls = tmp_path / "calls"
    body = f"""
open({str(calls)!r}, "a").write("x")
print(json.dumps({{"is_error": True, "subtype": "error", "result": "Claude AI usage limit reached|4102444800"}}))
"""
    provider = ClaudeCLIProvider(settings, binary=fake_cli(tmp_path, body), retry_base_s=0)
    for _ in range(2):
        with pytest.raises(LLMError) as caught:
            await provider._parse(Answer, model="m", system_text="s", user_text="u", effort="low", max_tokens=10)
        assert caught.value.code == "rate_limit"
    assert calls.read_text() == "x"  # the second call failed fast, without starting the CLI
    assert "claude-cli" in cooldowns(settings)
    set_cooldown(settings, "claude-cli", None)
    assert "claude-cli" not in cooldowns(settings)


@posix_only
async def test_subscription_calls_cost_nothing_in_the_usage_log(tmp_path, settings):
    from jobhunt.llm import USAGE_FILE, read_usage

    logged = settings.model_copy(update={"llm_usage_log": True})
    provider = ClaudeCLIProvider(logged, binary=fake_cli(tmp_path, CLAUDE_OK))
    await provider._parse(Answer, model="claude-sonnet-5-5", system_text="s", user_text="u", effort="low", max_tokens=10)
    [row] = read_usage(tmp_path / "logs" / USAGE_FILE)
    assert row["provider"] == "claude-cli" and row["cost_usd"] == 0.0 and row["api_equiv_usd"] > 0
