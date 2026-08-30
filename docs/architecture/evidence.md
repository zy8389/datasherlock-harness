# Ground Truth Evidence Contract

This repository uses two complementary evidence fields in seed Ground Truth cases.

```yaml
expected_evidence:
  - human-readable expected signal

evidence_paths:
  - source_type: business_data
    asset: events
    signal: target-date business rows decrease
```

The fields deliberately have different scopes:

```text
Catalog expected_evidence = fault-family-level readable guidance
Ground Truth expected_evidence = concrete case-level readable guidance
evidence_source_types + evidence_paths = machine-readable contract
```

Neither layer of `expected_evidence` is compared by exact string equality or parsed with
keywords to decide whether evidence is independent. `evidence_source_types` and
`evidence_paths` are the machine-readable contract used by validation and tests.

## Independent Evidence

For metadata-dependent seed faults F01, F04, F05, F10, F11, and F12, a valid case has at
least two paths:

```text
business_data + one non-business source category
```

The supported non-business categories are:

| Source type | Evidence assets |
|---|---|
| `operational_metadata` | `pipeline_runs`, `partition_metadata` |
| `schema_metadata` | `schema_snapshots` |
| `metric_version` | `metric_versions` |
| `experiment_config` | `experiment_configs` |

Two business queries over different tables are not an independent metadata path.
Every path asset must also be listed in the case's `affected_assets`.

## Validation Boundary

`GroundTruthCase` performs field-level and same-case checks, including enum, non-empty,
duplicate, and affected-asset validation. `validate_ground_truth_case()` is the catalog-aware
complete validator. `load_ground_truth_cases()` is the production loading entry point and
applies that validator to every loaded case.

The catalog-aware validator treats the Fault Catalog as the canonical fault-family source and
checks every Ground Truth case for:

- `root_cause_type`
- `affected_metric`
- exact-set `affected_assets`
- `injection_strategy`
- `expected_direction`
- `effect_size_type`
- `minimum_effect_size`
- coverage of every required `evidence_source_types` category

It rejects both an undeclared Ground Truth source category and a Catalog-required category that
the case omits. This intentionally does not compare the readable `expected_evidence` strings.

Injected results retain their concrete Ground Truth case. `validate_expected_evidence()` then
checks the declared paths against the baseline and injected tables. The checks are bound to
the case's target date, target asset, target dimension, version, and configuration values.
They do not infer source categories from `expected_evidence` or `signal` text.

## Injector Coverage

F05 retains a `business_data + metric_version` contract. Its injector shifts CN boundary event
timestamps, records a pipeline warning, and appends a target-date `metric_versions` row that changes
the metric timezone from `UTC` to `Asia/Shanghai` without changing the metric SQL definition.
Runtime validation requires both the hourly business distribution change and that independent
metric-version record.

F12 preserves the experiment cohort while moving the configured allocation from 50/50 to 20/80.
The injector does not change `experiment_assignments.user_id` or use the Catalog
`minimum_effect_size` to target a generated effect. Treatment propensity produces the
subscription outcome from fixed latent user data, and the evaluator independently checks the
conversion-rate effect. The current F12-001 case satisfies the catalog threshold and Evidence
Contract.

These contract and SQL Runner tests intentionally focus on the six metadata-dependent seed
cases. The 60-case materialization, Benchmark Runner, and runtime Harness coverage are verified
by their dedicated benchmark and runtime test suites.
