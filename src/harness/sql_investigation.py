"""One controlled path for executing, validating, and recording SQL evidence."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict

from config.metrics import (
    DEFAULT_METRICS_PATH,
    MetricDefinition,
    load_metrics_config,
)
from harness.evidence import bind_sql_validation_to_incident
from harness.state import IncidentState
from tools.sql_runner import SqlExecutionResponse, execute_readonly_sql
from validators.sql_result import (
    MetricAggregation,
    NumericRange,
    SqlResultExpectation,
    SqlResultValidation,
    validate_sql_result,
    validate_sql_result_with_cardinality,
)


class MetricValidationError(ValueError):
    """Raised when a metric cannot be translated into a SQL result contract."""


class ValidatedSqlExecution(BaseModel):
    """The SQL Runner response and its mandatory validation outcome."""

    model_config = ConfigDict(extra="forbid")

    response: SqlExecutionResponse
    validation: SqlResultValidation


def metric_to_sql_result_expectation(
    metric: MetricDefinition,
) -> SqlResultExpectation:
    """Build a validator contract from the canonical metric configuration."""

    try:
        aggregation = MetricAggregation(metric.aggregation)
    except ValueError as exc:
        raise MetricValidationError(
            f"metric {metric.id} has unsupported aggregation {metric.aggregation!r}"
        ) from exc

    return SqlResultExpectation(
        required_columns=list(metric.validation.expected_column_types),
        expected_column_types=metric.validation.expected_column_types,
        numeric_ranges={
            column: NumericRange(
                minimum=value_range.minimum,
                maximum=value_range.maximum,
            )
            for column, value_range in metric.validation.numeric_ranges.items()
        },
        expected_aggregation=aggregation,
        max_result_rows=metric.validation.max_result_rows,
    )


def load_metric_sql_result_expectation(
    metric_id: str,
    metrics_path: str | Path = DEFAULT_METRICS_PATH,
) -> SqlResultExpectation:
    """Load one metric's validator contract from the canonical YAML config."""

    if not metric_id.strip():
        raise MetricValidationError("metric_id must not be blank")
    config = load_metrics_config(metrics_path)
    metric = next((item for item in config.metrics if item.id == metric_id), None)
    if metric is None:
        raise MetricValidationError(f"unknown metric: {metric_id}")
    return metric_to_sql_result_expectation(metric)


def execute_validated_sql(
    state: IncidentState,
    database_path: str | Path,
    sql: str,
    *,
    metric_id: str | None = None,
    metrics_path: str | Path = DEFAULT_METRICS_PATH,
    expectation: SqlResultExpectation | None = None,
    finding: str | None = None,
    incident_id: str | None = None,
    trace_id: str | None = None,
    audit_path: str | Path | None = None,
    timeout_seconds: float = 10.0,
    max_rows: int = 1000,
) -> ValidatedSqlExecution:
    """Execute a read-only query and always record its validated outcome.

    Callers may provide a metric id for a canonical config-derived contract, or
    a custom expectation for non-metric diagnostic SQL. Passing neither still
    enforces execution, completeness, empty-result, and evidence usability
    checks; callers cannot use this entry point to obtain an unvalidated result.
    """

    if metric_id is not None and expectation is not None:
        raise ValueError("provide either metric_id or expectation, not both")
    resolved_expectation = (
        load_metric_sql_result_expectation(metric_id, metrics_path)
        if metric_id is not None
        else expectation or SqlResultExpectation()
    )
    response = execute_readonly_sql(
        database_path,
        sql,
        incident_id=incident_id,
        trace_id=trace_id,
        audit_path=audit_path,
        timeout_seconds=timeout_seconds,
        max_rows=max_rows,
    )
    if resolved_expectation.max_result_rows is None:
        validation = validate_sql_result(response, resolved_expectation, sql=sql)
    else:
        validation = validate_sql_result_with_cardinality(
            database_path,
            sql,
            response,
            resolved_expectation,
            incident_id=incident_id,
            trace_id=trace_id,
            audit_path=audit_path,
            timeout_seconds=timeout_seconds,
        )
    bind_sql_validation_to_incident(state, response, validation, finding=finding)
    return ValidatedSqlExecution(response=response, validation=validation)


__all__ = [
    "MetricValidationError",
    "ValidatedSqlExecution",
    "execute_validated_sql",
    "load_metric_sql_result_expectation",
    "metric_to_sql_result_expectation",
]
