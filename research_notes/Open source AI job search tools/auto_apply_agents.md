# Open-source AI job application automation agents (auto-apply bots and agentic job-search assistants)

Snapshot date: 2026-11-17. Star counts, licenses, push dates and topics come from the GitHub API (`gh api repos/...`, `gh search repos`) on that date. Several `gh search` calls hit the API rate limit, so coverage of the long tail (n8n, MCP, browser-use) is thinner than for the headline projects. Primary sources are the repos' own READMEs and docs, cloned only as text via the API (nothing was executed).

## Which projects are significant, and what are their stats, stack, LLMs, automation method, supported boards and features?

### Takeaway
The field splits into three generations. (1) Selenium/Playwright LinkedIn Easy Apply bots (GodsScion, wodsuz, joaosilvalopes, AIHawk v1). (2) Agentic, LLM-driven form fillers that work on any ATS (ApplyPilot, the rewritten AIHawk, browser-use forks). (3) Human-in-the-loop "career agents" shipped as Claude Code/Codex skills, where career-ops (about 83k stars) dominates and deliberately never submits. Nothing found is archived among the headline projects, but several are dormant or went commercial.

### Cited Findings

#### Catalog table (GitHub API, 2026-11-17)
| Project | Stars | License | Created / last push | Stack | Status |
|---|---|---|---|---|---|
| [feder-cr/Jobs_Applier_AI_Agent](https://github.com/feder-cr/Jobs_Applier_AI_Agent) (AIHawk; formerly Auto_Jobs_Applier_AIHawk, old URLs redirect) | 32,574 (4,759 forks) | MIT | 2024-08-04 / 2026-11-17 | Python, terminal app, Playwright, Firefox | Active (full rewrite) |
| [career-ops-hq/career-ops](https://github.com/career-ops-hq/career-ops) (formerly santifer/career-ops) | 83,212 (15,284 forks) | MIT | 2026-04-04 / 2026-11-17 | JavaScript/Node, Claude Code plugin, Playwright | Active |
| [GodsScion/Auto_job_applier_linkedIn](https://github.com/GodsScion/Auto_job_applier_linkedIn) | 3,045 (837 forks) | MIT | 2023-06-17 / 2026-11-08 | Python, Selenium, undetected-chromedriver, local Flask control panel | Active |
| [Pickle-Pixel/ApplyPilot](https://github.com/Pickle-Pixel/ApplyPilot) | 1,836 (652 forks) | AGPL-3.0 | 2026-02-17 / 2026-03-08 | Python, Claude Code CLI plus Playwright MCP, Gemini | Dormant since March 2026 (no pushes in about 8 months) |
| [wodsuz/EasyApplyJobsBot](https://github.com/wodsuz/EasyApplyJobsBot) | 839 (225 forks) | custom "other" | last push 2026-05-18 | Python/Selenium | Free tier of a commercial product (Apllie) |
| [joaosilvalopes/linkedin-easy-apply-bot](https://github.com/joaosilvalopes/linkedin-easy-apply-bot) | 373 | MIT | 2023-02-19 / 2025-10-17 | TypeScript, regex-config, no LLM | Low activity |
| [NathanDuma/LinkedIn-Easy-Apply-Bot](https://github.com/NathanDuma/LinkedIn-Easy-Apply-Bot) | 256 | none | last push 2023-06-11 | Python/Selenium | Dead (unmaintained since 2023) |
| [madingess/EasyApplyBot](https://github.com/madingess/EasyApplyBot) | 175 | GPL-3.0 | last push 2025-03-12 | Python/Selenium | Stale |
| [naumov-alex/LinkedIn-AI-Job-Applier-Ultimate](https://github.com/naumov-alex/LinkedIn-AI-Job-Applier-Ultimate) | 186 | MIT | 2025-10-14 / 2026-10-23 | Python 3.12, Playwright, browser-use | Active AIHawk v1 fork |
| [kaymen99/Upwork-AI-jobs-applier](https://github.com/kaymen99/Upwork-AI-jobs-applier) | 170 | none | 2024-07-12 / 2025-05-01 | LangGraph/LangChain, Playwright | Stale; Upwork, prepares proposals only |
| [vaibhavarora14/job-application-agent](https://github.com/vaibhavarora14/job-application-agent) (JobAppAgent) | 175 | MIT | 2026-08-12 / 2026-11-17 | Node CLI plus Agent Skill (Claude Code/Codex/Cursor) | Active |
| [yvonnehe772/applypilot](https://github.com/yvonnehe772/applypilot) | 319 | MIT | 2026-06-28 / 2026-06-28 | unknown | Name-collision/clone; not verified |
| [averatec0773/offeros](https://github.com/averatec0773/offeros) | 46 | Apache-2.0 | 2026-07-26 / 2026-10-19 | Next.js, SQLite, Chrome side-panel extension | Pre-alpha |
| [shuheng-mo/career-ops-china](https://github.com/shuheng-mo/career-ops-china) | 114 | MIT | 2026-04-07 / 2026-11-17 | career-ops fork for China | Active |

Counts of 3k+ stars exist only for AIHawk, career-ops, and GodsScion; everything else is under 2k (this is from the star-sorted `gh search` results).

#### AIHawk (feder-cr)
- The repo was created in August 2024, and the fork ecosystem describes the original as "currently inactive" ([naumov-alex README](https://github.com/naumov-alex/LinkedIn-AI-Job-Applier-Ultimate)). The same repo is now a different product: a terminal app that "turns your resume into a search", searches "millions of postings of the last 30 days from 37 public sources, rebuilt every five hours, searched on your computer", scores each posting 1-5, lets the user pick jobs, then opens "a real Firefox" that fills the company's own form, attaches the CV and submits, keeping a screenshot per application — [README](https://github.com/feder-cr/Jobs_Applier_AI_Agent)
- LLM access is via the OrcaRouter gateway key (affiliate-linked in README); "an application costs a few cents". macOS can find and score jobs but cannot apply because the Firefox build has no Mac version — [README](https://github.com/feder-cr/Jobs_Applier_AI_Agent)
- Per-ATS status table (build of 2026-11-11, job library of 6.2M postings by host): Greenhouse (~93k postings), Ashby (~45k), Lever (~47k), JazzHR (~135k), BambooHR (~39k) are "sent and confirmed on a real form"; JazzHR and BambooHR are blocked by reCAPTCHA image puzzles on real postings; Personio "stops at a captcha about half the time" (Turnstile passed on 2 of 4). Many failures are required questions the CV and answers do not cover — [docs/sites.md](https://github.com/feder-cr/Jobs_Applier_AI_Agent/blob/main/docs/sites.md)
- Closed-posting pre-check by plain HTTP for Lever, Ashby, Greenhouse, Workable, BambooHR, Paylocity and Workday; the app now reports the user's two-letter country code to its makers; the job library window was cut from 60 to 30 days — [CHANGELOG](https://github.com/feder-cr/Jobs_Applier_AI_Agent/blob/main/CHANGELOG.md)
- Press: TechCrunch, The Verge, 404 Media (Oct 2024), Semafor, Business Insider (Nov 2024), Wired Italia, Vanity Fair Italia are listed in the README. TechCrunch's piece describes someone who used AIHawk to apply to 2,843 jobs (17 in an hour) — [TechCrunch](https://techcrunch.com/2024/10/10/a-reporter-used-ai-to-apply-to-2843-jobs/); more than 6,000 Telegram community members and the claim that it is hard to detect are from Business Insider coverage as summarised in search results — [Business Insider NL](https://www.businessinsider.nl/a-free-ai-tool-can-help-you-apply-to-hundreds-of-jobs-per-day-users-said-there-are-risks-but-theyre-worth-it/)
- Federico Elia (founder) has said he never encouraged people to use AIHawk to apply for jobs, and the old README carried an "education purposes only" disclaimer (search-result summary, not primary) — [Business Insider NL](https://www.businessinsider.nl/a-free-ai-tool-can-help-you-apply-to-hundreds-of-jobs-per-day-users-said-there-are-risks-but-theyre-worth-it/)
- The README promotes team-run SEO/affiliate content sites (autoapplytools.com, jobhuntlab.com etc.) and a sponsor banner, i.e. a funnel/monetisation model; the code stays MIT — [README](https://github.com/feder-cr/Jobs_Applier_AI_Agent)
- Notable forks: naumov-alex "Ultimate" fork (LinkedIn plus Indeed, Gemini/OpenAI/Claude/Ollama, Telegram reports, dashboard, uses browser-use to handle non-Easy-Apply LinkedIn jobs, per-vacancy tailored resumes; notes Indeed is mostly external redirects with frequent CAPTCHAs) — [README](https://github.com/naumov-alex/LinkedIn-AI-Job-Applier-Ultimate); `DerekSuu/aihawk-job-applier` (1 star, "Only works on BOSS", i.e. China's Boss Zhipin); several 0-12 star forks, one archived (`samolat/Auto_Jobs_Applier_AIHawk`, archived, 2 stars) — [gh search](https://github.com/search?q=AIHawk&type=repositories)

#### GodsScion/Auto_job_applier_linkedIn
- LinkedIn-only. Runs locally on the user's own Chrome and LinkedIn account; "can apply to 100+ jobs in under an hour"; a local control panel (Account/Profile/Search/Filters/Run tabs) writes `user_config.json`; AI is optional ("fills in the application questions — optionally with AI help"). A `stop_before_submit` dry-run setting stops at the Review step — [README](https://github.com/GodsScion/Auto_job_applier_linkedIn)
- Topics include selenium, undetected-chromedriver, easy-apply — [GitHub API](https://github.com/GodsScion/Auto_job_applier_linkedIn). Its legal doc says the user must review LinkedIn's automation policies and accepts no liability — [docs/legal.md](https://github.com/GodsScion/Auto_job_applier_linkedIn/blob/main/docs/legal.md)
- Specific LLM providers are in `docs/config-secrets.md`, which I did not read (gap).

#### ApplyPilot (Pickle-Pixel)
- Six stages: Discover (Indeed, LinkedIn, Glassdoor, ZipRecruiter, Google Jobs via JobSpy, plus 48 Workday employer portals and 30 direct career sites), Enrich (JSON-LD, then CSS selectors, then AI extraction), Score (1-10), Tailor resume ("never fabricates"; `resume_facts` preserved), Cover letter, Auto-apply. Auto-apply launches Claude Code CLI driving a Playwright MCP server in Chrome, with parallel workers, optional CapSolver key for hCaptcha/reCAPTCHA/Turnstile, `--dry-run`, and an HTML/live dashboard. Gemini free tier for scoring/tailoring — [README](https://github.com/Pickle-Pixel/ApplyPilot)
- README claims "Applied to 1,000 jobs in 2 days"; this is a self-claim, unverified. It first published 2026-02-17 and warns about unaffiliated applypilot.app / useapplypilot.com look-alike products — [README](https://github.com/Pickle-Pixel/ApplyPilot)
- AGPL-3.0 (the README comparison table frames AIHawk as "LinkedIn Easy Apply only", which describes AIHawk v1 and is outdated against the current rewrite). Dedicated forks: ibarrajo/ApplyPilot (27 stars), Deep6Cardinal/ApplyPilot-Enhanced, madnessbrainsbl/applypilot — [gh search](https://github.com/search?q=ApplyPilot&type=repositories)

#### career-ops (santifer -> career-ops-hq)
- "The open-source AI job search agent": evaluates a pasted job (is it still open, does it fit, scored 1-5 with an A-H evaluation), tailors an ATS CV (LaTeX/PDF), drafts form answers, interview prep with a STAR story bank, negotiation scripts, application email drafts, an application tracker/dashboard; runs inside Claude Code (plugin and slash commands), Codex, OpenCode, Gemini/Cursor-style CLIs. Portal scanner ships with 180+ preconfigured companies and 40+ search queries across Ashby, Greenhouse, Lever and Wellfound — [README](https://github.com/career-ops-hq/career-ops)
- Explicit design stance: "The script never POSTs"; `apply` mode fills the form via Playwright and "stops before Submit"; "career-ops is a filter, not a spray-and-pray auto-applier". The README disclaimer tells users to follow ToS of Greenhouse, Lever, Workday, LinkedIn and warns models "can behave unpredictably" — [README](https://github.com/career-ops-hq/career-ops)
- Origin: the author reports 740 listings evaluated, 68 applied, 12 interviews, 1 offer; self-reported. Press cited in README: WIRED (Greek edition), Business Insider (April 2026). 800+ contributors, 20 README translations, a "HIRED" wall of user stories, web-version waitlist (possible commercial path) — [README](https://github.com/career-ops-hq/career-ops)
- Repo moved from the personal account to the `career-ops-hq` org; forks and derivatives include a China edition (`shuheng-mo/career-ops-china`, 114 stars), a UI (`Fighter90/career-ops-ui`, 100), a browser extension (`itshoax/career-ops-extension`, 60), Taiwan/other regional variants — [gh search](https://github.com/search?q=career-ops&type=repositories)
- Region modes exist, e.g. an `eu-swe` mode for European software-engineering applications and a list of European pre-configured companies (Factorial, Attio, Tinybird, Clarity AI, TravelPerk) — [README](https://github.com/career-ops-hq/career-ops)

#### Claude Code / agent-skill / MCP-based and other agents
- JobAppAgent: an Agent Skill installed via `npx job-application-agent@latest install`; applies within user rules on Greenhouse, Ashby and Lever, uses one verified résumé, stores the profile in the OS credential store, never reads cookies, pauses for SSO/MFA/CAPTCHA, records only submissions confirmed by the employer site; has opt-out telemetry and a jobs board (jobappagent.com) — [README](https://github.com/vaibhavarora14/job-application-agent)
- OfferOS: local-first Next.js/SQLite app plus a Chrome side-panel extension covering Greenhouse, Lever, Ashby and Workday forms; agent loop with a budget of 6 steps and at most 2 changes per turn; submit gating enforced in tool code, not prompts; "Submitting is yours" — [README](https://github.com/averatec0773/offeros)
- Claude-Code-specific small repos (all single-digit stars): `lsdeva/cv-to-applied` (7), `wbohanw/joku` (2), `zxybs1/indeed-auto-apply-jobs` (Claude Code plus Playwright for Indeed, 2), `eurafaeldecarvalho/aplica` (desktop cockpit for an auto-apply plugin, 3), `Sma1lboy/coforce-apply` (5) — [gh search](https://github.com/search?q=claude+code+job+application&type=repositories)
- Other agent apps: `torontodeveloper/job-application-agent` (18, GPT with Playwright, human reviews and submits), `Aditya-00a/Instaply` (18, Ollama plus Playwright, local-first), `billmal071/job-agent` (9, LinkedIn/Indeed/Glassdoor with AI scoring), `YuexinJ778/AutoApply` (6, Chrome extension) — [gh search](https://github.com/search?q=job+application+agent&type=repositories)
- Job-search MCP servers are small (top: `hiddenjobs/job-search-mcp` 28 stars, `aryaminus/h1b-job-search-mcp` 17 for US H-1B/DOL data, `folathecoder/adzuna-job-search-mcp` 15, `ArsenOria/tau-linkedin-job-research-mcp` 14 via Apify). They are search/analysis only, not appliers (largely out of scope) — [gh search](https://github.com/search?q=job+search+mcp&type=repositories)
- n8n job workflows: only 0-star repos surfaced (e.g. `bandarsiri2026/bewerbung-automation`, Telegram to German Anschreiben generation). No significant n8n auto-apply project found — [gh search](https://github.com/search?q=n8n+job+application&type=repositories)
- browser-use itself is a general library (121k stars, MIT) — [repo](https://github.com/browser-use/browser-use); its use for job applying appears as a component in forks (naumov-alex) rather than a standalone prominent job project.
- Upwork variant: kaymen99 UpworkScribe scrapes, scores (7/10 threshold), writes cover letters and interview scripts using LangGraph; user submits — [README](https://github.com/kaymen99/Upwork-AI-jobs-applier)

### Inferences
- Stars are concentrated in two repos; career-ops's 83k stars in about 7.5 months, its 15k forks, and Trendshift/press mentions suggest a viral "agent skill" distribution effect rather than a cumulative base. Star counts may be partially promotion-driven; I did not audit.
- The market is moving from brittle site-specific selectors (Selenium, Easy Apply) to LLM-driven browser agents on ATS forms (Greenhouse/Ashby/Lever are the easy, standardised targets; Workday and CAPTCHA-protected boards the hard ones).
- Commonly paired components: a job feed (JobSpy or ATS public JSON), an LLM scorer (1-5 or 1-10), a resume/cover-letter tailor, an agentic form-filler, and a tracker/dashboard.

### Gaps
- LLM provider lists for GodsScion (docs/config-secrets.md) and the exact LinkedIn/Indeed coverage of the current AIHawk rewrite (docs/sites.md was only partially read) were not verified.
- No confirmed list of "LinkedIn Easy Apply bot" and browser-use projects beyond the star-sorted search; GitHub rate limiting cut the search on "greenhouse lever auto apply", "browser-use job apply", and the Welcome to the Jungle query.
- `yvonnehe772/applypilot` (319 stars, single push) was not inspected; it may be a copy or name-collision.
- Reddit/HN threads were not searched.

## What are the common architectural patterns, failure modes (bans, ToS, CAPTCHAs, ATS variability), and project fates?

### Takeaway
Typical pipeline: discover -> enrich -> LLM score -> tailor -> form-fill -> track. The main failure modes are CAPTCHAs, unanswerable required questions, closed/stale postings and UI churn; platform-ban risk is real for LinkedIn but I found no documented enforcement against a specific repo. Fates: pivot to commercial SaaS or paid tiers (EasyApplyJobsBot -> Apllie), rewrite/rebrand (AIHawk), org migration (career-ops), abandonment (NathanDuma, original AIHawk code).

### Cited Findings
- Pipeline pattern is stated by ApplyPilot (6 stages), AIHawk (resume -> search -> score -> pick -> apply -> keep answer) and career-ops (scan -> evaluate -> tailor -> apply -> track) — [ApplyPilot](https://github.com/Pickle-Pixel/ApplyPilot), [AIHawk](https://github.com/feder-cr/Jobs_Applier_AI_Agent), [career-ops](https://github.com/career-ops-hq/career-ops)
- Human-in-the-loop split: career-ops (never submits), OfferOS (gate enforced in tool code), JobAppAgent (user chooses whether the agent sends, pauses for SSO/MFA/CAPTCHA), GodsScion (`stop_before_submit` option) versus fully autonomous ApplyPilot and AIHawk (submits after one user approval) — READMEs linked above
- Automation methods: Selenium plus undetected-chromedriver (GodsScion), Selenium (wodsuz, NathanDuma, madingess), Playwright MCP driven by Claude Code (ApplyPilot), Playwright with Firefox and an LLM agent (AIHawk rewrite), browser-use (naumov-alex), Chrome extension (OfferOS, YuexinJ778/AutoApply) — READMEs linked above
- CAPTCHAs: ApplyPilot offers CapSolver integration and otherwise "fails gracefully"; AIHawk's own table shows reCAPTCHA image puzzles blocking JazzHR/BambooHR and Turnstile blocking Personio about half the time; naumov-alex says Indeed "aggressively deploys CAPTCHAs" and most Indeed jobs redirect to external sites — [ApplyPilot](https://github.com/Pickle-Pixel/ApplyPilot), [AIHawk sites](https://github.com/feder-cr/Jobs_Applier_AI_Agent/blob/main/docs/sites.md), [naumov-alex](https://github.com/naumov-alex/LinkedIn-AI-Job-Applier-Ultimate)
- ATS variability and stale postings: AIHawk logs closed-posting redirect behaviours per ATS (e.g. Greenhouse `?error=true`, BambooHR redirects to `/careers`) and added HTTP pre-checks; career-ops warns some companies leave closed postings in public ATS APIs and added `--verify` Playwright liveness checks; AIHawk reports forms stopping at required questions the CV doesn't cover, and disabled Submit buttons on Ashby — [AIHawk sites](https://github.com/feder-cr/Jobs_Applier_AI_Agent/blob/main/docs/sites.md), [career-ops](https://github.com/career-ops-hq/career-ops)
- Fragility disclaimers: naumov-alex warns LinkedIn and Indeed "frequently change their UI, so the bot may lose some of its functionality AT ANY TIME" — [README](https://github.com/naumov-alex/LinkedIn-AI-Job-Applier-Ultimate)
- ToS/ban: GodsScion tells users LinkedIn has automation policies and accepts no liability; career-ops tells users to follow the ToS of Greenhouse, Lever, Workday, LinkedIn. Vendor blogs (competitors, so biased) say LinkedIn's User Agreement bans bots and may restrict accounts; I found no source confirming a specific ban or takedown action against AIHawk — [legal.md](https://github.com/GodsScion/Auto_job_applier_linkedIn/blob/main/docs/legal.md), [career-ops](https://github.com/career-ops-hq/career-ops), [Scoutify (competitor blog)](https://scoutify.com/blog/linkedin-auto-apply/)
- Commercial pivot: wodsuz/EasyApplyJobsBot README opens "Automated Bots is now Apllie", positions the repo as "the free version", lists PRO features (CSV export, AI-answered questions), sells packages and collects crypto donations, and warns about scam forks with phishing donation links — [README](https://github.com/wodsuz/EasyApplyJobsBot)
- Name-squatting/look-alikes: ApplyPilot warns about applypilot.app and useapplypilot.com, which it says are unaffiliated — [README](https://github.com/Pickle-Pixel/ApplyPilot)
- Fates: AIHawk's original Selenium/LangChain codebase was abandoned and replaced in the same repo by the rewrite, with forks carrying the old line (naumov-alex); NathanDuma bot last pushed June 2023; ApplyPilot has had no pushes since March 2026; career-ops moved to an org and is considering a hosted web version (waitlist) — [naumov-alex](https://github.com/naumov-alex/LinkedIn-AI-Job-Applier-Ultimate), [NathanDuma](https://github.com/NathanDuma/LinkedIn-Easy-Apply-Bot), [career-ops](https://github.com/career-ops-hq/career-ops)
- Societal criticism: The Verge ("AI is enabling job seekers to think like spammers"), Semafor, 404 Media — headlines as listed in the AIHawk README — [README](https://github.com/feder-cr/Jobs_Applier_AI_Agent)

### Inferences
- The shift to "assist, don't submit" (career-ops, OfferOS, JobAppAgent) looks like a response to ToS, ban, accuracy and spam-backlash concerns; this is my reading, not stated by the authors except for career-ops's "filter, not spray-and-pray".
- Open-source auto-apply projects have not been taken down in the sources found; the dominant fate is drift to commercial SaaS or abandonment due to UI churn.
- ATS standardisation (Greenhouse/Lever/Ashby) is the reason new projects target those three first.

### Gaps
- No evidence found on LinkedIn legal action, DMCA takedowns, or documented account-ban rates tied to a named repo.
- No primary HN/Reddit threads read; no measured success or interview-conversion rates beyond self-reports.
- Exact date and reasoning of AIHawk's rewrite not established (commit history not read).

## Which projects have French-market or US-market specificity?

### Takeaway
No major open-source auto-apply agent is France-specific; the headline projects are global or English/US-centric. France-specific work exists only as tiny tools around France Travail and HelloWork. US specificity is visible in ATS coverage (Workday, Greenhouse, EEO fields) and H-1B tooling.

### Cited Findings
- France-related repos found: `DorianABDS/france-travail-chrome-extension` (4 stars, monthly job-seeker status refresh, pushed 2026-11-11), `hollitizz/francetravail-extension` (2, application tracking), `catsmoker/France-Travail-Job-Automator` (2), `Zeffut/JobScraper` (3, aggregator: LinkedIn, HelloWork, France Travail, WTTJ, Adzuna), `Stiimy/sylph` (3, "automated apprenticeship search agent for French job platforms (HelloWork, France Travail, ...)"), all via star-sorted `gh search repos "france travail"` — [gh search](https://github.com/search?q=france+travail&type=repositories)
- Searches for a French auto-apply agent ("candidature automatique", "welcome to the jungle auto apply", "jobs bot indeed france") returned nothing relevant (the latter hit rate limiting, so treat as inconclusive)
- career-ops has a French README translation and a European SWE mode (`eu-swe`) plus a list of European companies, but is not FR-targeted; AIHawk's ATS list includes EU-hosted Greenhouse and Lever (`jobs.eu.lever.co`) and Personio, which is common in Germany; it reports the user's country code to its makers — [career-ops](https://github.com/career-ops-hq/career-ops), [AIHawk sites](https://github.com/feder-cr/Jobs_Applier_AI_Agent/blob/main/docs/sites.md), [CHANGELOG](https://github.com/feder-cr/Jobs_Applier_AI_Agent/blob/main/CHANGELOG.md)
- Other country/market variants: career-ops-china, a TW variant, a German-language n8n workflow (`bandarsiri2026/bewerbung-automation`), an Indian-oriented discovery list in JobAppAgent, a Brazilian MCP (`lauroPereira/job-search-mcp`, Portuguese), and AIHawk fork for China's BOSS; `aryaminus/h1b-job-search-mcp` is US-only (DOL H-1B data) — [gh search](https://github.com/search?q=job+search+mcp&type=repositories)
- US specificity: ApplyPilot's 48 Workday employer portals and ZipRecruiter/Indeed US boards; OfferOS EEO presets; JobAppAgent eligibility checks (work authorisation); joaosilvalopes config has a `REQUIRES_VISA_SPONSORSHIP` field — [ApplyPilot](https://github.com/Pickle-Pixel/ApplyPilot), [OfferOS](https://github.com/averatec0773/offeros), [joaosilvalopes](https://github.com/joaosilvalopes/linkedin-easy-apply-bot)

### Inferences
- For a French user, practical options are global tools (career-ops with a custom portal list, AIHawk, ApplyPilot) with ATS coverage of Greenhouse/Lever/Ashby/Personio; French boards (France Travail, WTTJ, HelloWork, APEC) are not natively covered by any significant auto-apply project found, which is a market gap.
- Not confirmed: whether career-ops's French README is a full translation of features (only the language link was seen).

### Gaps
- No evidence on APEC, Indeed France or Welcome to the Jungle auto-apply support in any project; GitHub search was rate limited and no web search for these was run.
- France Travail has an official API, but the legality/ToS of automating the candidate side was not researched.
