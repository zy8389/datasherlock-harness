# Benchmark Fixture Reproducibility

Benchmark fixture identity has two separate forms. They are both recorded for
new ablation runs, but they answer different questions.

## Physical Artifact Hash

`database_sha256` is the SHA-256 digest of the `.duckdb` file bytes. It is useful
for confirming exact copies within one run and for diagnosing replaced or
corrupted runtime artifacts.

Physical DuckDB bytes are not a stable cross-run scientific identity. Equivalent
databases can have different page allocation, checkpoints, metadata, and storage
layout. A physical hash mismatch therefore does not by itself prove that fixture
data changed.

## Logical Fixture Fingerprint

`python -m benchmark.fixture_fingerprint DATABASE` computes the versioned
logical contract used for cross-run fixture equality. Use `--json` to print the
full JSON contract or `--output PATH` to persist it.

Schema version 1 uses SHA-256 and covers the ten benchmark-owned tables declared
by `data.generator.BENCHMARK_FIXTURE_TABLES`. It intentionally ignores DuckDB
internal objects and non-fixture tables.

Each table fingerprint includes:

- table name;
- ordered columns, including one-based ordinal, name, and DuckDB logical type;
- row count;
- the complete multiset of rows, including duplicate multiplicity.

Rows do not depend on physical or insertion order. Each value is converted to a
typed canonical JSON representation, each row is encoded independently, and the
encoded rows are sorted before incremental hashing. Length framing separates
every hash component and prevents concatenation ambiguity.

Metric fixture materialization runs DuckDB with one execution thread so
floating-point aggregate reduction order is stable before values enter the
exact fingerprint contract.

The value contract distinguishes NULL, booleans, integers, exact floating-point
values, decimals, strings, dates, datetimes, times, bytes, lists, and structs.
Finite floats use Python's exact hexadecimal representation; negative zero is
preserved, while NaN and positive or negative infinity have explicit encodings.
Timezone-aware datetimes are normalized to UTC. Naive datetimes remain naive and
are encoded with microsecond precision, matching the benchmark's current DuckDB
timestamp contract. No rounding or local timezone conversion is applied.

The database-level hash is composed from the schema version and the sorted
benchmark table identities. `compare_fixture_fingerprints` returns structured
contract, missing-table, schema, row-count, and table-hash differences rather
than only a boolean.

## Read-Only And Scale Contract

Fingerprinting opens DuckDB with `read_only=True` and disables external access.
It does not issue writes, checkpoints, or vacuum operations.

Rows are fetched in bounded batches and hashed incrementally after ordering.
Canonical row encodings are currently sorted in memory, so peak memory grows
with the encoded size of the largest table. This is acceptable for the current
benchmark scale, including roughly 10,000 events per case, but should be replaced
with an external sort before substantially increasing fixture size.

## Ablation Artifacts And Compatibility

New `case_inputs.jsonl` records the complete logical contract for every
case/variant database. `fairness.json` records case/variant logical hashes,
logical mismatch details, and physical hashes. Scientific fixture fairness uses
logical equality whenever logical contracts are present. Physical equality is
still reported as an artifact diagnostic.

Runs created before schema version 1 do not contain logical fields. They remain
readable and fall back to physical SHA checks; accepted frozen report snapshots
are not migrated or rewritten. Resume validates the fixture identity recorded by
the run and fails closed when a persisted database no longer matches.

Logical fixture equality proves only that benchmark input schemas and data are
equal. It does not prove model determinism, output equality, or score
reproducibility.
