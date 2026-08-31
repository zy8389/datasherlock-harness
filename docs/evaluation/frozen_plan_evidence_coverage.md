# Frozen Planner Evidence-Source Coverage Audit

## Scope

This is a read-only static analysis of the immutable Full Harness Planner traces. It does not execute a model, tool, Harness step, or benchmark case, and it does not rewrite historical outcomes. Ground Truth is not injected into any runtime component; the frozen result's expected label is used only to select the golden candidate for offline measurement.

| Identity | Value |
| --- | --- |
| Run ID | `full-60-4arch-post-pr14-20260831` |
| Raw artifact | `experiments/ablation/results/full-60-4arch-post-pr14-20260831/full_harness/results.jsonl` |
| Raw SHA256 | `65bc8741a7baf92fbe6137b563e3ae1954bdce248ebebc00a74a5d23400d12ad` |
| Full Harness records | 60 |

## Aggregate Coverage

| Funnel stage | Count |
| --- | ---: |
| Golden hypothesis present | 42 / 60 |
| Golden hypothesis missing | 18 / 60 |
| Declared multi-source golden candidates | 20 |
| Coverage complete | 0 |
| Coverage incomplete | 20 |

A source counts only when one distinct step resolves to one canonical source. Queries over unknown assets and SQL that mixes source classes count as unclassified, never as independent coverage.

## Missing Sources

| Source | Missing cases |
| --- | ---: |
| `business_data` | 7 |
| `operational_metadata` | 10 |
| `schema_metadata` | 0 |
| `metric_version` | 2 |
| `experiment_config` | 3 |

## Per-Fault Coverage

| Fault | Root cause | Candidates | Complete | Incomplete | Missing sources |
| --- | --- | ---: | ---: | ---: | --- |
| `F01` | `missing_partition` | 5 | 0 | 5 | operational_metadata=5 |
| `F04` | `data_delay` | 5 | 0 | 5 | operational_metadata=5 |
| `F05` | `timezone_error` | 2 | 0 | 2 | metric_version=2 |
| `F10` | `schema_change` | 5 | 0 | 5 | business_data=5 |
| `F11` | `metric_definition_change` | 0 | 0 | 0 | None |
| `F12` | `ab_split_anomaly` | 3 | 0 | 3 | business_data=2, experiment_config=3 |

## Case Detail

| Case | Fault | Required | Planned | Missing | Complete |
| --- | --- | --- | --- | --- | --- |
| `F01-001` | `F01` | business_data, operational_metadata | business_data | operational_metadata | NO |
| `F01-002` | `F01` | business_data, operational_metadata | business_data | operational_metadata | NO |
| `F01-003` | `F01` | business_data, operational_metadata | business_data | operational_metadata | NO |
| `F01-004` | `F01` | business_data, operational_metadata | business_data | operational_metadata | NO |
| `F01-005` | `F01` | business_data, operational_metadata | business_data | operational_metadata | NO |
| `F04-001` | `F04` | business_data, operational_metadata | business_data | operational_metadata | NO |
| `F04-002` | `F04` | business_data, operational_metadata | business_data | operational_metadata | NO |
| `F04-003` | `F04` | business_data, operational_metadata | business_data | operational_metadata | NO |
| `F04-004` | `F04` | business_data, operational_metadata | business_data | operational_metadata | NO |
| `F04-005` | `F04` | business_data, operational_metadata | business_data | operational_metadata | NO |
| `F05-001` | `F05` | business_data, metric_version | business_data | metric_version | NO |
| `F05-002` | `F05` | business_data, metric_version | business_data | metric_version | NO |
| `F10-001` | `F10` | business_data, schema_metadata | schema_metadata | business_data | NO |
| `F10-002` | `F10` | business_data, schema_metadata | schema_metadata | business_data | NO |
| `F10-003` | `F10` | business_data, schema_metadata | schema_metadata | business_data | NO |
| `F10-004` | `F10` | business_data, schema_metadata | schema_metadata | business_data | NO |
| `F10-005` | `F10` | business_data, schema_metadata | schema_metadata | business_data | NO |
| `F12-002` | `F12` | business_data, experiment_config | business_data | experiment_config | NO |
| `F12-004` | `F12` | business_data, experiment_config | None | business_data, experiment_config | NO |
| `F12-005` | `F12` | business_data, experiment_config | None | business_data, experiment_config | NO |

## Undeclared Source-Contract Debt

These catalog families declare no `evidence_source_types`. This PR does not guess or add contracts for them; they receive only the universal requirement that every proposed hypothesis has at least one investigation step.

- `F02 duplicate_batch`
- `F03 null_value_anomaly`
- `F06 unit_error`
- `F07 join_filter`
- `F08 join_explosion`
- `F09 field_drift`

## Interpretation

The report measures whether a frozen plan was capable of reaching every catalog-declared independent source. It does not claim that a planned query would produce supporting evidence, raise confidence, or pass the Validator. Those remain runtime admission and authorization decisions.

This audit analyzes the immutable post-PR14 frozen run. Later PRs do not rewrite its traces, scores, errors, or abstentions.
