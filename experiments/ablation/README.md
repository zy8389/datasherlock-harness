# Four-Architecture Ablation

This experiment compares exactly these four variants over the canonical
benchmark cases:

1. `single_prompt`: one structured model call, no tools or hidden evidence.
2. `react`: a bounded public action/observation loop using the current tool
   registry, `ToolExecutor`, and `GuardrailRuntime`.
3. `state_graph_no_validator`: the current `Planner`, `HarnessGraph`, evidence
   interpreter, and `HypothesisManager`, stopping before authoritative root-cause
   validation.
4. `full_harness`: the production `build_harness_executor` adapter, including
   the current validator gate.

The only task covered by this directory is the four-architecture ablation.
Streamlit and unrelated tasks are out of scope.

## Phase 0 Audit

The experiment reuses the canonical APIs already present on `main`:

- `load_case_manifest` and `load_case_manifests` select the committed cases;
- `validate_case_manifest` preserves the case contract;
- `materialize_case` injects one case once;
- `build_runtime_input` removes benchmark-answer fields from the alert;
- `build_default_tool_registry`, `ToolExecutor`, and `GuardrailRuntime` are the
  only tool and SQL path used by tool-using variants;
- `Planner`, `HarnessGraph`, `HypothesisManager`, and the current evidence
  interpreter are reused for the state-graph variant;
- `build_harness_executor` and `CurrentHarnessExecutor` are reused for Full
  Harness;
- `ModelSettings` and the existing model factory create the configured client.

The existing benchmark runner, manifests, Ground Truth, RootCauseValidator
thresholds, Streamlit application, approval/sandbox code, and unrelated modules
remain unchanged. The experiment module is an orchestration/scoring layer and
does not implement a second benchmark runner or SQL executor.

For each selected case, the runner calls `materialize_case` once and writes a
single base DuckDB. It then makes four independent filesystem copies. SHA-256
hashes of the physical files and versioned logical fixture fingerprints are
recorded in `case_inputs.jsonl` and `fairness.json`. Physical hashes diagnose
artifact-byte identity. The scientific fairness gate uses the logical fixture
fingerprints, which include benchmark-owned schemas and row multisets while
normalizing storage and insertion order. Runs created before logical
fingerprints were introduced remain readable and fall back to physical SHA.

Ground Truth stays outside adapters. Adapter inputs contain only an opaque run
identifier, sanitized detector alert, metric semantics, allowed taxonomy, and
database path. They never receive `case_id`, `fault_id`, `expected_root_cause`,
`expected_evidence`, `source_seed_case_id`, `manifest`, or a Ground Truth object.
The outer scorer joins the adapter output to the manifest after execution.

Top-1 uses the adapter's explicit `primary_prediction` authority. Single
Prompt, ReAct, and State Graph No Validator derive it from their first returned
ranked label; Full Harness derives it only from the production validator's
`predicted_root_cause`, which may be null even when hypothesis rankings exist.
Top-3 is stored as an ordered list of at most three model labels. For scoring,
unknown labels are invalid and are never mapped to a canonical label. Top-3
searches the first three valid, deduplicated canonical labels, preserving order.

Tool and SQL attempt denominators come from guardrail preflight events when
available; without them, SQL tool-result records are counted conservatively.
Invalid SQL means a blocked `unsafe_sql` or SQL `invalid_tool_contract`, or an
allowed SQL whose `ToolExecutionResult` failed due validation, execution,
timeout, or tool-contract error. A successful empty SQL result and a budget
block are not invalid, and `sql_validation.passed == false` alone is not
enough to classify an attempt as invalid. Unsafe and duplicate rates use the
actual GuardrailRuntime reasons `unsafe_sql`, `non_read_only_tool`,
`unsafe_tool`, and `duplicate_tool_call`.
Unknown token rates or unknown token counts produce `null` cost, never zero.

## Running

The example config is intentionally configured for a real provider but contains
no credentials. A deterministic wiring smoke can be run without credentials:

```bash
python experiments/ablation/run.py \
  --config experiments/ablation/config.example.yaml \
  --smoke \
  --run-id wiring-smoke
```

The smoke case set is `F01-001`, `F03-001`, `F06-001`, `F11-001`, and `F12-001`.
Its report is explicitly labeled as wiring smoke and is not scientific ablation
accuracy.

For the real acceptance run, configure one provider, one model, credentials,
and connectivity in `.env`, then run:

```bash
python experiments/ablation/run.py \
  --config experiments/ablation/config.example.yaml \
  --full \
  --run-id full-60
```

The full run requires exactly 60 cases and produces 240 case/variant pairs in
case-major interleaving. `--resume` reuses completed pairs after validating the
stored config/model/variant fingerprint and persisted fixture identities. New
runs validate both physical hashes and logical fingerprint contracts; legacy
runs without logical fields validate physical hashes. A mismatch fails closed.

## Artifacts

Each run is written below `experiments/ablation/results/<run_id>/`:

```text
config.json
fairness.json
case_inputs.jsonl
single_prompt/results.jsonl
single_prompt/summary.json
react/results.jsonl
react/summary.json
state_graph_no_validator/results.jsonl
state_graph_no_validator/summary.json
full_harness/results.jsonl
full_harness/summary.json
comparison.json
comparison.csv
report.md
```

The runtime databases and large traces stay below the gitignored `.runtime`
directory. `comparison.json` and `report.md` contain aggregate and per-fault
results only when a run has actually been executed; no result is fabricated by
the implementation.

Accuracy uses all attempted pairs as denominator. Errors, timeouts, unresolved
cases, abstentions, and invalid predictions remain incorrect. Latency reports
mean, nearest-rank p50, and nearest-rank p95. Cost is reported only when both
token counts and both configured rates are known.
