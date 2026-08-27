"""Pure validation for the bounded result of one SQL execution.

This module deliberately has no database dependency beyond the response model.
It validates the response that the SQL Runner already produced and may inspect
the original SQL text for static AST checks, but it never executes SQL.
"""

from __future__ import annotations

import math
from decimal import Decimal
from enum import StrEnum
from numbers import Real

import sqlglot
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlglot import exp
from sqlglot.errors import SqlglotError

from tools.sql_runner import SqlExecutionResponse


class SqlResultFailureReason(StrEnum):
    """Stable reason codes for a result that cannot satisfy its contract."""

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
    RESULT_ROW_COUNT_EXCEEDED = "result_row_count_exceeded"


class MetricAggregation(StrEnum):
    """Aggregation structures understood by the static SQL validator."""

    COUNT = "count"
    COUNT_DISTINCT = "count_distinct"
    SUM = "sum"
    AVERAGE = "average"
    RATIO = "ratio"
    AVERAGE_AFTER_GROUP_SUM = "average_after_group_sum"


class NumericRange(BaseModel):
    """Inclusive numeric bounds for one output column."""

    model_config = ConfigDict(extra="forbid")

    minimum: float | None = None
    maximum: float | None = None

    @model_validator(mode="after")
    def require_valid_bounds(self) -> NumericRange:
        if self.minimum is None and self.maximum is None:
            raise ValueError("a numeric range must define a minimum or maximum")
        if self.minimum is not None and not math.isfinite(self.minimum):
            raise ValueError("numeric range minimum must be finite")
        if self.maximum is not None and not math.isfinite(self.maximum):
            raise ValueError("numeric range maximum must be finite")
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
        if len(columns) != len(set(columns)):
            raise ValueError("required_columns must not contain duplicates")
        return columns

    @field_validator("expected_column_types")
    @classmethod
    def require_nonblank_column_types(
        cls, column_types: dict[str, str]
    ) -> dict[str, str]:
        if any(not column or not data_type for column, data_type in column_types.items()):
            raise ValueError("expected column types must not contain blank values")
        return column_types


class SqlResultValueViolation(BaseModel):
    """One value that violates a numeric result contract."""

    model_config = ConfigDict(extra="forbid")

    column: str
    row_index: int = Field(ge=0)
    observed_value: float | str | None
    minimum: float | None = None
    maximum: float | None = None


class SqlResultTypeViolation(BaseModel):
    """One output field whose DuckDB type differs from the contract."""

    model_config = ConfigDict(extra="forbid")

    column: str
    expected_type: str
    actual_type: str


class SqlResultRowShapeViolation(BaseModel):
    """One response shape inconsistency that prevents safe inspection."""

    model_config = ConfigDict(extra="forbid")

    row_index: int | None = Field(default=None, ge=0)
    expected_column_count: int = Field(ge=0)
    actual_column_count: int = Field(ge=0)


class SqlResultEvidence(BaseModel):
    """Trace metadata retained for a validation decision.

    This is validator metadata, not a Harness ``EvidenceReference`` and is
    never registered as root-cause evidence by this module.
    """

    model_config = ConfigDict(extra="forbid")

    query_id: str
    statement_type: str | None = None
    columns: list[str]
    column_types: list[str]
    row_count: int = Field(ge=0)
    truncated: bool
    usable: bool
    ast_validated: bool = False
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


_TYPE_ALIASES = {
    "INT": "INTEGER",
    "INT4": "INTEGER",
    "INT8": "BIGINT",
    "FLOAT8": "DOUBLE",
    "BOOL": "BOOLEAN",
    "DATETIME": "TIMESTAMP",
    "STRING": "VARCHAR",
}


def _normalise_type(data_type: str) -> str:
    compact = "".join(data_type.upper().split())
    return _TYPE_ALIASES.get(compact, compact)


def _evidence(
    response: SqlExecutionResponse,
    *,
    usable: bool,
    ast_validated: bool = False,
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
        error=response.error,
    )


def _failure(
    response: SqlExecutionResponse,
    reason: SqlResultFailureReason,
    *,
    missing_columns: list[str] | None = None,
    row_shape_violations: list[SqlResultRowShapeViolation] | None = None,
    type_violations: list[SqlResultTypeViolation] | None = None,
    value_violations: list[SqlResultValueViolation] | None = None,
    ast_validated: bool = False,
) -> SqlResultValidation:
    return SqlResultValidation(
        passed=False,
        reason=reason,
        missing_columns=missing_columns or [],
        row_shape_violations=row_shape_violations or [],
        type_violations=type_violations or [],
        value_violations=value_violations or [],
        evidence=_evidence(response, usable=False, ast_validated=ast_validated),
    )


def _has_aggregate(expression: exp.Expression) -> bool:
    return any(
        isinstance(node, (exp.Count, exp.Sum, exp.Avg))
        for node in expression.walk()
    )


def _matches_aggregation(
    expression: exp.Expression, expected: MetricAggregation
) -> bool:
    if expected is MetricAggregation.COUNT:
        return any(
            isinstance(node, exp.Count)
            and not isinstance(node.this, exp.Distinct)
            for node in expression.walk()
        )
    if expected is MetricAggregation.COUNT_DISTINCT:
        return any(
            isinstance(node, exp.Count) and isinstance(node.this, exp.Distinct)
            for node in expression.walk()
        )
    if expected is MetricAggregation.SUM:
        return any(isinstance(node, exp.Sum) for node in expression.walk())
    if expected is MetricAggregation.AVERAGE:
        return any(isinstance(node, exp.Avg) for node in expression.walk())
    if expected is MetricAggregation.RATIO:
        return any(
            _has_aggregate(node.this) and _has_aggregate(node.expression)
            for node in expression.find_all(exp.Div)
        )
    if expected is MetricAggregation.AVERAGE_AFTER_GROUP_SUM:
        has_average = any(isinstance(node, exp.Avg) for node in expression.walk())
        has_grouped_sum = any(
            select.args.get("group") is not None
            and any(isinstance(node, exp.Sum) for node in select.walk())
            for select in expression.find_all(exp.Select)
        )
        return has_average and has_grouped_sum
    raise AssertionError(f"unsupported aggregation: {expected}")


def _validate_sql_ast(
    sql: str, expectation: SqlResultExpectation
) -> SqlResultFailureReason | None:
    if not isinstance(sql, str) or not sql.strip():
        return SqlResultFailureReason.INVALID_SQL_FOR_AST_VALIDATION
    try:
        expressions = [item for item in sqlglot.parse(sql, read="duckdb") if item]
    except (SqlglotError, TypeError, ValueError):
        return SqlResultFailureReason.INVALID_SQL_FOR_AST_VALIDATION
    if len(expressions) != 1:
        return SqlResultFailureReason.INVALID_SQL_FOR_AST_VALIDATION
    expression = expressions[0]

    for join in expression.find_all(exp.Join):
        if str(join.args.get("kind", "")).upper() == "CROSS":
            return SqlResultFailureReason.CROSS_JOIN_DETECTED
        if join.args.get("on") is None and not join.args.get("using"):
            return SqlResultFailureReason.MISSING_JOIN_CONDITION

    if (
        expectation.expected_aggregation is not None
        and not _matches_aggregation(expression, expectation.expected_aggregation)
    ):
        return SqlResultFailureReason.AGGREGATION_MISMATCH
    return None


def _observed_value(value: object) -> float | str | None:
    if value is None:
        return None
    if isinstance(value, (str, float, int)) and not isinstance(value, bool):
        return value
    return str(value)


def _numeric_value(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (Real, Decimal)):
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return numeric if math.isfinite(numeric) else None


def validate_sql_result(
    response: SqlExecutionResponse,
    expectation: SqlResultExpectation | None = None,
    *,
    sql: str | None = None,
) -> SqlResultValidation:
    """Validate a previously returned response without executing SQL.

    ``sql`` is optional source text used only for sqlglot AST checks. The
    validator never opens DuckDB, calls the SQL Runner, or mutates Harness
    state. By default an empty successful result is not usable.
    """

    if not isinstance(response, SqlExecutionResponse):
        response = SqlExecutionResponse.model_validate(response)
    expectation = expectation or SqlResultExpectation()

    if response.status != "success":
        return _failure(response, SqlResultFailureReason.SQL_EXECUTION_FAILED)
    if response.truncated:
        return _failure(response, SqlResultFailureReason.RESULT_TRUNCATED)

    if len(response.columns) != len(response.column_types):
        return _failure(response, SqlResultFailureReason.INVALID_SCHEMA_METADATA)
    if response.row_count != len(response.rows):
        return _failure(
            response,
            SqlResultFailureReason.INVALID_ROW_SHAPE,
            row_shape_violations=[
                SqlResultRowShapeViolation(
                    row_index=None,
                    expected_column_count=len(response.columns),
                    actual_column_count=len(response.rows),
                )
            ],
        )
    for row_index, row in enumerate(response.rows):
        if len(row) != len(response.columns):
            return _failure(
                response,
                SqlResultFailureReason.INVALID_ROW_SHAPE,
                row_shape_violations=[
                    SqlResultRowShapeViolation(
                        row_index=row_index,
                        expected_column_count=len(response.columns),
                        actual_column_count=len(row),
                    )
                ],
            )

    if response.row_count == 0 and not expectation.allow_empty:
        return _failure(response, SqlResultFailureReason.EMPTY_RESULT)

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
        column for column in expected_columns if column not in available_columns
    ]
    if missing_columns:
        return _failure(
            response,
            SqlResultFailureReason.MISSING_REQUIRED_COLUMNS,
            missing_columns=missing_columns,
        )

    type_violations: list[SqlResultTypeViolation] = []
    for column, expected_type in expectation.expected_column_types.items():
        actual_type = response.column_types[response.columns.index(column)]
        if _normalise_type(actual_type) != _normalise_type(expected_type):
            type_violations.append(
                SqlResultTypeViolation(
                    column=column,
                    expected_type=expected_type,
                    actual_type=actual_type,
                )
            )
    if type_violations:
        return _failure(
            response,
            SqlResultFailureReason.COLUMN_TYPE_MISMATCH,
            type_violations=type_violations,
        )

    ast_validated = False
    if sql is not None:
        ast_failure = _validate_sql_ast(sql, expectation)
        if ast_failure is not None:
            return _failure(response, ast_failure)
        ast_validated = True

    for column, numeric_range in expectation.numeric_ranges.items():
        column_index = response.columns.index(column)
        for row_index, row in enumerate(response.rows):
            value = row[column_index]
            numeric_value = _numeric_value(value)
            if numeric_value is None:
                return _failure(
                    response,
                    SqlResultFailureReason.NON_NUMERIC_VALUE,
                    value_violations=[
                        SqlResultValueViolation(
                            column=column,
                            row_index=row_index,
                            observed_value=_observed_value(value),
                            minimum=numeric_range.minimum,
                            maximum=numeric_range.maximum,
                        )
                    ],
                    ast_validated=ast_validated,
                )
            if (
                numeric_range.minimum is not None
                and numeric_value < numeric_range.minimum
            ) or (
                numeric_range.maximum is not None
                and numeric_value > numeric_range.maximum
            ):
                return _failure(
                    response,
                    SqlResultFailureReason.NUMERIC_VALUE_OUT_OF_RANGE,
                    value_violations=[
                        SqlResultValueViolation(
                            column=column,
                            row_index=row_index,
                            observed_value=numeric_value,
                            minimum=numeric_range.minimum,
                            maximum=numeric_range.maximum,
                        )
                    ],
                    ast_validated=ast_validated,
                )

    if (
        expectation.max_result_rows is not None
        and not response.truncated
        and response.row_count > expectation.max_result_rows
    ):
        return _failure(
            response,
            SqlResultFailureReason.RESULT_ROW_COUNT_EXCEEDED,
            ast_validated=ast_validated,
        )

    return SqlResultValidation(
        passed=True,
        evidence=_evidence(
            response,
            usable=True,
            ast_validated=ast_validated,
        ),
    )


__all__ = [
    "MetricAggregation",
    "NumericRange",
    "SqlResultEvidence",
    "SqlResultExpectation",
    "SqlResultFailureReason",
    "SqlResultRowShapeViolation",
    "SqlResultTypeViolation",
    "SqlResultValidation",
    "SqlResultValueViolation",
    "validate_sql_result",
]
