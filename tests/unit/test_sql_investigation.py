import pytest

from harness.sql_investigation import (
    MetricValidationError,
    load_metric_sql_result_expectation,
)
from validators.sql_result import MetricAggregation


def test_metric_config_builds_daily_active_users_result_expectation() -> None:
    expectation = load_metric_sql_result_expectation("daily_active_users")

    assert expectation.required_columns == ["metric_date", "daily_active_users"]
    assert expectation.expected_column_types == {
        "metric_date": "DATE",
        "daily_active_users": "BIGINT",
    }
    assert expectation.numeric_ranges["daily_active_users"].minimum == 0
    assert expectation.expected_aggregation is MetricAggregation.COUNT_DISTINCT
    assert expectation.max_result_rows == 366


def test_metric_config_builds_ratio_range_and_aggregation() -> None:
    expectation = load_metric_sql_result_expectation("conversion_rate")

    assert expectation.expected_aggregation is MetricAggregation.RATIO
    assert expectation.numeric_ranges["conversion_rate"].minimum == 0
    assert expectation.numeric_ranges["conversion_rate"].maximum == 1
    assert expectation.expected_column_types["conversion_rate"] == "DOUBLE"


def test_metric_result_expectation_rejects_unknown_metric() -> None:
    with pytest.raises(MetricValidationError, match="unknown metric"):
        load_metric_sql_result_expectation("unknown_metric")
