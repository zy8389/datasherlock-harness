# Scoped SQL Evidence Rule Inventory

## Scope and frozen-run identity

This inventory is the Phase A admission review for typed SQL evidence rules.
It analyzes only the 15 cases classified as primary `EVIDENCE_NEUTRALIZED` in
the PR #18 audit of the immutable `full-60-4arch-post-pr14-20260831` Full
Harness run.

- Raw artifact: `experiments/ablation/results/full-60-4arch-post-pr14-20260831/full_harness/results.jsonl`
- SHA256: `65bc8741a7baf92fbe6137b563e3ae1954bdce248ebebc00a74a5d23400d12ad`
- Record count: 60
- Audited cases: 15
- Audited golden-hypothesis SQL steps: 19
- Ground Truth use: offline comparison only; no Ground Truth value enters a
  runtime recognizer.

The admission gate requires a successful, validated, usable, complete SQL
result plus exact hypothesis, metric, target-date, table, column, and numeric
relation checks. Query purpose and aliases alone are never evidence. F10 is
excluded because its missing second independent source is a plan-coverage
problem, not a SQL-recognition problem.

## Admission summary

| Fault | Root cause | Decision | Candidate rule | Reason |
| --- | --- | --- | --- | --- |
| F02 | `duplicate_batch` | ADMIT | `duplicate_identity_counts` | The returned total, distinct identity count, and duplicate excess prove the abnormality in the scoped AI-task population. |
| F05 | `timezone_error` | BLOCK | `BLOCKED_RULE_NO_TRUSTED_BASELINE` | Daily counts and timestamp extrema do not prove a fixed timezone offset or a configuration mismatch. |
| F06 | `unit_error` | BLOCK | `BLOCKED_RULE_NO_TRUSTED_BASELINE` | One-day average/minimum/maximum values have no baseline or robust within-result scale comparator such as max/median. |
| F07 | `join_filter` | ADMIT | `join_filter_survivor_counts` | A single target-day LEFT JOIN result contains the full event-user population and the subscription-matched survivor population; the latter is lower. |
| F08 | `join_explosion` | BLOCK | no rule | The usable result shows no event duplication; the other candidate result failed SQL validation. |
| F09 | `field_drift` | BLOCK | no rule | Event-name counts aggregate the target date together with an adjacent date, so `execute_ai_task` cannot be bound to the incident date. |
| F12 | `ab_split_anomaly` | BLOCK | `BLOCKED_RULE_NO_TRUSTED_BASELINE` | The results expose schema/catalog presence, not assignment ratios or versioned experiment configuration values. |

## Per-step inventory

All listed steps have no segment filter unless explicitly stated. Sanitized
values retain only the returned diagnostic shape and numeric/date values.

### F02 duplicate_batch

Canonical evidence is duplicate event identity in the `ai_task_count`
population. The admitted recognizer requires `events`, the canonical
`event_name = 'run_ai_task'` filter, target-date coverage, and a numeric
`total > distinct` relation. If a duplicate-excess column is present, it must
also be positive and consistent with that relation.

| Case / step | Target and query scope | Returned shape and sanitized values | Current reason | Candidate source / rule | Safe |
| --- | --- | --- | --- | --- | --- |
| F02-001 / S1 | `ai_task_count`; 2026-01-30; `events`; exact date; AI-task filter | `rows_on_date, distinct_event_ids, duplicate_event_id_rows`; `[126, 90, 36]`; 1 row | matched no rule | `business_data` / `duplicate_identity_counts` | YES: 126 > 90 and excess is 36. |
| F02-001 / S5 | `ai_task_count`; range 2026-01-23..30; `events`; AI-task filter; target row present | `metric_date, ai_task_count, distinct_ai_task_count`; target `[2026-01-30, 126, 90]`; 8 rows | matched no rule | `business_data` / `duplicate_identity_counts` | YES: only the target-dated row is evaluated and 126 > 90. |
| F02-002 / S1 | `ai_task_count`; range 2026-01-22..30; `events`; AI-task filter; target 2026-01-29 row present | `metric_date, row_count, nonnull_event_id_count, distinct_event_id_count, repeated_event_id_rows`; target `[2026-01-29, 118, 118, 84, 34]`; 9 rows | matched no rule | `business_data` / `duplicate_identity_counts` | YES: target total 118 > distinct 84 and excess is 34. |
| F02-004 / S1 | `ai_task_count`; half-open 2026-01-27..28; `events`; AI-task filter | `row_count, nonnull_event_id_count, distinct_event_id_count, repeated_row_excess`; `[112, 112, 86, 26]`; 1 row | matched no rule | `business_data` / `duplicate_identity_counts` | YES: exact target window and 112 > 86 with excess 26. |
| F02-004 / S2 | `ai_task_count`; range 2026-01-24..30; `events`; AI-task filter; target 2026-01-27 row present | `event_date, row_count, distinct_event_id_count, repeated_row_excess`; target `[2026-01-27, 112, 86, 26]`; 6 rows | matched no rule | `business_data` / `duplicate_identity_counts` | YES: only the target row is evaluated and its relation is self-proving. |

### F05 timezone_error

| Case / step | Target and query scope | Returned shape and sanitized values | Current reason | Candidate source / rule | Safe |
| --- | --- | --- | --- | --- | --- |
| F05-002 / S5 | `daily_active_users`; 2026-01-29; `events`; range 2026-01-27..31 | `cast_date, distinct_users, first_event_time, last_event_time`; target `[2026-01-29, 91, 00:13:04, 23:36:19]`; 5 rows | matched no rule | `business_data` / `BLOCKED_RULE_NO_TRUSTED_BASELINE` | NO: date-level extrema and counts do not demonstrate a fixed offset, CN boundary movement, or timezone configuration mismatch. |

### F06 unit_error

| Case / step | Target and query scope | Returned shape and sanitized values | Current reason | Candidate source / rule | Safe |
| --- | --- | --- | --- | --- | --- |
| F06-002 / S01 | `average_session_duration`; 2026-01-29; `events`; exact date | `total_rows, average_duration, minimum_duration, maximum_duration`; `[348, 57077.5666, 2.77, 838660]`; 1 row | matched no rule | `business_data` / `BLOCKED_RULE_NO_TRUSTED_BASELINE` | NO: no baseline or median/percentile comparison; absolute values cannot define the rule. |
| F06-003 / S01 | `average_session_duration`; 2026-01-28; `events`; exact date | same columns; `[347, 36738.4067, 1.99, 390465]`; 1 row | matched no rule | `business_data` / `BLOCKED_RULE_NO_TRUSTED_BASELINE` | NO: same missing comparator. |
| F06-004 / S01 | `average_session_duration`; 2026-01-27; `events`; exact date | same columns; `[348, 68098.5122, 10.33, 1509560]`; 1 row | matched no rule | `business_data` / `BLOCKED_RULE_NO_TRUSTED_BASELINE` | NO: same missing comparator. |
| F06-005 / S01 | `average_session_duration`; 2026-01-26; `events`; exact date | same columns; `[369, 67920.7685, 5.48, 608400]`; 1 row | matched no rule | `business_data` / `BLOCKED_RULE_NO_TRUSTED_BASELINE` | NO: same missing comparator. |

### F07 join_filter

The admitted rule is intentionally limited to the exact self-contained LEFT
JOIN shape: `event_users` is the pre-filter event population and
`subscribed_users` is the population that would survive the subscription
join. It requires the target date, `events`, `subscriptions`, and the LEFT JOIN
in the SQL. Equal counts remain neutral.

| Case / step | Target and query scope | Returned shape and sanitized values | Current reason | Candidate source / rule | Safe |
| --- | --- | --- | --- | --- | --- |
| F07-001 / S04 | `daily_active_users`; 2026-01-30; `events LEFT JOIN subscriptions`; exact event date | `event_users, subscribed_users`; `[111, 26]`; 1 row | matched no rule | `business_data` / `join_filter_survivor_counts` | YES: the same scoped query proves that only 26 of 111 event users survive subscription matching. |

### F08 join_explosion

| Case / step | Target and query scope | Returned shape and sanitized values | Current reason | Candidate source / rule | Safe |
| --- | --- | --- | --- | --- | --- |
| F08-003 / S3 | `ai_task_count`; 2026-01-28; `events`; range 2026-01-27..29; AI-task filter | `metric_date, counted_rows, distinct_events, excess_rows`; target `[2026-01-28, 81, 81, 0]`; 3 rows | matched no rule | `business_data` / joined-duplication candidate | NO: no join is present and target total equals distinct identities. |
| F08-005 / S2 | `ai_task_count`; 2026-01-26; `events`; exact date; AI-task filter | `event_id, row_count, distinct_event_times`; no usable rows | SQL result validation failed | none | NO: fail-closed validation gate applies before any recognizer. |

### F09 field_drift

| Case / step | Target and query scope | Returned shape and sanitized values | Current reason | Candidate source / rule | Safe |
| --- | --- | --- | --- | --- | --- |
| F09-003 / S4 | `ai_task_count`; target 2026-01-28; `events`; combined 2026-01-27..28 window | `event_name, event_count`; includes `[run_ai_task, 127]` and `[execute_ai_task, 40]`; 7 rows | matched no rule | `business_data` / field-name-count candidate | NO: the aggregate has no date column and combines baseline and target dates, so the new name cannot be attributed to the target date. |

### F12 ab_split_anomaly

| Case / step | Target and query scope | Returned shape and sanitized values | Current reason | Candidate source / rule | Safe |
| --- | --- | --- | --- | --- | --- |
| F12-002 / S6 | `conversion_rate`; 2026-01-29; `information_schema.columns`; experiment tables | table/column/type metadata; assignment columns include `experiment_id, variant`; 3 rows | matched no rule | none / `BLOCKED_RULE_NO_TRUSTED_BASELINE` | NO: schema presence contains no allocation value. |
| F12-002 / S7 | `conversion_rate`; 2026-01-29; `subscriptions LEFT JOIN events`; 2026-01-27..29 | `metric_date, subscription_users, active_subscription_users`; target `[2026-01-29, 16, 16]`; 3 rows | matched no rule | `business_data` / `BLOCKED_RULE_NO_TRUSTED_BASELINE` | NO: no assignment variant or experiment-config observation. |
| F12-004 / S6 | `conversion_rate`; 2026-01-27; `information_schema.tables` | experiment table names only; 2 rows | matched no rule | none / `BLOCKED_RULE_NO_TRUSTED_BASELINE` | NO: table existence is not split evidence. |
| F12-005 / S6 | `conversion_rate`; 2026-01-26; `information_schema.columns` | table/column/type metadata; 18 rows | matched no rule | none / `BLOCKED_RULE_NO_TRUSTED_BASELINE` | NO: schema presence contains no allocation value. |
| F12-005 / S7 | `conversion_rate`; 2026-01-26; `information_schema.tables` | `table_name, rows`; one catalog row for each requested table | matched no rule | none / `BLOCKED_RULE_NO_TRUSTED_BASELINE` | NO: this counts catalog entries, not assignments or configuration values. |

## Counterexample requirements for admitted rules

Both admitted rules must remain neutral for the wrong hypothesis, metric,
target date, table, event-name population, result columns, and non-numeric
values. F02 must also remain neutral when total equals distinct or duplicate
excess is zero/inconsistent. F07 must remain neutral when matched users equal
event users, when the join is not a LEFT JOIN to `subscriptions`, or when the
target date is not proven. Invalid, empty, truncated, or unusable results stay
neutral through the existing SQL envelope gate.
