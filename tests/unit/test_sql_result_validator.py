from pathlib import Path

import duckdb

from tools.sql_runner import SqlExecutionResponse, execute_readonly_sql
from validators.sql_result import (
    MetricAggregation,
    NumericRange,
    SqlResultExpectation,
    SqlResultFailureReason,
    validate_sql_result,
    validate_sql_result_with_cardinality,
)


def _success_response(**overrides: object) -> SqlExecutionResponse:
    payload: dict[str, object] = {
        "query_id": "query-123",
        "status": "success",
        "statement_type": "SELECT",
        "columns": ["metric_date", "daily_active_users"],
        "column_types": ["DATE", "INTEGER"],
        "rows": [["2026-08-12", 7600]],
        "row_count": 1,
        "truncated": False,
        "duration_ms": 4.2,
    }
    payload.update(overrides)
    return SqlExecutionResponse.model_validate(payload)


def test_accepts_nonempty_result_with_expected_schema() -> None:
    result = validate_sql_result(
        _success_response(),
        SqlResultExpectation(
            required_columns=["metric_date", "daily_active_users"]
        ),
    )

    assert result.passed is True
    assert result.reason is None
    assert result.evidence.usable is True
    assert result.evidence.query_id == "query-123"
    assert result.evidence.columns == ["metric_date", "daily_active_users"]
    assert result.evidence.column_types == ["DATE", "INTEGER"]
    assert result.evidence.row_count == 1


def test_validates_response_returned_by_sql_runner(tmp_path: Path) -> None:
    database_path = tmp_path / "validator.duckdb"
    with duckdb.connect(str(database_path)) as connection:
        connection.execute("CREATE TABLE events (event_id INTEGER)")
        connection.execute("INSERT INTO events VALUES (1)")

    response = execute_readonly_sql(
        database_path,
        "SELECT event_id FROM events",
    )
    result = validate_sql_result(
        response,
        SqlResultExpectation(required_columns=["event_id"]),
    )

    assert response.status == "success"
    assert result.passed is True
    assert result.evidence.query_id == response.query_id


def test_rejects_empty_result_and_marks_evidence_unusable() -> None:
    result = validate_sql_result(
        _success_response(rows=[], row_count=0),
        SqlResultExpectation(required_columns=["metric_date"]),
    )

    assert result.passed is False
    assert result.reason == SqlResultFailureReason.EMPTY_RESULT
    assert result.evidence.usable is False
    assert result.evidence.query_id == "query-123"


def test_rejects_missing_required_columns_and_reports_them() -> None:
    result = validate_sql_result(
        _success_response(),
        SqlResultExpectation(required_columns=["metric_date", "expected_value"]),
    )

    assert result.passed is False
    assert result.reason == SqlResultFailureReason.MISSING_REQUIRED_COLUMNS
    assert result.missing_columns == ["expected_value"]
    assert result.evidence.usable is False


def test_rejects_column_with_unexpected_type() -> None:
    result = validate_sql_result(
        _success_response(),
        SqlResultExpectation(
            expected_column_types={"daily_active_users": "VARCHAR"}
        ),
    )

    assert result.passed is False
    assert result.reason == SqlResultFailureReason.COLUMN_TYPE_MISMATCH
    assert result.type_violations[0].column == "daily_active_users"
    assert result.type_violations[0].expected_type == "VARCHAR"
    assert result.type_violations[0].actual_type == "INTEGER"
    assert result.evidence.usable is False


def test_rejects_response_without_type_for_every_column() -> None:
    result = validate_sql_result(_success_response(column_types=[]))

    assert result.passed is False
    assert result.reason == SqlResultFailureReason.INVALID_SCHEMA_METADATA
    assert result.evidence.usable is False


def test_rejects_row_with_wrong_number_of_values() -> None:
    result = validate_sql_result(
        _success_response(rows=[["2026-08-12"]]),
        SqlResultExpectation(required_columns=["daily_active_users"]),
    )

    assert result.passed is False
    assert result.reason == SqlResultFailureReason.INVALID_ROW_SHAPE
    assert result.row_shape_violations[0].row_index == 0
    assert result.row_shape_violations[0].expected_column_count == 2
    assert result.row_shape_violations[0].actual_column_count == 1
    assert result.evidence.usable is False


def test_rejects_response_with_inconsistent_row_count() -> None:
    result = validate_sql_result(_success_response(row_count=2))

    assert result.passed is False
    assert result.reason == SqlResultFailureReason.INVALID_ROW_SHAPE
    assert result.row_shape_violations[0].row_index is None
    assert result.evidence.usable is False


def test_rejects_metric_value_outside_declared_range() -> None:
    result = validate_sql_result(
        _success_response(rows=[["2026-08-12", -1]]),
        SqlResultExpectation(
            numeric_ranges={"daily_active_users": NumericRange(minimum=0)}
        ),
    )

    assert result.passed is False
    assert result.reason == SqlResultFailureReason.NUMERIC_VALUE_OUT_OF_RANGE
    assert result.value_violations[0].column == "daily_active_users"
    assert result.value_violations[0].observed_value == -1.0
    assert result.evidence.usable is False


def test_rejects_non_numeric_metric_value() -> None:
    result = validate_sql_result(
        _success_response(rows=[["2026-08-12", "not-a-number"]]),
        SqlResultExpectation(
            numeric_ranges={"daily_active_users": NumericRange(minimum=0)}
        ),
    )

    assert result.passed is False
    assert result.reason == SqlResultFailureReason.NON_NUMERIC_VALUE
    assert result.value_violations[0].observed_value == "not-a-number"
    assert result.evidence.usable is False


def test_rejects_cross_join_in_original_sql() -> None:
    result = validate_sql_result(
        _success_response(),
        sql="SELECT * FROM events CROSS JOIN users",
    )

    assert result.passed is False
    assert result.reason == SqlResultFailureReason.CROSS_JOIN_DETECTED
    assert result.evidence.usable is False


def test_rejects_join_without_join_condition_in_original_sql() -> None:
    result = validate_sql_result(
        _success_response(),
        sql="SELECT * FROM events JOIN users",
    )

    assert result.passed is False
    assert result.reason == SqlResultFailureReason.MISSING_JOIN_CONDITION
    assert result.evidence.usable is False


def test_accepts_join_with_condition_and_expected_distinct_count() -> None:
    result = validate_sql_result(
        _success_response(),
        SqlResultExpectation(
            expected_aggregation=MetricAggregation.COUNT_DISTINCT
        ),
        sql=(
            "SELECT COUNT(DISTINCT events.user_id) AS daily_active_users "
            "FROM events JOIN users ON events.user_id = users.user_id"
        ),
    )

    assert result.passed is True
    assert result.evidence.usable is True
    assert result.evidence.ast_validated is True


def test_rejects_query_that_does_not_match_expected_aggregation() -> None:
    result = validate_sql_result(
        _success_response(),
        SqlResultExpectation(
            expected_aggregation=MetricAggregation.COUNT_DISTINCT
        ),
        sql="SELECT COUNT(user_id) AS daily_active_users FROM events",
    )

    assert result.passed is False
    assert result.reason == SqlResultFailureReason.AGGREGATION_MISMATCH
    assert result.evidence.usable is False


def test_accepts_ratio_aggregation() -> None:
    result = validate_sql_result(
        _success_response(),
        SqlResultExpectation(expected_aggregation=MetricAggregation.RATIO),
        sql=(
            "SELECT COUNT(user_id)::DOUBLE / NULLIF(COUNT(*), 0) "
            "AS conversion_rate FROM events"
        ),
    )

    assert result.passed is True
    assert result.evidence.ast_validated is True


def test_accepts_average_after_group_sum_aggregation() -> None:
    result = validate_sql_result(
        _success_response(),
        SqlResultExpectation(
            expected_aggregation=MetricAggregation.AVERAGE_AFTER_GROUP_SUM
        ),
        sql=(
            "SELECT AVG(session_duration) FROM ("
            "SELECT user_id, session_id, SUM(duration_seconds) AS session_duration "
            "FROM events GROUP BY user_id, session_id"
            ") AS sessions"
        ),
    )

    assert result.passed is True
    assert result.evidence.ast_validated is True


def test_accepts_result_within_full_row_count_limit(tmp_path: Path) -> None:
    database_path = tmp_path / "cardinality.duckdb"
    with duckdb.connect(str(database_path)) as connection:
        connection.execute("CREATE TABLE events (event_id INTEGER)")
        connection.execute("INSERT INTO events VALUES (1), (2)")

    sql = "SELECT event_id FROM events"
    response = execute_readonly_sql(database_path, sql)
    result = validate_sql_result_with_cardinality(
        database_path,
        sql,
        response,
        SqlResultExpectation(max_result_rows=2),
    )

    assert result.passed is True
    assert result.evidence.usable is True
    assert result.evidence.cardinality is not None
    assert result.evidence.cardinality.observed_row_count == 2
    assert result.evidence.cardinality.count_query_id


def test_rejects_join_expansion_that_exceeds_full_row_count_limit(tmp_path: Path) -> None:
    database_path = tmp_path / "join-expansion.duckdb"
    with duckdb.connect(str(database_path)) as connection:
        connection.execute("CREATE TABLE events (event_id INTEGER, user_id INTEGER)")
        connection.execute(
            "CREATE TABLE assignments (user_id INTEGER, variant VARCHAR)"
        )
        connection.execute("INSERT INTO events VALUES (1, 10), (2, 20)")
        connection.execute(
            "INSERT INTO assignments VALUES (10, 'a'), (10, 'b'), (20, 'a'), (20, 'b')"
        )

    sql = (
        "SELECT events.event_id, assignments.variant FROM events "
        "JOIN assignments ON events.user_id = assignments.user_id"
    )
    response = execute_readonly_sql(database_path, sql)
    result = validate_sql_result_with_cardinality(
        database_path,
        sql,
        response,
        SqlResultExpectation(max_result_rows=2),
    )

    assert response.status == "success"
    assert result.passed is False
    assert result.reason == SqlResultFailureReason.RESULT_ROW_COUNT_EXCEEDED
    assert result.evidence.usable is False
    assert result.evidence.cardinality is not None
    assert result.evidence.cardinality.observed_row_count == 4
    assert result.evidence.cardinality.maximum_row_count == 2


def test_marks_failed_execution_as_unusable_evidence() -> None:
    response = SqlExecutionResponse(
        query_id="query-456",
        status="error",
        error={"type": "execution", "message": "missing table"},
    )

    result = validate_sql_result(response)

    assert result.passed is False
    assert result.reason == SqlResultFailureReason.SQL_EXECUTION_FAILED
    assert result.evidence.usable is False
    assert result.evidence.error == {
        "type": "execution",
        "message": "missing table",
    }


def test_marks_truncated_result_as_unusable_evidence() -> None:
    result = validate_sql_result(_success_response(truncated=True))

    assert result.passed is False
    assert result.reason == SqlResultFailureReason.RESULT_TRUNCATED
    assert result.evidence.usable is False
