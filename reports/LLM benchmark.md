# LLM benchmark

Date: 2026-11-17. Model-generated gold labels: human review required.

Corpus: 40 real postings × 3 profiles = 120 pairs; 4 good / 10 partial / 106 bad. Always-bad baseline: 88.3%. This is a small, deliberately hard-negative-heavy corpus, not evidence of universal job-fit quality. The web-junior profile has no unambiguous gold-good posting; compare balanced accuracy and inspect errors before choosing models.

## SCORE

| Config | Pairs | Accuracy | Spearman | Bad ≥70 | Failures | p50 / p95 batch (s) | $ / 100 jobs |
|---|---:|---:|---:|---:|---:|---:|---:|
| opus-low | 120 | 86.7% | 0.510 | 2 | 0/15 | 6.90 / 8.36 | $0.5353 |
| opus-medium | 120 | 85.0% | 0.510 | 0 | 0/15 | 7.85 / 10.81 | $0.5364 |
| haiku-low | 120 | 85.0% | 0.490 | 3 | 0/15 | 4.07 / 5.28 | $0.0138 |
| sonnet-low | 120 | 84.2% | 0.498 | 2 | 0/15 | 5.72 / 6.63 | $0.2750 |
| sonnet-medium | 120 | 81.7% | 0.487 | 5 | 0/15 | 5.99 / 6.91 | $0.2710 |
| sol-low | 120 | 85.0% | 0.506 | 5 | 0/15 | 10.98 / 14.33 | $0.2071 |
| luna-low | 120 | 82.5% | 0.481 | 4 | 0/15 | 6.25 / 9.20 | $0.0107 |
| opus-medium-cli | 120 | 85.8% | 0.507 | 0 | 0/15 | 9.01 / 11.49 | $0.7101 |
| sonnet-low-cli | 120 | 83.3% | 0.489 | 3 | 0/15 | 7.26 / 8.61 | $0.3463 |
| sol-low-cli | 120 | 83.3% | 0.489 | 7 | 0/15 | 15.44 / 17.58 | $0.5522 |
| sol62-low-cli | 120 | 85.0% | 0.493 | 7 | 0/15 | 18.87 / 22.61 | $0.5260 |
| sol62-medium-cli | 120 | 84.2% | 0.487 | 7 | 0/15 | 20.62 / 23.24 | $0.5971 |

Balanced accuracy (mean recall across present classes; only four positive pairs): opus-low 60.8%; opus-medium 49.2%; haiku-low 60.2%; sonnet-low 45.8%; sonnet-medium 41.9%; sol-low 73.2%; luna-low 56.2%; opus-medium-cli 60.5%; sonnet-low-cli 45.5%; sol-low-cli 69.6%; sol62-low-cli 76.2%; sol62-medium-cli 69.9%.

## API vs CLI (same model and effort)

CLI rows run through a subscription-signed `claude -p` / `codex exec` (tools, settings and plugins off; API keys removed from the CLI environment). Their cost is the API-equivalent, not money spent.

| Model | Via | Accuracy | Balanced accuracy | Bad ≥70 | p50 batch (s) |
|---|---|---:|---:|---:|---:|
| opus-medium | API | 85.0% | 49.2% | 0 | 7.85 |
| opus-medium-cli | CLI | 85.8% | 60.5% | 0 | 9.01 |
| sonnet-low | API | 84.2% | 45.8% | 2 | 5.72 |
| sonnet-low-cli | CLI | 83.3% | 45.5% | 3 | 7.26 |
| sol-low | API | 85.0% | 73.2% | 5 | 10.98 |
| sol-low-cli | CLI | 83.3% | 69.6% | 7 | 15.44 |

## PLAN

| Config | Profile | Queries | Checklist | FR declared / lexical | Invalid ROME codes | Seconds | Cost |
|---|---|---:|---:|---:|---:|---:|---:|
| opus-low | mechatronics | 25 | 100% | 84% / 80% | 0 | 11.54 | $0.03121 |
| opus-low | logistics | 25 | 83% | 72% / 60% | 0 | 7.97 | $0.02439 |
| opus-low | web-junior | 25 | 100% | 76% / 60% | 0 | 9.04 | $0.02851 |
| opus-medium | mechatronics | 25 | 100% | 84% / 76% | 0 | 10.33 | $0.02569 |
| opus-medium | logistics | 25 | 83% | 68% / 60% | 0 | 12.52 | $0.02909 |
| opus-medium | web-junior | 25 | 100% | 76% / 68% | 0 | 8.79 | $0.02319 |
| haiku-low | mechatronics | 26 | 100% | 85% / 85% | 0 | 5.95 | $0.00079 |
| haiku-low | logistics | 25 | 83% | 76% / 68% | 0 | 4.69 | $0.00076 |
| haiku-low | web-junior | 26 | 100% | 73% / 54% | 0 | 5.11 | $0.00078 |
| sol-low | mechatronics | 25 | 100% | 80% / 80% | 0 | 10.72 | $0.00858 |
| sol-low | logistics | 24 | 83% | 71% / 71% | 0 | 17.12 | $0.00879 |
| sol-low | web-junior | 25 | 100% | 64% / 44% | 0 | 9.56 | $0.00779 |
| luna-low | mechatronics | 25 | 100% | 80% / 80% | 0 | 8.49 | $0.00048 |
| luna-low | logistics | 25 | 83% | 72% / 72% | 0 | 8.21 | $0.00051 |
| luna-low | web-junior | 22 | 100% | 68% / 59% | 0 | 9.08 | $0.00045 |
| sonnet-low | mechatronics | 25 | 100% | 84% / 84% | 0 | 7.69 | $0.01524 |
| sonnet-low | logistics | 25 | 83% | 76% / 64% | 0 | 7.31 | $0.01509 |
| sonnet-low | web-junior | 25 | 100% | 72% / 64% | 0 | 6.90 | $0.01388 |
| sonnet-medium | mechatronics | 25 | 100% | 84% / 84% | 0 | 7.68 | $0.01252 |
| sonnet-medium | logistics | 25 | 83% | 72% / 60% | 0 | 7.80 | $0.01273 |
| sonnet-medium | web-junior | 25 | 100% | 76% / 68% | 0 | 5.97 | $0.01113 |
| opus-medium-cli | mechatronics | 25 | 100% | 84% / 80% | 0 | 12.61 | $0.03977 |
| opus-medium-cli | logistics | 25 | 83% | 76% / 68% | 0 | 13.11 | $0.03651 |
| opus-medium-cli | web-junior | 25 | 100% | 76% / 68% | 0 | 10.05 | $0.03029 |
| sonnet-low-cli | mechatronics | 25 | 100% | 88% / 88% | 0 | 7.88 | $0.01801 |
| sonnet-low-cli | logistics | 25 | 83% | 80% / 76% | 0 | 7.89 | $0.01515 |
| sonnet-low-cli | web-junior | 25 | 100% | 72% / 68% | 0 | 7.24 | $0.01448 |
| sol-low-cli | mechatronics | 25 | 100% | 80% / 80% | 0 | 19.71 | $0.04023 |
| sol-low-cli | logistics | 24 | 83% | 62% / 58% | 0 | 17.76 | $0.03661 |
| sol-low-cli | web-junior | 24 | 100% | 62% / 50% | 0 | 18.55 | $0.03926 |
| sol62-low-cli | mechatronics | 22 | 100% | 82% / 73% | 0 | 18.18 | $0.04005 |
| sol62-low-cli | logistics | 21 | 83% | 67% / 67% | 0 | 17.85 | $0.04089 |
| sol62-low-cli | web-junior | 21 | 100% | 76% / 57% | 0 | 17.39 | $0.03923 |
| sol62-medium-cli | mechatronics | 22 | 100% | 82% / 68% | 0 | 25.76 | $0.04506 |
| sol62-medium-cli | logistics | 20 | 83% | 70% / 70% | 0 | 18.97 | $0.04180 |
| sol62-medium-cli | web-junior | 21 | 80% | 71% / 43% | 0 | 19.21 | $0.04245 |

## Typical run projection

One profile, one 25-query plan, 300 scored jobs; arithmetic mean across the three profiles.

| Config | Plan | 300 jobs | Total USD |
|---|---:|---:|---:|
| opus-low | $0.0280 | $1.6060 | $1.6341 |
| opus-medium | $0.0260 | $1.6091 | $1.6350 |
| haiku-low | $0.0008 | $0.0415 | $0.0423 |
| sonnet-low | $0.0147 | $0.8251 | $0.8399 |
| sonnet-medium | $0.0121 | $0.8131 | $0.8253 |
| sol-low | $0.0084 | $0.6213 | $0.6297 |
| luna-low | $0.0005 | $0.0322 | $0.0327 |
| opus-medium-cli | $0.0355 | $2.1304 | $2.1659 |
| sonnet-low-cli | $0.0159 | $1.0390 | $1.0548 |
| sol-low-cli | $0.0387 | $1.6565 | $1.6952 |
| sol62-low-cli | $0.0401 | $1.5780 | $1.6180 |
| sol62-medium-cli | $0.0431 | $1.7914 | $1.8345 |

## Recommendation and chosen defaults

Keep **anthropic → openai → rules**. This small, model-labelled corpus is not sufficient evidence to promote OpenAI to primary.

| Task | Primary | Secondary |
|---|---|---|
| PLAN | claude-opus-5-5, medium effort | gpt-6-luna, low effort |
| SCORE | claude-opus-5-5, medium effort | gpt-6.1-sol, low effort |

- Opus medium made zero gold-bad ≥70 mistakes, versus two for Opus low, three for Haiku, five for Sol and four for Luna. For a personal shortlist, conservative rejection is preferable to misleading high recommendations; this is a safety/precision choice, not an overall quality win.
- Sol had the highest balanced accuracy (73.2%) but more bad-high errors. Use it as the scoring secondary for better minority-class recall, not as an unqualified upgrade over Claude. Luna is the economical planning secondary: all must-have mechatronics/web terms were covered and all returned ROME codes existed.
- Haiku low is a credible budget option, not the default: raw accuracy tied Opus medium (85.0%) at about 39× lower scoring cost, with three high-ranked bad jobs. Opt in with JOBHUNT_SCORING_MODEL=claude-haiku-5-5 and JOBHUNT_SCORING_EFFORT=low.
- Default primary run estimate: about $1.64 for one 25-query plan and 300 scored jobs. All-OpenAI fallback with Luna planning and Sol scoring: about $0.62. Search connector traffic is not included in LLM cost.
- Preserve the official Claude structured-output/cache/server-fallback request path. No temperature, top_p, thinking budget, dated aliases, or OpenAI-compatible Claude shim was introduced.

## Inspect high-ranked bad jobs

Examples below are gold-bad postings scored ≥70. Review the full source descriptions and generated gold labels, especially degree requirements beyond the 1,800-character production cap.

| Config | Profile | Job | Fit | Gold reason |
|---|---|---|---:|---|
| opus-low | mechatronics | job-04 | 72 | Stage ingénieur de cinq/six mois en navigation temps réel, niveau Bac+5 demandé très au-dessus du Bac+2. |
| opus-low | mechatronics | job-06 | 70 | Excellent domaine embarqué et STM32, mais dernière année d’école/Master 2 explicitement exigée. |
| haiku-low | mechatronics | job-01 | 72 | Stage de fin d’études/M2 explicite, trois années au-dessus de cet étudiant Bac+2 malgré la robotique pertinente. |
| haiku-low | mechatronics | job-04 | 75 | Stage ingénieur de cinq/six mois en navigation temps réel, niveau Bac+5 demandé très au-dessus du Bac+2. |
| haiku-low | mechatronics | job-06 | 80 | Excellent domaine embarqué et STM32, mais dernière année d’école/Master 2 explicitement exigée. |
| sonnet-low | mechatronics | job-01 | 72 | Stage de fin d’études/M2 explicite, trois années au-dessus de cet étudiant Bac+2 malgré la robotique pertinente. |
| sonnet-low | mechatronics | job-04 | 82 | Stage ingénieur de cinq/six mois en navigation temps réel, niveau Bac+5 demandé très au-dessus du Bac+2. |
| sonnet-medium | mechatronics | job-01 | 80 | Stage de fin d’études/M2 explicite, trois années au-dessus de cet étudiant Bac+2 malgré la robotique pertinente. |
| sonnet-medium | mechatronics | job-02 | 72 | Bac+5 et stage de fin d’études requis ; le sujet VLA ne compense pas le niveau d’études incompatible. |
| sonnet-medium | mechatronics | job-04 | 88 | Stage ingénieur de cinq/six mois en navigation temps réel, niveau Bac+5 demandé très au-dessus du Bac+2. |
| sol-low | mechatronics | job-01 | 72 | Stage de fin d’études/M2 explicite, trois années au-dessus de cet étudiant Bac+2 malgré la robotique pertinente. |
| sol-low | mechatronics | job-04 | 84 | Stage ingénieur de cinq/six mois en navigation temps réel, niveau Bac+5 demandé très au-dessus du Bac+2. |
| sol-low | mechatronics | job-06 | 84 | Excellent domaine embarqué et STM32, mais dernière année d’école/Master 2 explicitement exigée. |
| luna-low | mechatronics | job-01 | 82 | Stage de fin d’études/M2 explicite, trois années au-dessus de cet étudiant Bac+2 malgré la robotique pertinente. |
| luna-low | mechatronics | job-04 | 87 | Stage ingénieur de cinq/six mois en navigation temps réel, niveau Bac+5 demandé très au-dessus du Bac+2. |
| luna-low | mechatronics | job-06 | 88 | Excellent domaine embarqué et STM32, mais dernière année d’école/Master 2 explicitement exigée. |
| sonnet-low-cli | mechatronics | job-01 | 72 | Stage de fin d’études/M2 explicite, trois années au-dessus de cet étudiant Bac+2 malgré la robotique pertinente. |
| sonnet-low-cli | mechatronics | job-04 | 78 | Stage ingénieur de cinq/six mois en navigation temps réel, niveau Bac+5 demandé très au-dessus du Bac+2. |
| sonnet-low-cli | mechatronics | job-06 | 70 | Excellent domaine embarqué et STM32, mais dernière année d’école/Master 2 explicitement exigée. |
| sol-low-cli | mechatronics | job-01 | 86 | Stage de fin d’études/M2 explicite, trois années au-dessus de cet étudiant Bac+2 malgré la robotique pertinente. |
| sol-low-cli | mechatronics | job-02 | 81 | Bac+5 et stage de fin d’études requis ; le sujet VLA ne compense pas le niveau d’études incompatible. |
| sol-low-cli | mechatronics | job-04 | 94 | Stage ingénieur de cinq/six mois en navigation temps réel, niveau Bac+5 demandé très au-dessus du Bac+2. |
| sol62-low-cli | mechatronics | job-01 | 87 | Stage de fin d’études/M2 explicite, trois années au-dessus de cet étudiant Bac+2 malgré la robotique pertinente. |
| sol62-low-cli | mechatronics | job-02 | 80 | Bac+5 et stage de fin d’études requis ; le sujet VLA ne compense pas le niveau d’études incompatible. |
| sol62-low-cli | mechatronics | job-04 | 96 | Stage ingénieur de cinq/six mois en navigation temps réel, niveau Bac+5 demandé très au-dessus du Bac+2. |
| sol62-medium-cli | mechatronics | job-01 | 86 | Stage de fin d’études/M2 explicite, trois années au-dessus de cet étudiant Bac+2 malgré la robotique pertinente. |
| sol62-medium-cli | mechatronics | job-02 | 82 | Bac+5 et stage de fin d’études requis ; le sujet VLA ne compense pas le niveau d’études incompatible. |
| sol62-medium-cli | mechatronics | job-04 | 96 | Stage ingénieur de cinq/six mois en navigation temps réel, niveau Bac+5 demandé très au-dessus du Bac+2. |

## List-price inputs

USD per million tokens, standard synchronous tier, retrieved November 17, 2026.

| Model | Input | Output | Cache hit | Cache write (5m Claude / 30m OpenAI) |
|---|---:|---:|---:|---:|
| claude-opus-5-5 | $4 | $20 | $0.2 | $5 |
| claude-haiku-5-5 | $0.1 | $0.5 | $0.01 | $0.125 |
| claude-sonnet-5-5 | $2 | $10 | $0.1 | $2.5 |
| gpt-6.1-sol | $2 | $10 | $0.1 | $2.5 |
| gpt-6-luna | $0.1 | $0.5 | $0.01 | $0.125 |
| gpt-6.2-sol | $2 | $10 | $0.1 | $2.5 |

OpenAI 12-hour writes, if present in usage, are billed at 2.5× input; cached/write tokens are subtracted from its aggregate input. Anthropic input, cache-read and cache-creation counters are disjoint.

## Failover drill

Subprocess-only invalid Anthropic key → openai; .env unchanged.
Drill cost: $0.000089.

## Method and limitations

- All 3 profiles are crossed with all collected postings. Same 8-job batches and 1,800-character description cap as production.
- Student-profile assumption: second post-baccalaureate year (Bac+2), not second year of the three-year engineering cycle (Bac+4). Review this assumption and the model-generated labels before applying results to a real student.
- Gold bands: good ≥70, partial 40–69, bad <40. Existing production prompt retains its historical 85/60/35 scale; this mismatch is deliberately not tuned away for one model.
- Accuracy and Spearman exclude failed calls; failures are reported separately. Bad ≥70 counts gold-bad jobs scored at least 70.
- Spearman uses average ranks for ties. p95 is linearly interpolated over one pass, not a stable service-level estimate.
- French-ness is declared language plus a disclosed lexical heuristic, not a linguistic quality assessment.
- PLAN query count and checklist use the raw structured model plan; production subsequently deduplicates, fans out locations, and caps concrete searches at 25. Haiku exceeded the requested count in two raw plans. The logistics checklist’s literal CDI keyword is absent for all five configurations; contract fields are not counted by this keyword-only heuristic.
- Invalid ROME counts every returned code absent from France Travail’s October 2026 main ROME hierarchy; codes and provenance are frozen in tests/bench/rome_codes.json. This checks existence, not semantic suitability.
- Successful token usage includes reasoning tokens and cache reads/writes. Prices are standard list prices, not billing receipts. SDK parse failures may hide usage; reserve is conservative but cannot prove exact billing for hidden usage.
- Collection source errors are recorded without response bodies. No connector subscription or paid enrichment is used.
- Estimated total live benchmark + drill spend: **$2.491062 USD** (budget < $6).

Pricing sources (retrieved 2026-11-17): https://developers.openai.com/api/docs/pricing and https://platform.claude.com/docs/en/about-claude/pricing.

## Implementation files

- src/jobhunt/llm.py: provider protocol, official SDK implementations, ordered failover, diagnostics, usage tracking.
- src/jobhunt/config.py: provider order, independent model/effort settings, credential-safe representation.
- src/jobhunt/scoring.py: scored_by metadata; src/jobhunt/pipeline.py: run stats and client cleanup.
- src/jobhunt/cli.py: llm-check and provider configuration display.
- src/jobhunt/web/templates/_progress.html and shortlist.html: grouped provider badges and score provenance.
- tests/conftest.py: disable both live provider credentials in offline tests.
- tests/test_llm_providers.py and tests/test_bench_llm.py: mocked SDK, retries, failover, persistence/UI, and benchmark checks.
- tests/bench/: profiles, real job fixtures, generated gold labels, official ROME codes, collection/drill receipts and review guidance.
- scripts/bench_llm.py: live collection, capped/resumable evaluation, offline metrics/report, subprocess failover drill.
- README.md: configuration and operation; reports/llm-benchmark-results.json: raw outputs, usage, timings, costs.
- reports/LLM benchmark.md: this report; reports/llm-benchmark-chart.html: high-ranked bad-job comparison.
