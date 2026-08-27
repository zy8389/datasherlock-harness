"""Dependency-injected execution for the tools exposed by the Planner.

The executor is intentionally small.  It validates a planned step against the
same registry used by the Planner, then delegates SQL safety and execution to
``tools.sql_runner``.  It does not decide whether a successful query is valid
root-cause evidence; that decision belongs to the hypothesis and validator
layers.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Protocol, cast

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from agents.planner import InvestigationStep
from config.faults import EvidenceSourceType
from config.metrics import MetricDefinition, load_metrics_config
from harness.hypothesis import EvidenceReference
from tools.data_quality import (
    DataQualityCheckResult,
    DataQualityEvidence,
    DataQualityScope,
    check_duplicate_rate,
    check_freshness,
    check_null_rate,
    detect_distribution_drift,
    detect_schema_drift,
)
from tools.registry import (
    ToolRegistry,
    ToolRegistryError,
    build_default_tool_registry,
)
from tools.sql_runner import (
    SqlExecutionResponse,
    SqlRunnerError,
    execute_readonly_sql,
    validate_readonly_sql,
)
from validators.sql_result import (
    MetricAggregation,
    NumericRange,
    SqlResultExpectation,
    SqlResultValidation,
    validate_sql_result,
)


class SqlExecutionPort(Protocol):
    """Minimal adapter contract used to keep the executor easy to test."""

    def __call__(
        self,
        database_path: str | Path,
        sql: str,
        **kwargs: Any,
    ) -> SqlExecutionResponse: ...


DataQualityExecutionPort = Callable[..., DataQualityCheckResult]


DEFAULT_DATA_QUALITY_EXECUTORS: dict[str, DataQualityExecutionPort] = {
    "check_null_rate": check_null_rate,
    "check_duplicate_rate": check_duplicate_rate,
    "check_freshness": check_freshness,
    "detect_schema_drift": detect_schema_drift,
    "detect_distribution_drift": detect_distribution_drift,
}


class ToolExecutionResult(BaseModel):
    """Stable, JSON-serializable envelope for one planned tool step."""

    model_config = ConfigDict(extra="forbid")

    tool_name: str = Field(min_length=1)
    success: bool
    query_id: str | None = None
    result: JsonValue | None = None
    error: dict[str, str] | None = None
    sql_validation: SqlResultValidation | None = None
    # Evidence is opt-in.  A successful SQL response is a result, not proof
    # of a root cause, so the default adapter deliberately returns no entries.
    evidence: list[EvidenceReference] = Field(default_factory=list)


class ToolExecutor:
    """Execute registered read-only investigation tools."""

    def __init__(
        self,
        database_path: str | Path,
        *,
        registry: ToolRegistry | None = None,
        sql_execution: SqlExecutionPort = execute_readonly_sql,
        data_quality_execution: Mapping[str, DataQualityExecutionPort] | None = None,
        audit_path: str | Path | None = None,
    ) -> None:
        self.database_path = database_path
        self.registry = registry or build_default_tool_registry()
        self.sql_execution = sql_execution
        self.data_quality_execution = dict(
            DEFAULT_DATA_QUALITY_EXECUTORS
            if data_quality_execution is None
            else data_quality_execution
        )
        self.audit_path = audit_path
        self._metrics_config = load_metrics_config()

    def execute_step(
        self,
        step: InvestigationStep | Mapping[str, Any],
        *,
        incident_id: str | None = None,
        trace_id: str | None = None,
        timeout_seconds: float | None = None,
        max_rows: int | None = None,
        metric_id: str | None = None,
        sql_result_expectation: SqlResultExpectation | Mapping[str, Any] | None = None,
    ) -> ToolExecutionResult:
        """Validate and execute one plan step.

        Registry and argument failures are returned as normalized failures and
        never reach a tool adapter. Data Quality checks are explicit adapters
        and use the same read-only SQL Runner internally as their direct APIs.
        """

        try:
            normalized_step = (
                step
                if isinstance(step, InvestigationStep)
                else InvestigationStep.model_validate(step)
            )
        except (TypeError, ValueError) as exc:
            tool_name = _tool_name_from_payload(step)
            return self._failure(tool_name, "invalid_step", str(exc))

        tool_name = normalized_step.tool
        try:
            definition = self.registry.get(tool_name)
            self.registry.validate_arguments(tool_name, normalized_step.arguments)
        except ToolRegistryError as exc:
            return self._failure(tool_name, "tool_contract", str(exc))

        if not definition.read_only:
            return self._failure(
                tool_name,
                "unsafe_tool",
                f"tool is not read-only: {tool_name}",
            )

        if tool_name != "sql_query":
            if tool_name in self.data_quality_execution:
                return self._execute_data_quality(
                    tool_name,
                    normalized_step.arguments,
                    incident_id=incident_id,
                    trace_id=trace_id,
                    timeout_seconds=timeout_seconds,
                )
            return self._failure(
                tool_name,
                "unsupported_tool",
                f"no execution adapter is registered for tool: {tool_name}",
            )

        sql = normalized_step.arguments["sql"]
        if not isinstance(sql, str):
            # The registry normally catches this, but keep the adapter
            # boundary defensive if a custom registry is injected.
            return self._failure(tool_name, "tool_contract", "arguments.sql must be a string")
        try:
            # Reuse the canonical AST/native SQL guard before invoking an
            # injected adapter; no SQL safety rules are reimplemented here.
            validate_readonly_sql(sql)
        except (SqlRunnerError, TypeError, ValueError) as exc:
            return self._failure(tool_name, "tool_contract", str(exc))

        try:
            sql_kwargs: dict[str, Any] = {
                "incident_id": incident_id,
                "trace_id": trace_id,
                "audit_path": self.audit_path,
            }
            if timeout_seconds is not None:
                sql_kwargs["timeout_seconds"] = timeout_seconds
            if max_rows is not None:
                sql_kwargs["max_rows"] = max_rows
            response = self.sql_execution(self.database_path, sql, **sql_kwargs)
            if not isinstance(response, SqlExecutionResponse):
                response = SqlExecutionResponse.model_validate(response)
        except Exception as exc:  # noqa: BLE001 - normalize adapter failures
            return self._failure(
                tool_name,
                "execution",
                str(exc),
                query_id=getattr(exc, "query_id", None),
            )

        payload = cast(dict[str, JsonValue], response.model_dump(mode="json"))
        expectation = self._sql_expectation(
            response,
            metric_id=metric_id,
            max_rows=max_rows,
            explicit=sql_result_expectation,
        )
        sql_validation = validate_sql_result(response, expectation, sql=sql)
        if response.status == "success":
            return ToolExecutionResult(
                tool_name=tool_name,
                success=True,
                query_id=response.query_id,
                result=payload,
                sql_validation=sql_validation,
            )
        return ToolExecutionResult(
            tool_name=tool_name,
            success=False,
            query_id=response.query_id,
            result=payload,
            error=response.error
            or {"type": "execution", "message": "SQL execution failed"},
            sql_validation=sql_validation,
        )

    def _sql_expectation(
        self,
        response: SqlExecutionResponse,
        *,
        metric_id: str | None,
        max_rows: int | None,
        explicit: SqlResultExpectation | Mapping[str, Any] | None,
    ) -> SqlResultExpectation:
        """Resolve the internal result contract without exposing it to Planner."""

        if explicit is not None:
            return (
                explicit
                if isinstance(explicit, SqlResultExpectation)
                else SqlResultExpectation.model_validate(explicit)
            )

        metric = self._metric_definition(metric_id)
        if metric is not None and metric.id in response.columns:
            try:
                aggregation = MetricAggregation(metric.aggregation)
            except ValueError:
                aggregation = None
            policy = metric.validation
            return SqlResultExpectation(
                required_columns=list(policy.expected_column_types),
                expected_column_types=dict(policy.expected_column_types),
                numeric_ranges={
                    column: NumericRange.model_validate(bounds.model_dump())
                    for column, bounds in policy.numeric_ranges.items()
                },
                expected_aggregation=aggregation,
                max_result_rows=policy.max_result_rows,
            )

        return SqlResultExpectation(max_result_rows=max_rows)

    def _metric_definition(self, metric_id: str | None) -> MetricDefinition | None:
        if metric_id is None:
            return None
        try:
            return next(
                metric
                for metric in self._metrics_config.metrics
                if metric.id == metric_id
            )
        except StopIteration:
            return None

    def _execute_data_quality(
        self,
        tool_name: str,
        arguments: Mapping[str, JsonValue],
        *,
        incident_id: str | None,
        trace_id: str | None,
        timeout_seconds: float | None,
    ) -> ToolExecutionResult:
        adapter = self.data_quality_execution.get(tool_name)
        if adapter is None:
            return self._failure(
                tool_name,
                "unsupported_tool",
                f"no execution adapter is registered for tool: {tool_name}",
            )

        try:
            call_arguments = _normalize_data_quality_arguments(tool_name, arguments)
        except (TypeError, ValueError, OverflowError) as exc:
            return self._failure(tool_name, "tool_contract", str(exc))

        try:
            quality_kwargs: dict[str, Any] = {
                "incident_id": incident_id,
                "trace_id": trace_id,
                "audit_path": self.audit_path,
            }
            if timeout_seconds is not None:
                quality_kwargs["timeout_seconds"] = timeout_seconds
            raw_result = adapter(
                self.database_path,
                **call_arguments,
                **quality_kwargs,
            )
            result = (
                raw_result
                if isinstance(raw_result, DataQualityCheckResult)
                else DataQualityCheckResult.model_validate(raw_result)
            )
        except Exception as exc:  # noqa: BLE001 - normalize injected tool failures
            return self._failure(tool_name, "execution", str(exc))

        result_payload = cast(dict[str, JsonValue], result.model_dump(mode="json"))
        if result.status == "error":
            return ToolExecutionResult(
                tool_name=tool_name,
                success=False,
                query_id=result.query_id,
                result=result_payload,
                error=result.error
                or {"type": "execution", "message": "data quality check failed"},
            )

        evidence = [
            data_quality_evidence_to_reference(
                evidence_item,
                result=result,
                tool_name=tool_name,
                incident_id=incident_id,
                sequence=index,
            )
            for index, evidence_item in enumerate(result.evidence, start=1)
        ]
        return ToolExecutionResult(
            tool_name=tool_name,
            success=True,
            query_id=result.query_id,
            result=result_payload,
            evidence=evidence,
        )

    @staticmethod
    def _failure(
        tool_name: str,
        error_type: str,
        message: str,
        *,
        query_id: str | None = None,
    ) -> ToolExecutionResult:
        return ToolExecutionResult(
            tool_name=tool_name or "unknown",
            success=False,
            query_id=query_id,
            error={"type": error_type, "message": message},
        )


def _tool_name_from_payload(step: object) -> str:
    if isinstance(step, Mapping):
        value = step.get("tool")
        if isinstance(value, str) and value.strip():
            return value
    return "invalid"


def _parse_datetime(value: object, *, name: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise TypeError(f"arguments.{name} must be an ISO-8601 string")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"arguments.{name} must be a valid ISO-8601 datetime") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"arguments.{name} must include a timezone")
    return parsed


def _normalize_data_quality_arguments(
    tool_name: str,
    arguments: Mapping[str, JsonValue],
) -> dict[str, Any]:
    """Map JSON planner values to the real typed Data Quality contracts."""

    normalized = dict(arguments)
    if tool_name in {"check_null_rate", "check_freshness"}:
        scope = normalized.get("scope")
        if scope is not None:
            if not isinstance(scope, Mapping):
                raise TypeError("arguments.scope must be an object")
            scope_payload = dict(scope)
            for name in ("start", "end"):
                if name in scope_payload:
                    scope_payload[name] = _parse_datetime(
                        scope_payload[name], name=f"scope.{name}"
                    )
            normalized["scope"] = DataQualityScope.model_validate(scope_payload)
    if tool_name == "check_freshness":
        normalized["reference_time"] = _parse_datetime(
            normalized.get("reference_time"), name="reference_time"
        )
        max_age = normalized.get("max_age")
        if isinstance(max_age, bool) or not isinstance(max_age, (int, float)):
            raise TypeError("arguments.max_age must be a number of seconds")
        normalized["max_age"] = timedelta(seconds=float(max_age))
    if tool_name == "detect_distribution_drift":
        for name in (
            "baseline_start",
            "baseline_end",
            "current_start",
            "current_end",
        ):
            normalized[name] = _parse_datetime(normalized.get(name), name=name)
    return normalized


def data_quality_evidence_to_reference(
    evidence: DataQualityEvidence,
    *,
    result: DataQualityCheckResult,
    tool_name: str,
    incident_id: str | None,
    sequence: int,
) -> EvidenceReference:
    """Convert a real Data Quality finding to the shared Harness evidence model."""

    if tool_name == "detect_schema_drift":
        source_type = EvidenceSourceType.SCHEMA_METADATA.value
    elif result.table in {"partition_metadata", "pipeline_runs"}:
        source_type = EvidenceSourceType.OPERATIONAL_METADATA.value
    elif result.table == "metric_versions":
        source_type = EvidenceSourceType.METRIC_VERSION.value
    elif result.table == "experiment_configs":
        source_type = EvidenceSourceType.EXPERIMENT_CONFIG.value
    else:
        source_type = EvidenceSourceType.BUSINESS_DATA.value
    observation = {
        "check_name": result.check_name,
        "status": result.status,
        "passed": result.passed,
        "table": result.table,
        "column": result.column,
        "columns": result.columns,
        "observed_value": result.observed_value,
        "threshold": result.threshold,
        "details": evidence.details,
    }
    identity = {
        "incident_id": incident_id or "unknown",
        "tool_name": tool_name,
        "query_id": evidence.query_id,
        "scope": evidence.details.get("scope"),
        "sequence": sequence,
    }
    evidence_id = "dq-" + hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:24]
    return EvidenceReference(
        evidence_id=evidence_id,
        source_type=source_type,
        description=evidence.finding,
        query_id=evidence.query_id,
        observation=cast(dict[str, JsonValue], observation),
    )


__all__ = [
    "DEFAULT_DATA_QUALITY_EXECUTORS",
    "DataQualityExecutionPort",
    "SqlExecutionPort",
    "ToolExecutionResult",
    "ToolExecutor",
    "data_quality_evidence_to_reference",
]
