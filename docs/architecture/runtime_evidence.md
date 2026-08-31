# Runtime Evidence Contract

Runtime evidence is the deterministic boundary between a successful tool call
and a hypothesis update. Tool success, SQL validity, Planner prose, and a
plausible-looking alias are not evidence by themselves.

## Admission pipeline

`RuntimeEvidenceInterpreter` receives only runtime state:

- an `IncidentEvidenceContext` derived from the alert;
- the active `HypothesisState`;
- the executed `InvestigationStep`; and
- the structured `ToolExecutionResult`.

It does not receive a benchmark case ID, fault ID, expected root cause, Ground
Truth, or fault manifest. Ground Truth is available only to offline evaluation.

SQL observations pass these gates in order:

1. execution succeeded;
2. SQL result validation exists, passed, and marked the result usable;
3. the result status is `success`;
4. columns and rows are present, the row count is consistent, and the result
   is complete and not truncated;
5. a typed rule is compatible with the active hypothesis;
6. metric, target date, dimensions, source tables, joins, and projections meet
   that rule's scope contract; and
7. returned structured values prove the rule's abnormal relation.

Failure at any gate returns `NEUTRAL`. There is no generic SQL-success fallback.
`purpose`, `expected_evidence`, and `stop_condition` describe Planner intent;
they never establish polarity.

## Implemented SQL rules

| Rule ID | Root cause | Required result | Source and scope | Support or contradiction | Neutral conditions | Source type |
| --- | --- | --- | --- | --- | --- | --- |
| `f01_android_event_count` | `missing_partition` | An Android event-count column | `events`; `daily_active_users`; target date; Android segment | `0` supports; a positive count contradicts | Non-numeric value or wrong date/segment/metric | `business_data` |
| `f01_partition_state` | `missing_partition` | `partition_value`, `row_count`, `status` | `partition_metadata`; `daily_active_users`; target-date Android partition | `row_count=0` and `status=missing` supports; positive ready/success partition contradicts | Missing target partition, unusable values, or any mixed state | `operational_metadata` |
| `f02_duplicate_identity_counts` | `duplicate_batch` | A projected event total and distinct event-ID count; optional projected duplicate excess | One unjoined `events` source; `ai_task_count`; `event_name='run_ai_task'`; target date or a target-dated row; matching incident dimensions | `total > distinct`; if present, duplicate excess must equal `total - distinct` and be positive | Equal counts, invalid/inconsistent values, alias literals, joins, wrong population/date/metric/dimension, or non-count projections | `business_data` |
| `f11_metric_divergence` | `metric_definition_change` | `raw_event_count`, `raw_user_count`, `daily_active_users` | `daily_active_users`; target-date business query/result | Positive raw activity with `daily_active_users < raw_user_count` supports | Non-numeric values, no raw activity, no divergence, or wrong metric/date | `business_data` |
| `f11_metric_version_change` | `metric_definition_change` | `metric_id` plus at least one of `version`, `definition_hash`, or `query`; at least two rows | `metric_versions`; exact incident metric; change date near/covers target when a time column exists | A returned version/hash/query change supports | Wrong metric, no comparable rows, no change, unusable/out-of-window dates, or incompatible hypothesis | `metric_version` |

The F02 rule parses the SQL with `sqlglot` and validates the source,
projections, and filter structure. Returned aliases cannot spoof an accepted
query shape. F07 subscription-survivor probes remain neutral because potential
population loss does not prove that the active metric definition contains an
erroneous join.

## Evidence identity and provenance

Admitted SQL evidence uses one `EvidenceReference` per observation. Its ID is
the SHA-256 digest of the incident, metric, target date, step, query, and rule,
so replaying the same observation is deterministic. The serialized observation
records the rule, incident scope, selected row, SQL validation metadata, and a
human-readable scope check.

`source_type` describes the real origin of the observation. A query over
business tables remains one `business_data` observation even if it joins more
than one table. The interpreter does not duplicate one query into multiple
source types to satisfy Validator independence.

## Binding and authorization

Only `SUPPORTS` and `CONTRADICTS` decisions are registered and attached by the
runtime runner. `NEUTRAL` decisions do not alter hypothesis confidence.
`HypothesisManager` retains its existing count and confidence thresholds, and
`RootCauseValidator` still requires at least two supports from at least two
independent source types. A single F02 business observation therefore improves
evidence coverage but cannot authorize a root cause on its own.
