# Provider evaluation fixtures

Collected on November 17, 2026 using the project's own WTTJ, Meteojob,
Indeed, and Free-Work connectors in headless mode. France Travail was also
attempted but did not contribute postings for these queries. No job postings
were invented, and no paid enrichment was used.

- `profiles.json`: three fictitious profiles, with no real personal information.
- `jobs.json`: 40 real postings, full descriptions, original URLs, source IDs,
  collection date, and exact queries. Five redundant negative examples were
  replaced with live supplementary results in the same collection session.
- `collection.json`: initial collection attempts, counts, and safe error types.
- `labels.json`: **model-generated labels, not human-reviewed gold**. Every
  profile is paired with every posting, with an explicit short justification.
- `rome_codes.json`: 1,923 codes from France Travail's October 2026 main ROME
  hierarchy, with the official source and discovery metadata URLs. The source
  XLSX stores the code in three columns: domain letter + two-digit professional
  family + two-digit occupation; these are concatenated, not inferred.
- `failover-drill.json`: live subprocess drill receipt without secrets.

The label distribution is deliberately disclosed: 4 good, 10 partial, 106 bad.
The corpus contains many degree/seniority traps, including internships requiring
Master 2 and supposedly junior vacancies asking for multiple years of experience.
There is no unambiguous good vacancy for the junior web profile. Do not treat
high raw accuracy on this corpus as proof that a model retrieves or recognizes
all suitable junior jobs. Review labels and collect more positives before making
a general quality claim or switching the primary provider.

The production scorer sees only the first 1,800 description characters, just as
the benchmark does. Human/model gold review uses the full stored text. Important
requirements near the end can therefore be invisible to every tested model;
this is a known input-context limitation, not necessarily a model reasoning error.

`scripts/bench_llm.py` defaults to offline report generation. `--live` explicitly
enables paid calls and resumes a checkpoint with a dataset hash. `--collect`
refreshes the initial connector sample; it does not recreate the subsequent
manual curation or labels. Archive results, re-review labels, and freeze a new
dataset before benchmarking a refreshed sample.
