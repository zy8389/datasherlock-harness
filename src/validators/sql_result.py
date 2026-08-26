"""Validate whether a read-only SQL response is usable as investigation evidence."""

from __future__ import annotations

from enum import StrEnum
from numbers import Real
from pathlib import Path

import sqlglot
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlglot import exp
from sqlglot.errors import SqlglotError

from tools.sql_runner import SqlExecutionResponse, execute_readonly_sql


class SqlResultFailureReason(StrEnum):
    """Stable reason codes for results that cannot support an investigation."""

    SQL_EXECUTION_FAILED = "sql_execution_failed"
    RESULT_TRUNCATED = "result_truncated"
    EMPTY_RESULT = "empty_result"
    MISSING_REQUIRED_COLUMNS = "missing_required_columns"
    INVALID_SCHEMA_METADATA = "invalid_schema_metadata"
    INVALID_ROW_SHAPE = "invalid_row_shape"
    COLUMN_TYPE_MISMATCH = "column_type_mismatch"
    NON_NUMERIC_VALUE = "non_numeric_value"
    NUMERIC_VALUE_OUT_OF_RANGE = "numeric_value_out_of_range"
    INVALID_SQL_FOR_AST_VALIDATION = "invalid_sql_for_ast_validation"
    CROSS_JOIN_DETECTED = "cross_join_detected"
    MISSING_JOIN_CONDITION = "missing_join_condition"
    AGGREGATION_MISMATCH = "aggregation_mismatch"
    CARDINALITY_CHECK_FAILED = "cardinality_check_failed"
    RESULT_ROW_COUNT_EXCEEDED = "result_row_count_exceeded"


class MetricAggregation(StrEnum):
    """Metric aggregation structures supported by the SQL AST validator."""

    COUNT = "count"
    COUNT_DISTINCT = "count_distinct"
    SUM = "sum"
    AVERAGE = "average"
    RATIO = "ratio"
    AVERAGE_AFTER_GROUP_SUM = "average_after_group_sum"


class NumericRange(BaseModel):
    """Inclusive numeric bounds for a result column."""

    model_config = ConfigDict(extra="forbid")

    minimum: float | None = None
    maximum: float | None = None

    @model_validator(mode="after")
    def require_valid_bounds(self) -> NumericRange:
        if self.minimum is None and self.maximum is None:
            raise ValueError("a numeric range must define a minimum or maximum")
        if (
            self.minimum is not None
            and self.maximum is not None
            and self.minimum > self.maximum
        ):
            raise ValueError("numeric range minimum must not exceed maximum")
        return self


class SqlResultExpectation(BaseModel):
    """Caller-declared contract for one SQL result."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    required_columns: list[str] = Field(default_factory=list)
    expected_column_types: dict[str, str] = Field(default_factory=dict)
    numeric_ranges: dict[str, NumericRange] = Field(default_factory=dict)
    expected_aggregation: MetricAggregation | None = None
    max_result_rows: int | None = Field(default=None, ge=0)
    allow_empty: bool = False

    @field_validator("required_columns")
    @classmethod
    def require_unique_columns(cls, columns: list[str]) -> list[str]:
        if any(not column for column in columns):
            raise ValueError("required_columns must not contain empty values")
        if len(set(columns)) != len(columns):
            raise ValueError("required_columns must not contain duplicates")
        return columns


class SqlResultValueViolation(BaseModel):
    """One value that makes a result unsuitable for metric evidence."""

    model_config = ConfigDict(extra="forbid")

    column: str
    row_index: int = Field(ge=0)
    observed_value: float | str | None
    minimum: float | None = None
    maximum: float | None = None


class SqlResultTypeViolation(BaseModel):
    """One output field whose DuckDB type violates the declared schema."""

    model_config = ConfigDict(extra="forbid")

    column: str
    expected_type: str
    actual_type: str


class SqlResultRowShapeViolation(BaseModel):
    """One response shape inconsistency that prevents safe result inspection."""

    model_config = ConfigDict(extra="forbid")

    row_index: int | None = Field(default=None, ge=0)
    expected_column_count: int = Field(ge=0)
    actual_column_count: int = Field(ge=0)


class SqlResultEvidence(BaseModel):
    """Trace metadata retained for every validation decision."""

    model_config = ConfigDict(extra="forbid")

    query_id: str
    statement_type: str | None = None
    columns: list[str]
    column_types: list[str]
    row_count: int = Field(ge=0)
    truncated: bool
    usable: bool
    ast_validated: bool = False
    cardinality: SqlResultCardinality | None = None
    error: dict[str, str] | None = None


class SqlResultValidation(BaseModel):
    """Normalized pass/fail result produced after a SQL tool call."""

    model_config = ConfigDict(extra="forbid")

    passed: bool
    reason: SqlResultFailureReason | None = None
    missing_columns: list[str] = Field(default_factory=list)
    row_shape_violations: list[SqlResultRowShapeViolation] = Field(
        default_factory=list
    )
    type_violations: list[SqlResultTypeViolation] = Field(default_factory=list)
    value_violations: list[SqlResultValueViolation] = Field(default_factory=list)
    evidence: SqlResultEvidence


class SqlResultCardinality(BaseModel):
    """The full result count measured through a separate read-only query."""

    model_config = ConfigDict(extra="forbid")

    count_query_id: str
    observed_row_count: int | None = Field(default=None, ge=0)
    maximum_row_count: int = Field(ge=0)
    error: dict[str, str] | None = None


def _evidence(
    response: SqlExecutionResponse,
    *,
    usable: bool,
    ast_validated: bool = False,
    cardinality: SqlResultCardinality | None = None,
) -> SqlResultEvidence:
    return SqlResultEvidence(
        query_id=response.query_id,
        statement_type=response.statement_type,
        columns=response.columns,
        column_types=response.column_types,
        row_count=response.row_count,
        truncated=response.truncated,
        usable=usable,
        ast_validated=ast_validated,
        cardinality=cardinality,
        error=response.error,
    )


def _count_query_sql(sql: str) -> str:
    candidate_sql = sql.strip().rstrip(";").rstrip()
    return (
        "SELECT COUNT(*) AS result_row_count "
        f"FROM ({candidate_sql}) AS validation_candidate"
    )


def measure_query_row_count(
    database_path: str | Path,
    sql: str,
    *,
    incident_id: str | None = None,
    trace_id: str | None = None,
    audit_path: str | Path | None = None,
    timeout_seconds: float = 10.0,
) -> SqlExecutionResponse:
    """Measure the complete row count of a SELECT/WITH query through SQL Runner."""

    return execute_readonly_sql(
        database_path,
        _count_query_sql(sql),
        incident_id=incident_id,
        trace_id=trace_id,
        audit_path=audit_path,
        timeout_seconds=timeout_seconds,
        max_rows=1,
    )


def _cardinality_from_response(
    response: SqlExecutionResponse, maximum_row_count: int
) -> SqlResultCardinality:
    if response.status != "success":
        return SqlResultCardinality(
            count_query_id=response.query_id,
            maximum_row_count=maximum_row_count,
            error=response.error
            or {"type": "execution", "message": "row-count query failed"},
        )
    if (
        response.truncated
        or response.columns != ["result_row_count"]
        or len(response.rows) != 1
        or len(response.rows[0]) != 1
    ):
        return SqlResultCardinality(
            count_query_id=response.query_id,
            maximum_row_count=maximum_row_count,
            error={
                "type": "execution",
                "message": "row-count query returned an unexpected result shape",
            },
        )

    observed_value = response.rows[0][0]
    if isinstance(observed_value, bool) or not isinstance(observed_value, int):
        return SqlResultCardinality(
            count_query_id=response.query_id,
            maximum_row_count=maximum_row_count,
            error={
                "type": "execution",
                "message": "row-count query returned a non-integer count",
            },
        )
    return SqlResultCardinality(
        count_query_id=response.query_id,
        observed_row_count=observed_value,
        maximum_row_count=maximum_row_count,
    )


def _has_aggregate(expression: exp.Expression) -> bool:
    return any(
        isinstance(node, (exp.Count, exp.Sum, exp.Avg)) for node in expression.walk()
    )


def _matches_aggregation(
    expression: exp.Expression, expected_aggregation: MetricAggregation
) -> bool:
    if expected_aggregation is MetricAggregation.COUNT:
        return any(
            isinstance(node, exp.Count) and not isinstance(node.this, exp.Distinct)
            for node in expression.walk()
        )
    if expected_aggregation is MetricAggregation.COUNT_DISTINCT:
        return any(
            isinstance(node, exp.Count) and isinstance(node.this, exp.Distinct)
            for node in expression.walk()
        )
    if expected_aggregation is MetricAggregation.SUM:
        return any(isinstance(node, exp.Sum) for node in expression.walk())
    if expected_aggregation is MetricAggregation.AVERAGE:
        return any(isinstance(node, exp.Avg) for node in expression.walk())
    if expected_aggregation is MetricAggregation.RATIO:
        return any(
            _has_aggregate(div.this) and _has_aggregate(div.expression)
            for div in expression.find_all(exp.Div)
        )
    if expected_aggregation is MetricAggregation.AVERAGE_AFTER_GROUP_SUM:
        has_average = any(isinstance(node, exp.Avg) for node in expression.walk())
        has_grouped_sum = any(
            select.args.get("group") is not None
            and any(isinstance(node, exp.Sum) for node in select.walk())
            for select in expression.find_all(exp.Select)
        )
        return has_average and has_grouped_sum
    raise AssertionError(f"unsupported aggregation: {expected_aggregation}")


def _validate_sql_ast(
    sql: str, expectation: SqlResultExpectation
) -> SqlResultFailureReason | None:
    try:
        expression = sqlglot.parse_one(sql, read="duckdb")
    except SqlglotError:
        return SqlResultFailureReason.INVALID_SQL_FOR_AST_VALIDATION

    for join in expression.find_all(exp.Join):
        if join.args.get("kind") == "CROSS":
            return SqlResultFailureReason.CROSS_JOIN_DETECTED
        if join.args.get("on") is None and not join.args.get("using"):
            return SqlResultFailureReason.MISSING_JOIN_CONDITION

    if (
        expectation.expected_aggregation is not None
        and not _matches_aggregation(expression, expectation.expected_aggregation)
    ):
        return SqlResultFailureReason.AGGREGATION_MISMATCH
    return None


def validate_sql_result(
    response: SqlExecutionResponse,
    expectation: SqlResultExpectation | None = None,
    *,
    sql: str | None = None,
) -> SqlResultValidation:
    """Validate a SQL Runner response without executing SQL again.

    A result is usable evidence only when execution succeeded, it is complete,
    it has the expected fields, and it satisfies the caller's empty-result rule.
    Supplying the original SQL additionally validates its join and aggregation AST.
    """

    expectation = expectation or SqlResultExpectation()
    if response.status != "success":
        return SqlResultValidation(
            passed=False,
            reason=SqlResultFailureReason.SQL_EXECUTION_FAILED,
            evidence=_evidence(response, usable=False),
        )
    if response.truncated:
        return SqlResultValidation(
            passed=False,
            reason=SqlResultFailureReason.RESULT_TRUNCATED,
            evidence=_evidence(response, usable=False),
        )
    if response.row_count == 0 and not expectation.allow_empty:
        return SqlResultValidation(
            passed=False,
            reason=SqlResultFailureReason.EMPTY_RESULT,
            evidence=_evidence(response, usable=False),
        )

    available_columns = set(response.columns)
    expected_columns = list(
        dict.fromkeys(
            [
                *expectation.required_columns,
                *expectation.expected_column_types,
                *expectation.numeric_ranges,
            ]
        )
    )
    missing_columns = [
        column
        for column in expected_columns
        if column not in available_columns
    ]
    if missing_columns:
        return SqlResultValidation(
            passed=False,
            reason=SqlResultFailureReason.MISSING_REQUIRED_COLUMNS,
            missing_columns=missing_columns,
            evidence=_evidence(response, usable=False),
        )

    if len(response.column_types) != len(response.columns):
        return SqlResultValidation(
            passed=False,
            reason=SqlResultFailureReason.INVALID_SCHEMA_METADATA,
            evidence=_evidence(response, usable=False),
        )

    if response.row_count != len(response.rows):
        return SqlResultValidation(
            passed=False,
            reason=SqlResultFailureReason.INVALID_ROW_SHAPE,
            row_shape_violations=[
                SqlResultRowShapeViolation(
                    expected_column_count=len(response.columns),
                    actual_column_count=len(response.rows),
                )
            ],
            evidence=_evidence(response, usable=False),
        )
    for row_index, row in enumerate(response.rows):
        if len(row) != len(response.columns):
            return SqlResultValidation(
                passed=False,
                reason=SqlResultFailureReason.INVALID_ROW_SHAPE,
                row_shape_violations=[
                    SqlResultRowShapeViolation(
                        row_index=row_index,
                        expected_column_count=len(response.columns),
                        actual_column_count=len(row),
                    )
                ],
                evidence=_evidence(response, usable=False),
            )

    type_violations = []
    for column, expected_type in expectation.expected_column_types.items():
        actual_type = response.column_types[response.columns.index(column)]
        if actual_type.casefold().replace(" ", "") != expected_type.casefold().replace(
            " ", ""
        ):
            type_violations.append(
                SqlResultTypeViolation(
                    column=column,
                    expected_type=expected_type,
                    actual_type=actual_type,
                )
            )
    if type_violations:
        return SqlResultValidation(
            passed=False,
            reason=SqlResultFailureReason.COLUMN_TYPE_MISMATCH,
            type_violations=type_violations,
            evidence=_evidence(response, usable=False),
        )

    if sql is not None:
        ast_failure = _validate_sql_ast(sql, expectation)
        if ast_failure is not None:
            return SqlResultValidation(
                passed=False,
                reason=ast_failure,
                evidence=_evidence(response, usable=False),
            )

    for column, numeric_range in expectation.numeric_ranges.items():
        column_index = response.columns.index(column)
        for row_index, row in enumerate(response.rows):
            value = row[column_index]
            if isinstance(value, bool) or not isinstance(value, Real):
                violation = SqlResultValueViolation(
                    column=column,
                    row_index=row_index,
                    observed_value=str(value) if value is not None else None,
                    minimum=numeric_range.minimum,
                    maximum=numeric_range.maximum,
                )
                return SqlResultValidation(
                    passed=False,
                    reason=SqlResultFailureReason.NON_NUMERIC_VALUE,
                    value_violations=[violation],
                    evidence=_evidence(response, usable=False),
                )

            numeric_value = float(value)
            below_minimum = (
                numeric_range.minimum is not None
                and numeric_value < numeric_range.minimum
            )
            above_maximum = (
                numeric_range.maximum is not None
                and numeric_value > numeric_range.maximum
            )
            if below_minimum or above_maximum:
                violation = SqlResultValueViolation(
                    column=column,
                    row_index=row_index,
                    observed_value=numeric_value,
                    minimum=numeric_range.minimum,
                    maximum=numeric_range.maximum,
                )
                return SqlResultValidation(
                    passed=False,
                    reason=SqlResultFailureReason.NUMERIC_VALUE_OUT_OF_RANGE,
                    value_violations=[violation],
                    evidence=_evidence(response, usable=False),
                )

    return SqlResultValidation(
        passed=True,
        evidence=_evidence(response, usable=True, ast_validated=sql is not None),
    )


def validate_sql_result_with_cardinality(
    database_path: str | Path,
    sql: str,
    response: SqlExecutionResponse,
    expectation: SqlResultExpectation | None = None,
    *,
    incident_id: str | None = None,
    trace_id: str | None = None,
    audit_path: str | Path | None = None,
    timeout_seconds: float = 10.0,
) -> SqlResultValidation:
    """Validate a response and, when configured, its complete result cardinality.

    The count probe is deliberately run only after the response and SQL AST
    pass their basic checks. It detects data-level expansion that can remain
    hidden behind a syntactically valid join condition.
    """

    expectation = expectation or SqlResultExpectation()
    validation = validate_sql_result(response, expectation, sql=sql)
    if not validation.passed or expectation.max_result_rows is None:
        return validation

    cardinality_response = measure_query_row_count(
        database_path,
        sql,
        incident_id=incident_id,
        trace_id=trace_id,
        audit_path=audit_path,
        timeout_seconds=timeout_seconds,
    )
    cardinality = _cardinality_from_response(
        cardinality_response, expectation.max_result_rows
    )
    if cardinality.error is not None:
        return SqlResultValidation(
            passed=False,
            reason=SqlResultFailureReason.CARDINALITY_CHECK_FAILED,
            evidence=validation.evidence.model_copy(
                update={"usable": False, "cardinality": cardinality}
            ),
        )
    if cardinality.observed_row_count is None:
        raise AssertionError("successful cardinality validation must include a count")
    if cardinality.observed_row_count > expectation.max_result_rows:
        return SqlResultValidation(
            passed=False,
            reason=SqlResultFailureReason.RESULT_ROW_COUNT_EXCEEDED,
            evidence=validation.evidence.model_copy(
                update={"usable": False, "cardinality": cardinality}
            ),
        )
    return validation.model_copy(
        update={"evidence": validation.evidence.model_copy(update={"cardinality": cardinality})}
    )
