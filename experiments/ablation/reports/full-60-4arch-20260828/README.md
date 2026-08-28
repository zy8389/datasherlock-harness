# Full 60 x 4 Architecture Ablation

Status: `SCIENTIFIC_RUN_VALID`

This is the sanitized acceptance snapshot for run `full-60-4arch-20260828`.
The run used the frozen source commit `d881541729dd89aba8d973bb6ae4663a5718f06c`
and evaluated all 60 canonical cases with these variants:

- `single_prompt`
- `react`
- `state_graph_no_validator`
- `full_harness`

All 240 case/variant pairs were attempted exactly once. The fairness audit
reported a complete pair matrix, identical per-case database hashes across
variants, identical model fingerprints, zero duplicate pairs, zero missing
pairs, and no Ground Truth runtime leakage.

The aggregate outputs in this directory were copied from the completed run
after raw JSONL recomputation. The full runtime directory, databases, traces,
provider responses, credentials, and local `.env` are intentionally excluded.

Failure counts are retained as measured observations rather than removed from
the result. The known `detect_schema_drift` execution message for the
single-snapshot `events` input is classified as `TOOL_RUNTIME_EXPECTED`.
Model-generated DuckDB binder failures are classified as
`MODEL_PLAN_INVALID`, and the blocked unsafe SQL action is classified as
`GUARDRAIL_EXPECTED`. No provider infrastructure failure or case-deadline
provider-latency failure was observed in the accepted run.

See `report.md` for the aggregate and per-fault tables, `comparison.json` for
machine-readable metrics, and `fairness.json` for the complete fairness audit.
