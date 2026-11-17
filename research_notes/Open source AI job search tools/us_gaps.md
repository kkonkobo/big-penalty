# US job-market coverage gaps for an open-source AI job search assistant

Research date: 2026-11-17. Complements france_us_markets.md (not duplicated: Greenhouse Job Board API, USAJOBS, JobSpy, hiQ, Proxycurl, NYC LL144, CA ADS, CO AI Act, state pay-transparency counts). No third-party code was run. Items marked "(unsourced background)" are my own knowledge, not fetched.

## Public ATS job-board APIs (Lever, Ashby, Workday, SmartRecruiters, Workable, Recruitee, iCIMS) and slug discovery

### Takeaway
Lever, Ashby, Workable and Recruitee expose keyless per-company read endpoints; Lever and Ashby return pay ranges (Ashby only with a flag). Workday has no official public API; its undocumented CXS endpoint is POST-based with a 20-per-page cap. iCIMS has no official public feed. I found no open-source, maintained slug dataset: slug discovery is mostly user-supplied or probed.

### Cited Findings
- Lever: `GET https://api.lever.co/v0/postings/{SITE}` (list), `/{SITE}/{POSTING-ID}` (one). Reads need no key in the documented examples; applying needs an API key from a Lever Super Admin passed as `key` param. Application POST is limited to 2/sec (429 on excess); no GET limit is documented. Filters: location, commitment, team, department, level, group. Pagination: `skip`/`limit`. Fields include `workplaceType` (unspecified/on-site/remote/hybrid), optional `salaryRange` {currency, interval, min, max} and `salaryDescription`. EU instance: `api.eu.lever.co`. Browser CORS works only from the company's own domains. — [Lever postings-api](https://github.com/lever/postings-api)
- Ashby: `GET https://api.ashbyhq.com/posting-api/job-board/{JOB_BOARD_NAME}`; the page documents no auth or rate limits. `?includeCompensation=true` adds a `compensation` object (`compensationTierSummary`, `scrapeableCompensationSalarySummary`, `compensationTiers[]`, `summaryComponents[]`). Fields include workplaceType, isRemote, employmentType, descriptionPlain/Html, applyUrl, publishedAt. — [Ashby docs](https://developers.ashbyhq.com/docs/public-job-posting-api)
- SmartRecruiters Posting API docs page covers auth only (API key or OAuth2 client credentials, scope `internal_postings_read`); endpoints, fields and rate limits are on linked reference pages I did not read. The page warns any valid key/token reaches all internal postings. Whether SmartRecruiters has a keyless public per-company endpoint was not verified. — [SmartRecruiters](https://developers.smartrecruiters.com/docs/posting-api)
- Workday CXS (undocumented): `POST https://{tenant}.wd{N}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs` with JSON body `limit`, `offset`, `appliedFacets`, `searchText`. Page size is capped at 20 per multiple third-party posts (sources disagree whether >20 returns HTTP 400 or an empty page). The total count may appear only on the first page, so stop paging on a short/empty page. Detail: GET site base + `externalPath` with `Accept: application/json`. No documented rate limit; no Workday doc. All sources are blogs/scraper vendors. — [DEV: Workday job boards have a JSON API](https://dev.to/udaninn/workday-job-boards-have-a-json-api-too-its-just-better-hidden-23fl), [DEV: 2000 jobs then 0 on page two](https://dev.to/glitchbound/workdays-job-api-tells-you-there-are-2000-jobs-then-says-0-on-page-two-2c8e), [Apify usestring/workday-jobs](https://apify.com/usestring/workday-jobs.md)
- Workable: Workable's help center documents a public account endpoint `https://www.workable.com/api/accounts/<subdomain>?details=true`; scrapers also use the keyless widget endpoint `apply.workable.com/api/v1/widget/accounts/{subdomain}`. A commercial aggregator claims access is tied to per-account keys (conflict). — [Workable help](https://help.workable.com/hc/en-us/articles/115012771647), [DEV: Workable public jobs API](https://dev.to/zsevic/integration-with-workable-public-jobs-api-3nk4), [fantastic.jobs](https://fantastic.jobs/ats/workable)
- Recruitee: Careers Site API `GET https://{client}.recruitee.com/api/offers`. Aggregators say no auth required; a Recruitee support article says tokens are generated in settings (conflict, unresolved). — [Recruitee docs](https://docs.recruitee.com/reference/offers), [100hires](https://100hires.com/recruitee-api.html)
- iCIMS: no official public jobs API found. A standard XML feed exists but only for approved job boards (3x daily). Scrapers parse public `*.icims.com` career sites. — [iCIMS developer community](https://developer-community.icims.com/platform/services/standard-xml-feed-job-boards), [jobspipe iCIMS](https://jobspipe.dev/blog/icims-api-jobs)
- Slug discovery: board slugs come from board URLs (boards.greenhouse.io/<slug>, jobs.lever.co/<slug>, jobs.ashbyhq.com/<slug>). Multi-ATS Apify actors accept a bare slug and probe each platform. My `gh search` for slug-list repos and "ats companies list" returned nothing; no curated open dataset found. — [Apify multi-ATS example](https://apify.com/alwaysprimedev/multi-ats-jobs-scraper)
- GitHub landscape (metadata only, not reviewed): Workday and Greenhouse scraper repos are all tiny (0-4 stars), e.g. orgupdate/Apify-Workday-Job-Scraper (4), erona-enner/workday-jobs-crawler (1), datacrawler-edu/greenhouse-jobs-scraper-python (0). — GitHub search via `gh search repos` (2026-11-17)
- SimplifyJobs/New-Grad-Positions: 18,382 stars, pushed 2026-11-17 (a curated Markdown list of new-grad SWE/Quant/PM roles). I could not confirm a licence (the licence API returned 404) or its data format/ToS; no check of the Summer internships repo succeeded. — [GitHub](https://github.com/SimplifyJobs/New-Grad-Positions)

### Inferences
- Salary extraction: Lever `salaryRange` and Ashby `compensation` (with flag) are structured; Greenhouse needs `pay_transparency=true` (from the earlier note). Workday pay usually sits in free-text descriptions (unsourced background), so a text parser is needed.
- Because no slug dataset exists, a tool would need user-supplied company lists plus URL-pattern detection from career-page links.
- Prefer the "no-key read" ATS connectors (Greenhouse, Lever, Ashby, Workable, Recruitee) as the low-risk tier. Treat Workday CXS as fragile since it is undocumented.

### Gaps
- SmartRecruiters public endpoint/fields, Workable/Recruitee rate limits and Ashby/Lever GET rate limits: not found in official docs.
- Any curated company-slug dataset: none found; SimplifyJobs licence and data format unverified.
- Whether unofficial endpoints (Workday CXS, Workable widget) are permitted by those vendors' ToS: not researched.

## Aggregator and board access status (Indeed, Glassdoor, ZipRecruiter, Dice, Wellfound, Handshake, Google, Adzuna, The Muse, Remotive/RemoteOK, HN)

### Takeaway
Indeed's and Glassdoor's public job-search APIs are closed to new users. Adzuna, Remotive and the HN Algolia API are the usable free sources. I found nothing reliable on ZipRecruiter, Dice, Wellfound, Handshake, Google Cloud Talent Solution, The Muse or RemoteOK access.

### Cited Findings
- Indeed: the Publisher (job search) API is retired and keys are no longer issued; sources disagree on the date (2022-2024). Remaining APIs push data into Indeed (Job Sync, Indeed Apply) or are partner/NDA-gated. Most detail comes from vendors that sell alternatives; Indeed docs not checked directly. — [100hires Indeed API guide](https://100hires.com/indeed-api.html), [jobspipe](https://jobspipe.dev/blog/indeed-publisher-api), [Indeed docs release notes](https://docs.indeed.com/release-notes/2026/2026-03-31)
- Glassdoor: the partner API is effectively closed to new developers; closure year disputed (2021 vs 2022); current access is enterprise sales-led. Secondhand vendor sources only. — [apis.io Glassdoor](https://apis.io/providers/glassdoor/), [jobspipe](https://jobspipe.dev/blog/does-glassdoor-have-an-api)
- Adzuna: auth is `app_id` + `app_key` on `https://api.adzuna.com/v1/api/jobs/{country}/search/{page}`. Country codes `us` and `fr` appear in an MCP server's code (unofficial). Free tier ~1,000 calls/month per a third party, not official; HTTP 429 on excess. — [Adzuna developer overview](https://developer.adzuna.com/overview), [glama MCP server code](https://glama.ai/mcp/servers/@folathecoder/adzuna-job-search-mcp/blob/2a1dccf28f89db8e9b261456bd103b5108fd6ded/server.py), [jobspipe on Adzuna](https://jobspipe.dev/answers/how-to-use-adzuna-api)
- Remotive: `GET https://remotive.com/api/remote-jobs` (params category, company_name, search, limit). Fetch at most about 4 times per day; over 2 requests/min is blocked. Jobs are delayed 24h. Must link back and name Remotive as source; must not resubmit jobs to Jooble, Google Jobs, LinkedIn etc.; no email/signup collection. — [remotive-io/remote-jobs-api](https://github.com/remotive-io/remote-jobs-api)
- Hacker News: the official Firebase API at `hacker-news.firebaseio.com/v0/` has no auth and "no rate limit". The Algolia search endpoint (hn.algolia.com/api) could not be fetched (page returned only its title), so its parameters are unverified. — [HackerNews/API](https://github.com/HackerNews/API)
- HN "Who is hiring" OSS: MaxwellVolz/hn_whos_hiring (11 stars, pushed 2026-10-02; ranks posts against a resume, drafts emails), others stale or tiny. — GitHub search 2026-11-17
- H-1B data tooling on GitHub is tiny, e.g. calvinlee326/h1b-job-scraper (1 star, daily GitHub Actions scraper), sihying0619/H1b-sponsored-employer-from-DOL (0). — GitHub search 2026-11-17

### Inferences
- The Remotive "do not submit to Google Jobs/LinkedIn" and attribution terms are compatible with a personal candidate-side tool but not with re-publishing.
- (unsourced background) Google Cloud Talent Solution is an employer-side search/indexing product, not a public job-search API; ZipRecruiter, Dice, Wellfound and Handshake have no open candidate APIs. Not verified.

### Gaps
- ZipRecruiter, Dice, Wellfound, Handshake, Google Cloud Talent Solution, The Muse (official page 403), RemoteOK: no findings. Adzuna official country list and quotas: not retrieved. Algolia HN API parameters: not retrieved.

## ATS market share and US resume conventions

### Takeaway
Estimates conflict heavily by sample. Workday dominates the Fortune 500 (39% to 75% depending on source), while Greenhouse, Lever and iCIMS rank high in broader or tech-skewed samples. Resume conventions (no photo, single column, no tables, contact info in the body) are consistent across blogs but come from vendor sources.

### Cited Findings
- Jobscan: ATS detected at 487 of 500 Fortune 500 firms (97.4%) in 2026; in 2025 Workday had 39%+ and SAP SuccessFactors 13.2%. In its 13,000+ company set: Greenhouse 19.0%, Workday 17.5%, Lever 15.6%, iCIMS 14.4%. — [Jobscan](https://www.jobscan.co/blog/99-percent-fortune-500-ats/)
- ResumeAI "State of ATS 2026" (743 large employers): Workday 75.4%, Greenhouse 16.7%; a ResumeAI post disputes the 75% figure. ResumeGeni (2,605 top employers, tech-skewed): Greenhouse 49.8%, Workday 22.9%. An older Ongig analysis of Fortune 500 found Workday 22.6% and Taleo 22.4%. All are vendor analyses. — [ResumeAI](https://withresumeai.com/reports/state-of-ats-2026), [ResumeAI myth post](https://withresumeai.com/blog/workday-ats-market-share-myth-2026), [ResumeGeni](https://resumegeni.com/research/ats-market-share-2026), [Ongig](https://blog.ongig.com/applicant-tracking-system/top-ats-systems-used-by-the-fortune-500-2019/)
- Resume conventions (blog consensus): no photo in the US; single-column layout; avoid tables; ATS may garble tables and read columns across; TopResume research cited as finding contact info in headers/footers unidentified 25% of the time; mirror posting language, include acronyms and full terms, avoid stuffing. — [Careerminds](https://careerminds.com/blog/ats-compliant-resume), [ATS verification blog](https://atsverification.com/blog/10-most-common-ats-parsing-failures/), [Toptal](https://www.toptal.com/techresume/career-advice/from-ats-to-interview-ensuring-your-tech-resume-passes-automated-tech-screening)

### Inferences
- Support Workday, Greenhouse, Lever, iCIMS (and Ashby, Taleo, SuccessFactors for form handling), since no single vendor dominates across employer types.
- Generate a plain single-column DOCX/PDF with text-based contact info; verify the extracted text (not visual output).

### Gaps
- Firm 1-vs-2-page rule and any credible recruiter survey: not found. The "75% of resumes never seen by a human" claim is unsourced; do not use.
- Independent (non-vendor) ATS market-share report: none found.

## Candidate-specific US questions (H-1B/sponsorship, self-ID, pay transparency, clearance, location)

### Takeaway
DOL OFLC publishes free quarterly LCA disclosure files that can power an "employer sponsors H-1B" filter, with caveats. Self-ID (EEO/veteran/disability) questions are voluntary; I could not retrieve details on the current forms. Clearance has a USAJOBS filter (earlier note); other items unsourced.

### Cited Findings
- DOL OFLC LCA disclosure data: FY2026 Q1-Q4 posted (determinations 2025-10-01 to 2026-06-30); .xlsx, per-fiscal-year cumulative files released quarterly, each with a record-layout PDF; PERM FY2026 Q4 is also posted. PII (FEIN, worker names) and open-text job duties are excluded; only final determinations; some records change in later releases; wage data comes from employer applications. — [DOL OFLC performance data](https://www.dol.gov/agencies/eta/foreign-labor/performance)
- An LCA is not an approved petition or visa; USCIS Employer Data Hub gives approval outcomes by employer, so combine both for sponsor likelihood. Source is a third-party commentary. — [Davidson Morris](https://www.davidsonmorris.com/h1b-database/), [f1jobs](https://www.f1jobs.io/resources/blog/lca-uscis-employer-data-hub-build-target-company-list)
- Voluntary self-ID: the disability form (CC-305, OMB 1250-0005) is voluntary, answers kept confidential and not seen by selecting officials; a recent copy shows an expiry 04/30/2026 (so a later version likely exists, unconfirmed). — [employer copy of CC-305](https://www.muw.edu/hr/wp-content/uploads/sites/51/2025/06/Voluntary-Self-Identification-of-Disability-CC-305.pdf), [Checkpoint copy](https://www.checkpoint.com/privacy/voluntary-self-identification-of-disability-form-cc-305/)

### Inferences
- Tool design: store self-ID answers per user with a default "decline to self-identify" and never auto-pick a non-decline answer without explicit user input (design suggestion, not sourced).
- Sponsorship filter: employer-name normalization against LCA/EIN-less data is the hard part (the data excludes FEIN), so fuzzy matching is needed (inference from DOL's exclusion of FEIN).
- (unsourced background) Typical application forms ask "authorized to work in the US?" and "will you now or in future require sponsorship?", where the answers must be truthful and user-confirmed; US-state eligibility for remote roles often depends on the employer's registered states.

### Gaps
- Veteran self-ID (VEVRAA) form text, the 2023 CC-305 changes, and race/gender EEO question wording: not retrieved (search returned only copies of the disability form).
- Federal security clearance and remote/state-eligibility conventions beyond USAJOBS: not researched.
- Any maintained OSS H-1B sponsor dataset/tool of note: none found (only tiny repos).
- Whether OFCCP self-ID obligations changed in 2025-26: not checked.

## US legal (Van Buren, CFAA, Meta v. Bright Data, LinkedIn ToS, candidate-side tools)

### Takeaway
After Van Buren the CFAA covers only gate-bypassing access, and the Ninth Circuit applied this to public pages; the real exposure is contract (and account bans). Meta v. Bright Data ended in 2024 with no appeal. LinkedIn's User Agreement (effective Nov 3, 2025) bars scraping software and bots including browser extensions. I found no law that restricts candidate-side AI application tools; litigation targets vendors of employer-side screening (Mobley v. Workday).

### Cited Findings
- Van Buren v. US (2021): "gates-up-or-down" test for "exceeds authorized access"; on remand the Ninth Circuit in hiQ held that "without authorization" does not apply to public websites because there is no gate. Commentators note contract, copyright and trespass-to-chattels claims remain and sites may move content behind logins. — [Proskauer](https://www.proskauer.com/blog/taking-cue-from-the-supreme-courts-van-buren-decision-ninth-circuit-releases-new-opinion-holding-scraping-of-publicly-available-website-data-falls-outside-of-cfaa), [EFF](https://www.eff.org/deeplinks/2022/04/scraping-public-websites-still-isnt-crime-court-appeals-declares), [White & Case](https://whitecase.com/insight-our-thinking/web-scraping-website-terms-and-cfaa-hiqs-preliminary-injunction-affirmed-again)
- Meta v. Bright Data: summary judgment for Bright Data Jan 23, 2024 (N.D. Cal., Judge Chen; contract claim needed scraping while logged in). Meta moved to dismiss the remaining tortious-interference claim without prejudice around Feb 23, 2024 and waived appeal. Bright Data says there was no settlement. No later appeal or follow-on found (one low-authority 2026 blog says the ruling stands). Docket 3:23-cv-03573 not checked. — [Eric Goldman blog](https://blog.ericgoldman.org/archives/2024/02/facebook-drops-anti-scraping-lawsuit-against-bright-data-guest-blog-post.htm), [TechCrunch](https://techcrunch.com/2024/02/26/meta-drops-lawsuit-against-web-scraping-firm-bright-data-that-sold-millions-of-instagram-records/)
- LinkedIn User Agreement, "Effective on November 3, 2025", Section 8.2 "Don'ts": item 2 bars developing, supporting or using "software, devices, scripts, robots or any other means or processes" to scrape or copy the Services, listing crawlers and "browser plugins and add-ons"; item 13 bars using "bots or other unauthorized automated methods to access the Services". — [LinkedIn User Agreement](https://www.linkedin.com/legal/user-agreement)
- Mobley v. Workday (N.D. Cal. 3:23-cv-00770): March 2026 ruling held ADEA disparate-impact protection covers applicants; June 2026 ruling denied most of Workday's motion to dismiss and allowed California FEHA claims on an agent theory; no merits decision yet (dates vary June 16 vs 22). — [Law360](https://www.law360.com/california/articles/2492344&read_main=1&nlsidx=0&nlaidx=6), [Duane Morris](https://blogs.duanemorris.com/classactiondefense/2026/06/25/the-class-action-weekly-wire-episode-153-california-federal-court-grants-in-part-and-denies-in-part-motion-to-dismiss-in-algorithmic-bias-suit/), [Integral Recruiting](https://integralrecruiting.com/?p=4168)

### Inferences
- A tool that automates a logged-in LinkedIn session violates 8.2 on its face. The risk is account restriction and contract claims (see hiQ and Proxycurl in the earlier note), not CFAA. Keep LinkedIn out of the default path (e.g. read links the user pastes) and make any LinkedIn integration an explicit, user-run, at-own-risk option.
- Public ATS JSON endpoints and government APIs sit on much safer ground than login-gated sites, though per-vendor ToS were not read.
- Mobley-style liability attaches to vendors acting for employers; a candidate-side tool does not screen others, so exposure appears low, but this is my inference and I found no source that addresses it.

### Gaps
- No source found on any law or case restricting candidate-side auto-apply or AI resume tools, or on employer ToS bans on auto-apply. The search returned nothing on the topic.
- Full text of Van Buren and the hiQ remand opinions not read; Bright Data docket not checked.
- Other LinkedIn terms (Professional Community Policies, robots.txt) not checked.
