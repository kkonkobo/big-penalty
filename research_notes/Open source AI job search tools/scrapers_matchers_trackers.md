# Open-source scrapers, matchers/resume tools, trackers and job-hunt agents (state as of 2026-11-17)

Method note: stars/license/push dates come from the `gh` CLI (GitHub API) on 2026-11-17; feature details from each repo's README/file tree read via `gh api` (no code was cloned or run). GitHub search was rate-limited partway, so long-tail searches (especially French scrapers and embedding-matcher repos) are incomplete.

## Scrapers and aggregators (JobSpy, JobFunnel, ATS boards, French boards)

### Takeaway
JobSpy is the dominant maintained multi-board scraper library, and it now covers France via Indeed, Glassdoor and a dedicated HelloWork scraper. JobFunnel is archived. ATS-board (Greenhouse/Lever/Ashby/Workday) scraping is handled by small or embedded modules, not one canonical project. French-specific scrapers (WTTJ, APEC, France Travail) exist only as tiny, unvetted repos, so no strong standalone OSS option turned up.

### Cited Findings
- JobSpy (speedyapply/JobSpy): 4,751 stars, MIT, Python, last push 2026-11-13, not archived. — [repo](https://github.com/speedyapply/JobSpy)
- JobSpy's current sites: LinkedIn, Indeed, Glassdoor, ZipRecruiter, Bayt, Naukri, BDJobs and HelloWork; `google` is still listed as a parameter but the FAQ says "Google Jobs is currently unavailable". Python >=3.10, output as a pandas DataFrame, plus a CLI (`jobspy`) and an MCP server (`mcp-jobspy`). Proxy support, `hours_old`, `is_remote`, `fetch_description`, `description_format` (markdown/html/plain). — [README](https://github.com/speedyapply/JobSpy)
- JobSpy country coverage: LinkedIn is global (uses `location` only); ZipRecruiter is US/Canada; Indeed and Glassdoor take `country_indeed` (default `usa`). Indeed covers ~60 countries, including France, Germany, Belgium, Switzerland, Luxembourg, UK, Spain, Italy, Netherlands, Canada, Australia, India, Japan and Singapore. Glassdoor (marked `*`) covers fewer: France, Germany, Belgium, Switzerland, UK, Spain, Italy, Netherlands, Ireland, Canada, USA and others, but not Luxembourg. Bayt covers the Middle East, Naukri India, BDJobs Bangladesh. — [README](https://github.com/speedyapply/JobSpy)
- JobSpy HelloWork: searches France, location takes a French city/postcode/region, and "most jobs are posted in French, so French search terms find more". — [README](https://github.com/speedyapply/JobSpy)
- JobSpy caveats: most boards are capped at about 1,000 results per search; LinkedIn returns 429 on bursts; Glassdoor rate-limits above ~2 requests/second; Indeed is described as the best scraper with no rate limiting; Indeed search also matches descriptions, so use `-`, quotes and OR operators. Default `country_indeed` is USA, so non-US users must set it. — [README](https://github.com/speedyapply/JobSpy)
- JobSpy wrappers and forks: rainmanjam/jobspy-api (384 stars, MIT, Dockerized API with key auth, last push 2025-05-29); borgius/jobspy-mcp-server (128 stars, JS, last push 2026-03-13); alpharomercoma/ts-jobspy (17 stars, TypeScript rewrite, last push 2026-09-17); t0mer/Applyfy (7 stars, FastAPI + JobSpy + n8n LinkedIn digest). — [gh search](https://github.com/search?q=jobspy&type=repositories)
- JobFunnel (PaulMcInnis/JobFunnel): 2,198 stars, MIT, ARCHIVED (read-only since 2025-12-10). Last release 3.1.0 (Oct 2024). Targeted Indeed, Monster and Glassdoor (Glassdoor needed manual CAPTCHA); CSV output with de-duplication. Dead. — [repo](https://github.com/PaulMcInnis/JobFunnel), [search summary](https://github.com/PaulMcInnis/JobFunnel)
- agentic-career-search (Francis1998): 102 stars, no license file shown by gh, Python 3.11+/FastAPI, last push 2026-11-13. Claims 119 ATS/job-board adapters (Greenhouse, Lever, Workday, Ashby, SmartRecruiters, schema.org JSON-LD). Deterministic scoring with a stored rationale, Atom feed, read-only MCP server, `POST /jobs/match` for resume matching, optional LLM enrichment, SQLite by default. Not a French-specific tool. The repo is small and new, so treat the "119 adapters" claim as self-reported. — [repo](https://github.com/Francis1998/agentic-career-search)
- ATS-endpoint scraping in other projects: JobSync's discovery covers Greenhouse (directory of 2,400+ companies), Lever (1,160+, with automatic EU/US API selection) and Ashby (1,860+), with no API key needed. — [JobSync README](https://github.com/Gsync/jobsync)
- career-ops has ATS scan scripts (`scan-ats-full.mjs`, `scan-dayforce.mjs`, `scan-hn.mjs`, `scan-interamt.mjs` for German public sector, `scan-seek.mjs` for Australia/NZ, `scan-topdev.mjs`, `discover-ats.mjs`, `check-liveness.mjs`). Source: file listing of the repo. — [repo tree](https://github.com/career-ops-hq/career-ops)
- LinkedIn access via MCP: stickerdaniel/linkedin-mcp-server, 4,235 stars, Apache-2.0, Python, last push 2026-11-17. Gives MCP-compatible agents access to LinkedIn profiles and companies (the description also mentions jobs). — [repo](https://github.com/stickerdaniel/linkedin-mcp-server)
- French-board scrapers found by `gh search` are all tiny and unvetted: APEC (Alexis2mlt/apec-scraper, 0 stars, Python, "commercial CDD/stage/alternance offers via a JSON endpoint", last push 2026-09-17; plus Apify-actor stubs); Welcome to the Jungle (datadmon/welcome-to-the-jungle-jobs-scraper, 0 stars, MIT, last push 2026-11-06; MaximeBF2000/welcome-to-the-jungle-scraper, 0 stars; piloterr/welcometothejungle-scraper, a commercial API stub; Gemna-growth, 2 stars, last push 2023-10-04); France Travail API wrappers (cmnemoi/france-travail-api, 0 stars, Apache-2.0, "high-level Python SDK", last push 2026-07-01; other 0-1 star repos). — [gh search: welcome to the jungle](https://github.com/search?q=welcome+to+the+jungle+scraper&type=repositories), [gh search: france travail api](https://github.com/search?q=france+travail+api&type=repositories)
- Search results also contained many 0-star "Greenhouse/Lever/Ashby scraper" repos that are Apify/API marketing stubs (e.g. Rel8ble, datadmon, bondvit). These are not real OSS building blocks. — [gh search](https://github.com/search?q=greenhouse+lever+ashby+jobs+scraper&type=repositories)

### Inferences
- For a French-market tool, the practical OSS path is JobSpy (Indeed FR, Glassdoor FR, HelloWork) plus the official France Travail API, using a thin wrapper like cmnemoi's or a custom client. WTTJ and APEC would need custom or fragile scraping. I found no mature OSS scraper for them.
- Python+pandas JobSpy output is easy to feed into an LLM scoring step. Its 1,000-result cap and 429 behavior mean scheduled, low-volume runs with proxies are needed.
- ATS public-board coverage (Greenhouse/Lever/Ashby) skews to US/global tech startups, not French employers.

### Gaps
- Not verified: whether the WTTJ/APEC scrapers actually work, or their legal and ToS posture (out of scope).
- Not found: a maintained OSS Workday scraper with meaningful stars; Workday endpoints appear only inside multi-adapter repos.
- Not verified: whether JobSpy's LinkedIn and Glassdoor scrapers currently work reliably in France (README only claims support).
- Google Jobs: only a README claim that it is unavailable in JobSpy; no replacement OSS tool was checked.

## Matching/scoring and resume tools (Resume-Matcher, Reactive Resume, OpenResume, RenderCV)

### Takeaway
Resume-Matcher and Reactive Resume are the active LLM-enabled resume tailoring tools, and both ship French UI translations. RenderCV is the best deterministic YAML-to-PDF CV builder, with a French locale. OpenResume is US-centric and stale. Embedding-based matching exists mostly as an embedded feature, not as a standalone popular repo.

### Cited Findings
- Resume-Matcher (srbhr): 29,320 stars, Apache-2.0, last push 2026-11-16, release v1.4.0 (2026-10-21). FastAPI backend, Next.js frontend, SQLite. Flow: upload master resume (PDF/DOCX), paste JD, review AI tailoring, cover letter, interview prep, PDF export. LLMs: OpenAI, Anthropic, Gemini, DeepSeek, OpenRouter, Groq, or local Ollama. The tailoring prompts forbid inventing skills or metrics. The ATS score (`services/ats.py`) and ATS parse check (pdfminer) use no LLM. MCP server for Claude Code/Cursor/Codex. — [README](https://github.com/srbhr/Resume-Matcher)
- Resume-Matcher UI locales (frontend messages): en, es, fr, ja, ko, pt-BR, zh. README is available in English, Spanish, Chinese, Japanese and Portuguese. Backend services include keyword_forcing, bullet_scoring, cover_letter, interview_prep and text_match. — [frontend messages](https://github.com/srbhr/Resume-Matcher/tree/main/apps/frontend/messages), [services](https://github.com/srbhr/Resume-Matcher/tree/main/apps/backend/app/services)
- Resume-Matcher templates are engineering-style (Deedy, Modern Deedy, LaTeX-like, single/two-column). Whether any template matches the French CV convention (photo, personal details) was not seen. — [README](https://github.com/srbhr/Resume-Matcher)
- Reactive Resume: 45,851 stars, MIT, TypeScript, last push 2026-11-16, release v6.1.1 (2026-11-13). Stack: React 19 SPA, TanStack Router, Vite, PostgreSQL + Drizzle, oRPC/OpenAPI/MCP, Redis and S3-style storage. AI Assistant connects OpenAI, Anthropic, Gemini, OpenRouter, Ollama or compatible endpoints. It can tailor a resume and draft a cover letter for a saved job, plus an AI writing review and PDF checks. Multiple interface languages. Imports: JSON, LinkedIn and PDF work without AI, and Word needs a provider. — [README](https://github.com/reactive-resume/reactive-resume)
- RenderCV (rendercv/rendercv): 17,965 stars, MIT, Python, last push 2026-10-31, release v2.8 (2026-03-21). YAML in, Typst-rendered PDF out, with themes (Classic, Engineeringresumes, Sb2nov). A French locale file exists at `src/rendercv/schema/models/locale/other_locales/french.yaml`, so section/date strings can be French. No LLM inside, but there is an `rendercv-skill` repo for AI agents (25 stars). — [repo](https://github.com/rendercv/rendercv), [french.yaml path via code search](https://github.com/rendercv/rendercv/tree/main/src/rendercv/schema/models/locale)
- OpenResume (xitanggg/open-resume): 8,989 stars, AGPL-3.0, TypeScript (Next.js, Redux Toolkit), last push 2024-10-29 (stale, about 12+ months with no commits; not archived). A resume builder plus a resume parser to test ATS readability. Its README says the design "adheres to U.S. best practices". No LLM features. — [repo](https://github.com/xitanggg/open-resume)
- jsonresume/resume-cli: 4,710 stars, ARCHIVED (moved to jsonresume.org monorepo, npm `resume-cli` still published). JSON Resume schema ecosystem. — [repo](https://github.com/jsonresume/resume-cli)
- Other LLM tailoring repos found are tiny (AIResumeTailor 3 stars, Java; GenAI-Pipeline 0 stars). VibeHired-ai (17 stars, TypeScript) is a tracker that tailors CVs and scores ATS fit, and its license shows as `other`. — [gh search](https://github.com/search?q=resume+tailoring+llm&type=repositories)
- career-ops includes CV generators: HTML, LaTeX and Typst-like templates (jake, executive, compact, modern, leadership, CJK), `keyword-match.mjs`, `jd-skill-gap.mjs`, `verify-cv-facts.mjs`, `generate-cover-letter.mjs`, and ATS-rules YAML (`templates/ats-rules.yml`). — [repo tree](https://github.com/career-ops-hq/career-ops/tree/main/templates)
- agentic-career-search does deterministic resume-to-job matching with matched and missing skills, and does not store the resume. — [repo](https://github.com/Francis1998/agentic-career-search)

### Inferences
- Standalone "embeddings-based job matching" OSS repos with real adoption were not found. In the tools that matter, scoring is either LLM rubric scoring (career-ops, ai-job-search), keyword/deterministic scoring (Resume-Matcher ATS, agentic-career-search), or a hybrid of a local relevance score plus LLM on the top candidates (JobSync).
- For a French user, Reactive Resume (French UI, multi-language, flexible templates) and RenderCV (French locale, YAML) are the most plausible CV renderers. Resume-Matcher gives French UI but is oriented to US engineer resumes. OpenResume is the weakest fit.

### Gaps
- Not verified: whether Reactive Resume or Resume-Matcher have French-CV-specific templates (photo, état civil, "Compétences" sections), or whether their LLM prompts handle French-language job descriptions well.
- Not verified: Reactive Resume's full language list.
- Not checked: other cover-letter generators, Kickresume-like OSS clones, and pure ATS-scoring libraries beyond the above.

## Job trackers/CRMs with AI (JobSync and others)

### Takeaway
JobSync is the most relevant self-hosted tracker with AI matching and job discovery. Other trackers are small or stale. career-ops also contains a tracker (markdown/TSV based) and a dashboard.

### Cited Findings
- JobSync (Gsync/jobsync): 1,532 stars, MIT, TypeScript, last push 2026-11-16, release v1.2.1 (2026-11-14). Docker Compose with bundled Postgres (includes pgvector; the README says an upcoming release will use it for knowledge base and resume embeddings). Features: application tracker, dashboard, resume management (PDF export, AI import of PDF/Word), scheduled automated discovery via Greenhouse/Lever/Ashby with a local relevance score and AI match only on top candidates, AI assistant chat (resume review, job match scoring, cover letters), interviews, contacts, companies, question bank, timeline, AI usage stats, MCP server. LLMs: Ollama or any cloud provider (keys encrypted in settings). — [README](https://github.com/Gsync/jobsync)
- JobSync discovery only covers three ATS vendors (more "on the way"), so LinkedIn/Indeed/French boards are not auto-discovered; users add jobs by pasting or via MCP. Language/locale: nothing French-specific found in the README. — [README](https://github.com/Gsync/jobsync)
- Muatasim-Aswad/job-tracker: 34 stars, MIT, TypeScript, last push 2026-11-15. Deduplicates one record per job across reposts, platforms and emails. — [gh search](https://github.com/search?q=job+application+tracker&type=repositories)
- Others (low adoption): machadop1407/job-application-tracker (91 stars, last push 2026-01-20); rpunia29/applic (26, stale since 2024-12); adamrangwala/Job-Application-Tracker (22, Apps Script Gmail parser, 2025-05); ganainy/VibeHired-ai (17 stars, AI CV tailoring and ATS scoring, last push 2026-05-09). — [gh search](https://github.com/search?q=job+application+tracker&type=repositories)
- career-ops tracker components: `tracker.mjs`, `dedup-tracker.mjs`, `merge-tracker.mjs`, `followup-cadence.mjs`, `reply-watch.mjs`, `funnel-stages.mjs`, `ics-interview.mjs`, a dashboard build, and `outcome.mjs`. — [repo tree](https://github.com/career-ops-hq/career-ops)

### Inferences
- JobSync is the only widely used self-hosted tracker plus AI matcher in a standard web stack (Next.js/Postgres). It is the closest OSS analogue of a "copilot with tracking" UI.

### Gaps
- Not checked: JobSync's i18n; Huntr/Teal-style OSS clones with significant stars (none surfaced in my searches).
- Not checked: Notion or Airtable-based templates.

## Personal job-hunt agent pipelines (ai-job-search, career-ops, daily digests)

### Takeaway
The category exploded in 2026: two Claude-Code-style frameworks (career-ops and ai-job-search) have tens of thousands of stars and are copilots with human-submit, not auto-apply. Simpler Python + LLM + Telegram/Discord/n8n digest scripts exist but are tiny.

### Cited Findings
- ai-job-search: the repo originally at MadsLorentzen/ai-job-search now resolves to ai-job-search-dev/ai-job-search (72,195 stars per `gh`, MIT, Python, last push 2026-11-17). Built on Claude Code; commands: `/setup`, `/scrape`, `/rank`, `/apply`, `/interview`, `/outcome`. Requires Claude Code (subscription or API credits), Python 3.10+, Bun (portal CLIs) and LaTeX (`lualatex`/`xelatex`) for CV and cover letter PDFs. Drafter-reviewer pipeline for CV and cover letter, ATS parseability check via pypdf. Stores profile data in tracked files, so a private copy rather than a public fork is advised. Author reports 69 applications, 20 first interviews and 1 job. — [README](https://github.com/ai-job-search-dev/ai-job-search)
- ai-job-search is "language- and country-agnostic"; there are no bundled scrapers and `/scrape` falls back to WebSearch. Per-board portal skills live in community-portals (8 stars, last push 2026-11-10), which currently has `dk` (Denmark, jobindex) and `global` (linkedin-search, freehire-search) only, with no France folder. Other agents (Codex, Gemini CLI) are supported via AGENTS.md. — [community-portals](https://github.com/ai-job-search-dev/community-portals), [README](https://github.com/ai-job-search-dev/ai-job-search)
- career-ops (career-ops-hq/career-ops): 83,211 stars per `gh`, MIT, JavaScript (Node .mjs scripts plus markdown "modes"), last push 2026-11-17. Pitch: paste or scan a job, get a 1-5 score against your CV and a do-not-apply recommendation, with tailored CV and drafted answers, and "You press Submit". Works across Claude Code, Codex, Gemini, OpenCode, Cursor and others, and has runners for OpenAI, Gemini, OpenRouter and Ollama (free/local model docs). Landed press in Business Insider and WIRED (Greece), per its README. README exists in 20 languages. — [README](https://github.com/career-ops-hq/career-ops)
- career-ops French support: `modes/fr/` (offre, postuler, pipeline, interview) is a French translation of the main modes for France, Belgium, French-speaking Switzerland, Luxembourg and Quebec. The README names Welcome to the Jungle, Indeed FR, APEC, Pole emploi/France Travail and LinkedIn FR as target sources, and French contract specifics (convention collective, RTT, mutuelle, 13th month, période d'essai, préavis, titres-restaurant, intéressement/participation, portage salarial). Activated via `language.modes_dir: modes/fr` in `config/profile.yml`. Also `modes/regional/eu-swe.md` and jurisdiction YAMLs (AI screening disclosure, vacancy disclosure and others). — [modes/fr/README.md](https://github.com/career-ops-hq/career-ops/blob/main/modes/fr/README.md)
- I could not confirm that career-ops ships a native scanner for WTTJ, APEC or France Travail (portals.example.yml returned nothing for those names; the scan scripts seen target ATS boards, HN, Germany, Australia and TopDev). The French modes are evaluation/prompting translations. — [repo tree](https://github.com/career-ops-hq/career-ops)
- Forks and spinoffs: career-ops-plugin (512 stars, Claude Cowork plugin); career-ops-china (114); tw-house-ops (115); Fighter90/career-ops-ui (100); career-ops-docs (90). — [gh search: career-ops](https://github.com/search?q=career-ops&type=repositories)
- AIHawk (feder-cr/Jobs_Applier_AI_Agent): 32,574 stars, MIT, active (push 2026-11-17). It is an auto-apply bot, so out of scope; listed only to distinguish it from the copilots. — [repo](https://github.com/feder-cr/Jobs_Applier_AI_Agent)
- Small digest/alert pipelines (all under 20 stars): t0mer/Applyfy (FastAPI+JobSpy+n8n LinkedIn digest, Apache-2.0), nihalpothunoori/robotics-job-scout (hourly Discord alerts), nes3152/job-alert-bot (LinkedIn/Indeed/Glassdoor/Handshake with AI match scoring), lyt3ral/job-scraper-ts (Workday/Greenhouse/Lever/Ashby scraper with AI analysis and Telegram), Shashwat-Manglam-Jain/job-scraper (CSV/Google Sheet). — [gh search](https://github.com/search?q=job+scraper+linkedin+indeed&type=repositories)

### Inferences
- The two big star counts (72k and 83k) are very high for this niche; they are as reported by the GitHub API on the research date, and I did not verify organic growth. The renaming of ai-job-search to a new org and its trending badges suggest viral marketing. I recommend treating stars as a popularity signal, not a quality signal.
- ai-job-search and career-ops are prompt/skill frameworks that run inside an agent CLI, not services. The cost model is the user's LLM subscription or API tokens, and the "database" is local markdown/TSV/files. Scraping is delegated to plug-ins (portals) or LLM WebSearch.
- Of the big agent frameworks, only career-ops has explicit French modes; ai-job-search is neutral but has no French portal skill yet.

### Gaps
- Not read in depth: career-ops scoring rubric and ai-job-search `/rank` criteria; ai-job-search CV template and whether it supports French CV conventions (LaTeX templates are English by default as far as I saw).
- No standalone French "daily digest" repo with Telegram/email alerts and LLM scoring was found by the searches that completed.
- Legal/regulatory aspects of scraping and data sources are out of scope.

## Summary table (stars as of 2026-11-17)

| Project | Role | Stars | License | Activity | Stack | LLM | FR support |
|---|---|---|---|---|---|---|---|
| JobSpy | scraper | 4,751 | MIT | active | Python | none | Indeed FR, Glassdoor FR, HelloWork |
| JobFunnel | scraper | 2,198 | MIT | ARCHIVED 2025-12 | Python | none | no |
| agentic-career-search | ATS aggregator + matcher | 102 | not shown | active | Python/FastAPI | optional | no |
| Resume-Matcher | tailoring | 29,320 | Apache-2.0 | active | FastAPI/Next/SQLite | many + Ollama | FR UI |
| Reactive Resume | resume builder + AI | 45,851 | MIT | active | React/Postgres | many + Ollama | multi-language UI |
| RenderCV | CV renderer | 17,965 | MIT | active | Python/Typst | none | French locale |
| OpenResume | builder/parser | 8,989 | AGPL-3.0 | stale (2024-10) | Next.js | none | US-centric |
| JobSync | tracker + discovery | 1,532 | MIT | active | Next.js/Postgres | Ollama + cloud | none seen |
| career-ops | agent framework | 83,211 | MIT | active | Node + modes | multi-provider | modes/fr |
| ai-job-search | agent framework | 72,195 | MIT | active | Claude Code + Python | Claude | neutral, no FR portal |
| AIHawk | auto-apply (out of scope) | 32,574 | MIT | active | Python | yes | n/a |
