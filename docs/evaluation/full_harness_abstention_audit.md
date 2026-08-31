# Full Harness Abstention Audit

## 1. Scope

This report explains the earliest causal bottleneck for the 24 Full Harness
cases that completed without a runtime error but still returned
`root_cause=null` and abstained. It is a measurement and forensic diagnosis,
not a runtime behavior change.

The audit excludes the 36 original runtime-error cases. It does not reinterpret
or rewrite their historical `TOOL_FAILED` outcomes.

Ground Truth is audit-only. It is used after execution to locate the golden
runtime hypothesis and compare the frozen trace with the canonical case
contract. It is never injected into the Planner, Harness runtime, evidence
interpreter, model context, or any tool call.

This audit analyzes the immutable post-PR14 frozen run. PR #17 changes future
execution behavior but does not rewrite historical outcomes.

## 2. Frozen Run Identity

| Field | Value |
| --- | --- |
| Run ID | `full-60-4arch-post-pr14-20260831` |
| Variant | `full_harness` |
| Raw artifact | `experiments/ablation/results/full-60-4arch-post-pr14-20260831/full_harness/results.jsonl` |
| Raw SHA256 | `65bc8741a7baf92fbe6137b563e3ae1954bdce248ebebc00a74a5d23400d12ad` |
| Raw records | 60 |
| Runtime-error records excluded | 36 |
| No-runtime-error abstentions audited | 24 |
| Audit base commit | `1132e809adb54fcf27096ec103276a58c2c12d63` |

The run ID comes from the adjacent frozen `config.json`; individual JSONL
records do not duplicate it. The raw artifact was read only and was not added
to this change.

## 3. Audit Method

The audit follows the causal path instead of mapping final `UNRESOLVED` states
directly to `PLAN_EXHAUSTED`:

1. Load and validate all JSONL records with `AblationCaseResult`.
2. Validate the 60 case identities against the existing generated case loader,
   canonical Ground Truth seed loader, and fault catalog.
3. Select only `status=completed`, `abstention=true`, and
   `primary_prediction=null` records.
4. Recover the frozen Planner hypotheses, investigation steps, final
   hypothesis states, tool trace, and registered evidence.
5. Replay every frozen `(step, tool_result)` pair through the existing pure
   `RuntimeEvidenceInterpreter` with the frozen alert scope.
6. Require replayed admitted evidence IDs to exactly match the evidence IDs
   registered in the frozen runtime state.
7. Reconstruct the final golden-hypothesis validation result with the existing
   `RootCauseValidator` and registered runtime evidence.
8. Assign the earliest supported bottleneck in the canonical taxonomy.

The raw trace does not persist neutral decisions or validator call events as
separate records. They are reconstructable here because the trace persists all
interpreter inputs, and the production runner invokes the validator after each
successful step. The audit emits no inferred value when the trace and runtime
contract cannot prove it.

## 4. 24-case Funnel

| Stage | Count |
| --- | ---: |
| Successful tool observations produced | 140 |
| Evidence references registered | 9 |
| Runtime `SUPPORTS` decisions | 7 |
| Runtime `CONTRADICTS` decisions | 2 |
| Runtime `NEUTRAL` decisions | 131 |
| Supports bound to golden hypotheses | 6 |
| Contradictions bound to golden hypotheses | 0 |
| Golden hypotheses with at least 2 supports | 0 |
| Golden hypotheses with at least 2 source types | 0 |
| Golden hypotheses eligible for the validator gate | 0 |

Only 6 of 140 observations became support for a golden hypothesis. No golden
hypothesis crossed the HypothesisManager support count threshold, so none could
be authorized as a root cause.

## 5. Abstention Taxonomy

| Cause | Count | Percent of 24 |
| --- | ---: | ---: |
| `HYPOTHESIS_MISSING` | 4 | 16.7% |
| `EVIDENCE_MISSING` | 5 | 20.8% |
| `EVIDENCE_NEUTRALIZED` | 15 | 62.5% |
| `CONFIDENCE_SHORTFALL` | 0 | 0.0% |
| `VALIDATOR_REJECTED` | 0 | 0.0% |
| `CONTRADICTION_BLOCKED` | 0 | 0.0% |
| `PLAN_EXHAUSTED` | 0 | 0.0% |
| `OTHER` | 0 | 0.0% |

All 24 plans were exhausted, but `PLAN_EXHAUSTED` is only a secondary terminal
fact. Every case has an earlier, more specific bottleneck.

## 6. Hypothesis Coverage

Golden hypothesis proposed: **20 / 24**.

Golden hypothesis missing: **4 / 24** (`F08-001`, `F11-001`, `F12-001`,
`F12-003`).

| Fault | Eligible cases | Golden proposed | Golden missing |
| --- | ---: | ---: | ---: |
| F01 | 0 | 0 | 0 |
| F02 | 3 | 3 | 0 |
| F03 | 0 | 0 | 0 |
| F04 | 0 | 0 | 0 |
| F05 | 1 | 1 | 0 |
| F06 | 4 | 4 | 0 |
| F07 | 1 | 1 | 0 |
| F08 | 3 | 2 | 1 |
| F09 | 2 | 2 | 0 |
| F10 | 4 | 4 | 0 |
| F11 | 1 | 0 | 1 |
| F12 | 5 | 3 | 2 |

Planner coverage is a real bottleneck for four cases, but it is not the
dominant bottleneck across the audited set.

## 7. Evidence Coverage

Coverage among the 20 cases where the golden hypothesis was proposed:

| Golden support count | Cases |
| --- | ---: |
| 0 | 14 |
| 1 | 6 |
| At least 2 | 0 |

| Independent golden source types | Cases |
| --- | ---: |
| 0 | 14 |
| 1 | 6 |
| At least 2 | 0 |

The six one-support cases are `F09-001`, `F09-003`, `F10-001`, `F10-003`,
`F10-004`, and `F10-005`. F09 support is `business_data`; F10 support is
`schema_metadata`. None collected and admitted the second independent evidence
source required to reach a supported candidate.

### Neutral reasons

The top replayed neutral reasons across all hypotheses in the 24 traces are:

| Rank | Reason | Count |
| ---: | --- | ---: |
| 1 | `the returned SQL values matched no evidence rule` | 105 |
| 2 | `passed data-quality checks are neutral by default` | 11 |
| 3 | `F01 partition observation is outside the metric scope` | 4 |
| 4 | `check_freshness is not compatible with missing_partition` | 4 |
| 5 | `null-rate observation has no rows; null rate is undefined` | 4 |

Three additional observations were neutral because SQL result validation
failed. The counts above describe all planned hypotheses, not only golden
hypotheses. A neutral SQL result is not automatically a defect: fail-closed
behavior is correct when a result has no scoped diagnostic rule. The primary
classification only attributes a case to `EVIDENCE_NEUTRALIZED` when a golden
hypothesis had an executed observation that was neutralized before evidence
binding.

## 8. Confidence Analysis

| Confidence bucket | Initial | Final |
| --- | ---: | ---: |
| `<0.50` | 20 | 20 |
| `0.50-0.59` | 0 | 0 |
| `0.60-0.74` | 0 | 0 |
| `>=0.75` | 0 | 0 |

Six golden hypotheses gained exactly one support and increased by `0.15`; the
other 14 did not increase. There are no cases with at least two supports and
final confidence below `0.75`, so the frozen run provides no evidence that the
confidence lifecycle is the first bottleneck.

## 9. Validator Analysis

| Validator signal | Cases |
| --- | ---: |
| Golden validator never called | 4 |
| Golden validator function called | 20 |
| Golden candidate gate-eligible | 0 |
| Supported candidate rejected | 0 |
| Candidate validated | 0 |

The runner calls `RootCauseValidator.validate()` after each successful step,
including for `PROPOSED` or `TESTING` hypotheses. Therefore the 20
`validated=False` function results are not 20 validator rejections. A true
`VALIDATOR_REJECTED` attribution requires a gate-eligible supported hypothesis
that the validator then rejects. No audited case reached that state.

RootCauseValidator is not the first bottleneck in this frozen run.

## 10. Per-Fault Results

| Fault | No-error abstentions | Primary cause distribution | Dominant cause |
| --- | ---: | --- | --- |
| F01 | 0 | None | None |
| F02 | 3 | Neutralized 3 | `EVIDENCE_NEUTRALIZED` |
| F03 | 0 | None | None |
| F04 | 0 | None | None |
| F05 | 1 | Neutralized 1 | `EVIDENCE_NEUTRALIZED` |
| F06 | 4 | Neutralized 4 | `EVIDENCE_NEUTRALIZED` |
| F07 | 1 | Neutralized 1 | `EVIDENCE_NEUTRALIZED` |
| F08 | 3 | Neutralized 2, hypothesis missing 1 | `EVIDENCE_NEUTRALIZED` |
| F09 | 2 | Missing evidence 1, neutralized 1 | Tie |
| F10 | 4 | Missing evidence 4 | `EVIDENCE_MISSING` |
| F11 | 1 | Hypothesis missing 1 | `HYPOTHESIS_MISSING` |
| F12 | 5 | Neutralized 3, hypothesis missing 2 | `EVIDENCE_NEUTRALIZED` |

Worst three families by audited abstention count:

1. **F12 (5):** two Planner misses and three golden hypotheses whose SQL
   observations matched no evidence rule.
2. **F06 (4):** all four golden `unit_error` hypotheses were proposed, but the
   relevant SQL observations remained neutral.
3. **F10 (4):** all four golden `schema_change` hypotheses received one schema
   support but no second independent support.

## 11. Deep Case Studies

### F02-001

- Golden: `duplicate_batch`, Planner rank 1, confidence `0.30 -> 0.30`.
- Tools: 5 successful calls, including 4 SQL calls; all 5 plan steps executed.
- Evidence: both golden SQL observations matched no evidence rule; 0 supports.
- Validator: function called, but the hypothesis remained `PROPOSED` and was
  never gate-eligible.
- Primary chain: observation produced -> neutral -> no binding ->
  `EVIDENCE_NEUTRALIZED` -> EvidenceInterpreter.

### F05-002

- Golden: `timezone_error`, Planner rank 4, confidence `0.15 -> 0.15`.
- Tools: 5 successful calls, including 3 SQL calls; all steps executed.
- Evidence: the golden SQL observation matched no evidence rule; 0 supports.
- Validator: function called on a `PROPOSED` candidate, not a supported one.
- Primary chain: scoped SQL result unrecognized -> neutral ->
  `EVIDENCE_NEUTRALIZED` -> EvidenceInterpreter.

### F06-002

- Golden: `unit_error`, Planner rank 1, confidence `0.45 -> 0.45`.
- Tools: 5 successful SQL calls; all steps executed.
- Evidence: the golden SQL observation matched no evidence rule; 0 supports.
- Validator: function called, but no supported candidate existed.
- Primary chain: successful investigation -> unrecognized SQL values ->
  `EVIDENCE_NEUTRALIZED` -> EvidenceInterpreter.

### F07-001

- Golden: `join_filter`, Planner rank 4, confidence `0.15 -> 0.15`.
- Tools: 5 successful SQL calls; all steps executed.
- Evidence: the golden observation matched no evidence rule; 0 supports.
- Validator: function called on a `PROPOSED` candidate.
- Primary chain: observation produced -> neutral -> no evidence binding ->
  `EVIDENCE_NEUTRALIZED` -> EvidenceInterpreter.

### F08-001

- Golden: `join_explosion`.
- Planner: the golden type was absent from all proposed root-cause types.
- Tools: 6 successful calls, including 5 SQL calls; all steps executed.
- Evidence: no evidence could be bound to a nonexistent golden candidate.
- Validator: no golden candidate was ever checked.
- Primary chain: Planner omission -> no golden lifecycle ->
  `HYPOTHESIS_MISSING` -> Planner.

### F08-003

- Golden: `join_explosion`, Planner rank 2, confidence `0.25 -> 0.25`.
- Tools: 7 successful calls, including 6 SQL calls; all steps executed.
- Evidence: the golden SQL observation matched no evidence rule; 0 supports.
- Validator: function called, but the candidate remained `PROPOSED`.
- Primary chain: observation produced -> neutral ->
  `EVIDENCE_NEUTRALIZED` -> EvidenceInterpreter.

### F09-001

- Golden: `field_drift`, Planner rank 5, confidence `0.22 -> 0.37`.
- Tools: 7 successful calls, including 5 SQL calls; all steps executed.
- Evidence: 1 `business_data` support, 0 contradictions, no second support or
  independent source type.
- Validator: function called, but the hypothesis stayed `TESTING` and was not
  gate-eligible.
- Primary chain: one valid support -> second evidence path absent ->
  `EVIDENCE_MISSING` -> Tool coverage / plan.

### F10-001

- Golden: `schema_change`, Planner rank 5, confidence `0.10 -> 0.25`.
- Tools: 5 successful calls, including 2 SQL calls; all steps executed.
- Evidence: 1 `schema_metadata` support, 0 contradictions, no independent
  business support.
- Validator: function called, but the hypothesis stayed `TESTING`.
- Primary chain: schema support collected -> second source not collected ->
  `EVIDENCE_MISSING` -> Tool coverage / plan.

### F11-001

- Golden: `metric_definition_change`.
- Planner: the golden type was absent from the proposed hypotheses.
- Tools: 5 successful SQL calls; all steps executed.
- Evidence: observations existed, but no golden hypothesis existed for binding.
- Validator: no golden candidate was checked.
- Primary chain: Planner omission -> no golden evidence lifecycle ->
  `HYPOTHESIS_MISSING` -> Planner.

### F12-001

- Golden: `ab_split_anomaly`.
- Planner: golden type absent from the Top-5 hypotheses.
- Tools: 9 successful calls, including 7 SQL calls; all 9 steps executed.
- Evidence: multiple observations were collected, but none could be attached to
  `ab_split_anomaly` because that hypothesis did not exist.
- Validator: no golden candidate was checked.
- Primary chain: Planner omission -> no golden binding or validation path ->
  `HYPOTHESIS_MISSING` -> Planner.

### F12-002

- Golden: `ab_split_anomaly`, Planner rank 5, confidence `0.18 -> 0.18`.
- Tools: 7 successful SQL calls; all steps executed.
- Evidence: both golden SQL observations matched no evidence rule; 0 supports.
- Validator: function called, but the candidate remained `PROPOSED`.
- Primary chain: observations produced -> neutral ->
  `EVIDENCE_NEUTRALIZED` -> EvidenceInterpreter.

These 11 cases cover nine fault families, all three observed primary causes,
Planner omissions, zero-support neutralization, and one-support tool coverage
gaps.

## 12. Component Attribution

| Component | Cases | Representative cases | Recommended next action |
| --- | ---: | --- | --- |
| Planner | 4 | `F08-001`, `F11-001`, `F12-001`, `F12-003` | Add golden-family hypothesis coverage regressions after the P0 evidence work. |
| Tool coverage / plan | 5 | `F09-001`, `F10-001`, `F10-003` | Require a second independent, executable evidence path for these plans. |
| EvidenceInterpreter | 15 | `F02-001`, `F06-002`, `F08-003`, `F12-002` | P0: recognize recurring structured, scoped diagnostic observations without weakening fail-closed rules. |
| HypothesisManager | 0 | None | No threshold or confidence change justified by this run. |
| RootCauseValidator | 0 | None | No validator contract or threshold change justified by this run. |
| Other | 0 | None | None. |

## 13. Recommended Next Fix

### P0: EvidenceInterpreter recognition and scope contracts

This is the single P0 engineering target. It is the earliest bottleneck in
15 of 24 audited cases (62.5%), spanning F02, F05, F06, F07, F08, F09, and
F12. The dominant pattern is a successful, non-empty SQL observation that
cannot match a typed evidence rule and therefore remains neutral.

The next change should add narrow, structured recognizers or dedicated
read-only diagnostic result adapters for the recurring golden observation
shapes. It must preserve fail-closed behavior: a result should support a
hypothesis only when its columns, values, query scope, metric, date, and segment
prove the specific abnormality. Broad keyword matching or treating SQL success
as evidence would violate the current safety contract.

Regression strategy:

1. Add synthetic frozen `(alert, hypothesis, step, ToolExecutionResult)` pairs
   for representative F02, F05, F06, F07, F08, F09, and F12 observations.
2. Assert the exact abnormal, in-scope result becomes `SUPPORTS`.
3. Assert normal values, wrong metrics, wrong dates, wrong segments, empty or
   truncated results, and failed SQL validation remain `NEUTRAL`.
4. Add no-model runtime tests showing that two genuinely independent admitted
   sources can move the intended hypothesis to `SUPPORTED` and then invoke the
   existing validator contract.
5. Re-run only synthetic and standard repository tests in that PR. A new real
   benchmark run requires separate authorization and must not rewrite this
   frozen audit.

## 14. Limitations

- Neutral decisions are deterministically replayed because the frozen trace
  stores their complete inputs; the historical trace did not persist the
  decisions themselves.
- Validator invocation is derived from the production runner's fixed
  successful-step loop. Gate eligibility is reported separately to prevent a
  routine `validated=False` call from being mislabeled as validator rejection.
- The audit measures the 24 originally non-runtime-error Full Harness cases
  only. It does not estimate how PR #17 changes future case eligibility.
- The audit does not claim that every neutral observation should become
  support. It identifies where typed recognition must be investigated next.
- No real model, selective case, or 60x4 execution was performed.
