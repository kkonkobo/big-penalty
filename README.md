<img src="assets/icon.svg" width="64" height="64" alt="">

# Big Penalty

Created in [T3 Code](https://t3.codes).

**Desktop app:** Big Penalty also ships as a double-click app for macOS and Windows. See
[packaging/README.md](packaging/README.md) for installing, where keys and data live, and building.

## Daily schedule

Open **Planification** (`/schedule`) to add or remove local times, switch automatic runs on or off, or
choose **Lancer maintenant**. Automatic runs start only while the desktop app or `big-penalty serve`
is open at the scheduled time. Opening the app later does not replay missed runs. If the Mac sleeps
with the app open, a slot is still eligible on wake for up to ten minutes. A run already in progress
blocks the scheduled run; the skipped slot is not retried.

The app stores settings in `schedule.json` in its data directory (`JOBHUNT_DATA_DIR`). On first use it
imports times from the old launchd plist if present; otherwise it uses `07:30`. To remove the old agent,
open Planification once to migrate the times, then run `uv run jobhunt schedule uninstall-launchd`.
Migration does not change the old agent, and `schedule install` no longer installs it.

The CLI also supports `jobhunt schedule set 07:30 19:00`, `add HH:MM`, `remove HH:MM`, `status`, and `run`.
The last result and profile details appear in Planification and in `logs/daily-YYYY-MM-DD.log`.

## Auto-apply diagnostics

`uv run big-penalty apply diagnostics --days 30` reports completed apply runs from the local SQLite database.
Use `--db PATH` to inspect a separate database, such as a fictional test database. The command opens the database
read-only, does not migrate it, and does not read logs, run payloads, CVs or screenshots, open a browser, or make network calls.

Rows show fixed ATS and reason codes with distinct application and run counts. Simulations and submit attempts are
counted separately. Only engine symptoms attributed to a known ATS on at least two distinct applications receive a rank;
application count sorts first, then run count. The ATS is the application's saved label, not a verified failure-page vendor.
Unknown labels and unmatched historical reasons stay in `unknown` and `unclassified_failure` rather than exposing raw text.
Missing input, CAPTCHA and email verification appear as handoffs, not ranked failures. A later auto pass does not reopen
a CAPTCHA or email pause; use an explicit resume after checking the handoff. A ranked symptom is a lead for a fixture, not
proof of an ATS bug or permission to retry an application.

## LLM providers

Always start with `cd /Volumes/SSD/Dev/big-penalty && . ./env.sh`.
The official Anthropic and OpenAI SDKs are used directly. API credentials stay in
the environment; do not paste them into commands, reports, or tracked files.

### Subscription CLIs first, API as a backup

Every LLM task (search planning, job scoring, form filling, answer checks, CV and
letter writing, CV and letter checks) runs either **through the subscription CLIs
first** — Codex (`gpt-6.2-sol`), then Claude Code — with the API providers as
the backup, or **through the API only**. Switch each task, or all of them, on
the **IA** page of the app (stored in `<data_dir>/llm-routing.json`, applied to
the next call). Verifications call Claude Code before Codex so that another
model family than the writer re-reads. The page also shows each task's call
order, which CLIs are installed or on cooldown, and 7-day usage.

CLI calls (`src/jobhunt/llm_cli.py`) run in an empty temporary folder with
memory and session saving off: Claude Code with `autoMemoryEnabled=false`,
`CLAUDE_CODE_DISABLE_AUTO_MEMORY=1`, no session persistence, no settings,
tools or MCP servers; Codex with `--ephemeral`, `--ignore-user-config`, memory
generation and use off and no prompt history. API keys are removed from the
CLI's environment so only the subscription is used. A usage limit puts that CLI
on cooldown until its reset (shared by all processes); its calls then go
straight to the next provider. A transient CLI failure is retried once.

| Variable | Use |
|---|---|
| `JOBHUNT_LLM_ROUTE` | Default route of tasks not set on the IA page: `cli` (default) or `api` |
| `JOBHUNT_LLM_CLI=0` | Never use the CLIs (API only) |
| `JOBHUNT_CODEX_CLI_MODEL` / `_EFFORT` / `_CV_EFFORT` | Codex model and efforts (`gpt-6.2-sol`, low, medium for CV writing) |
| `JOBHUNT_CODEX_CLI_BIN` / `JOBHUNT_CLAUDE_CLI_BIN` | CLI paths (default: on `PATH`, else the ChatGPT app's bundled Codex) |
| `JOBHUNT_CODEX_HOME` | Optional dedicated Codex home (run `codex login` with it once): nothing is then written to `~/.codex` |
| `JOBHUNT_CLI_MAX_PARALLEL` / `JOBHUNT_CLI_TIMEOUT_S` | Concurrent CLI processes per CLI (3) and per-call timeout (120 s) |

Usage log rows from CLIs record `cost_usd` 0 and the list-price `api_equiv_usd`.

### API providers

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
| `JOBHUNT_APPLY_PLANNER_MODEL` / `JOBHUNT_APPLY_PLANNER_EFFORT` | Apply-form planner, one call per form step (`claude-sonnet-5-5` / medium) |
| `JOBHUNT_CV_MODEL` / `JOBHUNT_CV_EFFORT` | CV import, tailoring and cover letters (`claude-sonnet-5-5` / medium) |
| `JOBHUNT_APPLY_PLAN_CACHE_DAYS` | Rehearsals reuse the plan of an identical inspection prompt for this many days (7; 0 = off) |
| `JOBHUNT_LLM_USAGE_LOG` | One line per LLM call in `data/logs/llm-usage.jsonl` (1; 0 = off); `jobhunt llm-usage` sums it |

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
for search planning and scoring; the apply-form planner and the CV writer use
`claude-sonnet-5-5` (about half the cost per call). OpenAI fallback uses `gpt-6-luna` for planning and `gpt-6.1-sol`
for scoring, both at low effort. The benchmark favors Opus medium for avoiding
high-ranked bad jobs, not for raw accuracy on the imbalanced dataset.

## Verify an uncertain send

In the local **Envois → À vérifier** queue, open the listed attempt to see its
recorded click times, status and any local captures. A click or a technical failure
is not a receipt. Confirm **sent** only when the company's application history or
a matching confirmation email shows receipt; confirm **not sent** only when you
can verify no submission occurred. If unsure, leave the attempt unresolved.
The reservation stays in place until verification. A confirmed non-send returns
the application to draft (unless another attempt still needs verification); only
a manual send can retry it. No evidence is uploaded.

For CLI use, `big-penalty apply list` shows the exact attempt ID.
Use `big-penalty apply resolve APP_ID --run-id RUN_ID --sent` or `--not-sent`
only after verifying that attempt. Bulk resolution requires verified `APP_ID:RUN_ID` pairs.

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

Candidates ending in `-cli` run the same model through a subscription-signed
CLI (`claude -p`, `codex exec`; `src/jobhunt/llm_cli.py`) instead of the API:
no tools, no user settings or plugins, API keys removed from the CLI's
environment. They spend plan quota, not API credit; their cost columns show
the API-equivalent.

The apply-form planner has its own benchmark: [Planner benchmark](reports/Planner%20benchmark.md),
the production planner prompt on 26 synthetic fixture pages with hand-written
expectations (`tests/bench/planner/`).

- `JOBHUNT_FORCE_HEADLESS=1 uv run python scripts/bench_planner.py --snapshot`: refresh page snapshots (offline fixture server).
- `uv run python scripts/bench_planner.py --live --candidates sonnet-medium-cli,sol62-low-cli --repeats 2 --concurrency 1`: run (resumable; failed calls are retried on the next run).
- `uv run python scripts/bench_planner.py`: regenerate the report offline.

Archive existing `reports/llm-benchmark-results.json` before changing fixture
data or starting a fresh measurement; resumes never repeat recorded calls.
The budget guard reserves a conservative amount for unreported usage on failed
requests and stops before $5.50, leaving room below the $6 overall target.
Prices are centralized in the script with dated official sources.
