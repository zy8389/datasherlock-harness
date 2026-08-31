# Post-PR19 Evidence Replay

## Scope

This report replays the PR #19 `RuntimeEvidenceInterpreter` against the
immutable Full Harness traces from `full-60-4arch-post-pr14-20260831`. It covers
only the 15 no-runtime-error cases classified as primary
`EVIDENCE_NEUTRALIZED` by PR #18.

This is a counterfactual interpreter replay against immutable traces. It is not
a replacement benchmark run. No Planner, model, tool, or case was executed;
the frozen raw result, PR #18 audit artifact, historical scores, and Ground
Truth were not modified.

| Identity | Value |
| --- | --- |
| Raw artifact | `experiments/ablation/results/full-60-4arch-post-pr14-20260831/full_harness/results.jsonl` |
| Raw SHA256 | `65bc8741a7baf92fbe6137b563e3ae1954bdce248ebebc00a74a5d23400d12ad` |
| Raw records | 60 |
| Cases replayed | 15 |
| Golden decisions replayed | 20 |
| Production rules added | `f02_duplicate_identity_counts`, `f07_join_filter_survivor_counts` |

Ground Truth is used offline only to identify each golden runtime hypothesis.
It is not passed to `RuntimeEvidenceInterpreter` or any runtime component.

## Method

The replay loads each frozen state, Planner plan, and `ToolExecutionResult`,
reconstructs alert scope with `IncidentEvidenceContext.from_alert()`, and calls
the current interpreter for every executed golden-hypothesis step. Unlike the
immutable PR #18 audit, this counterfactual comparison intentionally does not
require newly admitted evidence IDs to exist in the historical state.

The before counts come from the committed PR #18 audit artifact. The after
counts include every golden decision in these 15 cases, not only the SQL rows.
The set contains 19 originally neutral SQL decisions and one existing
`detect_distribution_drift` support in `F09-003`.

## Aggregate funnel

| Golden decision | Before | After | Change |
| --- | ---: | ---: | ---: |
| `NEUTRAL` | 19 | 13 | -6 |
| `SUPPORTS` | 1 | 7 | +6 |
| `CONTRADICTS` | 0 | 0 | 0 |

- Cases gaining at least one support: **4 / 15**.
- Newly admitted golden SQL observations: **6**.
- Cases still fully neutral: **10 / 15**.
- Case with a pre-existing support but no new support: **1 / 15**
  (`F09-003`).
- Implemented neutralized families: **2 / 7** (`F02`, `F07`).

## Per-case replay

| Case | Family | Neutral before -> after | Supports before -> after | Newly admitted steps | Counterfactual earliest cause |
| --- | --- | ---: | ---: | --- | --- |
| `F02-001` | F02 | 2 -> 0 | 0 -> 2 | `S1`, `S5` | `CONFIDENCE_SHORTFALL` |
| `F02-002` | F02 | 1 -> 0 | 0 -> 1 | `S1` | `EVIDENCE_MISSING` |
| `F02-004` | F02 | 2 -> 0 | 0 -> 2 | `S1`, `S2` | `CONFIDENCE_SHORTFALL` |
| `F05-002` | F05 | 1 -> 1 | 0 -> 0 | None | `EVIDENCE_NEUTRALIZED` |
| `F06-002` | F06 | 1 -> 1 | 0 -> 0 | None | `EVIDENCE_NEUTRALIZED` |
| `F06-003` | F06 | 1 -> 1 | 0 -> 0 | None | `EVIDENCE_NEUTRALIZED` |
| `F06-004` | F06 | 1 -> 1 | 0 -> 0 | None | `EVIDENCE_NEUTRALIZED` |
| `F06-005` | F06 | 1 -> 1 | 0 -> 0 | None | `EVIDENCE_NEUTRALIZED` |
| `F07-001` | F07 | 1 -> 0 | 0 -> 1 | `S04` | `EVIDENCE_MISSING` |
| `F08-003` | F08 | 1 -> 1 | 0 -> 0 | None | `EVIDENCE_NEUTRALIZED` |
| `F08-005` | F08 | 1 -> 1 | 0 -> 0 | None | `EVIDENCE_NEUTRALIZED` |
| `F09-003` | F09 | 1 -> 1 | 1 -> 1 | None | `EVIDENCE_NEUTRALIZED` |
| `F12-002` | F12 | 2 -> 2 | 0 -> 0 | None | `EVIDENCE_NEUTRALIZED` |
| `F12-004` | F12 | 1 -> 1 | 0 -> 0 | None | `EVIDENCE_NEUTRALIZED` |
| `F12-005` | F12 | 2 -> 2 | 0 -> 0 | None | `EVIDENCE_NEUTRALIZED` |

The counterfactual earliest cause applies the unchanged HypothesisManager
support count, `+0.15` support delta, and `0.75` confidence threshold to the
replayed decisions. It is a bottleneck projection, not a claim that a root
cause would be validated. `F02-001` and `F02-004` each reach two supports but
only `0.60` confidence. `F02-002` and `F07-001` gain one support and still lack
a second evidence path. All newly admitted observations have the single real
source type `business_data`; none creates Validator independence.

Primary `EVIDENCE_NEUTRALIZED` cases whose projected earliest category changes:
**4** (`F02-001`, `F02-002`, `F02-004`, `F07-001`).

## Remaining neutral cases

The 10 fully neutral cases are `F05-002`, `F06-002`, `F06-003`, `F06-004`,
`F06-005`, `F08-003`, `F08-005`, `F12-002`, `F12-004`, and `F12-005`.
`F09-003` is not fully neutral because its distribution-drift tool already
provided one support, but its mixed-date SQL observation remains neutral.

The blocked families stay neutral for specific precision reasons:

| Family | Reason retained |
| --- | --- |
| F05 | Timestamp extrema and daily counts do not prove a fixed offset or a timezone configuration mismatch. |
| F06 | One-day extrema and averages lack a trusted baseline or robust within-result scale comparator. |
| F08 | The usable target row shows no duplication; another candidate result failed SQL validation. |
| F09 | The event-name count combines target and adjacent dates, so the new name is not target-scoped. |
| F12 | Catalog/schema presence contains no assignment ratio or versioned configuration value. |

## Safety conclusion

The replay records material but bounded improvement: six structured SQL
observations become support without admitting any normal, wrong-date,
wrong-metric, wrong-segment, spoofed-projection, or unsafe-join counterexample.
The remaining observations stay neutral instead of relying on guessed
baselines. Planner behavior, HypothesisManager thresholds, RootCauseValidator,
Ground Truth, scoring, and frozen benchmark truth are unchanged.
