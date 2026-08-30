# Post-PR #14 Benchmark Comparison

## 1. Experiment Identity

| Field | Accepted old run | Post-PR #14 run |
|---|---|---|
| Run ID | `full-60-4arch-20260828` | `full-60-4arch-post-pr14-20260831` |
| Source commit | `d881541729dd89aba8d973bb6ae4663a5718f06c` | `35562c1b5963a7c2f5a67d209f2b0bdeca057a13` |
| Provider / model | `openai / gpt-5.6-luna` | `openai / gpt-5.6-luna` |
| Model fingerprint | `5ebafd590852307e766ec9587ec32e1614794ed535b42e7f7fc2b4cc3a0a0d45` | same |
| Cases | 60 | 60 |
| Architectures | 4 | 4 |
| Attempted pairs | 240/240 | 240/240 |

The model endpoint, timeout and retry policy, budgets, case order, scoring,
variant order, case generator, and ablation runner match. PR #14 intentionally
changes metric/fault diagnostic capabilities and Planner-visible bindings; that
code change is the intervention under evaluation.

Acceptance status: `MEASUREMENT_COMPLETE_COMPARATIVE_ACCEPTANCE_BLOCKED`.
The frozen run is complete as a standalone measurement. Old/new deltas are
descriptive because provider availability and cross-run database byte identity
do not satisfy the causal-comparison gates.

## 2. Fairness Verification

| Check | Result | Evidence / interpretation |
|---|---|---|
| Same 60-case set and order | PASS | Canonical case YAML is unchanged; 60 unique inputs are present in the same order. |
| Same canonical Ground Truth | PASS | `benchmark/ground_truth` is unchanged and runtime leakage is `false`. |
| Same materialization and runner code | PASS | `src/benchmark/case_generator.py` and `experiments/ablation/run.py` are unchanged. |
| Within-run database-copy hash | PASS | Every case has one hash shared by all four variant copies. |
| Cross-run database byte hash | **FAIL** | Only 32/60 old/new DuckDB SHA-256 values match; 28 differ. A separate no-model rematerialization check matched 30/60 against the frozen new run. Physical DuckDB SHA is not a stable logical fixture fingerprint. |
| Same model configuration/fingerprint | PASS | Provider, model, base URL, retry, timeout, budgets, and model fingerprint match. |
| Same architecture definitions | PASS | Same four adapters, order, and budgets; implementation changes are limited to the target PR intervention. |
| Provider availability | **FAIL** | Eight `OpenAI connection failed` records affected four Single Prompt and the same four ReAct cases. |
| Complete pair matrix | PASS | 240 attempted, 0 missing, 0 duplicate pairs. |
| Ground Truth runtime leakage | PASS | `false`. |

The database result does not establish logical data drift: case definitions,
Ground Truth, and materialization code are identical. It does establish that
the requested physical-file equality gate is not reproducible. A future run
needs a canonical logical-content fingerprint before causal acceptance.

## 3. Aggregate Comparison

| Variant | Top-1 old -> new (delta) | Top-3 old -> new (delta) | Errors old -> new | Abstentions old -> new | Avg tools old -> new | Avg SQL old -> new |
|---|---:|---:|---:|---:|---:|---:|
| Single Prompt | 0.2167 -> 0.2333 (+0.0167) | 0.4333 -> 0.3833 (-0.0500) | 0 -> 4 | 0 -> 4 | 0.000 -> 0.000 | 0.000 -> 0.000 |
| ReAct | 0.4500 -> 0.3500 (-0.1000) | 0.6500 -> 0.4667 (-0.1833) | 3 -> 10 | 4 -> 10 | 3.967 -> 2.883 | 3.850 -> 2.850 |
| State Graph No Validator | 0.0000 -> 0.0833 (+0.0833) | 0.0833 -> 0.2000 (+0.1167) | 53 -> 42 | 0 -> 0 | 4.667 -> 5.350 | 1.850 -> 3.350 |
| Full Harness | 0.0000 -> 0.0000 (0.0000) | 0.0833 -> 0.2500 (+0.1667) | 53 -> 36 | 60 -> 60 | 4.833 -> 5.367 | 1.983 -> 3.317 |

New-run operational metrics:

| Variant | Invalid SQL | Unsafe op. | Duplicate op. | P50 latency (ms) | P95 latency (ms) |
|---|---:|---:|---:|---:|---:|
| Single Prompt | 0.0000 | 0.0000 | 0.0000 | 8,948.79 | 12,423.22 |
| ReAct | 0.0174 | 0.0057 | 0.0000 | 25,677.36 | 73,825.02 |
| State Graph No Validator | 0.0149 | 0.0000 | 0.0000 | 73,437.94 | 97,057.30 |
| Full Harness | 0.0050 | 0.0000 | 0.0031 | 62,457.44 | 96,517.51 |

Token and cost aggregates are unknown because the provider did not return
usable accounting for every record; unknown values remain `null`, not zero.

## 4. Per-Fault Comparison

Each metric is shown as `old -> new`. Errors and abstentions use counts out of
five cases per fault/variant.

| Fault | Variant | Top-1 | Top-3 | Errors | Abstentions |
|---|---|---:|---:|---:|---:|
| F01 | Single Prompt | 0.6 -> 0.8 | 1.0 -> 1.0 | 0 -> 0 | 0 -> 0 |
| F01 | ReAct | 0.6 -> 0.6 | 1.0 -> 0.6 | 0 -> 1 | 0 -> 1 |
| F01 | State Graph No Validator | 0.0 -> 0.0 | 0.0 -> 0.0 | 5 -> 5 | 0 -> 0 |
| F01 | Full Harness | 0.0 -> 0.0 | 0.0 -> 0.0 | 5 -> 5 | 5 -> 5 |
| F02 | Single Prompt | 1.0 -> 1.0 | 1.0 -> 1.0 | 0 -> 0 | 0 -> 0 |
| F02 | ReAct | 1.0 -> 1.0 | 1.0 -> 1.0 | 0 -> 0 | 0 -> 0 |
| F02 | State Graph No Validator | 0.0 -> 0.4 | 0.0 -> 0.6 | 5 -> 2 | 0 -> 0 |
| F02 | Full Harness | 0.0 -> 0.0 | 0.0 -> 0.6 | 5 -> 2 | 5 -> 5 |
| F03 | Single Prompt | 0.0 -> 0.0 | 0.0 -> 0.0 | 0 -> 0 | 0 -> 0 |
| F03 | ReAct | 0.8 -> 0.4 | 0.8 -> 0.4 | 0 -> 0 | 0 -> 0 |
| F03 | State Graph No Validator | 0.0 -> 0.0 | 0.0 -> 0.0 | 5 -> 5 | 0 -> 0 |
| F03 | Full Harness | 0.0 -> 0.0 | 0.0 -> 0.0 | 5 -> 5 | 5 -> 5 |
| F04 | Single Prompt | 0.6 -> 0.6 | 1.0 -> 1.0 | 0 -> 0 | 0 -> 0 |
| F04 | ReAct | 0.4 -> 0.6 | 1.0 -> 0.8 | 0 -> 1 | 0 -> 1 |
| F04 | State Graph No Validator | 0.0 -> 0.0 | 0.0 -> 0.0 | 5 -> 5 | 0 -> 0 |
| F04 | Full Harness | 0.0 -> 0.0 | 0.0 -> 0.0 | 5 -> 5 | 5 -> 5 |
| F05 | Single Prompt | 0.0 -> 0.0 | 1.0 -> 1.0 | 0 -> 0 | 0 -> 0 |
| F05 | ReAct | 0.2 -> 0.0 | 0.6 -> 0.6 | 1 -> 0 | 1 -> 0 |
| F05 | State Graph No Validator | 0.0 -> 0.0 | 0.0 -> 0.0 | 5 -> 5 | 0 -> 0 |
| F05 | Full Harness | 0.0 -> 0.0 | 0.0 -> 0.0 | 4 -> 4 | 5 -> 5 |
| F06 | Single Prompt | 0.4 -> 0.4 | 1.0 -> 0.4 | 0 -> 3 | 0 -> 3 |
| F06 | ReAct | 1.0 -> 0.4 | 1.0 -> 0.4 | 0 -> 3 | 0 -> 3 |
| F06 | State Graph No Validator | 0.0 -> 0.6 | 0.0 -> 0.6 | 5 -> 2 | 0 -> 0 |
| F06 | Full Harness | 0.0 -> 0.0 | 0.0 -> 0.8 | 5 -> 1 | 5 -> 5 |
| F07 | Single Prompt | 0.0 -> 0.0 | 0.0 -> 0.0 | 0 -> 1 | 0 -> 1 |
| F07 | ReAct | 0.2 -> 0.0 | 0.4 -> 0.0 | 0 -> 2 | 0 -> 2 |
| F07 | State Graph No Validator | 0.0 -> 0.0 | 0.0 -> 0.0 | 5 -> 4 | 0 -> 0 |
| F07 | Full Harness | 0.0 -> 0.0 | 0.0 -> 0.0 | 5 -> 4 | 5 -> 5 |
| F08 | Single Prompt | 0.0 -> 0.0 | 0.0 -> 0.0 | 0 -> 0 | 0 -> 0 |
| F08 | ReAct | 0.0 -> 0.0 | 0.0 -> 0.0 | 0 -> 1 | 1 -> 1 |
| F08 | State Graph No Validator | 0.0 -> 0.0 | 0.0 -> 0.2 | 5 -> 4 | 0 -> 0 |
| F08 | Full Harness | 0.0 -> 0.0 | 0.0 -> 0.4 | 5 -> 2 | 5 -> 5 |
| F09 | Single Prompt | 0.0 -> 0.0 | 0.2 -> 0.2 | 0 -> 0 | 0 -> 0 |
| F09 | ReAct | 0.6 -> 0.4 | 0.8 -> 0.8 | 0 -> 0 | 0 -> 0 |
| F09 | State Graph No Validator | 0.0 -> 0.0 | 0.0 -> 0.2 | 5 -> 4 | 0 -> 0 |
| F09 | Full Harness | 0.0 -> 0.0 | 0.0 -> 0.4 | 5 -> 3 | 5 -> 5 |
| F10 | Single Prompt | 0.0 -> 0.0 | 0.0 -> 0.0 | 0 -> 0 | 0 -> 0 |
| F10 | ReAct | 0.0 -> 0.0 | 0.0 -> 0.0 | 0 -> 1 | 0 -> 1 |
| F10 | State Graph No Validator | 0.0 -> 0.0 | 1.0 -> 0.8 | 0 -> 1 | 0 -> 0 |
| F10 | Full Harness | 0.0 -> 0.0 | 1.0 -> 0.8 | 0 -> 1 | 5 -> 5 |
| F11 | Single Prompt | 0.0 -> 0.0 | 0.0 -> 0.0 | 0 -> 0 | 0 -> 0 |
| F11 | ReAct | 0.6 -> 0.8 | 0.6 -> 1.0 | 1 -> 0 | 1 -> 0 |
| F11 | State Graph No Validator | 0.0 -> 0.0 | 0.0 -> 0.0 | 5 -> 5 | 0 -> 0 |
| F11 | Full Harness | 0.0 -> 0.0 | 0.0 -> 0.0 | 5 -> 4 | 5 -> 5 |
| F12 | Single Prompt | 0.0 -> 0.0 | 0.0 -> 0.0 | 0 -> 0 | 0 -> 0 |
| F12 | ReAct | 0.0 -> 0.0 | 0.6 -> 0.0 | 1 -> 1 | 1 -> 1 |
| F12 | State Graph No Validator | 0.0 -> 0.0 | 0.0 -> 0.0 | 3 -> 0 | 0 -> 0 |
| F12 | Full Harness | 0.0 -> 0.0 | 0.0 -> 0.0 | 4 -> 0 | 5 -> 5 |

For Full Harness Top-3, the largest gains are F06 (+0.8), F02 (+0.6),
and F08/F09 (tied at +0.4). The only negative family is F10 (-0.2), so
there are not three distinct declining families. The lowest new Top-3 is 0.0;
F01, F03, and F04 are representative, tied with F05, F07, F11, and F12.

## 5. Failure Taxonomy Comparison

The taxonomy counts classify error records exactly once. Accuracy misses and
non-error Full Harness abstentions are tracked separately.

| Failure type | Old | New | Delta |
|---|---:|---:|---:|
| PROVIDER_INFRASTRUCTURE | 0 | 8 | +8 |
| CASE_DEADLINE_PROVIDER_LATENCY | 0 | 0 | 0 |
| GUARDRAIL_EXPECTED | 1 | 2 | +1 |
| MODEL_PLAN_INVALID | 4 | 8 | +4 |
| TOOL_RUNTIME_EXPECTED | 104 | 74 | -30 |
| IMPLEMENTATION_BUG | 0 | 0 | 0 |
| UNRESOLVED | 0 | 0 | 0 |
| OTHER | 0 | 0 | 0 |

`detect_schema_drift` two-snapshot failures account for all 74 new
`TOOL_RUNTIME_EXPECTED` records: ReAct 1, State Graph 39, and Full Harness 34.
The old run had 104 such records. Restricted to the two graph variants, the
change is 102 -> 73. The issue decreased but did not disappear.

The eight new `MODEL_PLAN_INVALID` records are six model-generated SQL binder
failures and two strict ReAct structured-output validation failures. The two
guardrail records are an unsafe multi-statement ReAct call and a duplicate Full
Harness call; both are expected enforcement.

## 6. PR #14 Impact Assessment

Supported observations:

- State Graph errors decreased 53 -> 42 and Full Harness errors decreased
  53 -> 36. Combined graph errors decreased 106 -> 78.
- Full Harness Top-3 increased 0.0833 -> 0.2500, while Top-1 remained 0.0000.
- State Graph Top-1/Top-3 increased 0.0000/0.0833 -> 0.0833/0.2000.
- Graph-only two-snapshot failures decreased 102 -> 73.
- Full Harness average tool and SQL calls increased 4.833/1.983 ->
  5.367/3.317. This correlates with more completed graph executions and deeper
  evidence collection after the capability changes.

Claims not supported by this run:

- PR #14 improved Full Harness Top-1; it did not.
- PR #14 caused every accuracy delta. Cross-run physical DB hashes fail the
  required equality gate, and model sampling remains nondeterministic.
- Single Prompt or ReAct regressions are code regressions. Eight provider
  failures materially confound those architectures.
- The remaining `detect_schema_drift` failures prove a new implementation bug.
  The observed symptom is the established tool precondition being unmet.

The evidence supports correlation between PR #14 capability exposure and
improved graph completion/Top-3 behavior. It does not support causal benchmark
acceptance under the requested fairness rules.

## 7. Remaining Blockers

### P0: Cross-run logical fixture fingerprint is missing

- Problem: DuckDB file SHA-256 is unstable across materialization processes.
- Affected cases: 28/60 old/new byte hashes differ; a separate rematerialization
  check differed on 30/60 against the frozen new run.
- Failure stage: pre-comparison fairness validation.
- Reproducible symptom: identical case YAML, Ground Truth, generator, and runner
  do not produce a stable physical-file SHA.
- Suspected root cause: DuckDB physical serialization metadata or layout; the
  exact mechanism is not established here.
- Implementation bug: not established in Harness behavior; it is a benchmark
  reproducibility design blocker.
- Recommended next action: define and test a canonical logical fingerprint from
  deterministic schema plus ordered table content, while retaining within-run
  copy hashes for contamination detection.
- Priority: P0 before another causal old/new acceptance run.

### P0: `detect_schema_drift` lacks two snapshots

- Problem: plans invoke the tool when only one schema snapshot is available.
- Affected cases: 74 attempts (ReAct 1, State Graph 39, Full Harness 34), spanning
  F01-F11; exact case lists are recoverable from the immutable local raw run.
- Failure stage: diagnostic tool execution.
- Reproducible symptom: `at least two schema snapshots are required for events`.
- Suspected root cause: capability exposure does not encode or enforce the
  two-snapshot runtime precondition before planning/execution.
- Implementation bug: not classified as one in this accepted measurement; the
  tool correctly rejects insufficient input.
- Recommended next action: separately design snapshot-aware capability gating
  or provide a legitimate prior snapshot. Do not alter this frozen run.
- Priority: P0 because it causes 74/92 errors.

### P1: Full Harness validator never emits a primary prediction

- Problem: all 60 Full Harness cases abstain; 24 non-error executions end
  `UNRESOLVED`, and 36 end `TOOL_FAILED`.
- Affected cases: all F01-001 through F12-005.
- Failure stage: root-cause validation/finalization after evidence collection.
- Reproducible symptom: Top-3 contains 15 correct cases, but primary prediction
  remains null and Top-1 is 0.0000.
- Suspected root cause: evidence is not promoted to an authoritative validated
  root cause under current validator requirements.
- Implementation bug: not established; this may be intended conservative
  validator behavior with insufficient evidence.
- Recommended next action: inspect representative completed-but-unresolved
  cases and evidence-to-hypothesis validation transitions.
- Priority: P1 after the dominant tool precondition blocker.

### P1: Model-generated plans violate strict contracts

- Problem: six SQL statements fail DuckDB binding and two ReAct outputs fail
  strict structured-output validation.
- Affected cases: ReAct F01-002, F08-004, F10-005, F12-004; State Graph
  F06-001, F09-001, F10-005; Full Harness F06-001.
- Failure stage: model response validation or SQL execution.
- Reproducible symptom: invalid JSON/ranked-root-cause action shape, missing
  columns, invalid grouping, or unsupported `COUNT(DISTINCT ...)` syntax.
- Suspected root cause: model plan quality and schema grounding.
- Implementation bug: no; strict validation and DuckDB correctly reject them.
- Recommended next action: evaluate schema-grounded plan repair without
  relaxing strict output or SQL safety semantics.
- Priority: P1.

### P1: Provider availability confounds two architectures

- Problem: one outage window generated eight transport failures.
- Affected cases: F06-003, F06-004, F06-005, and F07-001 in both Single Prompt
  and ReAct.
- Failure stage: provider transport before a usable model response.
- Reproducible symptom: `ModelTransportError: OpenAI connection failed` after
  approximately 6.2-6.5 seconds.
- Suspected root cause: provider/base-URL availability outside Harness.
- Implementation bug: no.
- Recommended next action: preserve this frozen result; require a fully healthy
  provider window for a future complete 60x4 run. Never replace only failed
  pairs or architectures.
- Priority: P1 for comparative acceptance.

## Preservation

- The accepted old report tree remains unchanged.
- No Harness, case manifest, Ground Truth, scoring, or README behavior was
  changed in this measurement task.
- Raw provider payloads, runtime databases, credentials, and local paths are
  excluded from the committed report package.
- Repository README status: outdated; update deferred to a separate task.
