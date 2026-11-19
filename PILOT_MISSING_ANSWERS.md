# Pilot 1: missing confirmed answers — portal handoff

The local implementation is at `GET /profiles/{pid}/answers`, under **À compléter une fois**, **À décider**, and **Blocages techniques**. This extends the first pilot without a database migration or dependency on the VPS web-portal worktree.

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

New captures carry `fact_id`, `reason`, and `category` where appropriate; older records retain the exact FR/EN/DE label and control-kind fallback in `missing_answers.py`. Bare country is ambiguous unless a new capture has an explicit residential-address group. Use `facts.identity_answers` with the authorized profile and confirmed master to mirror the engine's identity resolution; pass presence only. Use `missing_answers.review_groups` on the same queue snapshot for decisions and technical blockers. Recorded or unsupported fields that are still paused remain visible for re-check. The local GET builds the queue once.

This integration must not prefill, infer, or copy an ATS answer. Keep the portal's current answer-write and stale-edit checks, including `facts.answers_lock` for answer mutations. Preserve existing write-only fields and don't add answer values to the projection, URLs, markup for the widget, or logs. For integration tests use fictional records only.

## Per-form choices

- Native, identifiable single-select/radio controls can carry `choice_binding` and `choice_indices`. Use `scoped_choices.labels(question)` to render only the presentation labels and opaque index tokens, with a blank default. Disabled and indistinguishable choices are omitted. Do not expose raw ATS values or collapse opposite consent questions. Unbound, ambiguous and unsupported controls remain for site/operator review. Use established standing choices such as internship agreement and declared device type; this checkout has no verified general country catalog.
- The local `POST /profiles/{pid}/answers/choice` validates the profile, latest paused run, captured origin, revision and approval state under `facts.answers_lock`. `scoped_choices.save_once` refuses a second confirmation for the same binding; saving never launches a run. Older runs without a captured origin must be checked again.
- Only a later explicit manual run can apply a saved choice. The binding requires the same native form and submit destination (resolved action and method), question wording and group, and full ordered option set, including unavailable alternatives. The raw action is not shown. A changed or unhandled selection stops the engine before dispatch; privacy and terms are handled on the site, not auto-confirmed. A preselected calling code is accepted only when it uniquely matches the confirmed international phone number. Existing send gates still apply.
- Reuse a calling code only from a supported unambiguous international number. Device type and residence must not be inferred. France permit prose only answers the exact descriptive field; an explicitly configured sponsorship answer can match a unique simple yes/no option on the exact question, not a qualified option or a blanket authorization question.
