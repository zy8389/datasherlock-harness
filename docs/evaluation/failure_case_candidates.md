# Post-PR #14 Failure Case Candidates

This is a diagnosis worklist for a later Harness iteration. It does not change
the frozen benchmark, production behavior, Ground Truth, or scoring. Raw
references point to the immutable local run and are intentionally not committed.

The 15 candidates cover all F01-F12 fault families, all four variants, provider
transport, strict model output, SQL planning, tool preconditions, guardrails,
and completed-but-unresolved validator behavior.

## Prediction and Outcome

`None` means no primary or ranked prediction was emitted. Full Harness Top-3
comes from attempted hypotheses even when the authoritative primary prediction
is null.

| # | Case / variant | Golden root cause | Primary prediction | Top-3 predictions | Final state | Classification |
|---:|---|---|---|---|---|---|
| 1 | F06-003 / Single Prompt | `unit_error` | None | None | `error` | `PROVIDER_INFRASTRUCTURE` |
| 2 | F01-002 / ReAct | `missing_partition` | None | None | `tool_failed` | `MODEL_PLAN_INVALID` |
| 3 | F04-003 / ReAct | `data_delay` | None | None | `tool_failed` | `TOOL_RUNTIME_EXPECTED` |
| 4 | F07-002 / ReAct | `join_filter` | None | None | `tool_failed` | `GUARDRAIL_EXPECTED` |
| 5 | F08-004 / ReAct | `join_explosion` | None | None | `tool_failed` | `MODEL_PLAN_INVALID` |
| 6 | F10-005 / ReAct | `schema_change` | None | None | `error` | `MODEL_PLAN_INVALID` |
| 7 | F02-002 / State Graph No Validator | `duplicate_batch` | `duplicate_batch` | `duplicate_batch`, `field_drift`, `join_explosion` | `TOOL_FAILED` | `TOOL_RUNTIME_EXPECTED` |
| 8 | F06-001 / State Graph No Validator | `unit_error` | `unit_error` | `unit_error`, `duplicate_batch`, `schema_change` | `TOOL_FAILED` | `MODEL_PLAN_INVALID` |
| 9 | F09-001 / State Graph No Validator | `field_drift` | `field_drift` | `field_drift`, `missing_partition`, `data_delay` | `TOOL_FAILED` | `MODEL_PLAN_INVALID` |
| 10 | F03-001 / Full Harness | `null_value_anomaly` | None | `null_value_anomaly`, `missing_partition`, `data_delay` | `TOOL_FAILED` | `TOOL_RUNTIME_EXPECTED` |
| 11 | F05-001 / Full Harness | `timezone_error` | None | `missing_partition`, `data_delay`, `schema_change` | `TOOL_FAILED` | `TOOL_RUNTIME_EXPECTED` |
| 12 | F06-001 / Full Harness | `unit_error` | None | `unit_error`, `duplicate_batch`, `timezone_error` | `TOOL_FAILED` | `MODEL_PLAN_INVALID` |
| 13 | F10-002 / Full Harness | `schema_change` | None | `missing_partition`, `schema_change`, `data_delay` | `TOOL_FAILED` | `GUARDRAIL_EXPECTED` |
| 14 | F11-002 / Full Harness | `metric_definition_change` | None | `missing_partition`, `data_delay`, `timezone_error` | `TOOL_FAILED` | `TOOL_RUNTIME_EXPECTED` |
| 15 | F12-001 / Full Harness | `ab_split_anomaly` | None | `join_filter`, `timezone_error`, `data_delay` | `UNRESOLVED` | `UNRESOLVED_OUTCOME` (not an error-taxonomy count) |

## Execution Details

| # | Failed step / tool | Tool / SQL calls | Validator result | Guardrail result | Raw artifact reference |
|---:|---|---:|---|---|---|
| 1 | Provider call before any tool | 0 / 0 | Not part of variant | No tool preflight occurred | `experiments/ablation/results/full-60-4arch-post-pr14-20260831/single_prompt/results.jsonl:28` |
| 2 | ReAct round 4 / `sql_query` | 4 / 4 | Not part of variant | All four calls allowed | `experiments/ablation/results/full-60-4arch-post-pr14-20260831/react/results.jsonl:2` |
| 3 | ReAct round 2 / `detect_schema_drift` | 2 / 1 | Not part of variant | Both calls allowed | `experiments/ablation/results/full-60-4arch-post-pr14-20260831/react/results.jsonl:18` |
| 4 | ReAct round 4 / blocked `sql_query` | 3 / 3 (4 attempts) | Not part of variant | Blocked: `unsafe_sql`, exactly one statement required | `experiments/ablation/results/full-60-4arch-post-pr14-20260831/react/results.jsonl:32` |
| 5 | ReAct round 4 / `sql_query` | 4 / 4 | Not part of variant | All four calls allowed | `experiments/ablation/results/full-60-4arch-post-pr14-20260831/react/results.jsonl:39` |
| 6 | ReAct model response after round 2 | 2 / 2 | Not part of variant | Both executed calls allowed | `experiments/ablation/results/full-60-4arch-post-pr14-20260831/react/results.jsonl:50` |
| 7 | Plan step S5 / `detect_schema_drift` | 5 / 3 | Deliberately absent in this variant | All five calls allowed | `experiments/ablation/results/full-60-4arch-post-pr14-20260831/state_graph_no_validator/results.jsonl:7` |
| 8 | Plan step S2 / `sql_query` | 2 / 2 | Deliberately absent in this variant | Both calls allowed | `experiments/ablation/results/full-60-4arch-post-pr14-20260831/state_graph_no_validator/results.jsonl:26` |
| 9 | Plan step S7 / `sql_query` | 7 / 6 | Deliberately absent in this variant | All seven calls allowed | `experiments/ablation/results/full-60-4arch-post-pr14-20260831/state_graph_no_validator/results.jsonl:41` |
| 10 | Plan step S5 / `detect_schema_drift` | 5 / 2 | No root cause authorized; `root_cause=null` | All five calls allowed | `experiments/ablation/results/full-60-4arch-post-pr14-20260831/full_harness/results.jsonl:11` |
| 11 | Plan step S5 / `detect_schema_drift` | 5 / 2 | No root cause authorized; `root_cause=null` | All five calls allowed | `experiments/ablation/results/full-60-4arch-post-pr14-20260831/full_harness/results.jsonl:21` |
| 12 | Plan step S2 / `sql_query` | 2 / 2 | No root cause authorized; `root_cause=null` | Both calls allowed | `experiments/ablation/results/full-60-4arch-post-pr14-20260831/full_harness/results.jsonl:26` |
| 13 | Plan step S7 / blocked `sql_query` | 6 / 3 (7 attempts) | No root cause authorized; `root_cause=null` | Blocked: `duplicate_tool_call` | `experiments/ablation/results/full-60-4arch-post-pr14-20260831/full_harness/results.jsonl:47` |
| 14 | Plan step S6 / `detect_schema_drift` | 6 / 3 | No root cause authorized; `root_cause=null` | All six calls allowed | `experiments/ablation/results/full-60-4arch-post-pr14-20260831/full_harness/results.jsonl:52` |
| 15 | No failed tool; graph exhausted at `UNRESOLVED` | 9 / 7 | Validator did not authorize a root cause; `root_cause=null` | All nine calls allowed | `experiments/ablation/results/full-60-4arch-post-pr14-20260831/full_harness/results.jsonl:56` |

## Why These Cases Are Representative

1. **F06-003 / Single Prompt** isolates the provider outage without any tool,
   SQL, guardrail, or Harness execution. The same outage affects ReAct, so it
   must not be attributed to an architecture regression.
2. **F01-002 / ReAct** is a pure model-generated SQL grouping error after three
   successful queries. It tests schema-aware repair without changing safety.
3. **F04-003 / ReAct** shows the two-snapshot precondition outside the graph
   adapters, proving the issue is not exclusive to graph orchestration.
4. **F07-002 / ReAct** confirms strict unsafe-SQL enforcement. The guardrail is
   working and should not be relaxed to improve completion rates.
5. **F08-004 / ReAct** selects a nonexistent `event_date` column after useful
   evidence collection, a concise schema-grounding failure.
6. **F10-005 / ReAct** is one of two strict structured-output failures. It
   preserves the intended adapter contract and is a candidate for model-side
   repair, not permissive parsing.
7. **F02-002 / State Graph No Validator** has the correct primary prediction
   but is still an error because a later diagnostic tool fails. It exposes the
   interaction between plan breadth and terminal status.
8. **F06-001 / State Graph No Validator** uses unsupported multi-column
   `COUNT(DISTINCT ...)` syntax and represents model SQL dialect quality.
9. **F09-001 / State Graph No Validator** orders by a nonexistent alias and
   represents late-plan schema grounding after six prior tool calls.
10. **F03-001 / Full Harness** includes the golden label in Top-3 but cannot
    finish because of the dominant schema-snapshot precondition.
11. **F05-001 / Full Harness** combines a wrong Top-3 with the same tool
    precondition, useful for separating planning quality from execution failure.
12. **F06-001 / Full Harness** shares the case with the no-validator variant but
    generates a different invalid query, illustrating model nondeterminism.
13. **F10-002 / Full Harness** is the only duplicate-operation block and proves
    the duplicate guardrail prevents repeated exact work.
14. **F11-002 / Full Harness** represents a metric-definition fault family that
    is routed into an unavailable schema-history diagnostic.
15. **F12-001 / Full Harness** has nine successful calls and no runtime error,
    yet remains unresolved with the golden cause absent from Top-3. It is the
    cleanest candidate for validator/evidence-transition analysis.

## Suggested Diagnostic Order

1. Define a stable logical fixture fingerprint before another comparative run.
2. Analyze snapshot-aware planning with candidates 3, 7, 10, 11, and 14.
3. Analyze strict plan/SQL repair with candidates 2, 5, 6, 8, 9, and 12.
4. Analyze completed-but-unresolved validation with candidate 15.
5. Keep candidates 4 and 13 as positive guardrail regression tests.

Do not selectively rerun or replace any candidate in the frozen 60x4 result.
