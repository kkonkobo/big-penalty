# Handoff — state on 2026-11-17

Personal job-search tool for the French market (family use, not for sale). Local web app + CLI.

## Product goal
Run **once a day**, surface **fresh** jobs matching a *function/major* (not a title), let the user approve,
then **apply on the company's own site/ATS** with a **CV tailored to the role**, only after the user's OK.
LinkedIn/Indeed/other aggregators are discovery only.

Job ads are written by HR, not the hiring manager: treat requirements as a **wish list**, not hard filters.
Score on function fit and realistic eligibility (contract, level, location); don't drop a job because it lists
5 tools the candidate only partly has. Hard blockers stay hard (wrong contract, PFE reserved to Bac+5, location).

## Setup
```
cd /Volumes/SSD/Dev/big-penalty && . ./env.sh     # always; keeps caches on the SSD, loads .env (keys, never commit)
uv run pytest -q                                  # 247 tests
uv run jobhunt search 1 -n 10                     # headful browser by default; JOBHUNT_HEADLESS=1 for headless
uv run jobhunt rescore 1                          # rescore stored jobs after profile/prompt change
uv run jobhunt serve                              # http://127.0.0.1:8765
```
Data: `data/jobhunt.sqlite3` (gitignored). Profile 1 = mechatronics student, **Bac+4** (2nd year of cycle ingénieur).

## Done (milestone 1: search + shortlist)
- 20 sources (aggregators, French boards, public/official, freelance); see `uv run jobhunt sources`.
- LLM plan + scoring: Anthropic Opus 5.5 primary, OpenAI failover, rule-based fallback. Benchmark in `reports/LLM benchmark.md`.
- Dedupe (fuzzy company/title/city), origin resolution to the company ATS/careers page (`src/jobhunt/origin.py`).
- Education-level rule in scoring; `rescore` command.
- First live run: 916 raw → 538 unique, 247 with origin URL, 43 scored ≥70 after the Bac+4 rescore.

## Freshness today
- `posted_at` is filled for 538/538 jobs, but it's the **source's** date. LinkedIn/Indeed reposts reset it,
  and some boards return relative dates ("il y a 30+ jours").
- `first_seen` / `last_seen` are stored per job, so "new since yesterday" is already computable.
- Not done yet: prefer the origin (ATS) posting date when found, flag reposts, freshness in the score/sort,
  hide jobs no longer seen (expired).

## Next steps (suggested order)
1. **Daily run**: scheduler (launchd/cron) + "new today" view, freshness ranking, expiry when not seen for N days.
2. **Speed**: 27 min for 10 queries; LinkedIn detail fetches dominate. Fetch details only after prefilter; cap per query.
3. **Wish-list scoring**: update `SCORE_TASK` in `src/jobhunt/llm.py` (bump fingerprint version in `scoring.py`), rescore.
4. Bugs: Jobijoba redirect URL with a contact-form-7 shortcode crashes origin resolution; 115 jobs with contract `unknown`;
   mis-parsed companies (e.g. "STATION F").
5. **Milestone 2 — tailored CV + apply**: per-job CV/cover letter from the user's real experience (never invent facts),
   user approval, autofill on the origin ATS (Greenhouse/Lever/Workday/SmartRecruiters/…), human solves CAPTCHAs, final submit
   confirmed by the user.
6. Coverage: La Bonne Alternance API token (best alternance source); APEC/Cadremploi need a headful run.
7. Bench labels (`tests/bench/labels.json`) still assume Bac+2 — relabel before re-benchmarking.

User preferences: work on the SSD; delegate to Sonnet or Codex subagents (Codex also for reviews); commit only when asked.
