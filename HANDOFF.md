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
uv run pytest -q                                  # ~1350 tests (~4.5 min: the apply engine tests drive a real headless browser)
uv run jobhunt search 1 -n 10                     # headful browser by default; JOBHUNT_HEADLESS=1 for headless
uv run jobhunt rescore 1                          # rescore stored jobs after profile/prompt change
uv run jobhunt serve                              # http://127.0.0.1:8765
uv run jobhunt daily [PROFILE_IDS...]             # headless search of every profile + French summary (~27 min/profile, paid LLM)
uv run jobhunt new 1                              # "new today" jobs of profile 1
uv run jobhunt schedule set 07:30 19:00           # replace all local times; also: add HH:MM | remove HH:MM
uv run jobhunt schedule run                       # start the same daily CLI in a separate process now
uv run jobhunt schedule status                    # enabled?, times, next run, run in progress, log tail
uv run jobhunt schedule uninstall-launchd         # remove the legacy macOS agent, after migrating its times
uv run big-penalty cv import 1 --pdf CV.pdf       # milestone 2a: CV -> data/profiles/1/master.pending.yaml + report (1 paid LLM call); then CONFIRM in the web UI; or --text FILE; --stage-qualifier "…"
uv run big-penalty cv tailor JOB_ID               # tailored CV for a job (2 paid LLM calls: tailor + verifier): new revision, flags, one-page PDF in data/applications/<job_id>/rev-<n>/
uv run big-penalty cv list [-p PROFILE]           # applications and status (web: /applications, master CV: /profiles/<id>/cv)
# Web UI: http://127.0.0.1:8765/schedule ("Planification": times, add/remove, on/off, "Lancer maintenant")
```
`daily`: non-blocking `flock` on the persistent `data/daily.lock` (released by the kernel when the process exits), log line per profile in `data/logs/daily-YYYY-MM-DD.log`, macOS notification
at the end (`JOBHUNT_NO_NOTIFY=1` to silence), exit ≠ 0 only if a profile's run failed (a failing source is not an error).
Always headless (passed explicitly to the pipeline, no global state), so sources needing a human (APEC/Cadremploi challenges) just report an error in the log.
The web server (including the desktop app) checks the atomic `data/schedule.json` every ~30 seconds. First use copies
`StartCalendarInterval` from `~/Library/LaunchAgents/com.bigpenalty.daily.plist` if present; otherwise it enables `07:30`.
Missed slots while the app is closed are not replayed. After sleep with the app open, only slots within a roughly
ten-minute grace window are eligible (the newest if several were crossed). Claimed slot IDs persist in `data/schedule-slots.json`,
including slots skipped because the daily lock was held. A daily subprocess inherits the server environment and uses
`python -m jobhunt daily` or, in the PyInstaller desktop app, the same bundled executable with the `daily` argument.
`schedule install` only enables in-app scheduling, with optional `--time` values; `schedule uninstall` only disables it.
Visit Planification (or run `schedule status`) before running `schedule uninstall-launchd`, which verifies that the legacy
agent is unloaded before deleting the plist. The app and its tests never uninstall it automatically.
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
- Settings `JOBHUNT_CV_MODEL` (default = `JOBHUNT_PLANNER_MODEL`, else `claude-sonnet-5-5`), `JOBHUNT_CV_EFFORT`. Web: "Préparer le CV" on shortlist rows, `/applications`, `/applications/<id>`, `/profiles/<id>/cv`.
- Master per candidate: `identity.stage_qualifier` in master.yaml; a re-import keeps it. `applications.job_id` has no foreign key on purpose.
- Not done: cover letter, auto-approval function (eligibility already accepts an approved revision).

## Milestone 2b: apply on the company's own site / ATS (branch `auto-apply`, not committed yet; automatic sending OFF)
Package `src/jobhunt/apply/`: `types.py` (shared contract), `snapshot.py` (page -> fields/labels/options/buttons, frames + open shadow roots),
`planner.py` (ONE LLM call per step: fields -> fact keys), `answer_check.py` (derivation rules, hard-fact matcher, LLM answer verifier),
`engine.py` (the step loop and every guardrail on what is typed / clicked), `facts.py` (known-facts catalogue from the Profile, the master and
`data/profiles/<pid>/answers.yaml`), `accounts.py` (ATS passwords: OS credential store via `keyring`, local JSON fallback, export/import),
`policy.py` (who may be sent), `store.py` (`application_runs`, `apply_reservations`), `service.py` (orchestration, queue, daily pass),
`letter.py` (cover letter only when a form requires one). Web: `web/apply_routes.py` + `apply_queue.html` ("Envois"), `apply_run.html`, `_apply_card.html`.
```
uv run big-penalty apply list [-p PROFILE]        # the sending queue (same as the web page /apply)
uv run big-penalty apply dry-run APP_ID           # fill the FIRST form page on the real site, send nothing; screenshots in data/applications/<job>/runs/<run>/
uv run big-penalty apply send APP_ID...           # the user's OK: approve + send (visible browser by default)
uv run big-penalty apply answer APP_ID            # answer the questions of a paused run (saved in answers.yaml `custom`, reused everywhere)
uv run big-penalty apply resolve APP_ID --sent/--not-sent   # settle an "envoi non confirmé"
uv run big-penalty apply trust PROFILE_ID HOST    # allow a form hosted on a site that is neither the company's nor a known ATS
uv run big-penalty apply auto PROFILE_ID          # what the daily run does (prepare CVs, auto-approve, send if switched on)
uv run big-penalty apply accounts | accounts-export FILE | accounts-import FILE | accounts-delete HOST EMAIL
```
- **Switches (all off by default)**: `JOBHUNT_APPLY_DAILY=1` = the daily run prepares CVs for fresh jobs scoring >= `JOBHUNT_APPLY_PREPARE_MIN_SCORE` (60)
  and auto-approves clean ones; `JOBHUNT_APPLY_AUTO=1` = it also SENDS those >= `JOBHUNT_APPLY_AUTO_MIN_SCORE` (60), at most `JOBHUNT_APPLY_DAILY_CAP` (10)
  per Paris day. Below the threshold an application waits on the "Envois" page for the user's OK ("Envoyer les N sélectionnées").
- **Who may be sent** (`policy.submit_problems`): approved exact revision + intact PDF + confirmed unchanged master; real company link (not an aggregator);
  job <= 14.0 days, not rejected/applied; no duplicate (same company + role, same origin URL, incl. reservations and `sent_origin_url`); no unresolved earlier attempt.
  Automatic trigger additionally: `service.is_eligible` (a manual override never auto-sends; `cvs.auto_approve` never bypasses blocking errors), score, cap,
  known age, not expired, `origin_confidence` >= 0.6. One send at a time across processes (`data/apply-send.lock`).
- **Answers (autonomy policy, user decision 2026-11-17)**: identity facts are typed verbatim by code, only into fields whose visible label fits
  (`facts.LABEL_RE`). Everything else goes through an independent LLM verifier (`supported | plausible | contradiction | hard_fact`). When no fact answers a
  REQUIRED question the planner DEDUCES a plausible answer; whatever code cannot back is recorded as `FilledField.inferred` and shown to the candidate as
  "Réponses déduites (à connaître avant l'entretien)". Only unknown HARD FACTS pause (`answer_check.is_hard_fact`: birth date, nationality, address, ID / bank
  numbers, driving licence, disability, work authorization, current salary). The candidate's explicit answers (`custom:` in answers.yaml) win over everything.
- **Dry run** is structurally unable to send: once any data is entered there is no click at all, every document navigation of the context is aborted, nothing is
  entered on account pages, pages go to about:blank at the end. So it only tests the first form page. Residual limit: an XHR/fetch fired by a page's own change handler.
- **Real send**: policy gate (`before_submit`) at the start of each step and before every click after data entry; destination binding (`host_allowed`: the job's
  registrable domain, a known ATS host, or a host the user trusted); a durable reservation BEFORE any click that may send (`nav_clicked_at` / `submit_clicked_at`);
  `submitted` needs the form gone + explicit receipt wording + the planner's verdict, anything else after such a click = "envoi non confirmé" (application `failed`,
  never retried until the user resolves it). Passwords never reach the LLM, logs, DB or screenshots (no screenshot after a password was typed).
- **Pauses** (bucket `paused` on /apply): unknown hard fact (`needs_input`), e-mail verification link (`needs_email`), visible CAPTCHA (`needs_human`, resume in a
  visible window), untrusted host. Headful windows open on the Mac's built-in display (`browser.window_args`, `JOBHUNT_WINDOW_SCREEN` / `JOBHUNT_WINDOW_POSITION`).
- **Web guard**: `origin_guard` in `web/app.py` (loopback Host on every request; writes need same-origin evidence). DB migrations 4-7.
- **Supervised dry runs so far** (2026-11-17/18, nothing sent): complete on a company careers site, Recruitee, a Teamtailor-style site and an in-house ATS;
  Talentsoft stops at the account page (accounts are created only on a real send); a Phenom/Workday flow reaches the form but pauses on address fields;
  SmartRecruiters showed a DataDome CAPTCHA after repeated visits. Reviewed three times by Codex (findings fixed each time); a fourth review is in progress.
- **Not done**: dedicated adapters (Workday, Talentsoft, SmartRecruiters), e-mail verification via IMAP, better origin resolution for jobs whose only link is a
  job board (16 board hosts were added to `origin.AGGREGATOR_DOMAINS`), a real send (waits for the user's OK on the dry-run screenshots).
- **November 18, 2026**: the legacy 07:30 launchd run failed with `operation not permitted` on the SSD. The in-app scheduler no longer uses launchd.

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
- Daily scheduler: covered by offline tests (see Setup); no real daily run or packaged scheduler smoke test performed.

## Scoring rules
- Only hard blockers: contract type and location. Education level, experience, tools, languages = wish list (moderate
  penalty, mentioned in the rationale). Fingerprint version 3. Jobs dropped by an older, stricter prefilter are rescored.

## Next steps (suggested order)
1. **Daily run**: after merge, migrate the times and remove the old agent. For a live check, leave Big Penalty open through
   a scheduled slot and inspect the Planification journal.
2. **Speed**: 27 min for 10 queries; LinkedIn detail fetches dominate. Fetch details only after prefilter; cap per query.
3. ~~Wish-list scoring~~ done (see Scoring rules). Rescore after any prompt change.
4. Bugs: Jobijoba redirect URL with a contact-form-7 shortcode crashes origin resolution; 115 jobs with contract `unknown`;
   mis-parsed companies (e.g. "STATION F").
5. **Milestone 2**: 2a tailored CV = done; 2b = built on branch `auto-apply` (see above): next are the user's OK on the dry runs, a first supervised send,
   then ATS adapters and switching `JOBHUNT_APPLY_DAILY` / `JOBHUNT_APPLY_AUTO` on.
6. Coverage: La Bonne Alternance API token (best alternance source); APEC/Cadremploi need a headful run.
7. Bench labels (`tests/bench/labels.json`) still assume Bac+2 — relabel before re-benchmarking.

User preferences: work on the SSD; delegate to Sonnet or Codex subagents (Codex also for reviews); commit only when asked.
