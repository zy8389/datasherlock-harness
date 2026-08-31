# Four-Architecture Ablation

This report contains the configured full ablation run.

Run ID: `full-60-4arch-post-pr14-20260831`
Source commit: `35562c1b5963a7c2f5a67d209f2b0bdeca057a13`
Model: `openai/gpt-5.6-luna`
Attempted pairs: 240/240

Result status: `MEASUREMENT_COMPLETE_COMPARATIVE_ACCEPTANCE_BLOCKED`

Within-run database copies, model fingerprint, pair accounting, and Ground
Truth isolation pass. Cross-run DuckDB byte identity fails (32/60 exact SHA-256
matches), and eight provider connection failures affect Single Prompt and
ReAct. Aggregate old/new deltas are therefore descriptive, not causal.

## Aggregate Metrics

| Variant | Top-1 | Top-3 | Invalid SQL rate | Unsafe operation rate | Duplicate operation rate | Avg tool calls | Avg SQL calls | Mean latency | P50 latency | P95 latency | Known avg cost | Errors | Timeouts | Abstentions |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| single_prompt | 0.2333 | 0.3833 | 0.0000 | 0.0000 | 0.0000 | 0.000 | 0.000 | 9232.14 | 8948.79 | 12423.22 | unknown | 4 | 0 | 4 |
| react | 0.3500 | 0.4667 | 0.0174 | 0.0057 | 0.0000 | 2.883 | 2.850 | 30229.93 | 25677.36 | 73825.02 | unknown | 10 | 0 | 10 |
| state_graph_no_validator | 0.0833 | 0.2000 | 0.0149 | 0.0000 | 0.0000 | 5.350 | 3.350 | 66656.22 | 73437.94 | 97057.30 | unknown | 42 | 0 | 0 |
| full_harness | 0.0000 | 0.2500 | 0.0050 | 0.0000 | 0.0031 | 5.367 | 3.317 | 62252.44 | 62457.44 | 96517.51 | unknown | 36 | 0 | 60 |

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
| F01 | single_prompt | 0.8000 | 1.0000 | 5 | 0 | 0 |
| F01 | react | 0.6000 | 0.6000 | 5 | 1 | 0 |
| F01 | state_graph_no_validator | 0.0000 | 0.0000 | 5 | 5 | 0 |
| F01 | full_harness | 0.0000 | 0.0000 | 5 | 5 | 0 |
| F02 | single_prompt | 1.0000 | 1.0000 | 5 | 0 | 0 |
| F02 | react | 1.0000 | 1.0000 | 5 | 0 | 0 |
| F02 | state_graph_no_validator | 0.4000 | 0.6000 | 5 | 2 | 0 |
| F02 | full_harness | 0.0000 | 0.6000 | 5 | 2 | 0 |
| F03 | single_prompt | 0.0000 | 0.0000 | 5 | 0 | 0 |
| F03 | react | 0.4000 | 0.4000 | 5 | 0 | 0 |
| F03 | state_graph_no_validator | 0.0000 | 0.0000 | 5 | 5 | 0 |
| F03 | full_harness | 0.0000 | 0.0000 | 5 | 5 | 0 |
| F04 | single_prompt | 0.6000 | 1.0000 | 5 | 0 | 0 |
| F04 | react | 0.6000 | 0.8000 | 5 | 1 | 0 |
| F04 | state_graph_no_validator | 0.0000 | 0.0000 | 5 | 5 | 0 |
| F04 | full_harness | 0.0000 | 0.0000 | 5 | 5 | 0 |
| F05 | single_prompt | 0.0000 | 1.0000 | 5 | 0 | 0 |
| F05 | react | 0.0000 | 0.6000 | 5 | 0 | 0 |
| F05 | state_graph_no_validator | 0.0000 | 0.0000 | 5 | 5 | 0 |
| F05 | full_harness | 0.0000 | 0.0000 | 5 | 4 | 0 |
| F06 | single_prompt | 0.4000 | 0.4000 | 5 | 3 | 0 |
| F06 | react | 0.4000 | 0.4000 | 5 | 3 | 0 |
| F06 | state_graph_no_validator | 0.6000 | 0.6000 | 5 | 2 | 0 |
| F06 | full_harness | 0.0000 | 0.8000 | 5 | 1 | 0 |
| F07 | single_prompt | 0.0000 | 0.0000 | 5 | 1 | 0 |
| F07 | react | 0.0000 | 0.0000 | 5 | 2 | 0 |
| F07 | state_graph_no_validator | 0.0000 | 0.0000 | 5 | 4 | 0 |
| F07 | full_harness | 0.0000 | 0.0000 | 5 | 4 | 0 |
| F08 | single_prompt | 0.0000 | 0.0000 | 5 | 0 | 0 |
| F08 | react | 0.0000 | 0.0000 | 5 | 1 | 0 |
| F08 | state_graph_no_validator | 0.0000 | 0.2000 | 5 | 4 | 0 |
| F08 | full_harness | 0.0000 | 0.4000 | 5 | 2 | 0 |
| F09 | single_prompt | 0.0000 | 0.2000 | 5 | 0 | 0 |
| F09 | react | 0.4000 | 0.8000 | 5 | 0 | 0 |
| F09 | state_graph_no_validator | 0.0000 | 0.2000 | 5 | 4 | 0 |
| F09 | full_harness | 0.0000 | 0.4000 | 5 | 3 | 0 |
| F10 | single_prompt | 0.0000 | 0.0000 | 5 | 0 | 0 |
| F10 | react | 0.0000 | 0.0000 | 5 | 1 | 0 |
| F10 | state_graph_no_validator | 0.0000 | 0.8000 | 5 | 1 | 0 |
| F10 | full_harness | 0.0000 | 0.8000 | 5 | 1 | 0 |
| F11 | single_prompt | 0.0000 | 0.0000 | 5 | 0 | 0 |
| F11 | react | 0.8000 | 1.0000 | 5 | 0 | 0 |
| F11 | state_graph_no_validator | 0.0000 | 0.0000 | 5 | 5 | 0 |
| F11 | full_harness | 0.0000 | 0.0000 | 5 | 4 | 0 |
| F12 | single_prompt | 0.0000 | 0.0000 | 5 | 0 | 0 |
| F12 | react | 0.0000 | 0.0000 | 5 | 1 | 0 |
| F12 | state_graph_no_validator | 0.0000 | 0.0000 | 5 | 0 | 0 |
| F12 | full_harness | 0.0000 | 0.0000 | 5 | 0 | 0 |
