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

## Known Injector Blocker

F05 retains a `business_data + metric_version` contract. After the boundary-respecting F05
injector rollback, it shifts CN boundary event timestamps and records a pipeline warning but does
not create fault-specific `metric_versions` metadata. The runtime evidence validator therefore
correctly rejects F05 for a missing newer target-date metric version. This is a blocker owned by
the Fault Injector task / YE; the contract must not be weakened to business-only evidence.

The contract and SQL Runner tests currently cover only the six metadata-dependent seed cases.
This does not claim that the 60-case Benchmark, Benchmark Runner, or later Harness modules are
complete.
