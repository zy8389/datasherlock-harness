# Planner Evidence-Source Coverage

## Purpose

A syntactically valid investigation plan is not necessarily capable of
satisfying root-cause authorization. `RootCauseValidator` requires at least two
supporting observations from two independent canonical source types. Planner
validation therefore checks evidence-path capability in addition to tool and
SQL legality.

```text
Candidate hypothesis
  -> catalog evidence objectives
  -> planned independent source paths
  -> tool execution
  -> RuntimeEvidenceInterpreter admission
  -> HypothesisManager confidence and status
  -> RootCauseValidator authorization
```

Planning a tool is not the same as planning enough evidence. Coverage is a
pre-execution capability check; it does not predict that a query will return an
anomaly or that the runtime interpreter will admit it as support.

## Source Of Truth

`config/fault_catalog.yaml`, loaded as `FaultDefinition`, is the only source of
required evidence contracts. Planner does not maintain a root-cause allowlist
for coverage.

- Every hypothesis must have at least one investigation step.
- A fault with fewer than two declared `evidence_source_types` receives no
  inferred multi-source contract.
- A fault with two or more declared source types must have distinct planned
  steps covering every declared type.
- Multiple steps over the same source type still count as one source.
- One SQL statement spanning multiple source classes is ambiguous and counts
  as no source for coverage.
- Unknown assets count as no source.

The catalog's `verification_fields`, `expected_evidence`, and
`evidence_source_types` are generic candidate-family objectives. Planner prompt
text explicitly states that they are not observed facts and do not confirm a
candidate. Case IDs, expected root causes, injection parameters, source seed
IDs, and Ground Truth objects are not Planner inputs.

## Canonical Provenance Policy

`config.faults.EVIDENCE_SOURCE_BY_ASSET` is shared by planned-step inference and
ToolExecutor data-quality evidence conversion.

| Source type | Canonical assets |
| --- | --- |
| `business_data` | `events`, `users`, `subscriptions`, `experiment_assignments`, `daily_metrics` |
| `operational_metadata` | `partition_metadata`, `pipeline_runs` |
| `schema_metadata` | `schema_snapshots` |
| `metric_version` | `metric_versions` |
| `experiment_config` | `experiment_configs` |

`detect_schema_drift` has canonical `schema_metadata` provenance because its
observation comes from schema history even though its `table` argument names
the business table under inspection. Other data-quality tools derive provenance
from their actual table asset. An unknown DQ result asset is rejected instead
of defaulting to `business_data`.

## SQL Inference

`infer_step_evidence_source()` parses DuckDB SQL with `sqlglot` and inspects
physical `Table` nodes. CTE aliases are excluded, while base tables inside CTEs,
subqueries, aliases, and joins remain visible.

The inference result is one `EvidenceSourceType` or `None`:

- all known physical tables map to the same source: return that source;
- no physical table, an unknown table, parse failure, or multiple source
  classes: return `None`.

SQL safety remains the responsibility of the existing read-only SQL Runner
validation. Source inference does not create a second SQL authorization path.

## Semantic Validation

`validate_plan_semantics()` applies the following gates in order:

1. incident identity;
2. canonical root-cause labels;
3. registered, read-only tools and valid arguments;
4. catalog diagnostic-tool binding;
5. per-hypothesis step and evidence-source coverage.

A missing-source error names the hypothesis, root-cause type, missing sources,
and full required source set. Planner repair retries receive the existing
bounded and sanitized validation feedback.

## Deterministic Fallback

Fallback plans are subject to the same semantic validator as model plans. They
iterate each selected fault's declared source contract and create one globally
unique read-only step per required source. Undeclared families retain one
deterministic step. With at most five hypotheses and at most two currently
declared sources per fault, fallback remains within `MAX_STEPS = 10`.

The F11 fallback pair demonstrates the intended shape:

- `business_data`: compare raw target-day event/user activity with the
  materialized DAU;
- `metric_version`: inspect version, definition hash, query, effective time,
  timezone, and date grain for the metric.

The runtime still decides whether either result is usable and supporting.

## Frozen Measurement

`benchmark.plan_evidence_coverage` is offline-only. It reads immutable Full
Harness JSONL traces, selects the frozen golden candidate for evaluation, and
applies current deterministic source inference to the historical plan. It does
not execute a model, tool, Harness graph, or benchmark case and does not modify
historical scores or raw artifacts.

See `docs/evaluation/frozen_plan_evidence_coverage.md` for the post-PR14 static
measurement.
