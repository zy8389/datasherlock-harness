# Final Harness Failure Analysis

## 1. Scope and Run Identity

This report closes the failure-analysis task by consolidating the frozen
post-PR #14 benchmark, the PR #18 Full Harness abstention audit, and the fixes
merged in PRs #16-#20. It is a documentation closeout, not a new benchmark or
runtime change.

The only benchmark measurement discussed here is the immutable run below.
Ground Truth is used only by the offline audits; it was never injected into the
Planner, model context, Harness runtime, evidence interpretation, or tools.

| Field | Value |
| --- | --- |
| Run ID | `full-60-4arch-post-pr14-20260831` |
| Source commit | `35562c1b5963a7c2f5a67d209f2b0bdeca057a13` |
| Provider / model | `openai / gpt-5.6-luna` |
| Cases / architectures | 60 / 4 |
| Attempted pairs | 240 / 240 |
| Full Harness raw SHA256 | `65bc8741a7baf92fbe6137b563e3ae1954bdce248ebebc00a74a5d23400d12ad` |
| Full Harness raw records | 60 |
| Real model calls in this closeout | 0 |

The evidence base is limited to the committed reports
[`post_pr14_benchmark_comparison.md`](./post_pr14_benchmark_comparison.md),
[`failure_case_candidates.md`](./failure_case_candidates.md),
[`full_harness_abstention_audit.md`](./full_harness_abstention_audit.md),
[`post_pr19_evidence_replay.md`](./post_pr19_evidence_replay.md),
[`frozen_plan_evidence_coverage.md`](./frozen_plan_evidence_coverage.md), and
[`benchmark_reproducibility.md`](./benchmark_reproducibility.md).

Frozen post-PR #14 results:

| Variant | Top-1 | Top-3 | Errors | Abstentions |
| --- | ---: | ---: | ---: | ---: |
| Single Prompt | 0.2333 | 0.3833 | 4 | 4 |
| ReAct | 0.3500 | 0.4667 | 10 | 10 |
| State Graph No Validator | 0.0833 | 0.2000 | 42 | 0 |
| Full Harness | 0.0000 | 0.2500 | 36 | 60 |

These remain historical measurements. PRs #17-#20 did not trigger a new 60x4
run. Offline replay and strengthened contracts therefore must not be reported
as new accuracy, error, or abstention numbers.

## 2. Executive Findings

The observed failures do not reduce to one component:

- Planning omitted the golden hypothesis in 4 of the 24 completed Full Harness
  abstentions. Separately, all 20 frozen golden candidates with declared
  multi-source requirements had incomplete planned source coverage.
- Tool execution produced 74 `TOOL_RUNTIME_EXPECTED` records, all caused by
  `detect_schema_drift` receiving fewer than two snapshots. PR #17 made this
  valid history gap inconclusive rather than terminal while preserving genuine
  SQL, DuckDB, row-shape, and malformed-metadata errors.
- Data reproducibility was blocked by unstable physical DuckDB bytes, not by
  demonstrated logical fixture drift. Physical hashes matched in only 32/60
  cases; PR #16 introduced a logical identity contract under which all 60/60
  fixtures matched.
- Evidence admission was the dominant earliest bottleneck among the 24
  completed abstentions: 15 were `EVIDENCE_NEUTRALIZED`, 5 were
  `EVIDENCE_MISSING`, and 4 were `HYPOTHESIS_MISSING`.
- RootCauseValidator was not the first bottleneck. No golden hypothesis reached
  the `SUPPORTED` gate, so there were zero post-gate validator rejections.
- Provider accounting was incomplete, so token and currency cost totals are
  unknown. Unknown (`null`) is not zero.
- Eight provider transport records and eight model-plan records were external
  availability or generated-plan quality failures, not implementation bugs.

The analysis task is complete, but the Harness accuracy problem is not proven
solved. There is no post-PR #20 real 60x4 measurement.

## 3. Six-Category Failure Taxonomy

| Required category | Measured signal | Earliest failure boundary | Closeout |
| --- | --- | --- | --- |
| Planning | 4/24 golden hypotheses missing; 20/20 declared multi-source candidates incomplete | Hypothesis generation or evidence-source plan coverage | Partly fixed by PR #20; four historical hypothesis misses remain |
| Tool | 74 `TOOL_RUNTIME_EXPECTED` records | `detect_schema_drift` was called with fewer than two snapshots | Fixed for future execution by PR #17 |
| Data | Physical SHA equality 32/60; logical equality 60/60 | Physical bytes were an invalid cross-run scientific identity | Fixed by PR #16 |
| Validation | 15 neutralized, 5 missing evidence, 0 gate-eligible golden candidates | Evidence recognition, binding, or independent source collection before validation | Diagnosed by PR #18; narrowly improved by PR #19 |
| Cost | Full Harness P50/P95 62,457.44/96,517.51 ms | Deeper graph and tool execution increases latency; provider cost data is incomplete | Measured; no currency claim |
| Model | 8 provider-infrastructure and 8 model-plan-invalid records | Provider transport, SQL/schema grounding, or strict output generation | Not an implementation bug |

`IMPLEMENTATION_BUG` was 0 in the frozen error taxonomy. This does not mean the
Harness had no design deficiencies; it means the classified runtime errors did
not establish an implementation defect under the measurement contract.

## 4. Planning Failures

The PR #18 audit isolated four no-runtime-error abstentions where the golden
root-cause type was absent: `F08-001`, `F11-001`, `F12-001`, and `F12-003`.
No amount of later evidence interpretation or validation can authorize a
candidate that the Planner never created.

The PR #20 static audit exposed a second planning failure. Across all 60 frozen
Full Harness traces, 42 golden hypotheses were present. Twenty of them had a
catalog-declared multi-source evidence contract, but none planned all required
sources:

| Funnel | Count |
| --- | ---: |
| Golden hypothesis present | 42/60 |
| Golden hypothesis missing | 18/60 |
| Declared multi-source golden candidates | 20 |
| Complete source coverage | 0 |
| Incomplete source coverage | 20 |

The Planner historically generated structurally legal plans that were often
not evidence-sufficient plans. PR #20 now requires at least one investigation
step for every hypothesis and requires every declared source for a multi-source
candidate. Same-source duplicate steps cannot satisfy independence, and a SQL
query that mixes source classes cannot masquerade as two independent paths.

The fix strengthens future plans; it does not retroactively change frozen
traces. It also does not repair the four historical hypothesis omissions.

## 5. Tool Failures

All 74 post-PR #14 `TOOL_RUNTIME_EXPECTED` records came from
`detect_schema_drift` with fewer than two schema snapshots: 1 ReAct, 39 State
Graph No Validator, and 34 Full Harness records.

Before PR #17, a legitimate history gap became an execution error and terminal
`TOOL_FAILED`. PR #17 established a tri-state contract:

```text
fewer than 2 snapshots
-> status=success
-> passed=None
-> assessment=insufficient_history
-> neutral evidence
-> investigation continues
```

This does not suppress genuine errors. SQL failures, DuckDB failures, malformed
row shapes, and malformed `schema_json` metadata remain execution errors. PR
#17 changes future execution semantics only; the 74 frozen outcomes remain
unchanged.

## 6. Data / Fixture Failures

Only 32/60 old/new physical DuckDB SHA256 values matched even though the case
YAML, Ground Truth, materializer, runner, and within-run copies passed their
checks. DuckDB page allocation, checkpoints, metadata, or storage layout can
change file bytes without changing logical data. Physical-file equality was
therefore unsuitable as a cross-run scientific identity.

PR #16 added a versioned, read-only logical fixture fingerprint over the
benchmark-owned schemas and complete row multisets. It preserves typed values,
duplicate multiplicity, table structure, and deterministic ordering. With that
contract, logical fixture equality was 60/60. Physical hashes remain useful for
copy/corruption diagnostics but no longer establish cross-run logical equality.

Ground Truth and fault manifests showed no drift. The data issue blocked causal
comparison; it was not evidence that Harness behavior or benchmark truth had
changed. Frozen reports were not migrated or rewritten.

## 7. Validation / Evidence Failures

PR #18 audited only the 24 Full Harness cases that completed without a runtime
error and still returned `root_cause=null`:

| Earliest abstention cause | Cases | Percent |
| --- | ---: | ---: |
| `HYPOTHESIS_MISSING` | 4 | 16.7% |
| `EVIDENCE_MISSING` | 5 | 20.8% |
| `EVIDENCE_NEUTRALIZED` | 15 | 62.5% |
| `CONFIDENCE_SHORTFALL` | 0 | 0.0% |
| `VALIDATOR_REJECTED` | 0 | 0.0% |
| `CONTRADICTION_BLOCKED` | 0 | 0.0% |
| `PLAN_EXHAUSTED` | 0 | 0.0% |
| `OTHER` | 0 | 0.0% |

All 24 plans exhausted, but exhaustion was only a terminal fact. Each case had
an earlier specific cause.

Evidence funnel:

| Stage | Count |
| --- | ---: |
| Successful observations | 140 |
| Evidence references registered | 9 |
| `SUPPORTS` | 7 |
| `CONTRADICTS` | 2 |
| `NEUTRAL` | 131 |
| Supports bound to golden hypotheses | 6 |
| Golden hypotheses with at least 2 supports | 0 |
| Golden hypotheses with at least 2 source types | 0 |
| Golden hypotheses gate eligible | 0 |
| Supported golden candidates rejected by Validator | 0 |

The runner mechanically calls `RootCauseValidator.validate()` after successful
steps, including while a hypothesis is `PROPOSED` or `TESTING`. Those pre-gate
`validated=False` results are not validator rejections. A rejection requires a
golden candidate to reach `SUPPORTED` first. No audited candidate did, so
RootCauseValidator was not the first bottleneck.

PR #19 added one narrow self-proving SQL rule for F02 duplicate identity counts.
Its offline replay admitted five SQL observations across three cases. Only F02
had result shapes that could safely prove the fault from the returned values.
The five new supports were all `business_data`, so they did not establish
independent source coverage or a new validated root cause. The F07 recognizer
was removed because survivor loss demonstrates potential impact, not that the
active metric definition actually used the faulty join. Keeping that result
neutral is a safety finding, not a missed easy win.

## 8. Cost / Latency Findings

| Variant | P50 latency (ms) | P95 latency (ms) |
| --- | ---: | ---: |
| Single Prompt | 8,948.79 | 12,423.22 |
| ReAct | 25,677.36 | 73,825.02 |
| State Graph No Validator | 73,437.94 | 97,057.30 |
| Full Harness | 62,457.44 | 96,517.51 |

Full Harness average tool calls increased from 4.833 to 5.367 and average SQL
calls from 1.983 to 3.317 between the accepted old and post-PR #14 runs. This
correlates with deeper completed investigations, but cross-run physical fixture
identity and provider availability failed the original causal-acceptance gates.
It does not prove that PR #14 alone caused the latency or call-count changes.

Provider accounting was not complete for every record. Token and cost
aggregates are therefore unknown where the source value is `null`; no dollar or
renminbi estimate is invented here.

## 9. Model / Provider Failures

The frozen error taxonomy contains eight `PROVIDER_INFRASTRUCTURE` records and
eight `MODEL_PLAN_INVALID` records.

The provider records were `OpenAI connection failed` results affecting four
Single Prompt and the same four ReAct cases in one outage window. They occurred
before usable model output and cannot be attributed to Harness architecture.

The model-plan group contains six generated SQL binder failures and two strict
ReAct structured-output failures. Examples include nonexistent columns or
aliases, invalid grouping, unsupported multi-column `COUNT(DISTINCT ...)`, and
an invalid ranked-root-cause action shape. DuckDB binding and strict schema
validation correctly rejected these plans. Future model-side schema grounding
or repair must not relax SQL safety or output contracts.

## 10. Deep Case Studies

The following 11 cases are all Full Harness no-runtime-error abstentions from
the immutable run. “Final state” refers to the golden runtime hypothesis, not
the overall completed record.

### F02-001

- **Golden root cause:** `duplicate_batch`.
- **Planner coverage:** Proposed at rank 1; confidence `0.30 -> 0.30`.
- **Tool trace summary:** Five successful calls, four SQL; all five steps ran.
- **Evidence result:** Both golden SQL observations matched no frozen rule; zero supports.
- **Final hypothesis state:** `PROPOSED`; validator was called but the candidate was never gate eligible.
- **Earliest bottleneck:** Structured duplicate observations were neutralized before binding.
- **Category:** Validation, `EVIDENCE_NEUTRALIZED`.
- **Fix status:** PR #19 partially fixed this shape; replay admits S1 and S5 as two supports.
- **Remaining issue:** Both supports are `business_data`, projected confidence is only 0.60, and F02 has no declared independent-source contract.

### F05-002

- **Golden root cause:** `timezone_error`.
- **Planner coverage:** Proposed at rank 4; confidence `0.15 -> 0.15`.
- **Tool trace summary:** Five successful calls, three SQL; all steps ran.
- **Evidence result:** The golden SQL observation matched no rule; zero supports.
- **Final hypothesis state:** `PROPOSED`; the pre-gate validator call did not reject it.
- **Earliest bottleneck:** Timestamp extrema and daily counts did not prove a fixed offset or timezone configuration mismatch.
- **Category:** Validation, `EVIDENCE_NEUTRALIZED`.
- **Fix status:** PR #20 now requires F05 plans to cover `business_data` and `metric_version`; PR #19 correctly kept the frozen SQL neutral.
- **Remaining issue:** A trusted metric-version/configuration observation is still needed; no post-fix benchmark exists.

### F06-002

- **Golden root cause:** `unit_error`.
- **Planner coverage:** Proposed at rank 1; confidence `0.45 -> 0.45`.
- **Tool trace summary:** Five successful SQL calls; all steps ran.
- **Evidence result:** The golden SQL observation matched no rule; zero supports.
- **Final hypothesis state:** `PROPOSED`; no supported candidate existed.
- **Earliest bottleneck:** One-day extrema and averages lacked a trusted baseline or robust scale comparator.
- **Category:** Validation, `EVIDENCE_NEUTRALIZED`.
- **Fix status:** Not fixed; PR #19 deliberately retained neutral behavior.
- **Remaining issue:** F06 has no declared source contract, and the trace cannot safely prove unit scale.

### F07-001

- **Golden root cause:** `join_filter`.
- **Planner coverage:** Proposed at rank 4; confidence `0.15 -> 0.15`.
- **Tool trace summary:** Five successful SQL calls; all steps ran.
- **Evidence result:** The golden observation matched no rule; zero supports.
- **Final hypothesis state:** `PROPOSED`; validator was called only pre-gate.
- **Earliest bottleneck:** Survivor-loss output showed potential impact but not active-metric root-cause proof.
- **Category:** Validation, `EVIDENCE_NEUTRALIZED`.
- **Fix status:** PR #19 removed the over-broad F07 recognizer and preserved fail-closed semantics.
- **Remaining issue:** F07 has no declared source contract and needs a diagnostic observation tying the faulty join to the active metric.

### F08-001

- **Golden root cause:** `join_explosion`.
- **Planner coverage:** Golden type absent from all proposed root-cause types.
- **Tool trace summary:** Six successful calls, five SQL; all steps ran.
- **Evidence result:** No evidence could bind to a nonexistent golden candidate.
- **Final hypothesis state:** No golden hypothesis existed.
- **Earliest bottleneck:** Hypothesis generation omitted the golden family.
- **Category:** Planning, `HYPOTHESIS_MISSING`.
- **Fix status:** Not fixed; PR #20 enforces steps and sources for proposed candidates, not hypothesis recall.
- **Remaining issue:** Add future hypothesis-coverage regression without using Ground Truth at runtime.

### F08-003

- **Golden root cause:** `join_explosion`.
- **Planner coverage:** Proposed at rank 2; confidence `0.25 -> 0.25`.
- **Tool trace summary:** Seven successful calls, six SQL; all steps ran.
- **Evidence result:** Golden SQL observation stayed neutral; zero supports.
- **Final hypothesis state:** `PROPOSED`.
- **Earliest bottleneck:** The usable target row showed no duplication; another candidate result failed SQL validation.
- **Category:** Validation, `EVIDENCE_NEUTRALIZED`.
- **Fix status:** Not fixed; PR #19 correctly admitted no support.
- **Remaining issue:** F08 has no declared source contract and needs a scoped, self-proving explosion diagnostic.

### F09-001

- **Golden root cause:** `field_drift`.
- **Planner coverage:** Proposed at rank 5; confidence `0.22 -> 0.37`.
- **Tool trace summary:** Seven successful calls, five SQL; all steps ran.
- **Evidence result:** One `business_data` support, no contradiction, and no second independent support.
- **Final hypothesis state:** `TESTING`; never gate eligible.
- **Earliest bottleneck:** The plan did not produce a second admissible evidence path.
- **Category:** Planning/tool coverage, `EVIDENCE_MISSING`.
- **Fix status:** Not fixed; PR #19 kept a mixed-date event-name count neutral because it was not target scoped.
- **Remaining issue:** F09 has no declared source contract and still needs an independent, executable path.

### F10-001

- **Golden root cause:** `schema_change`.
- **Planner coverage:** Proposed at rank 5; confidence `0.10 -> 0.25`.
- **Tool trace summary:** Five successful calls, two SQL; all steps ran.
- **Evidence result:** One `schema_metadata` support, no contradiction, and no independent business support.
- **Final hypothesis state:** `TESTING`.
- **Earliest bottleneck:** Schema evidence was collected but the business-data path was absent.
- **Category:** Planning/tool coverage, `EVIDENCE_MISSING`.
- **Fix status:** PR #20 now requires both `schema_metadata` and `business_data` in F10 plans.
- **Remaining issue:** The future plan contract is fixed, but no real rerun proves evidence admission or authorization.

### F11-001

- **Golden root cause:** `metric_definition_change`.
- **Planner coverage:** Golden type absent from proposed hypotheses.
- **Tool trace summary:** Five successful SQL calls; all steps ran.
- **Evidence result:** Observations existed, but there was no golden candidate for binding.
- **Final hypothesis state:** No golden hypothesis existed.
- **Earliest bottleneck:** Hypothesis generation omitted the golden family.
- **Category:** Planning, `HYPOTHESIS_MISSING`.
- **Fix status:** Not fixed; source-coverage enforcement cannot repair an absent candidate.
- **Remaining issue:** Golden-family hypothesis recall remains research debt.

### F12-001

- **Golden root cause:** `ab_split_anomaly`.
- **Planner coverage:** Golden type absent from the Top-5 hypotheses.
- **Tool trace summary:** Nine successful calls, seven SQL; all nine steps ran.
- **Evidence result:** Multiple observations existed, but none could bind to an absent golden candidate.
- **Final hypothesis state:** No golden hypothesis existed.
- **Earliest bottleneck:** Planner omission prevented the entire evidence and validation lifecycle.
- **Category:** Planning, `HYPOTHESIS_MISSING`.
- **Fix status:** Not fixed; PR #20 does not change hypothesis generation coverage.
- **Remaining issue:** F12 hypothesis recall needs a future regression; the frozen outcome remains `UNRESOLVED`.

### F12-002

- **Golden root cause:** `ab_split_anomaly`.
- **Planner coverage:** Proposed at rank 5; confidence `0.18 -> 0.18`.
- **Tool trace summary:** Seven successful SQL calls; all steps ran.
- **Evidence result:** Both golden SQL observations matched no rule; zero supports.
- **Final hypothesis state:** `PROPOSED`.
- **Earliest bottleneck:** Catalog/schema presence did not contain an assignment ratio or versioned configuration value.
- **Category:** Validation, `EVIDENCE_NEUTRALIZED`.
- **Fix status:** PR #20 now requires `business_data` and `experiment_config` paths for F12 plans; PR #19 retained neutral evidence safely.
- **Remaining issue:** The experiment-configuration path needs a self-proving value, and no post-fix benchmark exists.

These cases cover nine fault families, all three observed primary abstention
causes, Planner omissions, neutralized structured observations, and one-support
source-coverage gaps.

## 11. Fixes Completed

| Problem | Evidence | Fix | PR | Status |
| --- | --- | --- | --- | --- |
| Physical fixture hash unstable | 32/60 physical equality | Versioned logical fingerprint | #16 | Fixed; logical equality 60/60 |
| Schema history gap terminal | 74 failures | Tri-state inconclusive semantics | #17 | Fixed for future execution |
| Abstention cause unknown | 60 Full Harness abstentions | Causal offline audit of the 24 no-error cases | #18 | Fixed/diagnosed |
| Safe SQL evidence missing | F02 self-proving duplicate counts | Scoped typed recognizer | #19 | Partially fixed |
| Planner lacks independent source coverage | 20/20 declared candidates incomplete | Minimum-step and source-coverage contract | #20 | Fixed for future plans |
| Hypothesis missing | 4 audited no-error cases | Future Planner hypothesis coverage | - | Remaining |
| Undeclared contracts for F02/F03/F06/F07/F08/F09 | Catalog debt | Separate evidence-contract review | - | Remaining |
| Streamlit / docs | Delivery work | Later tasks | - | Remaining |

PR #18 is a diagnosis fix, not a production behavior change. PR #19 is
intentionally partial: it admitted only the observation family that met the
scope and self-proof contract.

## 12. Remaining Issues

This closeout leaves three research limitations on the historical Harness:

- Four no-error Full Harness cases omitted the golden hypothesis:
  `F08-001`, `F11-001`, `F12-001`, and `F12-003`.
- F02, F03, F06, F07, F08, and F09 still have no catalog-declared independent
  evidence-source contracts. PR #20 does not guess these contracts.
- Blocked evidence families need stronger diagnostic observations rather than
  broad recognition: trusted timezone configuration for F05, a robust scale
  comparator for F06, active-metric join proof for F07, scoped explosion proof
  for F08, target-scoped field change for F09, and assignment/configuration
  values for F12.

PR #19's counterfactual replay also shows that F02's newly admitted supports
remain one source type and below the confidence threshold. This is diagnostic,
not a new Full Harness score. None of these remaining issues justifies changing
Validator thresholds based on the frozen data.

## 13. Recommended Next Work

- **P0:** README and architecture documentation. Align public status, explain
  the Planner evidence-source contract, tri-state diagnostic semantics, scoped
  evidence admission, and the limits of the frozen benchmark.
- **P1:** Streamlit end-to-end demo. Demonstrate the stabilized investigation
  path without changing benchmark truth or presenting replay as measured
  accuracy.
- **Experimental follow-up:** After the system and documentation stabilize,
  authorize one fresh deterministic 60x4 benchmark with logical fixture
  fingerprints, a healthy provider window, complete accounting, and immutable
  outputs.

No new production feature is proposed in this PR.

## 14. Closeout Assessment

The required six categories are backed by frozen aggregate or trace-level
evidence, 11 cases have explicit causal chains, and the completed fixes are
separated from remaining research debt. The chain from Planner through tool
trace, evidence admission, hypothesis state, and validator eligibility shows
why `root_cause=null` did not imply an over-strict Validator.

The correct closeout statement is **failure-analysis task complete**. It is not
"Harness accuracy problem completely solved." No post-PR #20 real 60x4 run
exists, so this report makes no claim that Full Harness Top-1 improved or that
abstentions fell to a new value.

## 15. Notion Acceptance Mapping

| Acceptance item | Result | Evidence |
| --- | --- | --- |
| Six failure categories | PASS | Planning, tool, data, validation, cost, and model sections |
| At least 10 deep cases | PASS (11) | F02-001, F05-002, F06-002, F07-001, F08-001, F08-003, F09-001, F10-001, F11-001, F12-001, F12-002 |
| Trace-level evidence | PASS | Planner rank, tool/SQL counts, evidence decisions, final hypothesis state, and earliest bottleneck |
| Actual Harness fixes | PASS | PR #17, PR #19, and PR #20 |
| Data/reproducibility fix | PASS | PR #16 |
| Optimization recommendations | PASS | Documentation, demo, then controlled experiment |

Task: `分析失败案例并迭代Harness`

Recommended status: `完成`
