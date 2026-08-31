from __future__ import annotations

import hashlib
import shutil
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import duckdb
import pandas as pd
import pytest

from benchmark.case_generator import load_case_manifest, materialize_case
from benchmark.fixture_fingerprint import (
    canonicalize_value,
    compare_fixture_fingerprints,
    fingerprint_fixture,
)
from data.generator import (
    BENCHMARK_FIXTURE_TABLES,
    generate_dataset,
    write_outputs,
)


def _create_fixture(
    path: Path,
    *,
    users_schema: str = "value INTEGER",
    users_rows: list[tuple[object, ...]] | None = None,
    reverse_rows: bool = False,
    add_non_fixture_table: bool = False,
) -> None:
    default_rows: list[tuple[object, ...]] = [(1,), (2,), (3,)]
    with duckdb.connect(str(path)) as connection:
        for table_name in BENCHMARK_FIXTURE_TABLES:
            schema = users_schema if table_name == "users" else "value INTEGER"
            rows = users_rows if table_name == "users" else default_rows
            rows = list(rows if rows is not None else default_rows)
            if reverse_rows:
                rows.reverse()
            connection.execute(f'CREATE TABLE "{table_name}" ({schema})')
            if rows:
                placeholders = ", ".join("?" for _ in rows[0])
                connection.executemany(
                    f'INSERT INTO "{table_name}" VALUES ({placeholders})', rows
                )
        if add_non_fixture_table:
            connection.execute(
                "CREATE TABLE diagnostic_padding AS SELECT * FROM range(10000)"
            )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_copied_database_has_same_logical_fingerprint(tmp_path: Path) -> None:
    original = tmp_path / "original.duckdb"
    copied = tmp_path / "copied.duckdb"
    _create_fixture(original)
    shutil.copy2(original, copied)

    assert fingerprint_fixture(original) == fingerprint_fixture(copied)


def test_insertion_order_does_not_change_logical_fingerprint(tmp_path: Path) -> None:
    forward = tmp_path / "forward.duckdb"
    reverse = tmp_path / "reverse.duckdb"
    _create_fixture(forward)
    _create_fixture(reverse, reverse_rows=True)

    assert fingerprint_fixture(forward) == fingerprint_fixture(reverse)


def test_duplicate_row_multiplicity_changes_logical_fingerprint(
    tmp_path: Path,
) -> None:
    duplicated = tmp_path / "duplicated.duckdb"
    unique = tmp_path / "unique.duckdb"
    _create_fixture(duplicated, users_rows=[(1,), (1,), (2,)])
    _create_fixture(unique, users_rows=[(1,), (2,)])

    comparison = compare_fixture_fingerprints(
        fingerprint_fixture(duplicated), fingerprint_fixture(unique)
    )
    assert not comparison.equal
    assert comparison.row_count_differences[0].table_name == "users"


def test_one_cell_change_reports_changed_table(tmp_path: Path) -> None:
    left = tmp_path / "left.duckdb"
    right = tmp_path / "right.duckdb"
    _create_fixture(left, users_rows=[(1,), (2,)])
    _create_fixture(right, users_rows=[(1,), (9,)])

    comparison = compare_fixture_fingerprints(
        fingerprint_fixture(left), fingerprint_fixture(right)
    )
    assert not comparison.equal
    assert [item.table_name for item in comparison.changed_table_hashes] == ["users"]


def test_schema_type_change_changes_logical_fingerprint(tmp_path: Path) -> None:
    integer = tmp_path / "integer.duckdb"
    varchar = tmp_path / "varchar.duckdb"
    _create_fixture(integer, users_schema="value INTEGER", users_rows=[(1,)])
    _create_fixture(varchar, users_schema="value VARCHAR", users_rows=[("1",)])

    comparison = compare_fixture_fingerprints(
        fingerprint_fixture(integer), fingerprint_fixture(varchar)
    )
    assert not comparison.equal
    assert comparison.schema_differences[0].table_name == "users"


def test_column_order_is_part_of_schema_contract(tmp_path: Path) -> None:
    left = tmp_path / "left.duckdb"
    right = tmp_path / "right.duckdb"
    _create_fixture(
        left,
        users_schema="number INTEGER, label VARCHAR",
        users_rows=[(1, "one")],
    )
    _create_fixture(
        right,
        users_schema="label VARCHAR, number INTEGER",
        users_rows=[("one", 1)],
    )

    comparison = compare_fixture_fingerprints(
        fingerprint_fixture(left), fingerprint_fixture(right)
    )
    assert not comparison.equal
    assert comparison.schema_differences[0].table_name == "users"


def test_null_values_are_stable(tmp_path: Path) -> None:
    left = tmp_path / "left.duckdb"
    right = tmp_path / "right.duckdb"
    rows = [(None,), (1,), (None,)]
    _create_fixture(left, users_rows=rows)
    _create_fixture(right, users_rows=list(reversed(rows)))

    assert fingerprint_fixture(left) == fingerprint_fixture(right)


def test_datetime_encoding_is_deterministic_and_normalizes_aware_values() -> None:
    naive = datetime(2026, 8, 31, 12, 34, 56, 123456)  # noqa: DTZ001
    utc = datetime(2026, 8, 31, 12, 34, 56, 123456, tzinfo=UTC)
    plus_eight = datetime(
        2026,
        8,
        31,
        20,
        34,
        56,
        123456,
        tzinfo=timezone(timedelta(hours=8)),
    )

    assert canonicalize_value(naive) == {
        "type": "datetime_naive",
        "value": "2026-08-31T12:34:56.123456",
    }
    assert canonicalize_value(utc) == canonicalize_value(plus_eight)


def test_physical_bytes_can_differ_while_logical_fixture_matches(
    tmp_path: Path,
) -> None:
    plain = tmp_path / "plain.duckdb"
    with_diagnostic_object = tmp_path / "with-diagnostic-object.duckdb"
    _create_fixture(plain)
    _create_fixture(with_diagnostic_object, add_non_fixture_table=True)

    assert _sha256(plain) != _sha256(with_diagnostic_object)
    assert fingerprint_fixture(plain) == fingerprint_fixture(with_diagnostic_object)


def test_fingerprinting_does_not_modify_database(tmp_path: Path) -> None:
    database = tmp_path / "fixture.duckdb"
    _create_fixture(database)
    before = database.read_bytes()

    fingerprint_fixture(database)

    assert database.read_bytes() == before


@pytest.mark.parametrize("case_id", ["F01-001", "F06-001"])
def test_canonical_case_rematerialization_is_logically_stable(
    tmp_path: Path, case_id: str
) -> None:
    manifest = load_case_manifest(case_id)
    first = materialize_case(manifest)
    second = materialize_case(manifest)
    first_dir = tmp_path / "first"
    second_dir = tmp_path / "second"
    write_outputs(first_dir, first.tables)
    write_outputs(second_dir, second.tables)

    assert fingerprint_fixture(
        first_dir / "datasherlock.duckdb"
    ) == fingerprint_fixture(second_dir / "datasherlock.duckdb")


def test_generator_table_catalog_matches_generated_fixture() -> None:
    tables = generate_dataset(
        user_count=20,
        days=7,
        event_count=100,
        seed=42,
        start_date=pd.Timestamp("2026-01-01"),
    )

    assert tuple(tables) == BENCHMARK_FIXTURE_TABLES
