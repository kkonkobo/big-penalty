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
uv run pytest -q                                  # 441 tests
uv run jobhunt search 1 -n 10                     # headful browser by default; JOBHUNT_HEADLESS=1 for headless
uv run jobhunt rescore 1                          # rescore stored jobs after profile/prompt change
uv run jobhunt serve                              # http://127.0.0.1:8765
uv run jobhunt daily [PROFILE_IDS...]             # headless search of every profile + French summary (~27 min/profile, paid LLM)
uv run jobhunt new 1                              # "new today" jobs of profile 1
uv run jobhunt schedule install [--time 07:30 ...]  # launchd agent com.bigpenalty.daily; --time repeatable (several runs/day)
uv run jobhunt schedule set 07:30 19:00           # replace all times (installs if needed); also: add HH:MM | remove HH:MM
uv run jobhunt schedule run                       # on-demand run now (launchctl kickstart; the daily lock prevents overlap)
uv run jobhunt schedule status                    # loaded?, times, next run, run in progress, log tail (also: uninstall)
uv run big-penalty cv import 1 --pdf CV.pdf       # milestone 2a: CV -> data/profiles/1/master.pending.yaml + report (1 paid LLM call); then CONFIRM in the web UI; or --text FILE; --stage-qualifier "…"
uv run big-penalty cv tailor JOB_ID               # tailored CV for a job (2 paid LLM calls: tailor + verifier): new revision, flags, one-page PDF in data/applications/<job_id>/rev-<n>/
uv run big-penalty cv list [-p PROFILE]           # applications and status (web: /applications, master CV: /profiles/<id>/cv)
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

## Milestone 2a: tailored CV per job (done, not yet committed; review fixes applied)
Package `src/jobhunt/cv/`: `master.py` (Master + stable ids, `data/profiles/<pid>/master.yaml`, confirmation, photo lookup), `importer.py`
(PDF/text -> pending master, ONE LLM call, verbatim check + identity check), `tailor.py` (ONE structured call; titles/dates/schools expanded by
code from master ids; domain restricted to words of the ad), `tailored.py` (data model + code-built headline), `check.py` (rule fact check),
`verify.py` (LLM entailment verifier), `render.py` (HTML/PDF + page fitting), `store.py` (applications + revisions), `service.py`
(orchestration, approval, `is_eligible`), `errors.py`.
- **Policy (user decision 2026-11-17)**: tailored CVs aim to get through ATS / auto-reject filters. Embellishment is ALLOWED: assertive
  reframing, stronger verbs, larger scope, the ad's keywords/tools/skills added to skills, summary and bullets even without master support.
  HARD FACTS must match the master (a recruiter can verify them): employers / jobs / internships / any work experience (projects stay academic
  projects), degrees, schools, year of study, grades, dates and durations, certifications, language levels, identity, work authorization.
  Every embellishment is listed per revision in "Préparation entretien" (flag level `prep`, with a French hint) so the candidate can back it up.
- **Fact check** (`check.py`, on the FINAL page content). Blocking errors (hard facts / structure): bullet without source ids, invalid ids or ids
  from another project; altered project title/dates/context, education, language name/level; language-level claims (A1–C2, bilingue, natif,
  courant, TOEIC…) absent from the master anywhere in the text. `prep` (allowed): skill items not in the master; numbers, tools, proper nouns
  in a bullet not in its cited sources; terms in summary / headline / labels not in the master. Warning: summary too long.
- **LLM verifier** (`verify.py`): ONE structured call per tailored CV on the final content (bullets with their cited sources, summary and skills
  with the whole master). Each unsupported statement is classified `hard_fact` (blocking error) or `embellishment` (`prep` flag + prep_hint).
  Missing verdict or verifier not run = blocking error (fail closed). Prefers the OpenAI provider (a different model than the Anthropic tailor),
  else Anthropic; provider/model recorded in `verifier_json`.
- **Revisions**: table `application_revisions` (tailored_json UNTRIMMED, flags, verifier, pdf_path, pdf_sha256, fit_notes, master_hash). One
  active application per (profile, job); generate/edit/regenerate = new revision of the same application, created atomically (revision +
  `current_rev` + status draft + approval cleared; one transaction, `BEGIN IMMEDIATE`). PDFs live in `data/applications/<job_id>/rev-<n>/` and are
  never overwritten. Failure (LLM, render, stale rev) = no new revision, previous one untouched. Forms carry `rev`; a stale rev is refused.
- **Statuses**: draft->approved, approved->draft (edit), draft/approved->withdrawn, withdrawn->draft ("Restaurer"; also a new revision), approved->submitted|failed
  (milestone 2b). Approval records approved rev + PDF sha256 and re-hashes the file; "Ignorer et approuver" only with `ignore` = `1`/`on`, bound to the revision,
  sets `approval_override`.
- **Master confirmation**: import writes `master.pending.yaml` + `master.pending.report.json` (unmatched strings, identity fields name/email/phone/linkedin
  not in the source or different from the Profile) + `pending-photo.jpg`; `/profiles/<id>/cv` shows it with disputed fields highlighted and the button
  "Confirmer ce CV maître" (writes `confirmed_at` + `content_hash`, moves pending -> active). Any save/edit of master.yaml unconfirms it (hash check
  also catches edits on disk). Tailoring needs a confirmed master. The live profile-1 master is active but UNCONFIRMED: click confirm in the UI.
- **Eligibility for auto-submit** (`service.is_eligible(app) -> (bool, reasons)`): no blocking error AND verifier ran and passed AND master confirmed with the
  same hash the revision was tailored from AND the exact current revision is approved (no manual override) AND the one-page PDF inside
  `applications_dir` still matches the approved sha256. Human override only allows a manual approval; it never makes a CV auto-eligible.
  Application 1 (made before revisions) is a draft with revision 1 without hash/verifier: regenerate it to become eligible.
- **Page fit**: font to 92 %, then last bullet of the last project, then last project; recorded in `fit_notes` (separate from the untrimmed content, persists across
  edits). Fact check and verifier run AFTER trimming on the exact content of the PDF; a PDF is published only if the browser's pagination gives exactly 1 page.
  Otherwise no PDF and blocking flag "ne tient pas sur une page" (the user can edit).
- **Files/links**: PDF download requires a resolved path inside `applications_dir` with `.pdf`; photo must be an image inside the profile dir (symlinks resolved);
  job links are rendered only for http/https.
- **LLM calls**: `FailoverLLM.structured_ex/structured` have one overall deadline (`JOBHUNT_LLM_DEADLINE_S`, default 180, retries + failover included); errors are
  short codes (`LLMError.code`), never model output; web messages go through a server-side one-shot flash (`?f=token`), never in URLs.
  `JOBHUNT_CV_ALLOW_FAILOVER` (default 1): 0 = CV data goes only to the FIRST enabled provider (verifier included).
- DB: migration 3 (revisions, approval columns, partial unique index); migrations run under `BEGIN IMMEDIATE` before reading `user_version`.
- Settings `JOBHUNT_CV_MODEL` (default = planner model), `JOBHUNT_CV_EFFORT`. Web: "Préparer le CV" on shortlist rows, `/applications`, `/applications/<id>`, `/profiles/<id>/cv`.
- Master per candidate: `identity.stage_qualifier` in master.yaml; a re-import keeps it. `applications.job_id` has no foreign key on purpose.
- Not done: cover letter, auto-approval function (eligibility already accepts an approved revision).

## Next: milestone 2b = apply on the origin ATS
Open the company's own ATS page (`origin_url`, `origin_ats`) with the approved CV of the application (only if `service.is_eligible`; PDF = revision path), autofill (Greenhouse/Lever/Workday/
SmartRecruiters/…) using the profile + master identity, human solves CAPTCHAs, final submit confirmed by the user, then set
`applications.status` = submitted (or failed) and the job status = applied. Account creation on ATSs is automatic (see milestone 2 decisions).

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
5. **Milestone 2**: 2a tailored CV = done (see above); 2b = autofill on the origin ATS with the approved CV (Greenhouse/Lever/Workday/SmartRecruiters/…),
   human solves CAPTCHAs, final submit confirmed by the user.
6. Coverage: La Bonne Alternance API token (best alternance source); APEC/Cadremploi need a headful run.
7. Bench labels (`tests/bench/labels.json`) still assume Bac+2 — relabel before re-benchmarking.

User preferences: work on the SSD; delegate to Sonnet or Codex subagents (Codex also for reviews); commit only when asked.
