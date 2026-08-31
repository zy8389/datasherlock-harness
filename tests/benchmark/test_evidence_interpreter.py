from datetime import date
from typing import Any

import pytest

from agents.planner import InvestigationStep
from benchmark.evidence_interpreter import (
    EvidencePolarity,
    IncidentEvidenceContext,
    RuntimeEvidenceInterpreter,
)
from harness.hypothesis import EvidenceReference, HypothesisState
from tools.executor import ToolExecutionResult
from validators.sql_result import SqlResultEvidence, SqlResultValidation

TARGET_DATE = date(2026, 1, 30)


def _hypothesis(root_cause_type: str) -> HypothesisState:
    return HypothesisState(
        hypothesis_id="H01",
        root_cause_type=root_cause_type,
        description=f"Candidate explanation: {root_cause_type}.",
        confidence=0.60,
    )


def _context(
    *,
    metric_id: str = "daily_active_users",
    target_date: date = TARGET_DATE,
    device_type: str | None = None,
) -> IncidentEvidenceContext:
    return IncidentEvidenceContext(
        incident_id="INC-TEST",
        metric_id=metric_id,
        observed_at=f"{target_date.isoformat()}T00:00:00+00:00",
        target_date=target_date,
        device_type=device_type,
    )


def _step(sql: str, *, step_id: str = "S01", tool: str = "sql_query") -> InvestigationStep:
    return InvestigationStep(
        step_id=step_id,
        purpose="Inspect the bounded runtime observation.",
        hypothesis_id="H01",
        tool=tool,
        arguments={"sql": sql} if tool == "sql_query" else {},
        expected_evidence=["the bounded observation"],
        stop_condition="retain the observation",
    )


def _sql_result(
    columns: list[str],
    rows: list[list[Any]],
    *,
    sql: str = "SELECT 1",
    query_id: str = "Q01",
    success: bool = True,
    validation_passed: bool = True,
    usable: bool = True,
    truncated: bool = False,
    result_status: str = "success",
) -> ToolExecutionResult:
    result = {
        "query_id": query_id,
        "status": result_status,
        "statement_type": "SELECT",
        "columns": columns,
        "column_types": ["BIGINT"] * len(columns),
        "rows": rows,
        "row_count": len(rows),
        "truncated": truncated,
        "duration_ms": 1.0,
        "error": None,
    }
    validation = SqlResultValidation(
        passed=validation_passed,
        evidence=SqlResultEvidence(
            query_id=query_id,
            statement_type="SELECT",
            columns=columns,
            column_types=["BIGINT"] * len(columns),
            row_count=len(rows),
            truncated=truncated,
            usable=usable,
        ),
    )
    return ToolExecutionResult(
        tool_name="sql_query",
        success=success,
        query_id=query_id,
        result=result,
        sql_validation=validation,
    )


def _interpret(
    hypothesis: str,
    step: InvestigationStep,
    result: ToolExecutionResult,
    *,
    context: IncidentEvidenceContext | None = None,
):
    return RuntimeEvidenceInterpreter(
        context=context or _context(
            metric_id=(
                "daily_active_users"
                if hypothesis not in {"duplicate_batch", "join_explosion", "field_drift"}
                else "ai_task_count"
            )
        )
    ).interpret(
        hypothesis=_hypothesis(hypothesis),
        step=step,
        tool_result=result,
    )


def _decision(interpretation, index: int = 0):
    assert interpretation.decisions
    return interpretation.decisions[index]


def test_successful_ordinary_sql_is_neutral() -> None:
    result = _sql_result(
        ["event_count"],
        [[12]],
        sql="SELECT COUNT(*) AS event_count FROM events",
    )

    interpretation = _interpret(
        "missing_partition", _step("SELECT COUNT(*) FROM events"), result
    )

    assert interpretation.polarity is EvidencePolarity.NEUTRAL
    assert interpretation.evidence is None


def test_sql_validation_success_does_not_create_evidence_by_itself() -> None:
    result = _sql_result(["value"], [[1]], sql="SELECT 1")

    interpretation = _interpret("missing_partition", _step("SELECT 1"), result)

    assert interpretation.polarity is EvidencePolarity.NEUTRAL
    assert interpretation.evidence is None


@pytest.mark.parametrize(
    ("validation_passed", "usable", "rows", "truncated"),
    [
        (False, True, [[0]], False),
        (True, False, [[0]], False),
        (True, True, [], False),
        (True, True, [[0]], True),
    ],
)
def test_invalid_empty_or_truncated_sql_result_does_not_support(
    validation_passed: bool,
    usable: bool,
    rows: list[list[Any]],
    truncated: bool,
) -> None:
    result = _sql_result(
        ["android_event_count"],
        rows,
        sql=(
            "SELECT COUNT(*) AS android_event_count FROM events "
            "WHERE CAST(event_time AS DATE) = DATE '2026-01-30' "
            "AND device_type = 'android'"
        ),
        validation_passed=validation_passed,
        usable=usable,
        truncated=truncated,
    )

    interpretation = _interpret(
        "missing_partition",
        _step(
            "SELECT COUNT(*) AS android_event_count FROM events "
            "WHERE CAST(event_time AS DATE) = DATE '2026-01-30' "
            "AND device_type = 'android'"
        ),
        result,
    )

    assert interpretation.polarity is EvidencePolarity.NEUTRAL
    assert interpretation.evidence is None


def test_partition_or_metric_table_name_alone_is_not_evidence() -> None:
    partition = _sql_result(
        ["status"],
        [["ready"]],
        sql="SELECT status FROM partition_metadata",
    )
    metric_versions = _sql_result(
        ["metric_id", "version", "definition_hash", "query"],
        [["daily_active_users", 1, "same", "SELECT 1"]],
        sql="SELECT metric_id, version, definition_hash, query FROM metric_versions",
    )

    assert _interpret(
        "missing_partition", _step("SELECT status FROM partition_metadata"), partition
    ).evidence is None
    assert _interpret(
        "metric_definition_change",
        _step("SELECT metric_id, version, definition_hash, query FROM metric_versions"),
        metric_versions,
    ).evidence is None


def test_metric_versions_without_change_is_neutral() -> None:
    result = _sql_result(
        ["metric_id", "version", "definition_hash", "query", "effective_at"],
        [
            ["daily_active_users", 1, "same", "SELECT 1", "2026-01-01"],
            ["daily_active_users", 1, "same", "SELECT 1", "2026-01-30"],
        ],
        sql=(
            "SELECT metric_id, version, definition_hash, query, effective_at "
            "FROM metric_versions WHERE metric_id = 'daily_active_users' ORDER BY version"
        ),
    )

    interpretation = _interpret(
        "metric_definition_change",
        _step(
            "SELECT metric_id, version, definition_hash, query, effective_at "
            "FROM metric_versions WHERE metric_id = 'daily_active_users' ORDER BY version"
        ),
        result,
    )

    assert interpretation.polarity is EvidencePolarity.NEUTRAL
    assert interpretation.evidence is None


def test_f01_healthy_activity_and_partition_cannot_support_missing_partition() -> None:
    business = _sql_result(
        ["android_event_count"],
        [[7]],
        sql=(
            "SELECT COUNT(*) AS android_event_count FROM events "
            "WHERE CAST(event_time AS DATE) = DATE '2026-01-30' "
            "AND device_type = 'android'"
        ),
        query_id="Q-BUSINESS",
    )
    partition = _sql_result(
        ["partition_value", "row_count", "status"],
        [["2026-01-30/android", 7, "ready"]],
        sql="SELECT partition_value, row_count, status FROM partition_metadata",
        query_id="Q-PARTITION",
    )

    business_interpretation = _interpret(
        "missing_partition",
        _step(
            "SELECT COUNT(*) AS android_event_count FROM events "
            "WHERE CAST(event_time AS DATE) = DATE '2026-01-30' "
            "AND device_type = 'android'",
            step_id="S01",
        ),
        business,
    )
    partition_interpretation = _interpret(
        "missing_partition",
        _step(
            "SELECT partition_value, row_count, status FROM partition_metadata",
            step_id="S02",
        ),
        partition,
    )

    assert business_interpretation.polarity is EvidencePolarity.CONTRADICTS
    assert partition_interpretation.polarity is EvidencePolarity.CONTRADICTS


def test_f01_uses_actual_business_and_operational_values() -> None:
    business = _sql_result(
        ["android_event_count"],
        [[0]],
        sql=(
            "SELECT COUNT(*) AS android_event_count FROM events "
            "WHERE CAST(event_time AS DATE) = DATE '2026-01-30' "
            "AND device_type = 'android'"
        ),
        query_id="Q-BUSINESS",
    )
    partition = _sql_result(
        ["partition_value", "row_count", "status"],
        [["2026-01-30/android", 0, "missing"]],
        sql="SELECT partition_value, row_count, status FROM partition_metadata",
        query_id="Q-PARTITION",
    )

    first = _interpret(
        "missing_partition",
        _step(
            "SELECT COUNT(*) AS android_event_count FROM events "
            "WHERE CAST(event_time AS DATE) = DATE '2026-01-30' "
            "AND device_type = 'android'",
            step_id="S01",
        ),
        business,
    )
    second = _interpret(
        "missing_partition",
        _step(
            "SELECT partition_value, row_count, status FROM partition_metadata",
            step_id="S02",
        ),
        partition,
    )

    assert first.polarity is EvidencePolarity.SUPPORTS
    assert second.polarity is EvidencePolarity.SUPPORTS
    assert first.evidence is not None
    assert second.evidence is not None
    assert first.evidence.source_type == "business_data"
    assert second.evidence.source_type == "operational_metadata"
    assert "android_event_count=0" in first.evidence.description
    assert "partition_value=2026-01-30/android" in second.evidence.description
    assert first.evidence.evidence_id.startswith("runner-")
    assert first.evidence.evidence_id == _interpret(
        "missing_partition",
        _step(
            "SELECT COUNT(*) AS android_event_count FROM events "
            "WHERE CAST(event_time AS DATE) = DATE '2026-01-30' "
            "AND device_type = 'android'",
            step_id="S01",
        ),
        business,
    ).evidence.evidence_id
    assert set(first.evidence.observation) >= {
        "incident_id",
        "metric_id",
        "target_date",
        "step_id",
        "rule",
        "scope_check",
        "observed_row",
        "sql_validation",
    }


def test_f01_wrong_date_partition_is_neutral() -> None:
    result = _sql_result(
        ["partition_value", "row_count", "status"],
        [["2026-01-10/android", 0, "missing"]],
        sql="SELECT partition_value, row_count, status FROM partition_metadata",
    )

    interpretation = _interpret(
        "missing_partition",
        _step("SELECT partition_value, row_count, status FROM partition_metadata"),
        result,
    )

    assert interpretation.polarity is EvidencePolarity.NEUTRAL
    assert interpretation.evidence is None


def test_f01_wrong_segment_partition_is_neutral() -> None:
    result = _sql_result(
        ["partition_value", "row_count", "status"],
        [["2026-01-30/ios", 0, "missing"]],
        sql="SELECT partition_value, row_count, status FROM partition_metadata",
    )

    interpretation = _interpret(
        "missing_partition",
        _step("SELECT partition_value, row_count, status FROM partition_metadata"),
        result,
    )

    assert interpretation.polarity is EvidencePolarity.NEUTRAL
    assert interpretation.evidence is None


def test_f01_correct_target_partition_supports() -> None:
    result = _sql_result(
        ["partition_value", "row_count", "status"],
        [["2026-01-30/android", 0, "missing"]],
        sql="SELECT partition_value, row_count, status FROM partition_metadata",
    )

    interpretation = _interpret(
        "missing_partition",
        _step("SELECT partition_value, row_count, status FROM partition_metadata"),
        result,
    )

    assert interpretation.polarity is EvidencePolarity.SUPPORTS


def test_f11_requires_actual_activity_divergence_and_version_change() -> None:
    business = _sql_result(
        ["raw_event_count", "raw_user_count", "daily_active_users"],
        [[373, 111, 59]],
        sql=(
            "SELECT COUNT(*) AS raw_event_count, "
            "COUNT(DISTINCT user_id) AS raw_user_count, "
            "(SELECT daily_active_users FROM daily_metrics "
            "WHERE metric_date = DATE '2026-01-30') AS daily_active_users "
            "FROM events WHERE CAST(event_time AS DATE) = DATE '2026-01-30'"
        ),
        query_id="Q-BUSINESS",
    )
    versions = _sql_result(
        ["metric_id", "version", "definition_hash", "query", "effective_at"],
        [
            ["daily_active_users", 1, "hash-1", "SELECT 1", "2026-01-01"],
            ["daily_active_users", 2, "hash-2", "SELECT 2", "2026-01-30"],
        ],
        sql=(
            "SELECT metric_id, version, definition_hash, query, effective_at "
            "FROM metric_versions WHERE metric_id = 'daily_active_users' ORDER BY version"
        ),
        query_id="Q-VERSIONS",
    )

    first = _interpret(
        "metric_definition_change",
        _step(
            "SELECT COUNT(*) AS raw_event_count, COUNT(DISTINCT user_id) AS raw_user_count, "
            "(SELECT daily_active_users FROM daily_metrics WHERE metric_date = DATE '2026-01-30') "
            "AS daily_active_users FROM events WHERE CAST(event_time AS DATE) = DATE '2026-01-30'",
            step_id="S01",
        ),
        business,
    )
    second = _interpret(
        "metric_definition_change",
        _step(
            "SELECT metric_id, version, definition_hash, query, effective_at "
            "FROM metric_versions WHERE metric_id = 'daily_active_users' ORDER BY version",
            step_id="S02",
        ),
        versions,
    )

    assert first.polarity is EvidencePolarity.SUPPORTS
    assert second.polarity is EvidencePolarity.SUPPORTS
    assert first.evidence is not None
    assert second.evidence is not None
    assert first.evidence.source_type == "business_data"
    assert second.evidence.source_type == "metric_version"
    assert "raw_event_count=373" in first.evidence.description
    assert "daily_active_users=59" in first.evidence.description


def test_f11_wrong_metric_version_is_neutral() -> None:
    result = _sql_result(
        ["metric_id", "version", "definition_hash", "query", "effective_at"],
        [
            ["conversion_rate", 1, "hash-1", "SELECT 1", "2026-01-01"],
            ["conversion_rate", 2, "hash-2", "SELECT 2", "2026-01-30"],
        ],
        sql=(
            "SELECT metric_id, version, definition_hash, query, effective_at "
            "FROM metric_versions WHERE metric_id = 'conversion_rate' ORDER BY version"
        ),
    )

    interpretation = _interpret(
        "metric_definition_change",
        _step(
            "SELECT metric_id, version, definition_hash, query, effective_at "
            "FROM metric_versions WHERE metric_id = 'conversion_rate' ORDER BY version"
        ),
        result,
    )

    assert interpretation.polarity is EvidencePolarity.NEUTRAL
    assert interpretation.evidence is None


def test_f11_correct_metric_version_supports() -> None:
    result = _sql_result(
        ["metric_id", "version", "definition_hash", "query", "effective_at"],
        [
            ["daily_active_users", 1, "hash-1", "SELECT 1", "2026-01-01"],
            ["daily_active_users", 2, "hash-2", "SELECT 2", "2026-01-30"],
        ],
        sql=(
            "SELECT metric_id, version, definition_hash, query, effective_at "
            "FROM metric_versions WHERE metric_id = 'daily_active_users' ORDER BY version"
        ),
    )

    interpretation = _interpret(
        "metric_definition_change",
        _step(
            "SELECT metric_id, version, definition_hash, query, effective_at "
            "FROM metric_versions WHERE metric_id = 'daily_active_users' ORDER BY version"
        ),
        result,
    )

    assert interpretation.polarity is EvidencePolarity.SUPPORTS


def _dq_reference(
    *,
    evidence_id: str,
    table: str = "events",
    column: str = "user_id",
    scope: dict[str, Any] | None = None,
    source_type: str = "business_data",
    observed_value: float | None = 0.05,
    threshold: float | None = 0.01,
    total_rows: int = 100,
    null_rate: float | None = 0.05,
) -> EvidenceReference:
    details: dict[str, Any] = {
        "total_rows": total_rows,
        "null_rate": null_rate,
    }
    if scope is not None:
        details["scope"] = scope
    return EvidenceReference(
        evidence_id=evidence_id,
        source_type=source_type,
        description="canonical DQ finding",
        query_id="Q-DQ",
        observation={
            "check_name": "check_null_rate",
            "status": "success",
            "passed": False,
            "table": table,
            "column": column,
            "columns": [column],
            "observed_value": observed_value,
            "threshold": threshold,
            "details": details,
        },
    )


def _dq_result(
    references: list[EvidenceReference],
    *,
    passed: bool = False,
) -> ToolExecutionResult:
    observation = references[0].observation if references else {}
    return ToolExecutionResult(
        tool_name="check_null_rate",
        success=True,
        query_id="Q-DQ",
        result={
            "check_name": "check_null_rate",
            "status": "success",
            "passed": passed,
            "table": "events",
            "column": "user_id",
            "columns": ["user_id"],
            "observed_value": observation.get("observed_value"),
            "threshold": observation.get("threshold"),
            "query_id": "Q-DQ",
            "evidence": [reference.model_dump(mode="json") for reference in references],
        },
        evidence=references,
    )


def _dq_tool_reference(
    *,
    evidence_id: str,
    tool_name: str,
    table: str = "events",
    column: str | None = None,
    columns: list[str] | None = None,
    source_type: str = "business_data",
    observed_value: float | None = None,
    threshold: float | None = None,
    details: dict[str, Any] | None = None,
    passed: bool | None = False,
) -> EvidenceReference:
    return EvidenceReference(
        evidence_id=evidence_id,
        source_type=source_type,
        description="unrelated text must not determine DQ polarity",
        query_id="Q-DQ-GENERIC",
        observation={
            "check_name": tool_name,
            "status": "success",
            "passed": passed,
            "table": table,
            "column": column,
            "columns": columns or ([column] if column is not None else []),
            "observed_value": observed_value,
            "threshold": threshold,
            "details": details or {},
        },
    )


def _dq_tool_result(
    tool_name: str,
    references: list[EvidenceReference],
    *,
    passed: bool | None = False,
) -> ToolExecutionResult:
    observation = references[0].observation if references else {}
    return ToolExecutionResult(
        tool_name=tool_name,
        success=True,
        query_id="Q-DQ-GENERIC",
        result={
            "check_name": tool_name,
            "status": "success",
            "passed": passed,
            "table": observation.get("table"),
            "column": observation.get("column"),
            "columns": observation.get("columns", []),
            "observed_value": observation.get("observed_value"),
            "threshold": observation.get("threshold"),
            "query_id": "Q-DQ-GENERIC",
        },
        evidence=references,
    )


def _target_scope() -> dict[str, Any]:
    return {
        "equals": {"device_type": ["ios", "android"]},
        "time_column": "event_time",
        "start": "2026-01-30T00:00:00+00:00",
        "end": "2026-01-31T00:00:00+00:00",
    }


def test_failed_null_rate_does_not_support_missing_partition() -> None:
    reference = _dq_reference(evidence_id="dq-mismatch", scope=_target_scope())
    interpretation = RuntimeEvidenceInterpreter(context=_context()).interpret(
        hypothesis=_hypothesis("missing_partition"),
        step=_step("", tool="check_null_rate"),
        tool_result=_dq_result([reference]),
    )

    assert _decision(interpretation).polarity is EvidencePolarity.NEUTRAL
    assert _decision(interpretation).evidence == reference


def test_failed_null_rate_supports_null_value_anomaly() -> None:
    reference = _dq_reference(evidence_id="dq-compatible", scope=_target_scope())
    interpretation = RuntimeEvidenceInterpreter(context=_context()).interpret(
        hypothesis=_hypothesis("null_value_anomaly"),
        step=_step("", tool="check_null_rate"),
        tool_result=_dq_result([reference]),
    )

    assert _decision(interpretation).polarity is EvidencePolarity.SUPPORTS
    assert _decision(interpretation).evidence == reference


def test_empty_null_rate_is_neutral_even_when_passed_false() -> None:
    reference = _dq_reference(
        evidence_id="dq-empty",
        scope=_target_scope(),
        observed_value=None,
        total_rows=0,
        null_rate=None,
    )
    interpretation = RuntimeEvidenceInterpreter(context=_context()).interpret(
        hypothesis=_hypothesis("null_value_anomaly"),
        step=_step("", tool="check_null_rate"),
        tool_result=_dq_result([reference]),
    )

    decision = _decision(interpretation)
    assert decision.polarity is EvidencePolarity.NEUTRAL
    assert "undefined" in decision.reason


def test_null_rate_above_threshold_supports_null_value_anomaly() -> None:
    reference = _dq_reference(
        evidence_id="dq-high-null-rate",
        scope=_target_scope(),
        observed_value=0.05,
        threshold=0.01,
        null_rate=0.05,
    )
    interpretation = RuntimeEvidenceInterpreter(context=_context()).interpret(
        hypothesis=_hypothesis("null_value_anomaly"),
        step=_step("", tool="check_null_rate"),
        tool_result=_dq_result([reference]),
    )

    assert _decision(interpretation).polarity is EvidencePolarity.SUPPORTS


def test_inconsistent_null_rate_below_threshold_is_neutral() -> None:
    reference = _dq_reference(
        evidence_id="dq-inconsistent-null-rate",
        scope=_target_scope(),
        observed_value=0.005,
        threshold=0.01,
        null_rate=0.005,
    )
    interpretation = RuntimeEvidenceInterpreter(context=_context()).interpret(
        hypothesis=_hypothesis("null_value_anomaly"),
        step=_step("", tool="check_null_rate"),
        tool_result=_dq_result([reference]),
    )

    decision = _decision(interpretation)
    assert decision.polarity is EvidencePolarity.NEUTRAL
    assert "does not exceed" in decision.reason


def test_freshness_without_timestamps_is_neutral() -> None:
    reference = _dq_tool_reference(
        evidence_id="dq-no-timestamps",
        tool_name="check_freshness",
        column="event_time",
        observed_value=None,
        threshold=3600.0,
        details={
            "timestamp_rows": 0,
            "freshness_age_seconds": None,
            "reference_time": "2026-01-30T12:00:00+00:00",
            "scope": _target_scope(),
        },
    )
    interpretation = RuntimeEvidenceInterpreter(context=_context()).interpret(
        hypothesis=_hypothesis("data_delay"),
        step=_step("", tool="check_freshness"),
        tool_result=_dq_tool_result("check_freshness", [reference]),
    )

    decision = _decision(interpretation)
    assert decision.polarity is EvidencePolarity.NEUTRAL
    assert "timestamp rows" in decision.reason


def test_freshness_age_above_threshold_supports_data_delay() -> None:
    reference = _dq_tool_reference(
        evidence_id="dq-stale",
        tool_name="check_freshness",
        column="event_time",
        observed_value=7200.0,
        threshold=3600.0,
        details={
            "timestamp_rows": 10,
            "freshness_age_seconds": 7200.0,
            "max_age_seconds": 3600.0,
            "reference_time": "2026-01-30T12:00:00+00:00",
            "scope": _target_scope(),
        },
    )
    interpretation = RuntimeEvidenceInterpreter(context=_context()).interpret(
        hypothesis=_hypothesis("data_delay"),
        step=_step("", tool="check_freshness"),
        tool_result=_dq_tool_result("check_freshness", [reference]),
    )

    assert _decision(interpretation).polarity is EvidencePolarity.SUPPORTS


def test_negative_freshness_age_is_neutral_for_data_delay() -> None:
    reference = _dq_tool_reference(
        evidence_id="dq-future-timestamp",
        tool_name="check_freshness",
        column="event_time",
        observed_value=-60.0,
        threshold=3600.0,
        details={
            "timestamp_rows": 10,
            "freshness_age_seconds": -60.0,
            "max_age_seconds": 3600.0,
            "reference_time": "2026-01-30T12:00:00+00:00",
            "scope": _target_scope(),
        },
    )
    interpretation = RuntimeEvidenceInterpreter(context=_context()).interpret(
        hypothesis=_hypothesis("data_delay"),
        step=_step("", tool="check_freshness"),
        tool_result=_dq_tool_result("check_freshness", [reference]),
    )

    decision = _decision(interpretation)
    assert decision.polarity is EvidencePolarity.NEUTRAL
    assert "future-dated" in decision.reason


def test_duplicate_rate_is_neutral_without_incident_scope_support() -> None:
    reference = _dq_tool_reference(
        evidence_id="dq-duplicate-rate",
        tool_name="check_duplicate_rate",
        column=None,
        columns=["event_id"],
        observed_value=0.25,
        threshold=0.01,
        details={
            "total_rows": 100,
            "duplicate_rows": 25,
            "duplicate_rate": 0.25,
        },
    )
    interpretation = RuntimeEvidenceInterpreter(
        context=_context(metric_id="ai_task_count")
    ).interpret(
        hypothesis=_hypothesis("duplicate_batch"),
        step=_step("", tool="check_duplicate_rate"),
        tool_result=_dq_tool_result("check_duplicate_rate", [reference]),
    )

    decision = _decision(interpretation)
    assert decision.polarity is EvidencePolarity.NEUTRAL
    assert (
        "duplicate-rate check is not incident-scoped in the current tool contract"
        in decision.reason
    )


def _schema_details(**changes: Any) -> dict[str, Any]:
    details: dict[str, Any] = {
        "previous_effective_at": "2026-01-29T00:00:00+00:00",
        "current_effective_at": "2026-01-30T00:00:00+00:00",
        "added_columns": [],
        "removed_columns": [],
        "type_changes": [],
    }
    details.update(changes)
    return details


def test_schema_drift_without_actual_change_is_neutral() -> None:
    reference = _dq_tool_reference(
        evidence_id="dq-schema-unchanged",
        tool_name="detect_schema_drift",
        source_type="schema_metadata",
        observed_value=0.0,
        threshold=0.0,
        details=_schema_details(),
        passed=True,
    )
    interpretation = RuntimeEvidenceInterpreter(context=_context()).interpret(
        hypothesis=_hypothesis("schema_change"),
        step=_step("", tool="detect_schema_drift"),
        tool_result=_dq_tool_result("detect_schema_drift", [reference], passed=True),
    )

    decision = _decision(interpretation)
    assert decision.polarity is EvidencePolarity.NEUTRAL
    assert decision.reason == "passed data-quality checks are neutral by default"


def test_schema_drift_insufficient_history_is_neutral() -> None:
    reference = _dq_tool_reference(
        evidence_id="dq-schema-insufficient-history",
        tool_name="detect_schema_drift",
        source_type="schema_metadata",
        details={
            "assessment": "insufficient_history",
            "snapshot_count": 1,
            "required_snapshot_count": 2,
        },
        passed=None,
    )
    interpretation = RuntimeEvidenceInterpreter(context=_context()).interpret(
        hypothesis=_hypothesis("schema_change"),
        step=_step("", tool="detect_schema_drift"),
        tool_result=_dq_tool_result(
            "detect_schema_drift", [reference], passed=None
        ),
    )

    decision = _decision(interpretation)
    assert decision.polarity is EvidencePolarity.NEUTRAL
    assert interpretation.polarity is not EvidencePolarity.SUPPORTS
    assert interpretation.polarity is not EvidencePolarity.CONTRADICTS


def test_schema_drift_with_actual_change_supports_schema_change() -> None:
    reference = _dq_tool_reference(
        evidence_id="dq-schema-changed",
        tool_name="detect_schema_drift",
        source_type="schema_metadata",
        observed_value=1.0,
        threshold=0.0,
        details=_schema_details(
            type_changes=[
                {
                    "column": "app_build_number",
                    "previous_type": "BIGINT",
                    "current_type": "VARCHAR",
                }
            ]
        ),
    )
    interpretation = RuntimeEvidenceInterpreter(context=_context()).interpret(
        hypothesis=_hypothesis("schema_change"),
        step=_step("", tool="detect_schema_drift"),
        tool_result=_dq_tool_result("detect_schema_drift", [reference]),
    )

    assert _decision(interpretation).polarity is EvidencePolarity.SUPPORTS


def _distribution_details() -> dict[str, Any]:
    return {
        "current_window": {
            "start": "2026-01-30T00:00:00+00:00",
            "end": "2026-01-31T00:00:00+00:00",
            "row_count": 100,
        }
    }


def test_distribution_observed_none_is_neutral() -> None:
    reference = _dq_tool_reference(
        evidence_id="dq-distribution-undefined",
        tool_name="detect_distribution_drift",
        column="event_name",
        columns=["event_name", "event_time"],
        observed_value=None,
        threshold=0.1,
        details=_distribution_details(),
    )
    interpretation = RuntimeEvidenceInterpreter(
        context=_context(metric_id="ai_task_count")
    ).interpret(
        hypothesis=_hypothesis("field_drift"),
        step=_step("", tool="detect_distribution_drift"),
        tool_result=_dq_tool_result("detect_distribution_drift", [reference]),
    )

    decision = _decision(interpretation)
    assert decision.polarity is EvidencePolarity.NEUTRAL
    assert "no finite observed drift value" in decision.reason


def test_distribution_observed_above_threshold_and_scoped_supports() -> None:
    reference = _dq_tool_reference(
        evidence_id="dq-distribution-drift",
        tool_name="detect_distribution_drift",
        column="event_name",
        columns=["event_name", "event_time"],
        observed_value=0.4,
        threshold=0.1,
        details=_distribution_details(),
    )
    interpretation = RuntimeEvidenceInterpreter(
        context=_context(metric_id="ai_task_count")
    ).interpret(
        hypothesis=_hypothesis("field_drift"),
        step=_step("", tool="detect_distribution_drift"),
        tool_result=_dq_tool_result("detect_distribution_drift", [reference]),
    )

    assert _decision(interpretation).polarity is EvidencePolarity.SUPPORTS


def test_multiple_dq_evidence_do_not_share_one_polarity() -> None:
    compatible = _dq_reference(evidence_id="dq-compatible", scope=_target_scope())
    wrong_table = _dq_reference(
        evidence_id="dq-wrong-table",
        table="users",
        scope=_target_scope(),
    )
    interpretation = RuntimeEvidenceInterpreter(context=_context()).interpret(
        hypothesis=_hypothesis("null_value_anomaly"),
        step=_step("", tool="check_null_rate"),
        tool_result=_dq_result([compatible, wrong_table]),
    )

    assert [decision.polarity for decision in interpretation.decisions] == [
        EvidencePolarity.SUPPORTS,
        EvidencePolarity.NEUTRAL,
    ]
    assert [decision.evidence.evidence_id for decision in interpretation.decisions] == [
        "dq-compatible",
        "dq-wrong-table",
    ]


def test_passed_dq_is_neutral_and_preserves_canonical_reference() -> None:
    reference = _dq_reference(evidence_id="dq-passed", scope=_target_scope())
    interpretation = RuntimeEvidenceInterpreter(context=_context()).interpret(
        hypothesis=_hypothesis("null_value_anomaly"),
        step=_step("", tool="check_null_rate"),
        tool_result=_dq_result([reference], passed=True),
    )

    assert _decision(interpretation).polarity is EvidencePolarity.NEUTRAL
    assert _decision(interpretation).evidence == reference


def test_wrong_scope_evidence_does_not_raise_hypothesis_confidence() -> None:
    reference = _dq_reference(
        evidence_id="dq-wrong-window",
        scope={
            "equals": {"device_type": "android"},
            "time_column": "event_time",
            "start": "2026-01-10T00:00:00+00:00",
            "end": "2026-01-11T00:00:00+00:00",
        },
    )
    hypothesis = _hypothesis("null_value_anomaly")
    interpretation = RuntimeEvidenceInterpreter(context=_context()).interpret(
        hypothesis=hypothesis,
        step=_step("", tool="check_null_rate"),
        tool_result=_dq_result([reference]),
    )

    assert _decision(interpretation).polarity is EvidencePolarity.NEUTRAL
    assert hypothesis.confidence == pytest.approx(0.60)


def test_wrong_scope_observations_cannot_reach_root_cause_found() -> None:
    reference = _dq_reference(
        evidence_id="dq-wrong-window",
        scope={
            "equals": {"device_type": "android"},
            "time_column": "event_time",
            "start": "2026-01-10T00:00:00+00:00",
            "end": "2026-01-11T00:00:00+00:00",
        },
    )
    interpretation = RuntimeEvidenceInterpreter(context=_context()).interpret(
        hypothesis=_hypothesis("null_value_anomaly"),
        step=_step("", tool="check_null_rate"),
        tool_result=_dq_result([reference]),
    )

    assert all(
        decision.polarity is EvidencePolarity.NEUTRAL
        for decision in interpretation.decisions
    )
    assert interpretation.evidence == reference
