# Pilot 1: missing confirmed answers — portal handoff

The local implementation is at `GET /profiles/{pid}/answers`, under **Informations à confirmer**. This change does not add an API, schema migration, or new write or resume path. It has no dependency on the VPS web-portal worktree.

## Read model

Call `jobhunt.apply.missing_answers.project(profile_id, items, recorded_fact_keys=..., answered_labels=...)` on the server after authorizing the candidate profile. Use `apply.service.queue(profile_id)` or the equivalent **latest-run** queue snapshot: only `paused`/`input` applications whose latest run is `needs_input` on the current CV revision qualify. Do not aggregate run history or screen/telemetry data. Both dry-run and submit-mode pauses can qualify.

Build `recorded_fact_keys` from the supported keys with nonblank stored values in `answers.yaml` (`facts.get_answer`); build `answered_labels` from `facts.norm_question(question)` for nonblank stored custom answers. These are presence checks only: a saved value might still be rejected by a particular form. If the store is unreadable, omit the grouping and show the existing error; do not assume everything is missing.

Each row has `fact_key: str`, `application_ids: tuple[int, ...]`, and a computed `application_count: int`. If serializing, explicitly emit only those three fields (the computed count is not in the dataclass fields) and encode the IDs as an array. Sort by descending count, then ascending fact key; IDs are unique and sorted. Scope all links to applications the authorized reader can access. Do not serialize the queue items, questions, answer values, option lists, ATS URLs, or run content for this widget.

| `fact_key` (existing answer path) | Prompt meaning | Local link target |
| --- | --- | --- |
| `city` | Current city of residence | `#ans_city` |
| `postal_code` | Postal code of current address | `#ans_postal_code` |
| `country` | Country of residence, not nationality | `#ans_country` |
| `graduation_date` | Expected graduation date of the current program | `#ans_graduation_date` |
| `internship_agreement` | Whether the school can provide an agreement, not whether a document is signed or uploaded | `#ans_internship_agreement` |

Use the portal's existing editor for each key and its existing authorization for application detail links. On reload after an explicit save, a recorded key disappears from the missing list even if the run is still paused. Mark the count as “applications potentially unblocked”: it counts distinct applications with at least one matching missing question. The same application can appear in multiple rows, and this projection neither checks whether other blockers remain nor grants permission to retry or submit. Do not sum the row counts or automatically start a run from this widget.

## Matching and privacy

The existing question payload does not carry a canonical fact ID. Reuse the exact normalized FR/EN aliases and field-kind checks in `missing_answers.py`, not fuzzy matching. In particular, unqualified `City`, `Postal code`, `Country`, and `Graduation date` are ambiguous without context. Leave them in the existing question-level workflow instead of mapping them to candidate facts. An exact saved custom answer suppresses only its normalized question, not all aliases of a fact. Unknown questions and continued pauses with recorded values stay available for manual review.

This integration must not prefill, infer, or copy an ATS answer. Keep the portal's current answer-write and stale-edit checks, including `facts.answers_lock` for answer mutations. Preserve existing write-only fields and don't add answer values to the projection, URLs, markup for the widget, or logs. For integration tests use fictional records only.
