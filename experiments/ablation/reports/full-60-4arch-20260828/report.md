# Four-Architecture Ablation

This report contains the configured full ablation run.

Run ID: `full-60-4arch-20260828`
Model: `openai/gpt-5.6-luna`
Attempted pairs: 240/240

## Aggregate Metrics

| Variant | Top-1 | Top-3 | Invalid SQL rate | Unsafe operation rate | Duplicate operation rate | Avg tool calls | Avg SQL calls | Mean latency | P50 latency | P95 latency | Known avg cost | Errors | Timeouts | Abstentions |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| single_prompt | 0.2167 | 0.4333 | 0.0000 | 0.0000 | 0.0000 | 0.000 | 0.000 | 10306.38 | 9806.51 | 13862.31 | unknown | 0 | 0 | 0 |
| react | 0.4500 | 0.6500 | 0.0043 | 0.0042 | 0.0000 | 3.967 | 3.850 | 41349.11 | 32424.77 | 91974.45 | unknown | 3 | 0 | 4 |
| state_graph_no_validator | 0.0000 | 0.0833 | 0.0180 | 0.0000 | 0.0000 | 4.667 | 1.850 | 51712.78 | 48094.82 | 62654.10 | unknown | 53 | 0 | 0 |
| full_harness | 0.0000 | 0.0833 | 0.0168 | 0.0000 | 0.0000 | 4.833 | 1.983 | 49234.51 | 46996.50 | 64090.74 | unknown | 53 | 0 | 60 |

## Definitions

Top-1 uses only the adapter's explicit primary_prediction; Full Harness therefore abstains when the production validator returns null. Top-3 uses all attempted case/variant pairs as the denominator; errors, timeouts, unresolved cases, and abstentions are incorrect.
Top-3 uses the first three valid, deduplicated canonical labels. Unknown labels remain invalid and are never coerced.
Invalid SQL rate is invalid SQL attempts divided by SQL attempts. Blocked unsafe/invalid SQL and failed allowed SQL tool results count; budget blocks and valid empty results do not, and a false sql_validation.passed flag alone does not.
Unsafe operation rate and duplicate operation rate use actual GuardrailRuntime preflight reasons divided by total tool attempts.
Known cost is null/unknown unless both input and output token counts and both configured rates are available; unknown is never reported as zero.

## Component Comparisons

Single Prompt to ReAct measures the cost and gain of iterative tool use.
ReAct to State Graph No Validator measures state and hypothesis orchestration without an authoritative validator gate.
State Graph No Validator to Full Harness measures the validator gate while reusing the production Harness adapter.
Single Prompt to Full Harness reports the measured end-to-end tradeoff in accuracy, safety, tool use, latency, and cost.
Interpretation is limited to measured differences; no unsupported causal claim is made.

## Per-Fault Results

| Fault | Variant | Top-1 | Top-3 | Attempted | Errors | Timeouts |
|---|---|---:|---:|---:|---:|---:|
| F01 | single_prompt | 0.6000 | 1.0000 | 5 | 0 | 0 |
| F01 | react | 0.6000 | 1.0000 | 5 | 0 | 0 |
| F01 | state_graph_no_validator | 0.0000 | 0.0000 | 5 | 5 | 0 |
| F01 | full_harness | 0.0000 | 0.0000 | 5 | 5 | 0 |
| F02 | single_prompt | 1.0000 | 1.0000 | 5 | 0 | 0 |
| F02 | react | 1.0000 | 1.0000 | 5 | 0 | 0 |
| F02 | state_graph_no_validator | 0.0000 | 0.0000 | 5 | 5 | 0 |
| F02 | full_harness | 0.0000 | 0.0000 | 5 | 5 | 0 |
| F03 | single_prompt | 0.0000 | 0.0000 | 5 | 0 | 0 |
| F03 | react | 0.8000 | 0.8000 | 5 | 0 | 0 |
| F03 | state_graph_no_validator | 0.0000 | 0.0000 | 5 | 5 | 0 |
| F03 | full_harness | 0.0000 | 0.0000 | 5 | 5 | 0 |
| F04 | single_prompt | 0.6000 | 1.0000 | 5 | 0 | 0 |
| F04 | react | 0.4000 | 1.0000 | 5 | 0 | 0 |
| F04 | state_graph_no_validator | 0.0000 | 0.0000 | 5 | 5 | 0 |
| F04 | full_harness | 0.0000 | 0.0000 | 5 | 5 | 0 |
| F05 | single_prompt | 0.0000 | 1.0000 | 5 | 0 | 0 |
| F05 | react | 0.2000 | 0.6000 | 5 | 1 | 0 |
| F05 | state_graph_no_validator | 0.0000 | 0.0000 | 5 | 5 | 0 |
| F05 | full_harness | 0.0000 | 0.0000 | 5 | 4 | 0 |
| F06 | single_prompt | 0.4000 | 1.0000 | 5 | 0 | 0 |
| F06 | react | 1.0000 | 1.0000 | 5 | 0 | 0 |
| F06 | state_graph_no_validator | 0.0000 | 0.0000 | 5 | 5 | 0 |
| F06 | full_harness | 0.0000 | 0.0000 | 5 | 5 | 0 |
| F07 | single_prompt | 0.0000 | 0.0000 | 5 | 0 | 0 |
| F07 | react | 0.2000 | 0.4000 | 5 | 0 | 0 |
| F07 | state_graph_no_validator | 0.0000 | 0.0000 | 5 | 5 | 0 |
| F07 | full_harness | 0.0000 | 0.0000 | 5 | 5 | 0 |
| F08 | single_prompt | 0.0000 | 0.0000 | 5 | 0 | 0 |
| F08 | react | 0.0000 | 0.0000 | 5 | 0 | 0 |
| F08 | state_graph_no_validator | 0.0000 | 0.0000 | 5 | 5 | 0 |
| F08 | full_harness | 0.0000 | 0.0000 | 5 | 5 | 0 |
| F09 | single_prompt | 0.0000 | 0.2000 | 5 | 0 | 0 |
| F09 | react | 0.6000 | 0.8000 | 5 | 0 | 0 |
| F09 | state_graph_no_validator | 0.0000 | 0.0000 | 5 | 5 | 0 |
| F09 | full_harness | 0.0000 | 0.0000 | 5 | 5 | 0 |
| F10 | single_prompt | 0.0000 | 0.0000 | 5 | 0 | 0 |
| F10 | react | 0.0000 | 0.0000 | 5 | 0 | 0 |
| F10 | state_graph_no_validator | 0.0000 | 1.0000 | 5 | 0 | 0 |
| F10 | full_harness | 0.0000 | 1.0000 | 5 | 0 | 0 |
| F11 | single_prompt | 0.0000 | 0.0000 | 5 | 0 | 0 |
| F11 | react | 0.6000 | 0.6000 | 5 | 1 | 0 |
| F11 | state_graph_no_validator | 0.0000 | 0.0000 | 5 | 5 | 0 |
| F11 | full_harness | 0.0000 | 0.0000 | 5 | 5 | 0 |
| F12 | single_prompt | 0.0000 | 0.0000 | 5 | 0 | 0 |
| F12 | react | 0.0000 | 0.6000 | 5 | 1 | 0 |
| F12 | state_graph_no_validator | 0.0000 | 0.0000 | 5 | 3 | 0 |
| F12 | full_harness | 0.0000 | 0.0000 | 5 | 4 | 0 |
