"""Structured LLM calls through a locally installed CLI signed in with a subscription (Codex `codex exec`, Claude Code
`claude -p`) instead of the paid API. FailoverLLM puts them before the API providers for every task routed "cli"
(llm_routing.py); the API stays the backup.

Every call:
- runs in a fresh empty temporary directory, with no tools, no MCP servers, no plugins and none of the user's settings;
- has memory and session saving switched off: nothing is added to the user's Claude Code / Codex memories, history or
  session lists, and no saved memory is read into the prompt;
- removes API keys from the child environment, so the CLI bills the subscription and never the API;
- validates the CLI's JSON against the same pydantic model as the API path;
- raises LLMError with a short code, never model output or prompt text.

A usage limit puts the provider on cooldown (shared by every process through <data_dir>/cache/cli-cooldown.json): its
calls fail fast with code "rate_limit" until the reset, so the chain moves straight to the next provider.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import tempfile
import time
from pathlib import Path
from time import perf_counter
from typing import Any, TypeVar

from openai.lib._pydantic import to_strict_json_schema
from pydantic import BaseModel, ValidationError

from jobhunt.config import Settings, get_settings
from jobhunt.llm import CACHE_BREAK, AnthropicProvider, LLMError, current_site, prompt_sha, record_usage

T = TypeVar("T", bound=BaseModel)

# Removed from the child environment: with an API key present, Claude Code and Codex bill the API instead of the plan.
SCRUBBED_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "OPENAI_API_KEY", "OPENAI_BASE_URL",
                "CODEX_API_KEY", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY",
                "CODEX_HOME")
LIMIT_MARKERS = ("usage limit", "rate limit", "rate_limit", "limit reached", "hit your limit", "too many requests", "quota")
DEFAULT_COOLDOWN_S = 30 * 60
COOLDOWN_FILE = "cli-cooldown.json"
# Codex replaces its agent instructions with the system prompt (model_instructions_file); this line keeps it from
# reaching for its shell tool, which cannot be removed.
CODEX_SUFFIX = ("\n\nAnswer directly with the JSON object required by the output schema. Do not run commands, read files "
                "or use any tool.")
CODEX_APP_BINARY = "/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex"
# Claude Code: no auto-memory (read or written), no transcript, no user/project settings, hooks, skills or plugins.
CLAUDE_MEMORY_OFF_ENV = {"CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1"}
CLAUDE_SETTINGS = {"autoMemoryEnabled": False}
# Codex: no session file (--ephemeral), no memories generated from or injected into this call, no prompt history,
# none of the user's config.toml (plugins, MCP servers, model choice).
CODEX_MEMORY_OFF = ("-c", "memories.generate_memories=false", "-c", "memories.use_memories=false",
                    "-c", 'history.persistence="none"')

_semaphores: dict[tuple[str, int], asyncio.Semaphore] = {}


def subscription_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if key not in SCRUBBED_ENV}
    env.update(extra or {})
    return env


def output_schema(model_cls: type[BaseModel]) -> dict[str, Any]:
    """The strict JSON schema both CLIs accept (every property required, no additional properties)."""
    return to_strict_json_schema(model_cls)


def _limit_code(text: str) -> str:
    lowered = text.lower()
    return "rate_limit" if any(marker in lowered for marker in LIMIT_MARKERS) else "cli_error"


def _reset_epoch(text: str, now: float) -> float:
    """When a usage limit resets: Claude Code reports "...limit reached|<epoch>"; otherwise a default cooldown."""
    match = re.search(r"\|(\d{10})\b", text)
    if match and now < int(match.group(1)) < now + 8 * 86400:
        return float(match.group(1))
    return now + DEFAULT_COOLDOWN_S


# ------------------------------------------------------------------ cooldown (shared by every process)
def _cooldown_path(settings: Settings) -> Path:
    return Path(settings.data_dir) / "cache" / COOLDOWN_FILE


def cooldowns(settings: Settings) -> dict[str, float]:
    """Provider -> epoch until which it is on cooldown (expired entries dropped)."""
    try:
        data = json.loads(_cooldown_path(settings).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    now = time.time()
    return {name: float(until) for name, until in data.items() if isinstance(until, int | float) and until > now}


def set_cooldown(settings: Settings, name: str, until: float | None) -> None:
    """Put `name` on cooldown until `until` (None clears it). Never raises."""
    current = cooldowns(settings)
    if until is None:
        current.pop(name, None)
    else:
        current[name] = until
    path = _cooldown_path(settings)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(current), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass


# ------------------------------------------------------------------ providers
class CLIProvider(AnthropicProvider):
    """Base class: `_parse_once` runs one CLI process; `plan` / `score` / `structured` come from AnthropicProvider and
    read the model and effort from this provider's settings."""

    name = "cli"
    label = "CLI"
    via_cli = True
    default_binary = ""

    def __init__(self, settings: Settings | None = None, *, binary: str | None = None, timeout_s: float | None = None,
                 max_attempts: int = 2, retry_base_s: float = 2.0):
        settings = settings or get_settings()
        super().__init__(self._own_settings(settings), client=False, max_attempts=max_attempts, retry_base_s=retry_base_s)
        self.client = None  # no SDK client: nothing to close
        self.binary = binary or self._configured_binary(settings) or shutil.which(self.default_binary) \
            or self._fallback_binary()
        self.timeout_s = timeout_s or settings.cli_timeout_s
        self.max_parallel = max(1, int(settings.cli_max_parallel))
        self.usage.update({"reasoning": 0, "cost_usd_equiv": 0.0})
        self.last_failure = ""

    @staticmethod
    def _own_settings(settings: Settings) -> Settings:
        return settings

    @staticmethod
    def _configured_binary(settings: Settings) -> str | None:
        return None

    @staticmethod
    def _fallback_binary() -> str | None:
        return None

    @property
    def enabled(self) -> bool:
        return bool(self.binary) and bool(getattr(self.settings, "llm_cli_enabled", True))

    def cooldown_until(self) -> float | None:
        return cooldowns(self.settings).get(self.name)

    def _semaphore(self) -> asyncio.Semaphore:
        key = (self.name, id(asyncio.get_running_loop()))
        if key not in _semaphores:
            _semaphores[key] = asyncio.Semaphore(self.max_parallel)
        return _semaphores[key]

    async def _parse(self, model_cls: type[T], *, model: str, system_text: str, user_text: str, effort: str,
                     max_tokens: int) -> T:
        """One call: fail fast while on cooldown, at most `max_parallel` processes at once, ONE retry after a transient
        CLI failure (Codex fails now and then under load), cooldown on a usage limit."""
        if (until := self.cooldown_until()) is not None:
            raise LLMError(f"{self.name}: limite d'usage atteinte (reprise vers {time.strftime('%H:%M', time.localtime(until))})",
                           code="rate_limit")
        for attempt in range(1, self.max_attempts + 1):
            try:
                async with self._semaphore():
                    return await self._parse_once(model_cls, model=model, system_text=system_text, user_text=user_text,
                                                  effort=effort)
            except LLMError as exc:
                if exc.code == "rate_limit":
                    set_cooldown(self.settings, self.name, _reset_epoch(self.last_failure, time.time()))
                    raise
                # A timeout is not retried: a second full wait would leave no time for the API backup.
                if exc.code != "cli_error" or attempt >= self.max_attempts:
                    raise
                await asyncio.sleep(self.retry_base_s)
        raise LLMError(f"{self.name}: échec", code="cli_error")  # unreachable: the loop returns or raises

    async def _parse_once(self, model_cls: type[T], *, model: str, system_text: str, user_text: str, effort: str) -> T:
        raise NotImplementedError

    def _child_env(self) -> dict[str, str]:
        return subscription_env()

    async def _run(self, argv: list[str], stdin: str, cwd: str) -> tuple[int, str, str]:
        process = await asyncio.create_subprocess_exec(
            *argv, cwd=cwd, env=self._child_env(), stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE)
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(stdin.encode("utf-8")), self.timeout_s)
        except TimeoutError:
            process.kill()
            await process.wait()
            raise LLMError(f"{self.name}: délai dépassé", code="timeout") from None
        return process.returncode or 0, stdout.decode("utf-8", "replace"), stderr.decode("utf-8", "replace")

    def _account(self, model_cls: type[BaseModel], *, model: str, system_text: str, user_text: str, started: float,
                 tokens: dict[str, int], cost_equiv: float | None) -> None:
        self.usage["calls"] += 1
        for key, value in tokens.items():
            self.usage[key] = self.usage.get(key, 0) + value
        if cost_equiv is not None:
            self.usage["cost_usd_equiv"] += cost_equiv
        record_usage(self.settings, provider=self.name, model=model, site=current_site(model_cls.__name__),
                     input_tokens=tokens.get("input", 0), output_tokens=tokens.get("output", 0),
                     cache_read=tokens.get("cache_read", 0), cache_write=tokens.get("cache_write", 0),
                     sha=prompt_sha(model, system_text, user_text), latency_s=perf_counter() - started, subscription=True)

    @staticmethod
    def _validate(model_cls: type[T], data: Any) -> T:
        try:
            return model_cls.model_validate(data)
        except ValidationError as exc:  # never embed exc: it quotes the model output
            raise LLMError("Réponse structurée CLI invalide", code="invalid_response") from exc


class ClaudeCLIProvider(CLIProvider):
    """`claude -p` with a JSON schema, the caller's system prompt (Claude Code's own is replaced), no tools. Same models
    and efforts as the Anthropic API provider."""

    name = "claude-cli"
    label = "Claude Code"
    default_binary = "claude"

    @staticmethod
    def _configured_binary(settings: Settings) -> str | None:
        return settings.claude_cli_bin

    def _child_env(self) -> dict[str, str]:
        return subscription_env(CLAUDE_MEMORY_OFF_ENV)

    def argv(self, model_cls: type[BaseModel], *, model: str, system_text: str, effort: str) -> list[str]:
        return [str(self.binary), "-p", "--model", model, "--effort", effort, "--tools", "", "--strict-mcp-config",
                "--setting-sources", "", "--settings", json.dumps(CLAUDE_SETTINGS), "--disable-slash-commands",
                "--no-session-persistence", "--system-prompt", system_text.replace(CACHE_BREAK, ""),
                "--json-schema", json.dumps(output_schema(model_cls)), "--output-format", "json"]

    async def _parse_once(self, model_cls: type[T], *, model: str, system_text: str, user_text: str, effort: str) -> T:
        started = perf_counter()
        with tempfile.TemporaryDirectory(prefix="jobhunt-cli-") as cwd:
            code, stdout, stderr = await self._run(
                self.argv(model_cls, model=model, system_text=system_text, effort=effort), user_text, cwd)
        try:
            data = json.loads(stdout)
        except ValueError:
            self.last_failure = (stdout + "\n" + stderr)[-4000:]
            raise LLMError(f"{self.name}: sortie illisible (exit {code})", code=_limit_code(stdout + stderr)) from None
        usage = data.get("usage") or {}
        tokens = {"input": int(usage.get("input_tokens") or 0), "output": int(usage.get("output_tokens") or 0),
                  "cache_read": int(usage.get("cache_read_input_tokens") or 0),
                  "cache_write": int(usage.get("cache_creation_input_tokens") or 0)}
        cost = data.get("total_cost_usd")
        if usage:
            self._account(model_cls, model=model, system_text=system_text, user_text=user_text, started=started,
                          tokens=tokens, cost_equiv=float(cost) if isinstance(cost, int | float) else None)
        if data.get("is_error") or code:
            # Kept on the provider for local diagnostics and the reset time only (never in the error message).
            self.last_failure = (str(data.get("result") or "") + "\n" + stderr)[-4000:]
            raise LLMError(f"{self.name}: échec ({data.get('subtype') or 'error'})", code=_limit_code(self.last_failure))
        if data.get("structured_output") is None:
            raise LLMError(f"{self.name}: pas de sortie structurée", code="invalid_response")
        return self._validate(model_cls, data["structured_output"])


class CodexCLIProvider(CLIProvider):
    """`codex exec` with an output schema, the caller's system prompt as model instructions, the user's Codex config
    ignored, a read-only sandbox in an empty directory. One model (settings.codex_cli_model, Sol 6.2 by default) for
    every task; low effort except CV writing."""

    name = "codex-cli"
    label = "Codex"
    default_binary = "codex"

    @staticmethod
    def _own_settings(settings: Settings) -> Settings:
        model, low, cv = settings.codex_cli_model, settings.codex_cli_effort, settings.codex_cli_cv_effort
        return settings.model_copy(update={
            "planner_model": model, "scoring_model": model, "cv_model": model, "apply_planner_model": model,
            "planner_effort": low, "scoring_effort": low, "apply_planner_effort": low, "cv_effort": cv})

    @staticmethod
    def _configured_binary(settings: Settings) -> str | None:
        return settings.codex_cli_bin

    @staticmethod
    def _fallback_binary() -> str | None:
        return CODEX_APP_BINARY if Path(CODEX_APP_BINARY).exists() else None

    def _child_env(self) -> dict[str, str]:
        home = getattr(self.settings, "codex_home", None)
        return subscription_env({"CODEX_HOME": str(home)} if home else None)

    def argv(self, *, model: str, effort: str, workdir: Path) -> list[str]:
        return [str(self.binary), "exec", "--ephemeral", "--skip-git-repo-check", "--ignore-user-config",
                *CODEX_MEMORY_OFF, "-s", "read-only", "-m", model, "-c", f"model_reasoning_effort={json.dumps(effort)}",
                "-c", f"model_instructions_file={json.dumps(str(workdir / 'instructions.md'))}",
                "--output-schema", str(workdir / "schema.json"), "-o", str(workdir / "out.json"), "--json", "-"]

    async def _parse_once(self, model_cls: type[T], *, model: str, system_text: str, user_text: str, effort: str) -> T:
        started = perf_counter()
        with tempfile.TemporaryDirectory(prefix="jobhunt-cli-") as cwd:
            workdir = Path(cwd)
            (workdir / "instructions.md").write_text(system_text.replace(CACHE_BREAK, "") + CODEX_SUFFIX, encoding="utf-8")
            (workdir / "schema.json").write_text(json.dumps(output_schema(model_cls)), encoding="utf-8")
            code, stdout, stderr = await self._run(self.argv(model=model, effort=effort, workdir=workdir), user_text, cwd)
            out_path = workdir / "out.json"
            raw = out_path.read_text(encoding="utf-8") if out_path.exists() else ""
        usage: dict[str, Any] = {}
        failure = ""
        for line in stdout.splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get("type") == "turn.completed":
                usage = event.get("usage") or {}
            elif event.get("type") in {"error", "turn.failed"}:
                failure += json.dumps(event)
        cached = int(usage.get("cached_input_tokens") or 0)
        # OpenAI semantics: input_tokens includes the cached part; reasoning is billed as output.
        tokens = {"input": max(int(usage.get("input_tokens") or 0) - cached, 0), "cache_read": cached,
                  "output": int(usage.get("output_tokens") or 0) + int(usage.get("reasoning_output_tokens") or 0),
                  "reasoning": int(usage.get("reasoning_output_tokens") or 0)}
        if usage:
            self._account(model_cls, model=model, system_text=system_text, user_text=user_text, started=started,
                          tokens=tokens, cost_equiv=None)
        if code or failure or not raw.strip():
            # Kept on the provider for local diagnostics and the reset time only (never in the error message).
            self.last_failure = (failure + "\n" + stderr)[-4000:]
            raise LLMError(f"{self.name}: échec (exit {code})", code=_limit_code(self.last_failure))
        try:
            data = json.loads(raw)
        except ValueError:
            raise LLMError(f"{self.name}: sortie illisible", code="invalid_response") from None
        return self._validate(model_cls, data)


CLI_PROVIDERS: tuple[type[CLIProvider], ...] = (CodexCLIProvider, ClaudeCLIProvider)


# ------------------------------------------------------------------ overview for the UI
API_LABELS = {"anthropic": "API Anthropic", "openai": "API OpenAI"}


def provider_model(name: str, task: str, settings: Settings) -> tuple[str, str]:
    """(model, effort) that provider `name` uses for `task` (mirrors each provider's own settings mapping)."""
    if name == "codex-cli":
        low = task in {"search.plan", "search.score", "apply.planner"}
        return settings.codex_cli_model, settings.codex_cli_effort if low else settings.codex_cli_cv_effort
    if name == "openai":
        if task == "search.score":
            return settings.openai_scoring_model, settings.openai_scoring_effort
        return settings.openai_planner_model, settings.openai_planner_effort
    return {"search.plan": (settings.planner_model, settings.planner_effort),
            "search.score": (settings.scoring_model, settings.scoring_effort),
            "apply.planner": (settings.apply_planner_model, settings.apply_planner_effort)}.get(
        task, (settings.cv_model, settings.cv_effort))


class _Descriptor:
    def __init__(self, name: str, label: str, via_cli: bool, available: bool, detail: str = ""):
        self.name, self.label, self.via_cli, self.available, self.detail = name, label, via_cli, available, detail


def provider_status(settings: Settings) -> list[_Descriptor]:
    """Every provider with whether it can be used now (installed / key present / not on cooldown)."""
    out = []
    on_cooldown = cooldowns(settings)
    for factory in CLI_PROVIDERS:
        provider = factory(settings)
        detail = ""
        if not settings.llm_cli_enabled:
            detail = "désactivé (JOBHUNT_LLM_CLI=0)"
        elif not provider.binary:
            detail = "non installé"
        elif factory.name in on_cooldown:
            detail = "limite atteinte, reprise vers " + time.strftime("%H:%M", time.localtime(on_cooldown[factory.name]))
        out.append(_Descriptor(factory.name, factory.label, True, provider.enabled and not detail, detail or str(provider.binary)))
    for name in settings.llm_providers:
        key = settings.anthropic_api_key if name == "anthropic" else settings.openai_api_key
        out.append(_Descriptor(name, API_LABELS.get(name, name), False, bool(key), "" if key else "clé API absente"))
    return out


def route_overview(settings: Settings) -> list[dict[str, Any]]:
    """Per task: label, route, and the provider chain [{label, model, effort, available, detail}] in call order."""
    from jobhunt.llm_routing import TASKS, load_routing, order_providers  # noqa: PLC0415

    routing = load_routing(settings)
    status = provider_status(settings)
    rows = []
    for task, label, description in TASKS:
        chain = []
        for provider in order_providers(status, task, settings, routing):
            model, effort = provider_model(provider.name, task, settings)
            chain.append({"name": provider.name, "label": provider.label, "model": model, "effort": effort,
                          "available": provider.available, "detail": provider.detail, "via_cli": provider.via_cli})
        rows.append({"task": task, "label": label, "description": description,
                     "route": routing.route(task, settings.llm_route_default), "chain": chain})
    return rows
