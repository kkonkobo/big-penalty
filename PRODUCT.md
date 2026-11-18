# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users
A small family setup, not a commercial product. Two roles, both confirmed:

- **The operator** sets up and runs the tool for relatives: creates profiles, keeps sources, keys and the daily schedule working, and keeps an eye on what was found and what was sent.
- **The job seeker** (a relative; the first profile is an engineering student in mechatronics and embedded systems looking for an internship or work-study contract in France) reviews offers, checks tailored CVs and approves what needs approval.

Both use it on a computer (desktop app on macOS or Windows, or the local web page). Phone use is not a target; layouts only need to stay usable on narrow windows.

## Product Purpose
Find fresh jobs in France that match a *field or function* (not a job title) once a day, rank them, and apply on the company's own site with a CV tailored to each role. Success: every morning there are new, relevant offers ready to act on, and good applications go out quickly without the job seeker rewriting their CV each time.

## Positioning
One pipeline from search to sent application, built for the French market: around twenty sources (aggregators, French job boards, public and official sites), deduplicated and traced back to the company's own careers page or applicant tracking system; scoring that reads job ads as HR wish lists rather than hard filters; and per-job CVs generated from a confirmed master CV.

## Operating Context
- A search runs automatically once a day (macOS launchd; catches up when the Mac wakes) and can be launched by hand. A full search takes about 20–30 minutes.
- Daily routine: open the app, look at what is new since the last search, approve or reject offers, prepare a CV for the good ones, review and approve it, apply on the company site.
- Aggregators (LinkedIn, Indeed, …) are for discovery only; applications go through the employer's own site or ATS.
- Some sources need a human now and then: anti-bot checks, logins, email verification links, CAPTCHAs.

## Capabilities and Constraints
- **Freshness:** offers older than 14 days are hidden (never deleted); newer offers are strongly preferred; reposts are flagged; offers not seen again for several days count as expired.
- **Scoring:** requirements, including education level, lower the score but never exclude an offer. Only contract type (and location, when the profile restricts it) are hard blockers.
- **Tailored CVs:** generated from a master CV the user confirms. Wording may be strengthened to match the ad, but hard facts stay true: employers, degrees, dates, certifications, language levels, driving licence. Anything the CV claims beyond the master CV is listed as interview preparation.
- **Autonomy:** the product acts on its own where the rules allow it and tells the user clearly what it did. Applications with a fit score of 60 or more are sent automatically; lower scores wait for the user's OK. It pauses for things only a person can do (CAPTCHAs, email links, unknown facts) and never guesses an answer it does not know.
- AI: Anthropic primary, OpenAI fallback, rule-based mode when no provider is available. The interface must say which one produced a result.
- Interface language: French. Local data only (SQLite on the user's disk); API keys live in the environment and are never shown.

## Brand Commitments
- Name: **Big Penalty**. App icon: `assets/icon.svg`.
- Voice: plain, direct French. Buttons name their action; errors say what went wrong and what to do next.

## Evidence on Hand
- Real data exists only on the operator's machine (job database, CVs, photos, contact details). The repository is **public**: no personal data, real names, CV content or screenshots of real profiles may be committed.
- Benchmarks of the AI providers: `reports/LLM benchmark.md`.
- There are no testimonials, users beyond the family, or usage metrics; do not invent any.

## Product Principles
1. **Fresh first.** What is new today matters more than the full archive; the first screen answers "what should I act on now?".
2. **Act, then report.** Automate whatever the rules allow, and always show what was done, why, and how to undo or correct it.
3. **True on the facts.** Embellish wording to beat ATS filters, never the facts; flag every claim the job seeker will need to defend.
4. **Wish lists, not walls.** Never hide a plausible offer because of HR boilerplate; explain the score instead.
5. **A human for human things.** Pause cleanly for CAPTCHAs, logins and unknown answers; make the blocker obvious and quick to clear.
