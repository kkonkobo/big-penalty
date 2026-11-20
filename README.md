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

Archive existing `reports/llm-benchmark-results.json` before changing fixture
data or starting a fresh measurement; resumes never repeat recorded calls.
The budget guard reserves a conservative amount for unreported usage on failed
requests and stops before $5.50, leaving room below the $6 overall target.
Prices are centralized in the script with dated official sources.
