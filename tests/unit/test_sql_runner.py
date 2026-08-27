import json
from pathlib import Path

import duckdb
import pytest

from tools.sql_runner import (
    SqlExecutionError,
    SqlTimeoutError,
    SqlValidationError,
    execute_readonly_sql,
    run_readonly_sql,
    validate_readonly_sql,
)


@pytest.fixture
def database_path(tmp_path: Path) -> Path:
    path = tmp_path / "test.duckdb"
    with duckdb.connect(str(path)) as connection:
        connection.execute("CREATE TABLE numbers AS SELECT * FROM range(5) t(value)")
    return path


@pytest.mark.parametrize(
    ("sql", "expected_type"),
    [
        ("SELECT 1", "SELECT"),
        ("WITH value AS (SELECT 1 AS n) SELECT n FROM value", "SELECT"),
        ("DESCRIBE numbers", "DESCRIBE"),
        ("DESCRIBE SELECT * FROM numbers", "DESCRIBE"),
        ("EXPLAIN SELECT * FROM numbers", "EXPLAIN"),
        ("EXPLAIN ANALYZE SELECT * FROM numbers", "EXPLAIN"),
        ("EXPLAIN (ANALYZE true) SELECT * FROM numbers", "EXPLAIN"),
    ],
)
def test_validator_allows_supported_readonly_statements(
    sql: str, expected_type: str
) -> None:
    assert validate_readonly_sql(sql) == expected_type


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM numbers",
        "UPDATE numbers SET value = 0",
        "INSERT INTO numbers VALUES (6)",
        "DROP TABLE numbers",
        "ALTER TABLE numbers ADD COLUMN other INTEGER",
        "TRUNCATE numbers",
        "COPY numbers TO 'numbers.csv'",
        "ATTACH 'other.duckdb' AS other",
        "PRAGMA version",
        "SHOW TABLES",
        "/* looks safe: SELECT 1 */ DELETE FROM numbers",
        "SELECT 1; DELETE FROM numbers",
        "WITH value AS (SELECT 1) DELETE FROM numbers",
        "WITH removed AS (DELETE FROM numbers RETURNING *) SELECT * FROM removed",
        "SELECT * INTO copied_numbers FROM numbers",
        "EXPLAIN DELETE FROM numbers",
        "EXPLAIN ANALYZE DELETE FROM numbers",
        "EXPLAIN (ANALYZE true) DELETE FROM numbers",
    ],
)
def test_validator_rejects_writes_and_unsupported_statements(sql: str) -> None:
    with pytest.raises(SqlValidationError) as error:
        validate_readonly_sql(sql)

    assert error.value.query_id


def test_runner_returns_bounded_rows_and_query_id(database_path: Path) -> None:
    result = run_readonly_sql(
        database_path,
        "SELECT value FROM numbers ORDER BY value",
        max_rows=3,
    )

    assert result.query_id
    assert result.statement_type == "SELECT"
    assert result.columns == ["value"]
    assert result.column_types == ["BIGINT"]
    assert result.rows == [[0], [1], [2]]
    assert result.row_count == 3
    assert result.truncated is True
    assert result.duration_ms >= 0


def test_runner_reports_exact_duckdb_column_types(database_path: Path) -> None:
    result = run_readonly_sql(
        database_path,
        "SELECT COUNT(*) AS total, CAST('2026-01-01' AS DATE) AS day, "
        "1.5::DOUBLE AS ratio, 'ok'::VARCHAR AS label",
    )

    assert result.columns == ["total", "day", "ratio", "label"]
    assert result.column_types == ["BIGINT", "DATE", "DOUBLE", "VARCHAR"]


def test_runner_disables_external_file_access(
    database_path: Path, tmp_path: Path
) -> None:
    csv_path = tmp_path / "private.csv"
    csv_path.write_text("value\nsecret\n", encoding="utf-8")

    with pytest.raises(SqlExecutionError) as error:
        run_readonly_sql(
            database_path, f"SELECT * FROM read_csv('{csv_path.as_posix()}')"
        )

    assert error.value.query_id
    assert "cannot access file" in str(error.value).lower()


def test_runner_interrupts_queries_at_timeout(database_path: Path) -> None:
    with pytest.raises(SqlTimeoutError) as error:
        run_readonly_sql(
            database_path,
            "SELECT SUM(value) FROM range(100000000000) t(value)",
            timeout_seconds=0.01,
        )

    assert error.value.query_id


def test_rejected_write_does_not_change_database(database_path: Path) -> None:
    with pytest.raises(SqlValidationError):
        run_readonly_sql(database_path, "DELETE FROM numbers")

    with duckdb.connect(str(database_path), read_only=True) as connection:
        assert connection.execute("SELECT COUNT(*) FROM numbers").fetchone() == (5,)


def test_structured_execution_response_and_audit(database_path: Path, tmp_path: Path) -> None:
    audit_path = tmp_path / "audit" / "query_audit.jsonl"
    response = execute_readonly_sql(
        database_path,
        "SELECT value FROM numbers ORDER BY value",
        incident_id="INC-001",
        trace_id="TRACE-001",
        audit_path=audit_path,
        max_rows=2,
    )

    assert response.status == "success"
    assert response.query_id
    assert response.row_count == 2
    assert response.truncated is True
    record = json.loads(audit_path.read_text(encoding="utf-8"))
    assert record["incident_id"] == "INC-001"
    assert record["trace_id"] == "TRACE-001"
    assert record["query_id"] == response.query_id
    assert record["status"] == "success"


def test_structured_execution_response_contains_typed_error(
    database_path: Path, tmp_path: Path
) -> None:
    response = execute_readonly_sql(
        database_path,
        "DELETE FROM numbers",
        audit_path=tmp_path / "query_audit.jsonl",
    )

    assert response.status == "error"
    assert response.statement_type is None
    assert response.column_types == []
    assert response.error is not None
    assert response.error["type"] == "validation"
    assert response.query_id


def test_structured_execution_response_validates_resource_limits(
    database_path: Path,
) -> None:
    response = execute_readonly_sql(database_path, "SELECT 1", max_rows=0)

    assert response.status == "error"
    assert response.error == {
        "type": "validation",
        "message": "max_rows must be greater than zero",
    }
    assert response.query_id
