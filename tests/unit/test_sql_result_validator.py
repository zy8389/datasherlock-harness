from __future__ import annotations

from typing import Any

import pytest

from tools.sql_runner import SqlExecutionResponse
from validators.sql_result import (
    MetricAggregation,
    NumericRange,
    SqlResultExpectation,
    SqlResultFailureReason,
    validate_sql_result,
)


def _success_response(**overrides: Any) -> SqlExecutionResponse:
    payload: dict[str, Any] = {
        "query_id": "Q-VALIDATOR-001",
        "status": "success",
        "statement_type": "SELECT",
        "columns": ["metric_date", "daily_active_users"],
        "column_types": ["DATE", "BIGINT"],
        "rows": [["2026-08-12", 7600]],
        "row_count": 1,
        "truncated": False,
        "duration_ms": 4.2,
    }
    payload.update(overrides)
    return SqlExecutionResponse.model_validate(payload)


def _expectation(**overrides: Any) -> SqlResultExpectation:
    payload: dict[str, Any] = {
        "required_columns": ["metric_date", "daily_active_users"],
        "expected_column_types": {
            "metric_date": "DATE",
            "daily_active_users": "BIGINT",
        },
        "numeric_ranges": {
            "daily_active_users": NumericRange(minimum=0),
        },
    }
    payload.update(overrides)
    return SqlResultExpectation.model_validate(payload)


def test_normal_result_passes_without_creating_evidence_reference() -> None:
    result = validate_sql_result(_success_response(), _expectation())

    assert result.passed is True
    assert result.reason is None
    assert result.evidence.usable is True
    assert result.evidence.query_id == "Q-VALIDATOR-001"
    assert result.evidence.column_types == ["DATE", "BIGINT"]


def test_execution_failure_is_not_usable() -> None:
    result = validate_sql_result(
        SqlExecutionResponse(
            query_id="Q-FAIL",
            status="error",
            error={"type": "execution", "message": "missing table"},
        )
    )

    assert result.passed is False
    assert result.reason is SqlResultFailureReason.SQL_EXECUTION_FAILED
    assert result.evidence.usable is False
    assert result.evidence.error == {
        "type": "execution",
        "message": "missing table",
    }


def test_empty_result_requires_explicit_opt_in() -> None:
    result = validate_sql_result(_success_response(rows=[], row_count=0), _expectation())
    allowed = validate_sql_result(
        _success_response(rows=[], row_count=0),
        _expectation(allow_empty=True),
    )

    assert result.reason is SqlResultFailureReason.EMPTY_RESULT
    assert result.evidence.usable is False
    assert allowed.passed is True


def test_truncated_result_is_never_usable() -> None:
    result = validate_sql_result(_success_response(truncated=True), _expectation())

    assert result.reason is SqlResultFailureReason.RESULT_TRUNCATED
    assert result.evidence.usable is False


def test_missing_required_column_is_reported() -> None:
    result = validate_sql_result(
        _success_response(),
        SqlResultExpectation(required_columns=["missing_column"]),
    )

    assert result.reason is SqlResultFailureReason.MISSING_REQUIRED_COLUMNS
    assert result.missing_columns == ["missing_column"]


def test_schema_metadata_requires_one_type_per_column() -> None:
    result = validate_sql_result(_success_response(column_types=[]))

    assert result.reason is SqlResultFailureReason.INVALID_SCHEMA_METADATA
    assert result.evidence.usable is False


def test_every_row_must_match_the_declared_column_shape() -> None:
    wrong_width = validate_sql_result(
        _success_response(rows=[["2026-08-12"]]),
        _expectation(),
    )
    wrong_count = validate_sql_result(_success_response(row_count=2), _expectation())

    assert wrong_width.reason is SqlResultFailureReason.INVALID_ROW_SHAPE
    assert wrong_width.row_shape_violations[0].row_index == 0
    assert wrong_count.reason is SqlResultFailureReason.INVALID_ROW_SHAPE
    assert wrong_count.row_shape_violations[0].row_index is None


def test_column_types_are_explicit_and_not_numeric_coercions() -> None:
    result = validate_sql_result(
        _success_response(),
        _expectation(expected_column_types={"daily_active_users": "INTEGER"}),
    )

    assert result.reason is SqlResultFailureReason.COLUMN_TYPE_MISMATCH
    assert result.type_violations[0].actual_type == "BIGINT"
    assert result.type_violations[0].expected_type == "INTEGER"


@pytest.mark.parametrize(
    ("value", "reason"),
    [
        ("not-a-number", SqlResultFailureReason.NON_NUMERIC_VALUE),
        (float("nan"), SqlResultFailureReason.NON_NUMERIC_VALUE),
    ],
)
def test_numeric_range_rejects_non_numeric_and_nan(value: object, reason) -> None:
    result = validate_sql_result(
        _success_response(rows=[["2026-08-12", value]]),
        _expectation(),
    )

    assert result.reason is reason
    assert result.evidence.usable is False


@pytest.mark.parametrize("value", [-1, 2])
def test_numeric_range_is_inclusive_but_rejects_outside_values(value: int) -> None:
    result = validate_sql_result(
        _success_response(rows=[["2026-08-12", value]]),
        _expectation(
            numeric_ranges={
                "daily_active_users": NumericRange(minimum=0, maximum=1)
            }
        ),
    )

    assert result.reason is SqlResultFailureReason.NUMERIC_VALUE_OUT_OF_RANGE
    assert result.value_violations[0].observed_value == float(value)


@pytest.mark.parametrize(
    ("sql", "reason"),
    [
        (
            "SELECT * FROM events CROSS JOIN users",
            SqlResultFailureReason.CROSS_JOIN_DETECTED,
        ),
        (
            "SELECT * FROM events JOIN users",
            SqlResultFailureReason.MISSING_JOIN_CONDITION,
        ),
        ("SELECT FROM", SqlResultFailureReason.INVALID_SQL_FOR_AST_VALIDATION),
    ],
)
def test_sql_ast_safety_reasons(sql: str, reason: SqlResultFailureReason) -> None:
    result = validate_sql_result(_success_response(), _expectation(), sql=sql)

    assert result.reason is reason
    assert result.evidence.usable is False


def test_aggregation_validation_supports_count_distinct() -> None:
    result = validate_sql_result(
        _success_response(),
        _expectation(expected_aggregation=MetricAggregation.COUNT_DISTINCT),
        sql=(
            "SELECT COUNT(DISTINCT user_id) AS daily_active_users "
            "FROM events"
        ),
    )

    assert result.passed is True
    assert result.evidence.ast_validated is True


@pytest.mark.parametrize(
    ("aggregation", "sql"),
    [
        (MetricAggregation.COUNT, "SELECT COUNT(user_id) FROM events"),
        (MetricAggregation.SUM, "SELECT SUM(value) FROM events"),
        (MetricAggregation.AVERAGE, "SELECT AVG(value) FROM events"),
        (
            MetricAggregation.RATIO,
            "SELECT COUNT(user_id)::DOUBLE / NULLIF(COUNT(*), 0) FROM events",
        ),
        (
            MetricAggregation.AVERAGE_AFTER_GROUP_SUM,
            (
                "SELECT AVG(session_duration) FROM ("
                "SELECT user_id, SUM(duration) AS session_duration "
                "FROM events GROUP BY user_id"
                ") AS sessions"
            ),
        ),
    ],
)
def test_supported_aggregations_are_recognized(
    aggregation: MetricAggregation, sql: str
) -> None:
    result = validate_sql_result(
        _success_response(),
        _expectation(expected_aggregation=aggregation),
        sql=sql,
    )

    assert result.passed is True


def test_aggregation_mismatch_is_reported() -> None:
    result = validate_sql_result(
        _success_response(),
        _expectation(expected_aggregation=MetricAggregation.COUNT_DISTINCT),
        sql="SELECT COUNT(user_id) AS daily_active_users FROM events",
    )

    assert result.reason is SqlResultFailureReason.AGGREGATION_MISMATCH


def test_row_count_limit_uses_the_existing_response_without_a_count_query() -> None:
    result = validate_sql_result(
        _success_response(
            rows=[["2026-08-12", 1], ["2026-08-13", 2]],
            row_count=2,
        ),
        _expectation(max_result_rows=1),
    )

    assert result.reason is SqlResultFailureReason.RESULT_ROW_COUNT_EXCEEDED
