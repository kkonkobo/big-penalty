# Handoff — state on 2026-11-17

Personal job-search tool for the French market (family use, not for sale). Local web app + CLI.

## Product goal
Run **once a day**, surface **fresh** jobs matching a *function/major* (not a title), let the user approve,
then **apply on the company's own site/ATS** with a **CV tailored to the role**, only after the user's OK.
LinkedIn/Indeed/other aggregators are discovery only.

Job ads are written by HR, not the hiring manager: treat requirements as a **wish list**, not hard filters.
Score on function fit and realistic eligibility (contract, level, location); don't drop a job because it lists
5 tools the candidate only partly has. Hard blockers: contract type and location only; education level is a wish-list factor.

## Setup
```
cd /Volumes/SSD/Dev/big-penalty && . ./env.sh     # always; keeps caches on the SSD, loads .env (keys, never commit)
uv run pytest -q                                  # 347 tests
uv run jobhunt search 1 -n 10                     # headful browser by default; JOBHUNT_HEADLESS=1 for headless
uv run jobhunt rescore 1                          # rescore stored jobs after profile/prompt change
uv run jobhunt serve                              # http://127.0.0.1:8765
uv run jobhunt daily [PROFILE_IDS...]             # headless search of every profile + French summary (~27 min/profile, paid LLM)
uv run jobhunt new 1                              # "new today" jobs of profile 1
uv run jobhunt schedule install [--time 07:30 ...]  # launchd agent com.bigpenalty.daily; --time repeatable (several runs/day)
uv run jobhunt schedule set 07:30 19:00           # replace all times (installs if needed); also: add HH:MM | remove HH:MM
uv run jobhunt schedule run                       # on-demand run now (launchctl kickstart; the daily lock prevents overlap)
uv run jobhunt schedule status                    # loaded?, times, next run, run in progress, log tail (also: uninstall)
# Web UI: http://127.0.0.1:8765/schedule ("Planification": times, add/remove, "Lancer maintenant")
```
`daily`: non-blocking `flock` on the persistent `data/daily.lock` (released by the kernel when the process exits), log line per profile in `data/logs/daily-YYYY-MM-DD.log`, macOS notification
at the end (`JOBHUNT_NO_NOTIFY=1` to silence), exit ≠ 0 only if a profile's run failed (a failing source is not an error).
Always headless (passed explicitly to the pipeline, no global state), so sources needing a human (APEC/Cadremploi challenges) just report an error in the log.
`schedule install` writes `~/Library/LaunchAgents/com.bigpenalty.daily.plist` (StartCalendarInterval = array of {Hour, Minute}, the plist is the source of truth for the times; changes rewrite it then bootout+bootstrap; `/bin/zsh -lc`, launchd logs in
`~/Library/Logs/big-penalty/`, run output in `data/logs/launchd.*.log`); install/uninstall abort if a loaded agent cannot be booted out and runs `launchctl bootstrap gui/$UID`. If the Mac sleeps at the scheduled time, launchd runs the
missed job on wake (the catch-up we want); powered off = no run. The wrapper exits 0 quietly when the SSD is not mounted.
Nothing is installed by the code or tests until you run `schedule install` yourself.
Data: `data/jobhunt.sqlite3` (gitignored). Profile 1 = mechatronics student, **Bac+4** (2nd year of cycle ingénieur).

## Done (milestone 1: search + shortlist)
- 20 sources (aggregators, French boards, public/official, freelance); see `uv run jobhunt sources`.
- LLM plan + scoring: Anthropic Opus 5.5 primary, OpenAI failover, rule-based fallback. Benchmark in `reports/LLM benchmark.md`.
- Dedupe (fuzzy company/title/city), origin resolution to the company ATS/careers page (`src/jobhunt/origin.py`).
- Education-level rule in scoring; `rescore` command.
- First live run: 916 raw → 538 unique, 247 with origin URL, 43 scored ≥70 after the Bac+4 rescore.

## Freshness (done in this PR)
- Hard cutoff **14 days** (`MAX_JOB_AGE_DAYS` in `src/jobhunt/freshness.py`): searches request ≤14 days where the source
  supports it; the shortlist hides older jobs (kept in the DB, `include_stale=True` internally).
- Effective date: origin ATS date (`extra["origin_posted_at"]`) > earliest source date > `first_seen`; a date >1 day after
  `first_seen` = repost (badge "republiée"). "30+ jours" parses as a 30-day lower bound.
- Default ranking = fit × (0.4 + 0.6·0.5^(age/7)); sorts `rank` (default), `score`, `freshness`.
- **New today** (`freshness.is_new`, `db.query_jobs(new_only=True)`, web tab "Nouveautés du jour" with count badge, `jobhunt new`):
  `first_seen` ≥ the earlier of midnight Europe/Paris and the start of the profile's latest *completed* run, and not a repost.
  Still subject to the 14-day cutoff.
- **Expiry** (`EXPIRE_AFTER_DAYS = 7`, `db.query_jobs(include_expired=False)`): a job whose `last_seen` is older than 7 days is hidden
  (not deleted, status untouched), only if every source the job was seen on completed without error (`runs.stats.sources_ok`) in a run of the profile that started after its `last_seen` and finished within the last 2 days. Only real sightings bump `last_seen` (not rescore/origin updates).
  The web shortlist shows the number of hidden expired jobs under the table.
- Daily scheduler: done (see Setup). Not validated end-to-end yet: no real `daily` run or `schedule install` has been done.

## Scoring rules
- Only hard blockers: contract type and location. Education level, experience, tools, languages = wish list (moderate
  penalty, mentioned in the rationale). Fingerprint version 3. Jobs dropped by an older, stricter prefilter are rescored.

## Next steps (suggested order)
1. **Daily run**: code done (`daily`, `schedule`, `new`, expiry). To do by the user: one real `uv run jobhunt daily 1`, then
   `uv run jobhunt schedule install`, check `schedule status` the next morning.
2. **Speed**: 27 min for 10 queries; LinkedIn detail fetches dominate. Fetch details only after prefilter; cap per query.
3. ~~Wish-list scoring~~ done (see Scoring rules). Rescore after any prompt change.
4. Bugs: Jobijoba redirect URL with a contact-form-7 shortcode crashes origin resolution; 115 jobs with contract `unknown`;
   mis-parsed companies (e.g. "STATION F").
5. **Milestone 2 — tailored CV + apply**: per-job CV/cover letter from the user's real experience (never invent facts),
   user approval, autofill on the origin ATS (Greenhouse/Lever/Workday/SmartRecruiters/…), human solves CAPTCHAs, final submit
   confirmed by the user.
6. Coverage: La Bonne Alternance API token (best alternance source); APEC/Cadremploi need a headful run.
7. Bench labels (`tests/bench/labels.json`) still assume Bac+2 — relabel before re-benchmarking.

User preferences: work on the SSD; delegate to Sonnet or Codex subagents (Codex also for reviews); commit only when asked.
