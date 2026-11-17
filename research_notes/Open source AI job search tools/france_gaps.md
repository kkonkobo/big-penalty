# French job-market gaps: France Travail API status, board access, usage, application conventions, career-ops modes/fr

Research date 2026-11-17. About 30 tool calls, over the usual budget; GitHub API rate limit was hit near the end. This note complements france_us_markets.md and does not repeat it. No third-party code was run. Temp files are in /Volumes/SSD/Dev/big-penalty-scratch. Several service-public.gouv.fr and Legifrance fetches returned wrong or empty pages, so some legal points rest on secondary sources and are flagged.

## France Travail "Offres d'emploi v2" API: status in Nov 2026

### Takeaway
The earlier "no longer exposed" flag is now resolved with a dated primary-ish source. On 19 Oct 2026 France Travail said, in the data.gouv.fr discussion thread, that access is "currently suspended for new requesters". Existing users keep access. The stated cause is massive automated use by AI solutions that ignored the reuse conditions. A new open-source tool therefore cannot count on self-service API keys.

### Cited Findings
- The data.gouv.fr API page carries a notice, with no date on the page, that the API "n'est actuellement plus exposée" while France Travail analyses "les modalités de distribution et de réutilisation du service". Access condition is "Restreint", via the francetravail.io contact form. — [data.gouv.fr API Offres d'emploi](https://www.data.gouv.fr/dataservices/api-offres-demploi)
- The same page says the review follows "usages automatisés massifs par certaines solutions d'intelligence artificielle" (fetch summary of the page). — [data.gouv.fr API Offres d'emploi](https://www.data.gouv.fr/dataservices/api-offres-demploi)
- Discussion thread: on 16 Oct 2026 a user asked whether public access had been withdrawn. On 19 Oct 2026 France Travail replied: "L'accès à l'API Offres d'emploi est actuellement suspendu pour les nouveaux requérants". It added that this "ne remet pas en cause les accès déjà accordés" at this stage. It also said reuse of API data in AI services is among the elements it is examining. — [data.gouv.fr discussions](https://www.data.gouv.fr/dataservices/api-offres-demploi/discussions)
- Access requests: on 14 Oct France Travail pointed requesters to the francetravail.io contact form. On 19 Oct it said requests are being analysed, with no timeline. The producer did not answer whether daily polling for new offers is allowed. — [data.gouv.fr discussions](https://www.data.gouv.fr/dataservices/api-offres-demploi/discussions)
- A search result listing for the dataservice shows "Mis à jour le 30 déc. 2025" and describes the API as real-time. This is a stale snapshot and conflicts with the notice above, so prefer the Oct 2026 thread. — [data.gouv.fr France Travail dataservices](https://www.data.gouv.fr/organizations/france-travail/dataservices)
- Direct fetch of francetravail.io returned no content, so there is no francetravail.io announcement to cite.
- A bulk alternative exists for offers: the data.gouv.fr dataset "Offres d'emploi diffusées à France Travail" is quarterly according to one search summary. Further, a forum reply points to an "Offres d'emploi anonymisées" API. I could not open either to confirm. — [dataset page](https://www.data.gouv.fr/fr/datasets/offres-demploi-diffusees-a-france-travail)

### Inferences
- A tool can ship the France Travail connector as "bring your own credentials" (users who already hold keys), with the connector disabled by default. It should not be the only French source.
- The AI-reuse concern directly targets tools like this one. Rate-limit politely, cache, and show full offer content, logo and attribution per the licence terms noted in france_us_markets.md.

### Gaps
- Whether the suspension is lifted or made permanent, and the new reuse terms: no source found after 19 Oct 2026.
- Whether the anonymised-offers API or the quarterly dataset is usable for a candidate tool: not verified.

## Programmatic access to other French sources, ranked for usefulness

### Takeaway
Ranked for a tool: (1) La Bonne Alternance API for alternance (official, free with account); (2) Choisir le service public open dataset (Open Licence, weekly CSV, but thin and bulk); (3) per-company ATS board APIs (see earlier note); (4) France Travail API for existing key holders; (5) WTTJ Algolia endpoint, technically open but the ToS prohibit scraping; (6) APEC internal search endpoint, protected by DataDome on detail pages; (7) Indeed.fr, LinkedIn, HelloWork via JobSpy-style scraping, high ToS risk; (8) Cadremploi, Jobteaser, Malt, Free-Work: no access path found.

### Cited Findings
**La Bonne Alternance (alternance)**
- Access is "Ouvert avec compte" (account needed). Base URL `https://api.apprentissage.beta.gouv.fr/fr`, Swagger at `/fr/explorer`. Rate limits 5 to 20 calls/s depending on route. Licence "non renseignée". Availability shown as 98.82%. — [data.gouv.fr dataservice](https://www.data.gouv.fr/dataservices/672cf64ff8b5d52b76263394)
- Endpoints: search apprenticeship job opportunities (filters ROME, RNCP, location, diploma level, radius), get one opportunity, send a candidate application (CV, contact details, optional message; requires `apply.recipient_id` from search), search training programmes, build a contact link to a training centre, and create/update/delete offers by employer SIRET. Data is offers collected by La Bonne Alternance and partner sites, plus "companies with high apprenticeship hiring potential" that have no offer posted. — [data.gouv.fr dataservice](https://www.data.gouv.fr/dataservices/672cf64ff8b5d52b76263394)
- The apply endpoint would let a tool submit applications, which collides with a human-approval rule; keep it user-confirmed.

**Choisir le service public (CSP)**
- Open dataset "Les offres diffusées sur Choisir le Service Public": Licence Ouverte 2.0, weekly updates, CSV (main file about 141.8 MB, last updated 17 Nov 2026 per the page), described as raw data. — [data.gouv.fr dataset](https://www.data.gouv.fr/datasets/les-offres-diffusees-sur-choisir-le-service-public)
- A user comment, relayed by a search summary, says the dataset's offers hold very little information compared with the site. I did not read the thread. — [search listing](https://www.data.gouv.fr/datasets/les-offres-diffusees-sur-choisir-le-service-public/discussions)
- The "Documentation API CSP" PDF covers pushing offers from a third-party ATS (CEGID/Talentsoft) to the platform, not reading. I found no read API on PISTE. — [CSP PDF](https://choisirleservicepublic.gouv.fr/wp-content/uploads/2026/08/Documentation-API-CSP.pdf)
- DGAFP figure: 347,200 offers published in 2025 (per Acteurs publics). — [Acteurs publics](https://acteurspublics.fr/articles/347-000-offres-demploi-publiees-sur-choisir-le-service-public-en-2025/)

**Welcome to the Jungle**
- ToS (Partie A, art. 10) prohibit using robots or other tools for "une extraction automatisée de données (« scraping »)", and copying or downloading the Site or its contents (including company profiles) by other automated means. Art. 19.2 limits use to personal purposes only. Art. 19.1 claims database rights and says compilations of data are protected. Search-engine indexing is allowed only to build public search indexes, "à l'exclusion de tout cache ou archive". — [WTTJ terms](https://www.welcometothejungle.com/fr/pages/terms)
- Technical: a third-party write-up says the site is a React/InstantSearch client on Algolia app ID `CSEKHVMS53`, with jobs index `wk_cms_jobs_production` and organisations index `wk_cms_organizations_production`, using a public search-only key shipped in the front-end bundle. It also says a per-company REST endpoint exists at `api.welcometothejungle.com/api/v1/organizations/{slug}` and that robots.txt allows `/en/jobs` and `/en/companies`. This is third-party, not verified live, and credentials may rotate. I do not reproduce the key here. — [browse.sh skill page](https://browse.sh/skills/welcometothejungle.com/scrape-job-company-data-yt3gr8)
- My earlier guess of an index named `wttj_jobs_production_fr` was not confirmed by any source.

**APEC**
- No official API found. Third-party Apify actors say they call APEC's internal search service (`POST /cms/webservices/rechercheOffre`), and that individual offer pages are protected by DataDome. The guessed path `cms/webservices/offres/search` is not documented anywhere I found. — [Apify actor description](https://apify.com/unfenced-group/apec-fr-scraper)
- APEC's own pages are fetched through its site only; I did not read APEC's ToS.

**Indeed.fr / LinkedIn / HelloWork**
- LinkedIn: User Agreement Section 8.2 prohibits scraping and unauthorised bots (secondary summaries of the version effective 3 Nov 2025; LinkedIn's help page says prohibited tools violate Section 8.2). — [LinkedIn help](https://www.linkedin.com/help/linkedin/answer/a1341387)
- Indeed: secondary sources say its developer agreement bans scraping and building permanent databases; I did not read the primary terms. Indeed France legal pages exist (updated 1 Apr 2026). — [Indeed legal](https://ch.indeed.com/legal?hl=fr)
- HelloWork: supported by JobSpy (see earlier note). No ToS read.

**Cadremploi, Jobteaser, Malt, Free-Work**
- No API or access program found for job search. Malt has a documented API for invoices, payments and SCIM aimed at client enterprises, not mission search. — [Leptidigital](https://www.leptidigital.fr/logiciels/malt/)

### Inferences
- The only clean sources for French offers are La Bonne Alternance, the CSP dataset and employer ATS endpoints. A "paste URL" or browser-assisted flow (user opens the posting, tool reads the page the user is on) avoids bulk scraping and matches career-ops' design.
- The WTTJ ToS text makes WTTJ scraping a contract breach risk even if the Algolia endpoint is open. Treat it as opt-in with a warning, or skip it.

### Gaps
- Primary Indeed.fr terms, APEC ToS, HelloWork ToS, Cadremploi, Jobteaser, Malt and Free-Work terms: not read.
- Any LinkedIn FR job-search API: none found (not exhaustively searched).

## Market share and usage of French boards

### Takeaway
No source ranks boards by cadre versus non-cadre use. Fragmentary evidence: APEC is free for cadres and young graduates, Cadremploi is cadre-focused and paid, HelloWork and Indeed are generalist. The French offer market is falling.

### Cited Findings
- Indeed Hiring Lab: job-posting volume in France was 5.8% below its Feb 2020 level at end of May 2026, and fell 20.3% between end May 2025 and end May 2026 (as reported by Culture RH; France Travail separately reports about -5% in offers and hires). France was -9.2% year to date in the June European panorama versus -7.5% for the euro area. — [Hiring Lab June 2026](https://www.hiringlab.org/fr/blog/2026/06/24/panorama-du-marche-du-travail-europeen-juin-2026/), [Hiring Lab 23 Jun 2026](https://www.hiringlab.org/fr/blog/2026/06/23/le-marche-du-travail-en-juin-2026-les-offres-saisonnieres-resistent-au-ralentissement/), [Culture RH](https://culture-rh.com/recrutements-plus-de-candidats-2026/). The -20.3% figure comes from a summary of those pages; I did not verify it on the Hiring Lab page itself. Later 2026 data was not found.
- APEC 2018 sourcing study: 47% of cadre hires involved an offer posted on one or more cadre job sites (APEC, Cadremploi, CadresOnline, etc.). Old data. — [APEC sourcing cadres 2018](https://corporate.apec.fr/files/live/sites/corporate/files/Nos%20%C3%A9tudes/pdf/sourcing-cadres-edition-2018)
- A prefectures' channel list classes Indeed as generalist (free or paid), HelloWork as generalist paid, Cadremploi as paid for cadres, and APEC as free for cadres and young graduates. — [Livrable 3, prefectures-regions.gouv.fr](https://www.prefectures-regions.gouv.fr/irecontenu/telechargement/115648/863354/file/Livrable%203%20-%20liste%20de%20canaux%20de%20diff%20OE%20-%2009.04.2024.pdf)
- Self-reported audiences (unverified): Cadremploi 1.1 million unique visitors (Sept 2023), HelloWork 4 million monthly users. — [Cegid job-board ranking](https://www.cegid.com/fr/blog/top-des-meilleurs-jobboards-en-2025/)
- APEC baromètre 2026: 49% of firms that hired at least one cadre in 2025 reported difficulty filling roles. — [Lefebvre Dalloz](https://formation.lefebvre-dalloz.fr/actualite/la-publication-dune-offre-demploi-reste-le-canal-privilegie-pour-recruter-un-cadre)

### Inferences
- A tool serving cadre profiles should cover APEC and WTTJ first; for non-cadre, junior and alternance profiles, France Travail, Indeed, HelloWork and La Bonne Alternance matter more. This is an inference, not a sourced board ranking.

### Gaps
- DARES or APEC data on board market share, and audience figures from an independent panel (Médiamétrie): not found.

## French application conventions

### Takeaway
Contract rules are well documented. For CV and letter conventions I found the vous-moi-nous structure and one-page length from school and France Travail-style guides, but no authoritative source on photo or état civil beyond the earlier note.

### Cited Findings
- Lettre de motivation: "vous / moi / nous" means vous = show you understand the company's needs, moi = skills and experience backed by concrete examples, nous = your contribution and a request for an interview. Length is about one page, 3 to 4 paragraphs, and it should not repeat the CV. — [JobSeeker](https://www.jobseeker.com/fr/lettre-de-motivation/conseils/lettre-de-motivation-je-vous-nous), [JobTeaser](https://www.jobteaser.com/fr/advices/structure-lettre-motivation), [France Travail](https://www.francetravail.fr/candidat/vos-recherches/preparer-votre-candidature/cv-lettre-de-motivation-e-mail/10-astuces-pour-ecrire-votre-let.html) (the last two were only seen as search results)
- Période d'essai (CDI), per France Travail: max 2 months (ouvriers, employés), 3 (agents de maîtrise, techniciens), 4 (cadres). One renewal only, and only if an extended branch agreement provides for it and the contract says so. Caps including renewal are 4, 6 and 8 months. The CFDT notes that since 9 Sept 2023 a longer period cannot be imposed even if a branch agreement allows it. — [France Travail](https://www.francetravail.fr/employeur/lessentiel-pour-embaucher/les-contrats-de-travail/la-periode-dessai.html), [CFDT](https://www.cfdt.fr/mes-droits/fiches-droits/la-periode-dessai-cdi) (both from a search summary)
- Stage gratification 2026: minimum 4.50 EUR/hour (15% of the 30 EUR hourly Social Security ceiling, ceiling set by an arrêté of 23 Dec 2025). Mandatory above two months; sources disagree on whether the two months must be consecutive (Legisocial yes, Lefebvre Dalloz no). Some sites still show the 2025 figure of 4.35. — [Legisocial](https://www.legisocial.fr/reperes-sociaux/gratification-stagiaire-2026.html), [Lefebvre Dalloz](https://open.lefebvre-dalloz.fr/droit-social/statuts-particuliers/gratification-stage_t6475)
- Syntec (IDCC 1486): secondary sources give a 2026 cadre minimum of about 2,135 EUR gross/month at position 1.1 (coefficient 95) and about 5,755 at 3.3, point value 21.40 EUR (coefficient times point value), forfait jours capped at 218 days. Figures differ between sites; the primary source is code.travail.gouv.fr and Legifrance. — [Salerya](https://salerya.fr/conventions/syntec/), [Code du travail numérique](https://code.travail.gouv.fr/contribution/1486-quel-est-le-salaire-minimum)
- Anti-discrimination: L1132-1 Code du travail bars discrimination from hiring through termination on a list of criteria (one source counts 25). The generalised mandatory anonymous CV for companies of 50+ employees (2006 law) was never implemented and a 2015 law removed it; it remains optional. I could not retrieve the statute text itself. — [swim.legal](https://www.swim.legal/blog/motifs-de-discrimination-au-travail-criteres-prohibes-code-travail), [Hospitality-ON](https://hospitality-on.com/fr/ressources-humaines/pas-de-generalisation-du-cv-anonyme)
- Photo, one-page CV and CDD up to 18 months: see france_us_markets.md.

### Inferences
- Because L1132-1 forbids the employer from using criteria such as origin, age or family status, a modern default CV template should omit age, marital status and nationality, with photo optional (earlier note). This is a design recommendation, not a sourced rule.
- (unsourced background) Contract-type fields should include alternance (apprentissage versus professionnalisation) and stage with gratification. The préavis is set by convention collective or contract.

### Gaps
- Préavis (notice period) rules: no source retrieved this pass. The earlier career-ops text says 1 to 3 months, unverified.
- Portage salarial rules and the intérim contract regime: not searched this pass.
- Primary texts (Legifrance L1132-1, service-public pages for période d'essai and stage): fetches returned errors or wrong pages.
- Salary conventions (brut annuel in K€, variable, 13th month): only the career-ops mode text, not an independent source.

## Does career-ops `modes/fr/` handle these conventions?

### Takeaway
Partly. It covers contract and compensation vocabulary well but nothing on CV format (photo, état civil), the vous-moi-nous letter structure, alternance or stage, intérim, anonymisation or anti-discrimination law, and it has no French job-source connectors beyond the pipeline's mention of boards. Repo: career-ops-hq/career-ops, MIT licence, about 83.2k stars, pushed 2026-11-17 (GitHub API).

### Cited Findings
- `modes/fr/` has only 5 files: README.md, _shared.md, offre.md, pipeline.md, postuler.md. README says the first iteration covers the four highest-impact modes; scan, batch, pdf, tracker, outcome, auto-pipeline, deep, contacto, ofertas, project and training stay in EN/ES. — [modes/fr/README.md](https://github.com/career-ops-hq/career-ops/blob/main/modes/fr/README.md)
- Handled in `_shared.md`: a table of CDI, CDD, période d'essai ("3-4 mois cadre, renouvelable 1 fois, max 8 mois", flag if over 4 months initial), préavis ("1-3 mois selon convention collective et ancienneté"), statut cadre, Syntec (check position and coefficient), RTT, 13th month ("brut annuel = brut mensuel x 13"), intéressement/participation, titres-restaurant, mutuelle, prévoyance, CSE, 25 days paid leave, and portage salarial (about 10% cost). Also a salary-negotiation script asking for fixed gross annual, 13th month and variable separately, and a note that French freelance rates run 30-50% above the CDI hourly-gross equivalent. — [modes/fr/_shared.md](https://github.com/career-ops-hq/career-ops/blob/main/modes/fr/_shared.md)
- `offre.md` Block D checklist asks about 13th month, variable (bonus, commission, BSPCE or stock options), convention collective classification, CDI versus CDD (duration, motive, CDI-isation), and freelance/portage (TJM, requalification risk). Block G flags employment-form risk for "freelance" wording. — [modes/fr/offre.md](https://github.com/career-ops-hq/career-ops/blob/main/modes/fr/offre.md)
- `postuler.md` states salary fields in "brut annuel pour la France", suggests a EUR range from profile.yml, an availability date that accounts for préavis ("souvent 1-3 mois") and a "Je vous choisis" tone. — [modes/fr/postuler.md](https://github.com/career-ops-hq/career-ops/blob/main/modes/fr/postuler.md)
- Cover letter rule in `_shared.md`: always include one if the form allows, one page max, same visual design as the CV. No vous-moi-nous structure. — [modes/fr/_shared.md](https://github.com/career-ops-hq/career-ops/blob/main/modes/fr/_shared.md)
- Not found in `modes/fr/` (my own grep of the five files, case-insensitive): photo, état civil, vous-moi-nous, gratification, alternance, intérim, L1132, CV anonyme, La Bonne Alternance, France Travail API. "France Travail" appears only as a board name in the README, pipeline, postuler and _shared. A repo code search for "alternance", "L1132", "état civil" and "vous-moi" returned nothing.
- Repo-wide: `modes/pdf.md` selects letter or A4 by country, supports `--max-pages` and `cv.max_pages` (so a one-page CV is configurable), and the CV templates have a `photo` field. `templates/jurisdiction-unpaid-placement.yml` is a jurisdiction table for unpaid-placement checks (gratification appears there; I did not read which jurisdictions it lists, so France coverage is unconfirmed). — [modes/pdf.md](https://github.com/career-ops-hq/career-ops/blob/main/modes/pdf.md), [jurisdiction-unpaid-placement.yml](https://github.com/career-ops-hq/career-ops/blob/main/templates/jurisdiction-unpaid-placement.yml)
- Caveats on interpreting the mode text: the période d'essai line says "3-4 mois cadre", while France Travail gives 4 months for cadres; the "max 8 mois" matches. The leave claim "<25 jours = illegal" and the figure "participation obligatoire > 50 salariés" are not verified by me.

### Inferences
- Gaps in career-ops that this project could fill: a French CV profile (photo optional, no état civil, one page, "compétences" sections), vous-moi-nous letter template, alternance and stage modes, La Bonne Alternance and CSP connectors, and an anti-discrimination guard.
- The mode text is guidance for the LLM, not enforced code; no French-specific script exists that I saw.

### Gaps
- I did not read `modes/pdf.md` fully or the remaining repo for a French CV template; the GitHub API rate limit was exhausted, so file-by-file search for `fr` in templates was not completed.
- The French sections of `_shared.md` truncated in my first read; I re-grepped for keywords rather than reading every line.
