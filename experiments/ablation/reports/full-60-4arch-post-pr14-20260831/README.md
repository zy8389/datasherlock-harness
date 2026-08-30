# Post-PR #14 Four-Architecture Benchmark

This directory is the sanitized summary snapshot for the canonical 60-case,
four-architecture benchmark run after PR #14.

- Run ID: `full-60-4arch-post-pr14-20260831`
- Source commit: `35562c1b5963a7c2f5a67d209f2b0bdeca057a13`
- Model: `openai/gpt-5.6-luna`
- Model fingerprint: `5ebafd590852307e766ec9587ec32e1614794ed535b42e7f7fc2b4cc3a0a0d45`
- Attempted pairs: `240/240`
- Missing pairs: `0`
- Duplicate pairs: `0`

The pair matrix, model fingerprint, Ground Truth isolation, and database-copy
hashes across the four variants pass within this run. The old/new comparison
does not pass the requested cross-run database byte-hash check: only 32 of 60
DuckDB files have the same SHA-256 as the accepted 2026-08-28 run. A separate
no-model rematerialization check also reproduced byte-hash instability (30 of
60 matched the frozen new run). Canonical case YAML, Ground Truth, the case
generator, and the ablation runner are unchanged between source commits.

Eight case/variant attempts encountered a provider connection outage. The
frozen run is complete as a standalone measurement, but old/new accuracy
deltas are descriptive and comparative causal acceptance is blocked.

Committed files are sanitized summaries only. Raw `results.jsonl`, provider
payloads, runtime databases, credentials, environment files, and local paths
are intentionally excluded.

See `docs/evaluation/post_pr14_benchmark_comparison.md` for the acceptance
assessment and `docs/evaluation/failure_case_candidates.md` for the next
diagnostic worklist. The repository README is outdated; updating it is outside
this benchmark PR.
