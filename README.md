<img src="assets/icon.svg" width="64" height="64" alt="">

# Big Penalty

Created in [T3 Code](https://t3.codes).

**Desktop app:** Jobhunt also ships as a double-click app for macOS and Windows. See
[packaging/README.md](packaging/README.md) for installing, where keys and data live, and building.

## LLM providers

Always start with `cd /Volumes/SSD/Dev/big-penalty && . ./env.sh`.
The official Anthropic and OpenAI SDKs are used directly. API credentials stay in
the environment; do not paste them into commands, reports, or tracked files.

`JOBHUNT_LLM_PROVIDERS=anthropic,openai` controls call-level failover order.
Missing keys are skipped. Authentication errors, exhausted transient retries,
timeouts, refusals, and invalid structured responses try the next provider;
rules run only after every configured provider is unavailable or fails.
Set an empty provider list to force offline rule-based operation.

| Setting | Purpose |
|---|---|
| `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` | Credentials (never shown) |
| `JOBHUNT_PLANNER_MODEL` / `JOBHUNT_SCORING_MODEL` | Anthropic planning/scoring aliases |
| `JOBHUNT_OPENAI_PLANNER_MODEL` / `JOBHUNT_OPENAI_SCORING_MODEL` | OpenAI planning/scoring aliases |
| `JOBHUNT_PLANNER_EFFORT` / `JOBHUNT_SCORING_EFFORT` | Anthropic effort (medium / medium) |
| `JOBHUNT_OPENAI_PLANNER_EFFORT` / `JOBHUNT_OPENAI_SCORING_EFFORT` | OpenAI effort (low / low) |

An empty OpenAI effort omits reasoning options for models that do not support
them. OpenAI requests use Responses structured outputs and `store=False`.
Anthropic keeps structured messages, prompt caching, and server-side fallbacks.

`uv run jobhunt sources` shows credential presence and configured order without
making paid LLM calls. `uv run jobhunt llm-check` makes one small, paid planning
request per configured provider and shows status plus latency; non-OK statuses
exit with code 1. Each run stores per-task provider attempts and per-provider
token usage in `stats.llm_stats`; individual job scores store `scored_by`.
The run page and shortlist show provider badges.

Measured defaults keep Anthropic first: `claude-opus-5-5` with medium effort
for both tasks. OpenAI fallback uses `gpt-6-luna` for planning and `gpt-6.1-sol`
for scoring, both at low effort. The benchmark favors Opus medium for avoiding
high-ranked bad jobs, not for raw accuracy on the imbalanced dataset.

## Reproducible LLM benchmark

See [the benchmark report](reports/LLM%20benchmark.md) for measured defaults,
quality, latency, costs, limitations, and the subprocess failover drill.
`tests/bench/` contains synthetic profiles, real public connector postings, and
**model-generated gold labels that require human review**.

- `JOBHUNT_HEADLESS=1 uv run python scripts/bench_llm.py --collect`: refresh postings (public connectors; no LLM calls).
- `uv run python scripts/bench_llm.py --live --budget-usd 5`: paid benchmark, checkpointed/resumable.
- `uv run python scripts/bench_llm.py`: regenerate report offline from saved outputs.
- `uv run python scripts/bench_llm.py --drill`: tiny paid OpenAI score after an intentionally invalid Anthropic credential, only in a subprocess environment.
- `uv run pytest -q`: entirely offline test suite, with both provider credentials disabled by fixtures.

Archive existing `reports/llm-benchmark-results.json` before changing fixture
data or starting a fresh measurement; resumes never repeat recorded calls.
The budget guard reserves a conservative amount for unreported usage on failed
requests and stops before $5.50, leaving room below the $6 overall target.
Prices are centralized in the script with dated official sources.
