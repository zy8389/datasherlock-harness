"""Read-only data quality checks built on the controlled SQL runner."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from tools.sql_runner import execute_readonly_sql

QualityCheckStatus = Literal["success", "error"]
ScopeFilterValue = str | int | float | bool | None
_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class DataQualityEvidence(BaseModel):
    """Traceable observation produced by a data quality check."""

    finding: str
    query_id: str
    details: dict[str, Any] = Field(default_factory=dict)


class DataQualityCheckResult(BaseModel):
    """Stable result envelope shared by all data quality tools."""

    check_name: str
    status: QualityCheckStatus
    passed: bool | None
    table: str
    column: str | None = None
    columns: list[str] = Field(default_factory=list)
    observed_value: float | None = None
    threshold: float | None = None
    query_id: str | None = None
    evidence: list[DataQualityEvidence] = Field(default_factory=list)
    error: dict[str, str] | None = None


class DataQualityScope(BaseModel):
    """Safe equality and time-window restrictions for one data quality check."""

    model_config = ConfigDict(extra="forbid", strict=True)

    equals: dict[str, ScopeFilterValue | list[ScopeFilterValue]] = Field(
        default_factory=dict
    )
    time_column: str | None = None
    start: datetime | None = None
    end: datetime | None = None


def _quote_identifier(value: str, *, label: str) -> str:
    """Return one safe DuckDB identifier from a simple table or column name."""

    if not isinstance(value, str) or not _IDENTIFIER_PATTERN.fullmatch(value):
        raise ValueError(
            f"{label} must contain only letters, numbers, and underscores, "
            "and must not start with a number"
        )
    return f'"{value}"'


def _validate_rate_threshold(threshold: float) -> float:
    """Validate a proportion threshold before issuing a query."""

    if (
        isinstance(threshold, bool)
        or not isinstance(threshold, (int, float))
        or not math.isfinite(threshold)
        or not 0 <= threshold <= 1
    ):
        raise ValueError("threshold must be a finite number between 0 and 1")
    return float(threshold)


def _validate_max_age(max_age: timedelta) -> float:
    """Validate a positive freshness allowance and return it in seconds."""

    if not isinstance(max_age, timedelta) or max_age <= timedelta(0):
        raise ValueError("max_age must be a positive timedelta")
    return max_age.total_seconds()


def _normalize_reference_time(reference_time: datetime) -> datetime:
    """Require an explicit timezone-aware freshness reference time in UTC."""

    return _normalize_utc_datetime(reference_time, label="reference_time")


def _normalize_utc_datetime(value: datetime, *, label: str) -> datetime:
    """Require one explicit timezone-aware datetime and normalize it to UTC."""

    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError(f"{label} must be a timezone-aware datetime")
    return value.astimezone(UTC)


def _normalize_observed_time(value: object) -> datetime:
    """Interpret DuckDB's naive timestamp values as the baseline's UTC timestamps."""

    if not isinstance(value, datetime):
        raise TypeError("latest timestamp is not a datetime value")
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _parse_schema_json(value: object) -> dict[str, str]:
    """Parse one schema snapshot into a field-to-type mapping."""

    if not isinstance(value, str):
        raise TypeError("schema_json must be a JSON string")
    try:
        schema = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError("schema_json is not valid JSON") from exc
    if not isinstance(schema, dict) or not all(
        isinstance(column, str) and isinstance(data_type, str)
        for column, data_type in schema.items()
    ):
        raise ValueError("schema_json must map string column names to string types")
    return schema


def _validate_time_window(
    start: datetime, end: datetime, *, label: str
) -> tuple[datetime, datetime]:
    """Normalize one non-empty half-open interval to UTC."""

    normalized_start = _normalize_utc_datetime(start, label=f"{label}_start")
    normalized_end = _normalize_utc_datetime(end, label=f"{label}_end")
    if normalized_start >= normalized_end:
        raise ValueError(f"{label}_start must be earlier than {label}_end")
    return normalized_start, normalized_end


def _duckdb_timestamp_literal(value: datetime) -> str:
    """Render a validated UTC datetime as a DuckDB timestamp literal."""

    naive_utc = value.astimezone(UTC).replace(tzinfo=None)
    return naive_utc.isoformat(sep=" ", timespec="microseconds")


def _duckdb_scalar_literal(value: ScopeFilterValue) -> str:
    """Render one validated scalar as a DuckDB literal without SQL interpolation."""

    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, str):
        return "'" + value.replace("'", "''") + "'"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float) and math.isfinite(value):
        return repr(value)
    raise ValueError("scope filter values must be finite scalar values")


def _with_scope_details(
    details: dict[str, Any], scope_details: dict[str, Any]
) -> dict[str, Any]:
    """Attach scope evidence only when a caller has restricted the check."""

    return details if not scope_details else {**details, "scope": scope_details}


def _quote_identifiers(values: Sequence[str], *, label: str) -> tuple[list[str], list[str]]:
    """Validate and quote one or more column identifiers."""

    if isinstance(values, str) or not isinstance(values, Sequence) or not values:
        raise ValueError(f"{label} must be a non-empty list of column names")

    names = list(values)
    if len(set(names)) != len(names):
        raise ValueError(f"{label} must not contain duplicate column names")
    return names, [_quote_identifier(name, label="key") for name in names]


def _compile_scope(scope: DataQualityScope | None) -> tuple[str, dict[str, Any]]:
    """Compile a structured scope into a read-only SQL ``WHERE`` clause."""

    if scope is None:
        return "", {}
    if not isinstance(scope, DataQualityScope):
        raise TypeError("scope must be a DataQualityScope instance")

    conditions: list[str] = []
    for column, raw_values in scope.equals.items():
        quoted_column = _quote_identifier(column, label="scope filter column")
        values = raw_values if isinstance(raw_values, list) else [raw_values]
        if not values:
            raise ValueError(f"scope filter {column!r} must not be empty")

        non_null_values = [value for value in values if value is not None]
        has_null = len(non_null_values) != len(values)
        value_conditions: list[str] = []
        if non_null_values:
            literals = [_duckdb_scalar_literal(value) for value in non_null_values]
            if len(literals) == 1:
                value_conditions.append(f"{quoted_column} = {literals[0]}")
            else:
                value_conditions.append(f"{quoted_column} IN ({', '.join(literals)})")
        if has_null:
            value_conditions.append(f"{quoted_column} IS NULL")
        conditions.append("(" + " OR ".join(value_conditions) + ")")

    has_time_boundary = scope.start is not None or scope.end is not None
    if has_time_boundary:
        if scope.time_column is None or scope.start is None or scope.end is None:
            raise ValueError("scope time_column, start, and end must be provided together")
        quoted_time_column = _quote_identifier(scope.time_column, label="scope time_column")
        start, end = _validate_time_window(scope.start, scope.end, label="scope")
        conditions.append(
            f"({quoted_time_column} >= TIMESTAMP '{_duckdb_timestamp_literal(start)}' "
            f"AND {quoted_time_column} < TIMESTAMP '{_duckdb_timestamp_literal(end)}')"
        )
    elif scope.time_column is not None:
        raise ValueError("scope time_column requires both start and end")

    clause = "" if not conditions else "\n        WHERE " + "\n          AND ".join(conditions)
    return clause, scope.model_dump(mode="json")


def _invalid_input_result(
    *,
    check_name: str,
    table: str,
    column: str | None,
    columns: list[str] | None = None,
    threshold: float | object,
    message: str,
) -> DataQualityCheckResult:
    """Return an error envelope when no SQL query should be attempted."""

    valid_threshold = (
        float(threshold)
        if isinstance(threshold, (int, float))
        and not isinstance(threshold, bool)
        and math.isfinite(threshold)
        else None
    )
    return DataQualityCheckResult(
        check_name=check_name,
        status="error",
        passed=None,
        table=str(table),
        column=str(column) if column is not None else None,
        columns=columns or [],
        threshold=valid_threshold,
        error={"type": "validation", "message": message},
    )


def check_null_rate(
    database_path: str | Path,
    table: str,
    column: str,
    *,
    threshold: float = 0.01,
    scope: DataQualityScope | None = None,
    incident_id: str | None = None,
    trace_id: str | None = None,
    audit_path: str | Path | None = None,
    timeout_seconds: float = 10.0,
) -> DataQualityCheckResult:
    """Measure whether a table column's null rate is within its threshold.

    The check is executed only through :func:`execute_readonly_sql`, preserving
    the SQL Runner's AST validation, read-only connection, row limit, and audit
    behavior. Empty tables return a successful observation but do not pass,
    because a null rate cannot be meaningfully calculated without rows.
    """

    try:
        quoted_table = _quote_identifier(table, label="table")
        quoted_column = _quote_identifier(column, label="column")
        checked_threshold = _validate_rate_threshold(threshold)
        scope_sql, scope_details = _compile_scope(scope)
    except (TypeError, ValueError) as exc:
        return _invalid_input_result(
            check_name="check_null_rate",
            table=table,
            column=column,
            threshold=threshold,
            message=str(exc),
        )

    sql = f"""
        SELECT
            COUNT(*) AS total_rows,
            COUNT(*) FILTER (WHERE {quoted_column} IS NULL) AS null_rows
        FROM {quoted_table}{scope_sql}
    """
    response = execute_readonly_sql(
        database_path,
        sql,
        incident_id=incident_id,
        trace_id=trace_id,
        audit_path=audit_path,
        timeout_seconds=timeout_seconds,
    )
    if response.status == "error":
        return DataQualityCheckResult(
            check_name="check_null_rate",
            status="error",
            passed=None,
            table=table,
            column=column,
            columns=[column],
            threshold=checked_threshold,
            query_id=response.query_id,
            error=response.error,
        )

    if len(response.rows) != 1 or len(response.rows[0]) != 2:
        return DataQualityCheckResult(
            check_name="check_null_rate",
            status="error",
            passed=None,
            table=table,
            column=column,
            columns=[column],
            threshold=checked_threshold,
            query_id=response.query_id,
            error={
                "type": "execution",
                "message": "null-rate query returned an unexpected result shape",
            },
        )

    total_rows, null_rows = (int(value) for value in response.rows[0])
    if total_rows == 0:
        finding = f"{table}.{column} has no rows; null rate is undefined"
        return DataQualityCheckResult(
            check_name="check_null_rate",
            status="success",
            passed=False,
            table=table,
            column=column,
            columns=[column],
            threshold=checked_threshold,
            query_id=response.query_id,
            evidence=[
                DataQualityEvidence(
                    finding=finding,
                    query_id=response.query_id,
                    details=_with_scope_details(
                        {
                            "total_rows": 0,
                            "null_rows": 0,
                            "null_rate": None,
                        },
                        scope_details,
                    ),
                )
            ],
        )

    null_rate = null_rows / total_rows
    finding = (
        f"{table}.{column} null rate is {null_rate:.2%} "
        f"({null_rows} of {total_rows} rows)"
    )
    return DataQualityCheckResult(
        check_name="check_null_rate",
        status="success",
        passed=null_rate <= checked_threshold,
        table=table,
        column=column,
        columns=[column],
        observed_value=null_rate,
        threshold=checked_threshold,
        query_id=response.query_id,
        evidence=[
            DataQualityEvidence(
                finding=finding,
                query_id=response.query_id,
                details=_with_scope_details(
                    {
                        "total_rows": total_rows,
                        "null_rows": null_rows,
                        "null_rate": null_rate,
                    },
                    scope_details,
                ),
            )
        ],
    )


def check_duplicate_rate(
    database_path: str | Path,
    table: str,
    keys: Sequence[str],
    *,
    threshold: float = 0.0,
    incident_id: str | None = None,
    trace_id: str | None = None,
    audit_path: str | Path | None = None,
    timeout_seconds: float = 10.0,
) -> DataQualityCheckResult:
    """Measure the rate of rows duplicated by one or more key columns.

    A duplicate is any row beyond the first row sharing the same key combination.
    The query uses ``SELECT DISTINCT`` in a CTE so the same calculation works for
    both single-column and composite keys. SQL execution remains delegated to the
    read-only SQL Runner.
    """

    key_names: list[str] = []
    try:
        quoted_table = _quote_identifier(table, label="table")
        key_names, quoted_keys = _quote_identifiers(keys, label="keys")
        checked_threshold = _validate_rate_threshold(threshold)
    except (TypeError, ValueError) as exc:
        return _invalid_input_result(
            check_name="check_duplicate_rate",
            table=table,
            column=None,
            columns=key_names,
            threshold=threshold,
            message=str(exc),
        )

    key_selection = ", ".join(quoted_keys)
    sql = f"""
        WITH key_counts AS (
            SELECT COUNT(*) AS total_rows
            FROM {quoted_table}
        ), unique_keys AS (
            SELECT COUNT(*) AS unique_key_rows
            FROM (
                SELECT DISTINCT {key_selection}
                FROM {quoted_table}
            )
        )
        SELECT key_counts.total_rows, unique_keys.unique_key_rows
        FROM key_counts CROSS JOIN unique_keys
    """
    response = execute_readonly_sql(
        database_path,
        sql,
        incident_id=incident_id,
        trace_id=trace_id,
        audit_path=audit_path,
        timeout_seconds=timeout_seconds,
    )
    if response.status == "error":
        return DataQualityCheckResult(
            check_name="check_duplicate_rate",
            status="error",
            passed=None,
            table=table,
            columns=key_names,
            threshold=checked_threshold,
            query_id=response.query_id,
            error=response.error,
        )

    if len(response.rows) != 1 or len(response.rows[0]) != 2:
        return DataQualityCheckResult(
            check_name="check_duplicate_rate",
            status="error",
            passed=None,
            table=table,
            columns=key_names,
            threshold=checked_threshold,
            query_id=response.query_id,
            error={
                "type": "execution",
                "message": "duplicate-rate query returned an unexpected result shape",
            },
        )

    total_rows, unique_key_rows = (int(value) for value in response.rows[0])
    if total_rows == 0:
        return DataQualityCheckResult(
            check_name="check_duplicate_rate",
            status="success",
            passed=False,
            table=table,
            columns=key_names,
            threshold=checked_threshold,
            query_id=response.query_id,
            evidence=[
                DataQualityEvidence(
                    finding=(
                        f"{table} has no rows; duplicate rate for "
                        f"{', '.join(key_names)} is undefined"
                    ),
                    query_id=response.query_id,
                    details={
                        "total_rows": 0,
                        "unique_key_rows": 0,
                        "duplicate_rows": 0,
                        "duplicate_rate": None,
                    },
                )
            ],
        )

    duplicate_rows = total_rows - unique_key_rows
    duplicate_rate = duplicate_rows / total_rows
    return DataQualityCheckResult(
        check_name="check_duplicate_rate",
        status="success",
        passed=duplicate_rate <= checked_threshold,
        table=table,
        columns=key_names,
        observed_value=duplicate_rate,
        threshold=checked_threshold,
        query_id=response.query_id,
        evidence=[
            DataQualityEvidence(
                finding=(
                    f"{table} duplicate rate for {', '.join(key_names)} is "
                    f"{duplicate_rate:.2%} ({duplicate_rows} of {total_rows} rows)"
                ),
                query_id=response.query_id,
                details={
                    "total_rows": total_rows,
                    "unique_key_rows": unique_key_rows,
                    "duplicate_rows": duplicate_rows,
                    "duplicate_rate": duplicate_rate,
                },
            )
        ],
    )


def check_freshness(
    database_path: str | Path,
    table: str,
    timestamp_column: str,
    *,
    reference_time: datetime,
    max_age: timedelta,
    scope: DataQualityScope | None = None,
    incident_id: str | None = None,
    trace_id: str | None = None,
    audit_path: str | Path | None = None,
    timeout_seconds: float = 10.0,
) -> DataQualityCheckResult:
    """Check whether a table has a recent timestamp relative to a fixed reference.

    The caller supplies ``reference_time`` rather than using the system clock,
    making incident replay and benchmark runs reproducible. Source timestamps
    without timezone data are treated as UTC, matching the generated baseline.
    """

    try:
        quoted_table = _quote_identifier(table, label="table")
        quoted_column = _quote_identifier(timestamp_column, label="timestamp_column")
        normalized_reference_time = _normalize_reference_time(reference_time)
        max_age_seconds = _validate_max_age(max_age)
        scope_sql, scope_details = _compile_scope(scope)
    except (TypeError, ValueError) as exc:
        valid_max_age_seconds = (
            max_age.total_seconds() if isinstance(max_age, timedelta) else None
        )
        return _invalid_input_result(
            check_name="check_freshness",
            table=table,
            column=timestamp_column,
            columns=[timestamp_column] if isinstance(timestamp_column, str) else [],
            threshold=valid_max_age_seconds,
            message=str(exc),
        )

    sql = f"""
        SELECT
            COUNT(*) AS total_rows,
            COUNT({quoted_column}) AS timestamp_rows,
            MAX({quoted_column}) AS latest_timestamp
        FROM {quoted_table}{scope_sql}
    """
    response = execute_readonly_sql(
        database_path,
        sql,
        incident_id=incident_id,
        trace_id=trace_id,
        audit_path=audit_path,
        timeout_seconds=timeout_seconds,
    )
    if response.status == "error":
        return DataQualityCheckResult(
            check_name="check_freshness",
            status="error",
            passed=None,
            table=table,
            column=timestamp_column,
            columns=[timestamp_column],
            threshold=max_age_seconds,
            query_id=response.query_id,
            error=response.error,
        )

    if len(response.rows) != 1 or len(response.rows[0]) != 3:
        return DataQualityCheckResult(
            check_name="check_freshness",
            status="error",
            passed=None,
            table=table,
            column=timestamp_column,
            columns=[timestamp_column],
            threshold=max_age_seconds,
            query_id=response.query_id,
            error={
                "type": "execution",
                "message": "freshness query returned an unexpected result shape",
            },
        )

    total_rows, timestamp_rows, latest_value = response.rows[0]
    total_rows = int(total_rows)
    timestamp_rows = int(timestamp_rows)
    if timestamp_rows == 0:
        return DataQualityCheckResult(
            check_name="check_freshness",
            status="success",
            passed=False,
            table=table,
            column=timestamp_column,
            columns=[timestamp_column],
            threshold=max_age_seconds,
            query_id=response.query_id,
            evidence=[
                DataQualityEvidence(
                    finding=(
                        f"{table}.{timestamp_column} has no timestamp values; "
                        "freshness is undefined"
                    ),
                    query_id=response.query_id,
                    details=_with_scope_details(
                        {
                            "total_rows": total_rows,
                            "timestamp_rows": 0,
                            "latest_timestamp": None,
                            "reference_time": normalized_reference_time.isoformat(),
                            "freshness_age_seconds": None,
                        },
                        scope_details,
                    ),
                )
            ],
        )

    try:
        latest_timestamp = _normalize_observed_time(latest_value)
    except (TypeError, ValueError) as exc:
        return DataQualityCheckResult(
            check_name="check_freshness",
            status="error",
            passed=None,
            table=table,
            column=timestamp_column,
            columns=[timestamp_column],
            threshold=max_age_seconds,
            query_id=response.query_id,
            error={"type": "execution", "message": str(exc)},
        )

    freshness_age_seconds = (normalized_reference_time - latest_timestamp).total_seconds()
    passed = 0 <= freshness_age_seconds <= max_age_seconds
    if freshness_age_seconds < 0:
        finding = (
            f"{table}.{timestamp_column} latest timestamp {latest_timestamp.isoformat()} "
            "is later than the reference time"
        )
    else:
        finding = (
            f"{table}.{timestamp_column} freshness age is "
            f"{freshness_age_seconds:.0f} seconds"
        )
    return DataQualityCheckResult(
        check_name="check_freshness",
        status="success",
        passed=passed,
        table=table,
        column=timestamp_column,
        columns=[timestamp_column],
        observed_value=freshness_age_seconds,
        threshold=max_age_seconds,
        query_id=response.query_id,
        evidence=[
            DataQualityEvidence(
                finding=finding,
                query_id=response.query_id,
                details=_with_scope_details(
                    {
                        "total_rows": total_rows,
                        "timestamp_rows": timestamp_rows,
                        "latest_timestamp": latest_timestamp.isoformat(),
                        "reference_time": normalized_reference_time.isoformat(),
                        "freshness_age_seconds": freshness_age_seconds,
                        "max_age_seconds": max_age_seconds,
                    },
                    scope_details,
                ),
            )
        ],
    )


def detect_schema_drift(
    database_path: str | Path,
    table: str,
    *,
    incident_id: str | None = None,
    trace_id: str | None = None,
    audit_path: str | Path | None = None,
    timeout_seconds: float = 10.0,
) -> DataQualityCheckResult:
    """Compare the two latest metadata snapshots for a table's schema changes.

    The baseline generator and F10 injector persist schema history in
    ``schema_snapshots``. This tool treats a field addition, removal, or type
    change as drift and leaves current database schema inspection to later tools.
    """

    try:
        _quote_identifier(table, label="table")
    except ValueError as exc:
        return _invalid_input_result(
            check_name="detect_schema_drift",
            table=table,
            column=None,
            threshold=0.0,
            message=str(exc),
        )

    sql = f"""
        SELECT version, schema_json, effective_at
        FROM "schema_snapshots"
        WHERE table_name = '{table}'
        ORDER BY version DESC, effective_at DESC
        LIMIT 2
    """
    response = execute_readonly_sql(
        database_path,
        sql,
        incident_id=incident_id,
        trace_id=trace_id,
        audit_path=audit_path,
        timeout_seconds=timeout_seconds,
    )
    if response.status == "error":
        return DataQualityCheckResult(
            check_name="detect_schema_drift",
            status="error",
            passed=None,
            table=table,
            threshold=0.0,
            query_id=response.query_id,
            error=response.error,
        )

    if len(response.rows) < 2:
        return DataQualityCheckResult(
            check_name="detect_schema_drift",
            status="error",
            passed=None,
            table=table,
            threshold=0.0,
            query_id=response.query_id,
            error={
                "type": "execution",
                "message": f"at least two schema snapshots are required for {table}",
            },
        )
    if any(len(row) != 3 for row in response.rows):
        return DataQualityCheckResult(
            check_name="detect_schema_drift",
            status="error",
            passed=None,
            table=table,
            threshold=0.0,
            query_id=response.query_id,
            error={
                "type": "execution",
                "message": "schema-drift query returned an unexpected result shape",
            },
        )

    current_version, current_schema_json, current_effective_at = response.rows[0]
    previous_version, previous_schema_json, previous_effective_at = response.rows[1]
    try:
        current_schema = _parse_schema_json(current_schema_json)
        previous_schema = _parse_schema_json(previous_schema_json)
    except (TypeError, ValueError) as exc:
        return DataQualityCheckResult(
            check_name="detect_schema_drift",
            status="error",
            passed=None,
            table=table,
            threshold=0.0,
            query_id=response.query_id,
            error={"type": "execution", "message": str(exc)},
        )

    added_columns = sorted(set(current_schema) - set(previous_schema))
    removed_columns = sorted(set(previous_schema) - set(current_schema))
    type_changes = [
        {
            "column": column,
            "previous_type": previous_schema[column],
            "current_type": current_schema[column],
        }
        for column in sorted(set(previous_schema) & set(current_schema))
        if previous_schema[column] != current_schema[column]
    ]
    changed_columns = [
        *added_columns,
        *removed_columns,
        *(change["column"] for change in type_changes),
    ]
    drift_count = len(changed_columns)
    finding = (
        f"{table} schema is unchanged between versions {previous_version} and "
        f"{current_version}"
        if drift_count == 0
        else f"{table} schema has {drift_count} changed field(s) between versions "
        f"{previous_version} and {current_version}"
    )
    return DataQualityCheckResult(
        check_name="detect_schema_drift",
        status="success",
        passed=drift_count == 0,
        table=table,
        columns=changed_columns,
        observed_value=float(drift_count),
        threshold=0.0,
        query_id=response.query_id,
        evidence=[
            DataQualityEvidence(
                finding=finding,
                query_id=response.query_id,
                details={
                    "previous_version": int(previous_version),
                    "previous_effective_at": str(previous_effective_at),
                    "current_version": int(current_version),
                    "current_effective_at": str(current_effective_at),
                    "added_columns": added_columns,
                    "removed_columns": removed_columns,
                    "type_changes": type_changes,
                    "drift_count": drift_count,
                },
            )
        ],
    )


def detect_distribution_drift(
    database_path: str | Path,
    table: str,
    column: str,
    time_column: str,
    *,
    baseline_start: datetime,
    baseline_end: datetime,
    current_start: datetime,
    current_end: datetime,
    threshold: float = 0.1,
    incident_id: str | None = None,
    trace_id: str | None = None,
    audit_path: str | Path | None = None,
    timeout_seconds: float = 10.0,
) -> DataQualityCheckResult:
    """Detect categorical distribution drift using total variation distance.

    Both windows are half-open intervals, ``[start, end)``. Categories present
    in only one window are assigned zero probability in the other before
    calculating TVD, so renamed event values are treated as drift.
    """

    try:
        quoted_table = _quote_identifier(table, label="table")
        quoted_column = _quote_identifier(column, label="column")
        quoted_time_column = _quote_identifier(time_column, label="time_column")
        normalized_baseline_start, normalized_baseline_end = _validate_time_window(
            baseline_start, baseline_end, label="baseline"
        )
        normalized_current_start, normalized_current_end = _validate_time_window(
            current_start, current_end, label="current"
        )
        checked_threshold = _validate_rate_threshold(threshold)
    except ValueError as exc:
        return _invalid_input_result(
            check_name="detect_distribution_drift",
            table=table,
            column=column,
            columns=[value for value in (column, time_column) if isinstance(value, str)],
            threshold=threshold,
            message=str(exc),
        )

    baseline_start_sql = _duckdb_timestamp_literal(normalized_baseline_start)
    baseline_end_sql = _duckdb_timestamp_literal(normalized_baseline_end)
    current_start_sql = _duckdb_timestamp_literal(normalized_current_start)
    current_end_sql = _duckdb_timestamp_literal(normalized_current_end)
    sql = f"""
        WITH windowed_values AS (
            SELECT
                CASE
                    WHEN {quoted_time_column} >= TIMESTAMP '{baseline_start_sql}'
                     AND {quoted_time_column} < TIMESTAMP '{baseline_end_sql}'
                    THEN 'baseline'
                    ELSE 'current'
                END AS window_name,
                COALESCE(CAST({quoted_column} AS VARCHAR), '<NULL>') AS category
            FROM {quoted_table}
            WHERE ({quoted_time_column} >= TIMESTAMP '{baseline_start_sql}'
               AND {quoted_time_column} < TIMESTAMP '{baseline_end_sql}')
               OR ({quoted_time_column} >= TIMESTAMP '{current_start_sql}'
               AND {quoted_time_column} < TIMESTAMP '{current_end_sql}')
        )
        SELECT window_name, category, COUNT(*) AS row_count
        FROM windowed_values
        GROUP BY window_name, category
        ORDER BY window_name, category
    """
    response = execute_readonly_sql(
        database_path,
        sql,
        incident_id=incident_id,
        trace_id=trace_id,
        audit_path=audit_path,
        timeout_seconds=timeout_seconds,
    )
    if response.status == "error":
        return DataQualityCheckResult(
            check_name="detect_distribution_drift",
            status="error",
            passed=None,
            table=table,
            column=column,
            columns=[column, time_column],
            threshold=checked_threshold,
            query_id=response.query_id,
            error=response.error,
        )
    if response.truncated:
        return DataQualityCheckResult(
            check_name="detect_distribution_drift",
            status="error",
            passed=None,
            table=table,
            column=column,
            columns=[column, time_column],
            threshold=checked_threshold,
            query_id=response.query_id,
            error={
                "type": "execution",
                "message": "distribution query exceeded the result row limit",
            },
        )
    if any(len(row) != 3 for row in response.rows):
        return DataQualityCheckResult(
            check_name="detect_distribution_drift",
            status="error",
            passed=None,
            table=table,
            column=column,
            columns=[column, time_column],
            threshold=checked_threshold,
            query_id=response.query_id,
            error={
                "type": "execution",
                "message": "distribution query returned an unexpected result shape",
            },
        )

    distributions: dict[str, dict[str, int]] = {"baseline": {}, "current": {}}
    for window_name, category, row_count in response.rows:
        if window_name not in distributions or not isinstance(category, str):
            return DataQualityCheckResult(
                check_name="detect_distribution_drift",
                status="error",
                passed=None,
                table=table,
                column=column,
                columns=[column, time_column],
                threshold=checked_threshold,
                query_id=response.query_id,
                error={
                    "type": "execution",
                    "message": "distribution query returned invalid category data",
                },
            )
        distributions[window_name][category] = int(row_count)

    baseline_total = sum(distributions["baseline"].values())
    current_total = sum(distributions["current"].values())
    if baseline_total == 0 or current_total == 0:
        missing_window = "baseline" if baseline_total == 0 else "current"
        return DataQualityCheckResult(
            check_name="detect_distribution_drift",
            status="error",
            passed=None,
            table=table,
            column=column,
            columns=[column, time_column],
            threshold=checked_threshold,
            query_id=response.query_id,
            error={
                "type": "execution",
                "message": f"{missing_window} window contains no rows",
            },
        )

    categories = sorted(set(distributions["baseline"]) | set(distributions["current"]))
    baseline_distribution = {
        category: distributions["baseline"].get(category, 0) / baseline_total
        for category in categories
    }
    current_distribution = {
        category: distributions["current"].get(category, 0) / current_total
        for category in categories
    }
    total_variation_distance = 0.5 * sum(
        abs(current_distribution[category] - baseline_distribution[category])
        for category in categories
    )
    return DataQualityCheckResult(
        check_name="detect_distribution_drift",
        status="success",
        passed=total_variation_distance <= checked_threshold,
        table=table,
        column=column,
        columns=[column, time_column],
        observed_value=total_variation_distance,
        threshold=checked_threshold,
        query_id=response.query_id,
        evidence=[
            DataQualityEvidence(
                finding=(
                    f"{table}.{column} total variation distance between the "
                    f"baseline and current windows is {total_variation_distance:.4f}"
                ),
                query_id=response.query_id,
                details={
                    "baseline_window": {
                        "start": normalized_baseline_start.isoformat(),
                        "end": normalized_baseline_end.isoformat(),
                        "row_count": baseline_total,
                        "distribution": baseline_distribution,
                    },
                    "current_window": {
                        "start": normalized_current_start.isoformat(),
                        "end": normalized_current_end.isoformat(),
                        "row_count": current_total,
                        "distribution": current_distribution,
                    },
                    "total_variation_distance": total_variation_distance,
                },
            )
        ],
    )
