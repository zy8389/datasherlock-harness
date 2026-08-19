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

`expected_evidence` is for readable expectations, reports, and future presentation. It
must not be parsed with keyword matching to decide whether evidence is independent.
`evidence_paths` is the machine-readable contract used by validation and tests.

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

Injected results retain their concrete Ground Truth case. `validate_expected_evidence()` then
checks the declared paths against the baseline and injected tables. The checks are bound to
the case's target date, target asset, target dimension, version, and configuration values.
They do not infer source categories from `expected_evidence` or `signal` text.

The contract and SQL Runner tests currently cover only the six metadata-dependent seed cases.
This does not claim that the 60-case Benchmark, Benchmark Runner, or later Harness modules are
complete.
