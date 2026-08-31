"""Stable logical identity for canonical benchmark DuckDB fixtures."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import math
import struct
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, time
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

import duckdb
from pydantic import BaseModel, ConfigDict, Field

from data.generator import BENCHMARK_FIXTURE_TABLES

FINGERPRINT_SCHEMA_VERSION = 1
HASH_ALGORITHM = "sha256"
HASH_PREFIX = f"{HASH_ALGORITHM}:"
DEFAULT_FETCH_SIZE = 1_000


class LogicalColumnFingerprint(BaseModel):
    """One ordered DuckDB column in the logical fixture contract."""

    model_config = ConfigDict(extra="forbid")

    ordinal_position: int = Field(ge=1)
    column_name: str = Field(min_length=1)
    column_type: str = Field(min_length=1)


class LogicalTableFingerprint(BaseModel):
    """Canonical schema and row-multiset identity for one fixture table."""

    model_config = ConfigDict(extra="forbid")

    table_name: str = Field(min_length=1)
    columns: list[LogicalColumnFingerprint]
    row_count: int = Field(ge=0)
    table_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class LogicalFixtureFingerprint(BaseModel):
    """Versioned logical identity for all benchmark-owned user tables."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=FINGERPRINT_SCHEMA_VERSION, ge=1)
    algorithm: Literal["sha256"] = HASH_ALGORITHM
    database_logical_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    tables: list[LogicalTableFingerprint]


class SchemaDifference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    table_name: str
    left: list[LogicalColumnFingerprint]
    right: list[LogicalColumnFingerprint]


class RowCountDifference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    table_name: str
    left: int = Field(ge=0)
    right: int = Field(ge=0)


class TableHashDifference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    table_name: str
    left: str
    right: str


class FixtureFingerprintComparison(BaseModel):
    """Structured explanation of two fingerprint contracts."""

    model_config = ConfigDict(extra="forbid")

    equal: bool
    left_database_logical_hash: str
    right_database_logical_hash: str
    contract_differences: list[str] = Field(default_factory=list)
    missing_tables: list[str] = Field(default_factory=list)
    extra_tables: list[str] = Field(default_factory=list)
    schema_differences: list[SchemaDifference] = Field(default_factory=list)
    row_count_differences: list[RowCountDifference] = Field(default_factory=list)
    changed_table_hashes: list[TableHashDifference] = Field(default_factory=list)


def _json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _canonical_float(value: float) -> str:
    if math.isnan(value):
        return "nan"
    if math.isinf(value):
        return "+infinity" if value > 0 else "-infinity"
    return value.hex()


def canonicalize_value(value: Any) -> Any:
    """Return a typed, JSON-compatible representation of one DuckDB value."""

    if value is None:
        return {"type": "null", "value": None}
    if isinstance(value, bool):
        return {"type": "bool", "value": value}
    if isinstance(value, int):
        return {"type": "int", "value": str(value)}
    if isinstance(value, float):
        return {"type": "float", "value": _canonical_float(value)}
    if isinstance(value, Decimal):
        decimal_tuple = value.as_tuple()
        return {
            "type": "decimal",
            "value": {
                "sign": decimal_tuple.sign,
                "digits": "".join(str(digit) for digit in decimal_tuple.digits),
                "exponent": decimal_tuple.exponent,
            },
        }
    if isinstance(value, str):
        return {"type": "str", "value": value}
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            return {
                "type": "datetime_naive",
                "value": value.isoformat(timespec="microseconds"),
            }
        normalized = value.astimezone(UTC)
        return {
            "type": "datetime_utc",
            "value": normalized.isoformat(timespec="microseconds").replace(
                "+00:00", "Z"
            ),
        }
    if isinstance(value, date):
        return {"type": "date", "value": value.isoformat()}
    if isinstance(value, time):
        if value.tzinfo is None or value.utcoffset() is None:
            return {
                "type": "time_naive",
                "value": value.isoformat(timespec="microseconds"),
            }
        raise TypeError("timezone-aware time values without a date are unsupported")
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {
            "type": "bytes",
            "value": base64.b64encode(bytes(value)).decode("ascii"),
        }
    if isinstance(value, (list, tuple)):
        return {
            "type": "list",
            "value": [canonicalize_value(item) for item in value],
        }
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise TypeError("mapping keys must be strings")
        return {
            "type": "struct",
            "value": {
                key: canonicalize_value(value[key]) for key in sorted(value)
            },
        }
    raise TypeError(f"unsupported DuckDB value type: {type(value).__name__}")


def _canonical_row(row: Sequence[Any]) -> bytes:
    return _json_bytes(
        {"type": "row", "values": [canonicalize_value(value) for value in row]}
    )


def _update_framed(digest: Any, payload: bytes) -> None:
    digest.update(struct.pack(">Q", len(payload)))
    digest.update(payload)


def _quoted_identifier(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _columns_for_table(
    connection: duckdb.DuckDBPyConnection, table_name: str
) -> list[LogicalColumnFingerprint]:
    rows = connection.execute(
        """
        SELECT ordinal_position, column_name, data_type
        FROM information_schema.columns
        WHERE table_schema = 'main' AND table_name = ?
        ORDER BY ordinal_position
        """,
        [table_name],
    ).fetchall()
    return [
        LogicalColumnFingerprint(
            ordinal_position=int(ordinal),
            column_name=str(name),
            column_type=str(column_type).upper(),
        )
        for ordinal, name, column_type in rows
    ]


def _fingerprint_table(
    connection: duckdb.DuckDBPyConnection,
    table_name: str,
    *,
    fetch_size: int,
) -> LogicalTableFingerprint:
    columns = _columns_for_table(connection, table_name)
    if not columns:
        raise ValueError(f"fixture table has no columns: {table_name}")

    cursor = connection.execute(f"SELECT * FROM {_quoted_identifier(table_name)}")
    canonical_rows: list[bytes] = []
    while batch := cursor.fetchmany(fetch_size):
        canonical_rows.extend(_canonical_row(row) for row in batch)
    canonical_rows.sort()

    schema_payload = _json_bytes(
        {
            "table_name": table_name,
            "columns": [column.model_dump(mode="json") for column in columns],
        }
    )
    digest = hashlib.sha256()
    _update_framed(digest, b"datasherlock-logical-fixture-table-v1")
    _update_framed(digest, schema_payload)
    _update_framed(digest, str(len(canonical_rows)).encode("ascii"))
    for row in canonical_rows:
        _update_framed(digest, row)
    return LogicalTableFingerprint(
        table_name=table_name,
        columns=columns,
        row_count=len(canonical_rows),
        table_hash=HASH_PREFIX + digest.hexdigest(),
    )


def fingerprint_fixture(
    database_path: str | Path,
    *,
    fetch_size: int = DEFAULT_FETCH_SIZE,
) -> LogicalFixtureFingerprint:
    """Fingerprint benchmark-owned tables without modifying the database."""

    path = Path(database_path)
    if not path.is_file():
        raise FileNotFoundError(path)
    if fetch_size <= 0:
        raise ValueError("fetch_size must be positive")

    connection = duckdb.connect(
        str(path),
        read_only=True,
        config={"enable_external_access": "false"},
    )
    try:
        actual_tables = {
            str(row[0])
            for row in connection.execute(
                """
                SELECT table_name
                FROM information_schema.tables
                WHERE table_schema = 'main' AND table_type = 'BASE TABLE'
                """
            ).fetchall()
        }
        missing = sorted(set(BENCHMARK_FIXTURE_TABLES) - actual_tables)
        if missing:
            raise ValueError("database is missing fixture tables: " + ", ".join(missing))
        tables = [
            _fingerprint_table(connection, table_name, fetch_size=fetch_size)
            for table_name in sorted(BENCHMARK_FIXTURE_TABLES)
        ]
    finally:
        connection.close()

    database_digest = hashlib.sha256()
    _update_framed(database_digest, b"datasherlock-logical-fixture-database-v1")
    for table in tables:
        _update_framed(
            database_digest,
            _json_bytes(
                {
                    "table_name": table.table_name,
                    "table_hash": table.table_hash,
                }
            ),
        )
    return LogicalFixtureFingerprint(
        database_logical_hash=HASH_PREFIX + database_digest.hexdigest(),
        tables=tables,
    )


def compare_fixture_fingerprints(
    left: LogicalFixtureFingerprint,
    right: LogicalFixtureFingerprint,
) -> FixtureFingerprintComparison:
    """Compare contracts and retain actionable table-level differences."""

    contract_differences: list[str] = []
    if left.schema_version != right.schema_version:
        contract_differences.append("schema_version")
    if left.algorithm != right.algorithm:
        contract_differences.append("algorithm")

    left_tables = {table.table_name: table for table in left.tables}
    right_tables = {table.table_name: table for table in right.tables}
    missing_tables = sorted(set(left_tables) - set(right_tables))
    extra_tables = sorted(set(right_tables) - set(left_tables))
    schema_differences: list[SchemaDifference] = []
    row_count_differences: list[RowCountDifference] = []
    changed_table_hashes: list[TableHashDifference] = []
    for table_name in sorted(set(left_tables) & set(right_tables)):
        left_table = left_tables[table_name]
        right_table = right_tables[table_name]
        if left_table.columns != right_table.columns:
            schema_differences.append(
                SchemaDifference(
                    table_name=table_name,
                    left=left_table.columns,
                    right=right_table.columns,
                )
            )
        if left_table.row_count != right_table.row_count:
            row_count_differences.append(
                RowCountDifference(
                    table_name=table_name,
                    left=left_table.row_count,
                    right=right_table.row_count,
                )
            )
        if left_table.table_hash != right_table.table_hash:
            changed_table_hashes.append(
                TableHashDifference(
                    table_name=table_name,
                    left=left_table.table_hash,
                    right=right_table.table_hash,
                )
            )

    equal = (
        left.database_logical_hash == right.database_logical_hash
        and not contract_differences
        and not missing_tables
        and not extra_tables
        and not schema_differences
        and not row_count_differences
        and not changed_table_hashes
    )
    return FixtureFingerprintComparison(
        equal=equal,
        left_database_logical_hash=left.database_logical_hash,
        right_database_logical_hash=right.database_logical_hash,
        contract_differences=contract_differences,
        missing_tables=missing_tables,
        extra_tables=extra_tables,
        schema_differences=schema_differences,
        row_count_differences=row_count_differences,
        changed_table_hashes=changed_table_hashes,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", type=Path)
    parser.add_argument("--output", type=Path, help="write the JSON contract")
    parser.add_argument("--json", action="store_true", help="print JSON to stdout")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    fingerprint = fingerprint_fixture(args.database)
    payload = fingerprint.model_dump_json(indent=2)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload + "\n", encoding="utf-8", newline="\n")
    if args.json:
        print(payload)
    else:
        print(f"logical fixture: {fingerprint.database_logical_hash}")
        for table in fingerprint.tables:
            print(f"{table.table_name}: rows={table.row_count} {table.table_hash}")
        if args.output is not None:
            print(f"JSON: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "FINGERPRINT_SCHEMA_VERSION",
    "FixtureFingerprintComparison",
    "LogicalColumnFingerprint",
    "LogicalFixtureFingerprint",
    "LogicalTableFingerprint",
    "canonicalize_value",
    "compare_fixture_fingerprints",
    "fingerprint_fixture",
]
