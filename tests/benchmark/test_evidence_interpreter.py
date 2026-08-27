from typing import Any

import pytest

from agents.planner import InvestigationStep
from benchmark.evidence_interpreter import (
    EvidencePolarity,
    RuntimeEvidenceInterpreter,
)
from harness.hypothesis import EvidenceReference, HypothesisState
from tools.executor import ToolExecutionResult
from validators.sql_result import SqlResultEvidence, SqlResultValidation


def _hypothesis(root_cause_type: str) -> HypothesisState:
    return HypothesisState(
        hypothesis_id="H01",
        root_cause_type=root_cause_type,
        description=f"Candidate explanation: {root_cause_type}.",
        confidence=0.60,
    )


def _step(sql: str, *, step_id: str = "S01") -> InvestigationStep:
    return InvestigationStep(
        step_id=step_id,
        purpose="Inspect the bounded runtime observation.",
        hypothesis_id="H01",
        tool="sql_query",
        arguments={"sql": sql},
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
):
    return RuntimeEvidenceInterpreter("INC-TEST").interpret(
        hypothesis=_hypothesis(hypothesis),
        step=step,
        tool_result=result,
    )


def test_successful_ordinary_sql_is_neutral() -> None:
    result = _sql_result(
        ["event_count"],
        [[12]],
        sql="SELECT COUNT(*) AS event_count FROM events",
    )

    interpretation = _interpret("missing_partition", _step("SELECT COUNT(*) FROM events"), result)

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
        sql="SELECT COUNT(*) AS android_event_count FROM events WHERE device_type = 'android'",
        validation_passed=validation_passed,
        usable=usable,
        truncated=truncated,
    )

    interpretation = _interpret(
        "missing_partition",
        _step("SELECT COUNT(*) AS android_event_count FROM events WHERE device_type = 'android'"),
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
        ["version", "definition_hash", "query"],
        [[1, "same", "SELECT 1"]],
        sql="SELECT version, definition_hash, query FROM metric_versions",
    )

    assert _interpret(
        "missing_partition", _step("SELECT status FROM partition_metadata"), partition
    ).evidence is None
    assert _interpret(
        "metric_definition_change",
        _step("SELECT version, definition_hash, query FROM metric_versions"),
        metric_versions,
    ).evidence is None


def test_metric_versions_without_change_is_neutral() -> None:
    result = _sql_result(
        ["version", "definition_hash", "query"],
        [[1, "same", "SELECT 1"], [1, "same", "SELECT 1"]],
        sql="SELECT version, definition_hash, query FROM metric_versions ORDER BY version",
    )

    interpretation = _interpret(
        "metric_definition_change",
        _step("SELECT version, definition_hash, query FROM metric_versions ORDER BY version"),
        result,
    )

    assert interpretation.polarity is EvidencePolarity.NEUTRAL
    assert interpretation.evidence is None


def test_f01_healthy_activity_and_partition_cannot_support_missing_partition() -> None:
    business = _sql_result(
        ["android_event_count"],
        [[7]],
        sql="SELECT COUNT(*) AS android_event_count FROM events WHERE device_type = 'android'",
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
            "SELECT COUNT(*) AS android_event_count FROM events WHERE device_type = 'android'",
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
    assert business_interpretation.evidence is not None
    assert partition_interpretation.evidence is not None
    assert all(
        interpretation.polarity is not EvidencePolarity.SUPPORTS
        for interpretation in (business_interpretation, partition_interpretation)
    )


def test_f01_uses_actual_business_and_operational_values() -> None:
    business = _sql_result(
        ["android_event_count"],
        [[0]],
        sql="SELECT COUNT(*) AS android_event_count FROM events WHERE device_type = 'android'",
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
            "SELECT COUNT(*) AS android_event_count FROM events WHERE device_type = 'android'",
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
    assert "row_count=0" in second.evidence.description
    assert "status=missing" in second.evidence.description
    assert first.evidence.evidence_id.startswith("runner-")
    assert first.evidence.evidence_id == _interpret(
        "missing_partition",
        _step(
            "SELECT COUNT(*) AS android_event_count FROM events WHERE device_type = 'android'",
            step_id="S01",
        ),
        business,
    ).evidence.evidence_id


def test_f11_requires_actual_activity_divergence_and_version_change() -> None:
    business = _sql_result(
        ["raw_event_count", "raw_user_count", "daily_active_users"],
        [[373, 111, 59]],
        sql="SELECT raw_event_count, raw_user_count, daily_active_users FROM events",
        query_id="Q-BUSINESS",
    )
    versions = _sql_result(
        ["version", "definition_hash", "query"],
        [[1, "hash-1", "SELECT 1"], [2, "hash-2", "SELECT 2"]],
        sql="SELECT version, definition_hash, query FROM metric_versions ORDER BY version",
        query_id="Q-VERSIONS",
    )

    first = _interpret(
        "metric_definition_change",
        _step(
            "SELECT raw_event_count, raw_user_count, daily_active_users FROM events",
            step_id="S01",
        ),
        business,
    )
    second = _interpret(
        "metric_definition_change",
        _step(
            "SELECT version, definition_hash, query FROM metric_versions ORDER BY version",
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
    assert "changed_fields" in second.evidence.description


def test_failed_data_quality_check_preserves_canonical_evidence() -> None:
    canonical = EvidenceReference(
        evidence_id="dq-canonical-001",
        source_type="business_data",
        description="events.user_id null rate is 5.00%",
        query_id="Q-DQ",
        observation={"observed_value": 0.05},
    )
    result = ToolExecutionResult(
        tool_name="check_null_rate",
        success=True,
        query_id="Q-DQ",
        result={
            "check_name": "check_null_rate",
            "status": "success",
            "passed": False,
            "table": "events",
            "column": "user_id",
            "query_id": "Q-DQ",
            "evidence": [canonical.model_dump(mode="json")],
        },
        evidence=[canonical],
    )

    interpretation = RuntimeEvidenceInterpreter("INC-TEST").interpret(
        hypothesis=_hypothesis("null_value_anomaly"),
        step=InvestigationStep(
            step_id="S01",
            purpose="Check null rate.",
            hypothesis_id="H01",
            tool="check_null_rate",
            arguments={"table": "events", "column": "user_id", "threshold": 0.01},
            expected_evidence=["the failed null-rate check"],
            stop_condition="retain the finding",
        ),
        tool_result=result,
    )

    assert interpretation.polarity is EvidencePolarity.SUPPORTS
    assert interpretation.evidence == canonical
    assert interpretation.evidence.source_type == "business_data"
    assert interpretation.model_dump_json()
