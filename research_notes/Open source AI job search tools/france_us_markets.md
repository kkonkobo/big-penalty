# France and US job markets for an open-source AI job search assistant

Research date: 2026-11-17. About 17 tool calls. Several questions are only partly answered; see each Gaps section. Items under "Inferences" that rest on background knowledge, not a fetched source, are labelled "(unsourced background)".

## France: job data sources and programmatic access

### Takeaway
France Travail's "Offres d'emploi v2" is the only official, free, OAuth-based French job API found. It is licence-gated, caps each query at 1,150 results, and does not cover LinkedIn, WTTJ, APEC or most employer-site jobs. Everything else (WTTJ, Indeed.fr, HelloWork, LinkedIn FR) is scraping, with ToS risk. A data.gouv.fr page carries a notice that the API is "no longer exposed" on francetravail.io pending a review of its distribution terms, and I could not confirm or refute this. Verify before designing around it.

### Cited Findings
- France Travail's API returns active offers collected by France Travail or received from partners. For offers showing contact details online, the employer's consent is required before the offer is distributed in the API. — [data.gouv.fr API Offres d'emploi](https://www.data.gouv.fr/dataservices/api-offres-demploi)
- Rate limit is 10 calls/second. Access is restricted, via the francetravail.io contact form, for public bodies, companies/associations and individuals. The licence field is "non renseignée". — [data.gouv.fr API Offres d'emploi](https://www.data.gouv.fr/dataservices/api-offres-demploi)
- RECENT-CHANGE FLAG: the same data.gouv.fr page states the API is currently no longer exposed on francetravail.io, "le temps d'une analyse des modalités de distribution et de réutilisation". A follow-up search found no official France Travail announcement of a shutdown. The status is unresolved and the page has no update date. — [data.gouv.fr API Offres d'emploi](https://www.data.gouv.fr/dataservices/api-offres-demploi); [search found no official notice](https://www.data.gouv.fr/organizations/france-travail/dataservices)
- Developer-blog description of the API: OAuth2 client credentials, scope `api_offresdemploiv2`, free account plus app registration. A page holds at most 150 results and the `range` upper bound is 1149, so each query caps at 1,150 offers. The workaround is to slice by date, department or ROME code and deduplicate. Parameters include `motsCles`, `codeROME`, `commune`, `departement`, `distance`, `typeContrat`, `minCreationDate` and `maxCreationDate`. Reference endpoints exist under `/referentiel/`. — [jobspipe.dev](https://jobspipe.dev/blog/france-travail-api) (third-party, not official)
- The same blog says the licence requires showing the full content the API gives for each offer, including the logo. Redistribution must credit France Travail, give the last-update date and link back to the licence. It says to prefer the free-text salary field over structured salary data. It also says coverage excludes roles that appear only on employer sites, LinkedIn or ATS boards, and that ROME is French-only with no official ISCO-08 crosswalk. — [jobspipe.dev](https://jobspipe.dev/blog/france-travail-api) (third-party interpretation of the licence; not verified against the official terms)
- My attempt to fetch francetravail.io returned no content. Official rate-limit and terms text was not independently confirmed. A search for a "10 calls/s" quota on francetravail.io found no official source, but data.gouv.fr states 10 calls/s. — [data.gouv.fr](https://www.data.gouv.fr/dataservices/api-offres-demploi)
- Welcome to the Jungle: third-party Apify scrapers describe using WTTJ's public Algolia search endpoint, which needs no authentication. Vendors tell users to check WTTJ's ToS. I did not find WTTJ's ToS text or any official API. — [Apify listing](https://apify.com/shahidirfan/jungle-job-scraper)
- JobSpy (speedyapply/JobSpy, about 4.75k stars, pushed 2026-11-13) supports LinkedIn, Indeed, Glassdoor, ZipRecruiter, Google, Bayt, Naukri, BDJobs and HelloWork. Indeed and Glassdoor accept `country_indeed` (France and USA listed, both with Glassdoor support). HelloWork searches France only, with French city or postcode locations, and works better with French search terms. LinkedIn searches globally using only `location`. — [JobSpy README](https://github.com/speedyapply/JobSpy)
- Note: I did not find a source for the claim that HelloWork lacks rate limiting. JobSpy's README states that Indeed is "the best scraper currently with no rate limiting." — [JobSpy README](https://github.com/speedyapply/JobSpy)
- Apify has many paid WTTJ scraper actors, e.g. one marketed for "WTJ Tech Jobs FR/EU + Salaries". These confirm a market for WTTJ scraping but say nothing about legality. — [Apify dltik actor](https://apify.com/dltik/welcome-to-the-jungle-scraper.md)

### Inferences
- Design pattern: use France Travail where available (confirm status first). Treat WTTJ, HelloWork and Indeed.fr as scraper-backed connectors that are opt-in, user-run and rate-limited. Treat LinkedIn as the highest-risk connector.
- The full-content display condition could conflict with an AI tool that summarises or reformats offers. The user should read the licence.
- (unsourced background) Many French ATS boards (Greenhouse, Lever, Workable, Teamtailor, SmartRecruiters) are public per-company APIs and could cover French employers without scraping. I did not verify this for France specifically.

### Gaps
- Official francetravail.io terms and current API availability: the page did not render and the data.gouv.fr notice is undated.
- APEC, Cadremploi, Jobteaser, Malt, "Choisir le service public", Indeed.fr and LinkedIn FR: no programmatic-access or ToS findings. Not searched in depth.
- WTTJ ToS text: not found.

## France: application conventions and localization

### Takeaway
Only shallow, secondary sources were found. A photo is optional but still common in some sectors, a one-page CV is the norm for most profiles, and French-language output is expected for French-language postings.

### Cited Findings
- A photo is not mandatory but many French companies expect one. It is more typical in sales, hospitality and traditional large French firms, and omittable for tech start-ups, international firms and English-language postings. — [Expatica](https://expatica.com/fr/working/finding-a-job/french-cv-interview-tips-101770), [Annie André](https://annieandre.com/french-resume-cv/)
- One page is sufficient for most profiles. — [Studi](https://www.studi.com/fr/alternance/4-conseils-pour-rediger-un-cv-qui-retient-lattention-des-recruteurs)
- CDD can last up to 18 months. For alternance, the CV title can state the contract type (apprentissage or professionnalisation), the diploma and the rhythm. — [Thotis Media](https://thotismedia.com/cv-alternance-exemple-conseils-convaincre-recruteur/)

### Inferences
- (unsourced background) The tool should model French contract types as first-class fields: CDI, CDD, intérim, alternance (apprentissage and professionnalisation), stage, freelance and portage salarial. Salary should be handled as brut annuel in K€ with a variable component, and possibly 13th month. Convention collective (e.g. Syntec) affects minimums and benefits.
- (unsourced background) A lettre de motivation is still often expected, and French and English variants of CV and letter are needed.

### Gaps
- Convention collective / Syntec, brut annuel salary conventions, lettre de motivation expectations, and portage salarial: no useful sources found. The search returned nothing on these. They need a dedicated pass on service-public.fr or the ministry of labour site.
- French pay-transparency rules (EU Pay Transparency Directive transposition): not researched.

## France / EU legal

### Takeaway
GDPR and CNIL guidance treat scraping as allowed case by case, with minimization and respect for source-site limits. The EU AI Act classifies employment AI as high-risk, but the Annex III obligations were delayed to 2 Dec 2027 by the Digital Omnibus. Applicability to candidate-side tools is not settled by any source found.

### Cited Findings
- CNIL (June 2025) published practice sheets on legitimate interest for AI development and on web scraping. Scraping is not prohibited per se, needs case-by-case analysis and safeguards, and must respect data minimization (GDPR Art. 5.1.c). Site terms and copyright may limit it. A CNIL checklist followed in July 2025. — [Hogan Lovells](https://www.hoganlovells.com/fr/publications/development-of-an-ai-system-cnil-issues-guidelines-regarding-collection-of-data-via-web-scraping), [CNIL checklist PDF](https://www.cnil.fr/sites/default/files/2025-07/ia_liste_de_verification.pdf)
- CNIL has a 19-sheet recruiter guide that covers AI use in recruitment. One commentary says recruitment is a 2026 CNIL inspection priority. — [Culture RH](https://culture-rh.com/recrutement-rgpd-controles-cnil/) (secondary)
- A 2022 law-firm note says predictive recruitment tools can fall under GDPR Art. 22 (solely automated decisions). It predates the AI Act. — [Vigo Avocats](https://vigo-avocats.com/wp-content/uploads/2022/02/20220203111905677.pdf)
- EU AI Act, Digital Omnibus: multiple sources say standalone high-risk obligations, including employment/HR, now apply from 2 Dec 2027 instead of 2 Aug 2026. Annex I product systems move to 2 Aug 2028. Regulation (EU) 2026/1744 was published in the OJ on 24 July 2026 and took effect 27 July 2026, according to Cooley and Partena. Employment AI stays high-risk, and Article 50 transparency duties still apply from 2 Aug 2026 (per MinTHCM). — [Cooley](https://cdp.cooley.com/digital-ai-omnibus-delays-key-deadlines-introduces-new-rules/), [Partena](https://www.partena-professional.be/en/node/22570), [Sidley](https://datamatters.sidley.com/?p=8999), [Ogletree](https://ogletree.com/?p=89498), [MinTHCM](https://minthcm.org/ai-act-deadline-extension-confirmed-new-timeline-for-hr-recruitment-systems/)
- Conflict: a DLA Piper update (30 June 2026, revised 10 Aug 2026) still tells employers to plan against 2 Aug 2026 and is internally inconsistent. Also, Cooley and Airia disagree on whether the exemption-registration duty was dropped or reinstated. — [DLA Piper](https://knowledge.dlapiper.com/dlapiperknowledge/globalemploymentlatestdevelopments/2026/The-Digital-AI-Omnibus-Proposed-deferral-of-high-risk-AI-obligations-under-the-AI-Act), [Airia](https://airia.com/?p=4771)

### Inferences
- (unsourced background) Annex III's employment category covers AI used for recruitment/selection (placing targeted job ads, filtering and evaluating candidates). It is aimed at providers and deployers on the employer side. A candidate-side tool that tailors the user's own CV is not obviously covered, but a tool that scores or ranks other people would be. This should be confirmed against the Act text and counsel.
- A candidate-side tool still handles the user's personal data (CV, history), so GDPR applies. Local-first storage and explicit consent for sending data to LLM providers are the safest design. Article 50 transparency applies if the tool generates text that interacts with people.
- Scraping risk in France/EU is mostly contract, database right and GDPR, not criminal law. (unsourced background, e.g. CJEU Ryanair v PR Aviation and French Leboncoin/Vizzavi-type cases; not verified here.)

### Gaps
- Primary text of the AI Act and the Omnibus regulation was not read.
- EU database-right case law (CJEU Ryanair) and French scraping decisions: the search returned nothing.
- Whether Art. 22 applies to candidate-side tools: no source.

## US: job data sources and programmatic access

### Takeaway
Only government and ATS-hosted boards have clean public APIs: USAJOBS needs a free key, and Greenhouse board GETs need no auth. The big aggregators (LinkedIn, Indeed, Glassdoor, ZipRecruiter) are accessible only by scraping, mainly through JobSpy-style libraries.

### Cited Findings
- USAJOBS Search API uses an API key sent in the Authorization header (details are in a linked guide). It has a `ResultsPerPage` max of 500, `DatePosted` 0-60 days, `RemoteIndicator`, `SecurityClearanceRequired`, `WhoMayApply`, `HiringPath`, and a `Fields` parameter (Min/Full). `WhoMayApply` values All and Status require specific authorization. The response includes `PositionRemuneration`, `ApplyURI` and `UserArea.Details.MajorDuties`. Rate limit and ToS pages exist but were not read. — [USAJOBS Search API](https://developer.usajobs.gov/api-reference/get-api-search), [API reference index](https://developer.usajobs.gov/api-reference/)
- Greenhouse Job Board API: no authentication for GET endpoints (base `boards-api.greenhouse.io/v1/boards/{board_token}`; `/jobs`, `/jobs/{id}`, offices, departments). Adding `pay_transparency=true` returns `pay_input_ranges` (`min_cents`, `max_cents`, `currency_type`). `POST /jobs/{id}` application submission uses HTTP Basic auth with the board's secret API key (it must be proxied server-side), requires only first name, last name and email, and accepts pdf, doc, docx, txt and rtf resumes. Rate limits were not stated. — [Greenhouse docs](https://docs.greenhouse.io/job-board.html)
- Pay-transparency laws: no federal requirement. Counts range from 13 to 18 states plus DC across sources, with different as-of dates. California (SB 1162, 15+ employees), Colorado, New York, Washington, Illinois, Massachusetts and Minnesota are listed. Newer: Virginia (1 July 2026), Maine (29 July 2026, 10+ employees), Delaware (26 Sept 2027). Sources are mostly vendor blogs. — [Cooley, July 2026](https://www.cooley.com/news/insight/2026/2026-07-06-show-me-the-money-or-the-wage-range-new-state-pay-transparency-laws), [Paychex](https://www.paychex.com/articles/compliance/pay-transparency-laws-by-state), [JD Supra Virginia/Maine](https://www.jdsupra.com/legalnews/virginia-and-maine-enact-pay-6832918/)

### Inferences
- Salary ranges are therefore more reliably present in postings for CA, NY, CO, WA and other covered states. The tool should parse ranges, store currency and pay period, and treat missing ranges as "unknown".
- (unsourced background) Lever, Ashby, SmartRecruiters and Workable have comparable public board endpoints. Workday has no clean public API and is usually reached by per-tenant JSON endpoints. Google Jobs and Handshake have no public job-search API. Not verified here.

### Gaps
- Lever, Ashby, Workday, SmartRecruiters API docs, plus Indeed, Glassdoor, ZipRecruiter, Dice, Wellfound, Handshake and Google Jobs access status: not researched in this pass.
- USAJOBS rate limits and terms: linked pages not read.
- ATS market share, resume conventions (no photo, 1-2 pages), H-1B sponsorship filtering and EEO questions: no sources found. Nothing is claimed here.

## US legal

### Takeaway
There is no clean CFAA bar on scraping public pages, but breach-of-contract claims are a real risk, and LinkedIn actively litigates. Candidate-side tools are mainly outside the AI-hiring statutes, which target employers, but state-law details were only partly verified.

### Cited Findings
- hiQ v. LinkedIn: the 2019 Ninth Circuit ruling found hiQ raised serious questions that scraping public data was not unauthorized under the CFAA, and the Supreme Court vacated it in 2021. In Nov 2022 the N.D. Cal. court held LinkedIn's user-agreement ban on scraping and fake profiles enforceable in contract. In Dec 2022 a consent judgment required hiQ to pay $500,000, stop scraping and destroy derived data and algorithms. The settlement sets no precedent, so the CFAA question remains open. — [Proskauer](https://www.proskauer.com/blog/hiq-and-linkedin-reach-proposed-settlement-in-landmark-scraping-case), [Lexology](https://www.lexology.com/pro/content/hiq-ends-linkedin-scraping-litigation)
- LinkedIn sued Proxycurl in Jan 2025 (fake accounts and scraping of millions of profiles). The case ended with a permanent injunction and deletion of data, and Proxycurl shut down. LinkedIn sued ProAPIs in Oct 2025, and a Dec 2025 filing said the parties were exploring early resolution, with no outcome found. — [LinkedIn News](https://news.linkedin.com/2025/LinkedInWinsLegalBattleToProtectMemberData), [CyberMaterial](https://cybermaterial.com/linkedin-sues-proapis-over-fake-accounts), [Bloomberg Law](https://news.bloomberglaw.com/artificial-intelligence/linkedins-war-against-bot-scrapers-ramps-up-as-ai-gets-smarter)
- Meta v. Bright Data (N.D. Cal., Jan 2024): summary judgment for Bright Data on contract, because the terms were read to cover only logged-in use, so logged-out scraping of public data fell outside them. Only a tortious-interference claim remained. It is a district-court ruling. Appeal status was not found, and the commentary comes from a firm that represents scrapers. — [Farella Braun + Martel](https://www.fbm.com/data-analytics/publications/major-decision-affects-law-of-scraping-and-online-data-collection-meta-platforms-v-bright-data/)
- NYC Local Law 144: a State Comptroller audit (2 Dec 2025) called DCWP enforcement "ineffective". One violation was found among 32 sampled companies for July 2023 to June 2025. DCWP accepted most recommendations. Penalties can reach $1,500 per violation per day. — [NY State Comptroller](https://www.osc.ny.gov/state-agencies/audits/2025/12/02/enforcement-local-law-144-automated-employment-decision-tools), [DLA Piper](https://www.dlapiper.com/en/insights/publications/2026/01/critical-audit-of-nyc-ai-hiring-law-signals-increased-risk-for-employers)
- California Civil Rights Council ADS regulations took effect 1 Oct 2025 (FEHA). They require employers to keep ADS data for four years and anti-bias testing is a relevant factor. — [Mondaq](https://www.mondaq.com/unitedstates/employee-rights-labour-relations/1673530/california-adopts-new-employment-ai-regulations-effective-october-1-2025), [CalChamber](https://p-hrcalifornia.calchamber.com/cases-news/hrce-2025/what-employers-should-know-about-automated-decision-system-ads-regulations-starting-october-1)
- Colorado AI Act: the effective date moved from 1 Feb to 30 June 2026 (SB25B-004, Aug 2025). SB 26-189 (signed 14 May 2026) repealed and replaced it with a narrower, notice-based law effective 1 Jan 2027, with a 60-day cure period. A federal stay of enforcement was entered on 27 Apr 2026. — [Proskauer](https://www.proskauer.com/pub/what-colorado-ai-laws-major-rewrite-means-for-employers), [Mondaq](https://www.mondaq.com/unitedstates/new-technology/1680530/colorados-ai-act-implementation-delayed), [JD Supra](https://www.jdsupra.com/legalnews/colorado-repeals-and-replaces-ai-act-1665581/). I did not read the statute.
- Illinois HB 3773 (effective 1 Jan 2026) amends the Human Rights Act to restrict predictive data analytics use in employment decisions. I found only a truncated snippet. — [Illinois legislative summaries](https://ilga.gov/documents/reports/static/Public%20Acts%20Syn.pdf)

### Inferences
- NYC LL144, CA, IL and CO regulate employers and vendors making or substantially assisting hiring decisions. A tool that only helps a candidate apply is not the target. Anything that ranks or screens other people's applications, or is sold to employers, would be.
- The safest architecture is local, user-driven browsing (user's own session and logged-out reads, human-in-the-loop applying), and no mass account automation. LinkedIn account restriction is a practical risk even without a lawsuit.

### Gaps
- Van Buren v. United States and the CFAA: not covered by the search. The 2024-26 status of the Meta v. Bright Data appeal is unknown.
- LinkedIn User Agreement text on automation: not retrieved.
- Primary statute text for IL HB 3773, CO SB 26-189 and NYC LL144.

## Open-source projects with FR or US support

### Takeaway
French-specific OSS is thin: a few tiny France Travail clients and one MCP server, plus JobSpy's HelloWork and France-capable Indeed and Glassdoor support. US support is broad via JobSpy and ATS aggregators, but USAJOBS tooling is small.

### Cited Findings
- speedyapp/JobSpy — about 4.75k stars, pushed 2026-11-13. Scrapes LinkedIn, Indeed, Glassdoor, ZipRecruiter, Google, Bayt, Naukri, BDJobs and HelloWork. Country selection for France and the USA, plus HelloWork for France. — [GitHub](https://github.com/speedyapply/JobSpy)
- Forks and wrappers of JobSpy: rainmanjam/jobspy-api (about 384 stars, Docker/API key/proxy), borgius/jobspy-mcp-server (about 128), alpharomercoma/ts-jobspy (about 17). — [GitHub search](https://github.com/search?q=jobspy&type=repositories)
- Francis1998/agentic-career-search — about 102 stars, pushed 2026-11-13. Claims 119 ATS and job-board adapters (Greenhouse, Lever, Workday, JSON-LD), explainable scoring, resume-to-job matching and an MCP server. FR/US specifics were not verified. — [GitHub](https://github.com/Francis1998/agentic-career-search)
- kay-abraham/career-site-jobs-api — 0 stars, pushed 2026-11-11. Claims jobs from Greenhouse, Lever, Ashby, Workday and 11 more company boards. — [GitHub](https://github.com/kay-abraham/career-site-jobs-api)
- France Travail: cmnemoi/france-travail-api (Python SDK, 0 stars, 2026-07-01), mohamed-amine-ben-mallessa/francetravail-mcp ("first open-source MCP server" for the France Travail API, 5 tools, 2 stars, 2026-06-19), Sidi4PF/fr-tech-jobs-observatory (analysis project, 1 star), plus older wrappers (hkatoze/pole-emploi-api, ogrre/laravel-pole-emploi, both stale). — [GitHub search](https://github.com/search?q=france+travail+api&type=repositories)
- WTTJ scrapers: small repos only (datadmon/welcome-to-the-jungle-jobs-scraper, morvanvivien/welcometothejungle-scraper, MaximeBF2000/welcome-to-the-jungle-scraper, Gemna-growth/Welcome_to_the_jungle_Scraper, all 0-2 stars), plus vendor-owned piloterr/welcometothejungle-scraper. — [GitHub search](https://github.com/search?q=welcome+to+the+jungle+scraper&type=repositories)
- USAJOBS: skivuha/federal-compass-mcp (MCP server, 12 stars, 2026-10-21), Nir0g0/Job-Analytics-Dashboard (RSS plus USAJOBS pipeline), jobapis/jobs-usajobs (PHP, stale since 2016), and a CrewAI tutorial repo. — [GitHub search](https://github.com/search?q=usajobs+api&type=repositories)

### Inferences
- No mature, widely adopted France Travail client or WTTJ connector exists. A tool that ships a maintained one would fill a real gap, but must follow the licence terms.
- The star counts are low, so these projects are unlikely to be reliable dependencies. Read them, don't depend on them.
- I did not run any third-party code. All data came from GitHub metadata and READMEs.

### Gaps
- GitHub search was rate-limited partway. The following returned nothing or were not run: APEC, Indeed.fr-specific and H-1B-sponsorship tools. Absence from my searches is not proof that none exist. Repo maintenance quality and licences were not checked.
