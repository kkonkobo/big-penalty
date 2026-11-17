# Reference repo MadsLorentzen/ai-job-search (now ai-job-search-dev/ai-job-search) and its forks

Data pulled 2026-11-17 via `gh api`. Raw fork list (first ~6,800 of 23k) saved at /Volumes/SSD/Dev/big-penalty-scratch/forks_all.tsv.

## What does it do end-to-end, and how is it built?

### Takeaway
It is not a standalone app. It is a repo of Claude Code slash commands, skills and subagent prompts, plus small Python helpers and LaTeX templates. It runs the pipeline setup, scrape, rank, apply (tailored CV and cover letter PDFs), interview prep and outcome tracking. It deliberately stops before submitting: no auto-apply, and the human sends the application.

### Cited Findings
- Repo description: "The job search that runs on your machine. AI job application framework built on Claude Code: evaluate postings, tailor CVs, write cover letters, prep interviews. Fork it and own it." Language Python, MIT license, created 2026-03-18, last pushed 2026-11-17. Topics include claude-code, latex, cv, cover-letter. — [GitHub API](https://api.github.com/repos/MadsLorentzen/ai-job-search)
- Stars 72,195 and forks 23,216 (2026-11-17), open issues 71. The default branch is `master`. The repo moved to the org `ai-job-search-dev`, and the old URL redirects. — [GitHub API](https://api.github.com/repos/ai-job-search-dev/ai-job-search); [CHANGELOG.md](https://github.com/ai-job-search-dev/ai-job-search/blob/master/CHANGELOG.md)
- Release cadence: automatic weekly tags every Monday 06:00 UTC via `.github/workflows/release.yml` and commitizen. The changelog is frozen at 1.7.4. Latest releases are v1.7.4 (2026-11-02), v1.7.5 (2026-11-09) and v2.0.0 (2026-11-16). — [CHANGELOG.md](https://github.com/ai-job-search-dev/ai-job-search/blob/master/CHANGELOG.md); releases API
- Author origin story from the README: Mads Lorentzen, a geophysicist, lost his position in late 2025 and built it for his own search. Results claimed: 69 tailored applications, 20 first interviews, 1 signed contract, and he started as an AI engineer in June 2026. The README links a longer LinkedIn write-up. — [README](https://github.com/ai-job-search-dev/ai-job-search#readme); LinkedIn profile https://www.linkedin.com/in/mads-lorentzen
- Commands live in `.claude/commands/` (setup, scrape/job-scraper skill, rank, apply, interview, outcome, gmail-sync, notion-sync, html-report, expand, upskill, add-template, add-portal, reset). The README states six core commands and 14 in total.
  - Subagents are in `.claude/agents/`: posting-fetcher, posting-scorer, application-reviewer.
  - Methodology skill files are `.claude/skills/job-application-assistant/01..09-*.md`: candidate profile, behavioral profile, writing style, job evaluation, CV templates, cover-letter templates, interview prep, application forms, web research.
  - Profile data lives in `CLAUDE.md` with `[YOUR_*]` placeholders that `/setup` fills in. — [repo tree](https://github.com/ai-job-search-dev/ai-job-search)
- `/apply` is a drafter-reviewer loop:
  1. Parse the posting, which first passes a deterministic integrity gate (`tools/posting_guard.py`: hidden text, invisible characters, instruction-shaped lines, hidden links).
  2. Evaluate fit across five dimensions, including a "Language Gate" that hard-rejects postings requiring an undeclared language.
  3. Draft the CV and cover letter in LaTeX (moderncv banking style compiled with lualatex; custom `cover.cls` with Lato/Raleway compiled with xelatex).
  4. A fresh-context reviewer agent researches the company and critiques the drafts.
  5. Revise, then compile and inspect the PDFs (CV exactly 2 pages, letter exactly 1 page).
  6. ATS text-layer check via pypdf or pdftotext (`tools/verify_pdf.py`, `verify_layout.py`), with keyword coverage scored and gaps never stuffed.
  7. Present a verification checklist. Claims are verified against the profile; no fabrication.
  — [README](https://github.com/ai-job-search-dev/ai-job-search#readme)
- Discovery and scraping: `/scrape` discovers every portal skill under `.agents/skills/*/SKILL.md`, which are Bun/TypeScript CLIs with a `search`/`detail` contract, `--format json|table|plain` output and an `enabled:` frontmatter flag. WebSearch is the fallback. It queries one plain term per call, limits to the last 14 days, and dedupes via `tools/scrape_state.py`, `tools/job_key.py` and `job_scraper/seen_jobs.json`. Search queries come from `.claude/skills/job-scraper/search-queries.md`. — [job-scraper SKILL.md](https://github.com/ai-job-search-dev/ai-job-search/blob/master/.claude/skills/job-scraper/SKILL.md)
- Ranking: `/rank` fans out parallel agents to fetch and score postings against the fit framework. It handles deal-breaker vetoes, deadline urgency and expiry flags, and `tools/rank_state.py` records the results. — README
- Tracking and notifications: `job_search_tracker.csv` plus `documents/applications/<company>_<role>/outcome.md`. `/outcome followup` drafts follow-ups after 10 quiet days (drafts only, never sends). `/gmail-sync` proposes status updates from Gmail for approval. `/notion-sync` publishes a one-way read-only view via the official Notion MCP. `/html-report` generates an offline dashboard. There is no push or email notification system and no scheduler in core; the only scheduled item is the weekly upstream-watch GitHub workflow for forks. — README; `.github/workflows/upstream-watch.yml`
- LLM and agent framework: Claude Code only, with no model name pinned in the files I read. It needs a Pro/Max/Team subscription or API credits. `AGENTS.md` provides a pointer for Codex, Gemini CLI and Antigravity. There is no database or UI: storage is flat files (markdown, JSON, CSV, LaTeX/PDF) and the UI is the CLI. — README
- Security posture: postings are untrusted input and instructions embedded in them are not followed. A `.claude/settings.json` allowlist plus `tools/security_guards.py`, `tools/install_portal_clis.py` (installs with `--ignore-scripts`) and SECURITY.md. The README says the defenses are "instruction-level, not a sandbox". — README
- Auto-apply: none. The template "stops one step short of the send and always will", per the pinned fork-index thread. — [Discussion #78](https://github.com/ai-job-search-dev/ai-job-search/discussions/78)

### Inferences
- Architecture is a prompt-and-file framework. Adaptation to another market means changing portals, search queries, language and CV conventions, not code.
- The 72k stars and 23k forks (almost all forks pushed 2026-10-21, mass-created with 0 stars) suggest most forks are one-click personal copies, not real derivatives.

### Gaps
- I did not verify the exact Claude model used; no model pin was seen.
- I did not read the LinkedIn post (login-walled). I found no HN or Reddit threads by the author.

## Which job boards and market is it optimized for?

### Takeaway
The core is now pitched as country- and language-agnostic. Its origin is Denmark: the Danish boards were bundled until PR #829 (2026-11-16, "feat!: move the portal skills to community-portals", v2.0.0 breaking change). Core now ships no boards.

### Cited Findings
- Core ships zero portal CLIs. Portals live in https://github.com/ai-job-search-dev/community-portals (8 stars): `portals/dk/` (jobbank, jobdanmark, jobindex, jobnet) and `portals/global/` (linkedin-search, freehire-search). — [community-portals](https://github.com/ai-job-search-dev/community-portals)
- The README calls the core workflow "language- and country-agnostic".
- Remaining Nordic assumptions: `salary_lookup.py` is an optional tool that handles Danish/Nordic characters, legal suffixes (A/S, ApS) and union salary data. Recent commits include "match thorn and eth in Nordic ASCII spellings" (#720). — [README_SALARY_TOOL.md](https://github.com/ai-job-search-dev/ai-job-search/blob/master/tools/README_SALARY_TOOL.md); commit log
- The fork policy is that market-specific portals live in forks, not upstream (policy discussions #31, #39, #49, #67). — [Discussion #78](https://github.com/ai-job-search-dev/ai-job-search/discussions/78)

### Inferences
- Before November 2026, the default experience was Danish (Jobindex, Jobnet). It is now generic but still relies on WebSearch unless a portal is copied in.

### Gaps
- I did not diff the pre-#829 tree to list every Danish hardcode.

## Forks: census and notable adaptations

### Takeaway
There are about 23k forks. Notable adapted forks cover China, Brazil, Canada, UK, Spain, Italy, Poland, Germany, Netherlands, Norway, Vietnam, Korea, Colombia, Mexico, Austria and the US. I found no French fork and no real US-market fork beyond one SimplifyJobs-based skill.

### Cited Findings
- Enumeration: `gh api .../forks --paginate` was stopped after ~6,800 of 23,216 forks. Most are untouched copies, with `pushed_at` equal to the upstream 2026-10-21 push.
- The maintainer's curated index, pinned Discussion #78 (174 comments), lists these. It applies a vetting checklist covering clean branch, portal contract, safety pass, honest scoping, and a human gate on any send. — [Discussion #78](https://github.com/ai-job-search-dev/ai-job-search/discussions/78)
  - China: cypggs/ai-job-search-cn; youngledo/ai-job-search (China overlay)
  - Vietnam: jamesng16/ai-job-search-vn (ITviec skill)
  - Spain: erikpr1994/ai-job-search (InfoJobs, Tecnoempleo, GetManfred)
  - Brazil: davicalvarenga/ai-job-search (Gupy, Vagas.com, PT-BR guide)
  - Poland: janlukowicz/ai-job-search (justjoin.it, nofluffjobs); the earlier pandawidkur repo is deleted
  - US: chenyuan99/ai-job-search (swelist-search over SimplifyJobs new-grad and internship data)
  - Germany/DACH: rasstamann/ai-job-search-germany (xing-search; StepStone and Indeed documented as blocked)
  - Netherlands: Fabian-Schreuder/ai-job-search-nl (nationalevacaturebank JSON API)
  - Colombia: FARLEY-PIEDRAHITA-OROZCO/ai-job-search (computrabajo, elempleo)
  - Norway: ThomasHjartarson/ai-job-search-no (nav-search, nav-feed-sweep)
  - Flowxtra: 9mtm/ai-job-search (flowxtra-search, multi-country ATS API; author works at Flowxtra)
  - Harness ports: trikto (Codex), Affan-p (OpenCode), MadCkull/J-Argus (Google Antigravity)
- Top forks by stars (2026-11-17), with commit-ahead evidence from the compare API (`compare/master...owner:branch`) — [forks API](https://api.github.com/repos/ai-job-search-dev/ai-job-search/forks?sort=stargazers):
  - youngledo/ai-job-search-cn: 50 stars; 36 commits ahead, 1 behind (28 files); China market overlay, Chinese README and "China public job search workflow"; actively merges upstream.
  - davicalvarenga/ai-job-search: 29 stars; 37 ahead, 25 behind; Gupy and Vagas.com portal skills and "match CV language to posting"; PT-BR README.
  - hahaha375/ai-job-search-auto-scraper: 17 stars; 7 ahead, 236 behind, 95 files; mainland-China campus and SOE recruitment automation. It runs a daily workflow via the Codex CLI in WSL and reads WeChat official-account screenshots and links, local PDF/Word/Excel job docs, and monitored fixed state-enterprise and institution web pages. It produces Chinese daily reports, does local OCR of certificates, and helps fill web-form fields with user confirmation. Its `.agents/skills` include recruiter-discovery, website-job-monitor, wechat-job-inbox and portal-assistance. — [fork README](https://github.com/hahaha375/ai-job-search-auto-scraper)
  - Affan-p/ai-job-search: 16 stars; OpenCode port (`opencode.json`).
  - p3ji/ai-job-search-ca (branch `canada`): 15 stars; 92 ahead, 292 behind, 159 files. Job Bank and GC Jobs portal skills, Talent.com skill, Canadian CV conventions, and sector routing between public and private sector.
  - anjolok1997/ai-job-search-uk: 11 stars; 27 ahead, 316 behind. UK rebrand with city targeting, a Totaljobs portal, cross-portal graduate search and a visa-sponsorship gate.
  - erikpr1994/ai-job-search-spain: 10 stars; 16 ahead, 161 behind, 193 files. InfoJobs, Tecnoempleo, GetManfred and Joppy portals; localizes Danish assumptions; adds a `sync-upstream` command.
  - andreadorizza/ai-job-search-italy: 8 stars; 37 ahead. Italian conventions, a restricted-portal opt-in tier, eures-search, and schema.org/JobPosting reading (Randstad Italia).
  - MadCkull/J-Argus: 7 stars; Antigravity harness.
  - chenyuan99/ai-job-search: 4 stars; 5 ahead. swelist-search and `/gmail-sync` (the latter appears to have later been merged upstream).
  - Other small forks seen by description in the list:
    - sangwookp9591/ai-job-search-kr: Korean, JobKorea, Saramin and Wanted, with an `/apply-kr` cover-letter workflow.
    - alanoide97/ai-job-search-MX: Mexico.
    - VulpesZerda27/ai-job-search: Austria, Claude Code and Codex.
    - Byte-freq/ai-job-search: runs on local llama.cpp inference through Claude Code (other-LLM angle).
    - vmarmolr/ai-job-search: scheduled daily job delivery on Windows.
    - Drumbler/ai-job-search: Russian.
    - Dkui97: Chinese.
    - YoungsikMoon: Korean.
    - dattk2002: Codex-first.
    - estOnion/ai-job-search-tw: Taiwan.
    - ashutoshgh/ai-job-search-india: India.
- Fork-index posture on automation: any fork that fills or submits applications must have an unconditional human confirmation in code, single-recipient only, and no captcha or bot-protection bypass. — Discussion #78

### Inferences
- The fork ecosystem is mainly localization (portals plus language) and harness ports (Codex/OpenCode/Antigravity). Alternative LLMs appear only through the harness, apart from one local-llama.cpp fork. Auto-apply forks are rare and gated.
- Scheduling and automation (daily runs) is the main feature missing from the core and added by forks such as hahaha375 and vmarmolr.

### Gaps
- France: I found no fork named or described for France (Welcome to the Jungle, France Travail, APEC). It was checked in ~6,800 sampled fork names and descriptions, plus `gh search repos`. Possibly present in the unscanned ~16k, or only as an unlabeled copy. The French candidate would need to be built from the generic core plus `/add-portal`.
- US: only chenyuan99 (SimplifyJobs-based) was found; no Greenhouse, Lever or Workday portal fork surfaced.
- I did not inspect fork code for most of the long tail. Stars of most forks are 0-2.

## Author and third-party writing on design decisions

### Takeaway
The author's own explanation is the README plus a LinkedIn post I could not read. I found no HN or Reddit threads. Third-party blogs exist.

### Cited Findings
- Third-party review (Joao Queiros) stating design decisions: not an auto-apply bot, evidence base before generation, fit scoring before drafting, a separate reviewer agent, PDF verification, human-controlled submission, and outcome feedback. The author is quoted only via paraphrase. — [ai.joaoqueiros.com](https://www.ai.joaoqueiros.com/blog/ai-job-search-agent-mads-lorentzen-claude-human-in-loop)
- Other write-ups surfaced by search (not opened, quality varies, several look AI-generated): [dev.to review](https://dev.to/andrew-ooo/ai-job-search-review-claude-code-as-your-job-hunter-18c7), [knightli](https://knightli.com/en/2026/07/08/ai-job-search-claude-code-job-application-workflow/), [tosea.ai](https://tosea.ai/blog/ai-job-search-get-offer-madslorentzen-guide), [instagit](https://instagit.com/MadsLorentzen/ai-job-search/how-ai-job-search-framework-uses-claude-code/), [notes.nicolasdeville.com](https://notes.nicolasdeville.com/github/ai-job-search/).
- The README links a YouTube walkthrough by "The Next New Thing" (https://www.youtube.com/watch?v=HoVxjMNFYv4, recorded August 2026).

### Inferences
- Design rationale is documented mainly in CHANGELOG.md, CONTRIBUTING.md and the SKILL files, which give detailed reasoning per change.

### Gaps
- I did not retrieve the author's LinkedIn post, and found no HN or Reddit discussion.
