from typing import Any

import duckdb
import pytest

from tools.data_quality import DataQualityCheckResult, DataQualityEvidence
from tools.executor import ToolExecutor, data_quality_evidence_to_reference
from tools.sql_runner import SqlExecutionResponse


def _step(sql: str = "SELECT 1") -> dict[str, Any]:
    return {
        "step_id": "S01",
        "purpose": "Inspect one bounded result.",
        "hypothesis_id": "H01",
        "tool": "sql_query",
        "arguments": {"sql": sql},
        "expected_evidence": ["one row"],
        "stop_condition": "stop after the query",
    }


def _quality_step(tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return {
        "step_id": "S01",
        "purpose": f"Run {tool}.",
        "hypothesis_id": "H01",
        "tool": tool,
        "arguments": arguments,
        "expected_evidence": ["one structured check result"],
        "stop_condition": "stop after the check",
    }


def test_executor_validates_registry_and_normalizes_sql_success() -> None:
    calls: list[tuple[str, str]] = []

    def run_sql(database_path: str, sql: str, **_: object) -> SqlExecutionResponse:
        calls.append((database_path, sql))
        return SqlExecutionResponse(
            query_id="Q01",
            status="success",
            statement_type="SELECT",
            columns=["answer"],
            rows=[[1]],
            row_count=1,
        )

    result = ToolExecutor("test.duckdb", sql_execution=run_sql).execute_step(
        _step(), incident_id="INC-001"
    )

    assert result.success is True
    assert result.query_id == "Q01"
    assert result.error is None
    assert result.evidence == []
    assert calls == [("test.duckdb", "SELECT 1")]
    assert result.model_dump_json()


def test_executor_rejects_unknown_tool_without_calling_adapter() -> None:
    calls = 0

    def run_sql(_: str, __: str, **___: object) -> SqlExecutionResponse:
        nonlocal calls
        calls += 1
        raise AssertionError("unknown tools must not reach the SQL adapter")

    result = ToolExecutor("test.duckdb", sql_execution=run_sql).execute_step(
        {**_step(), "tool": "unknown_tool"}
    )

    assert result.success is False
    assert result.error == {
        "type": "tool_contract",
        "message": "unknown tool: unknown_tool",
    }
    assert calls == 0


def test_executor_does_not_duplicate_sql_guardrails() -> None:
    calls = 0

    def run_sql(_: str, __: str, **___: object) -> SqlExecutionResponse:
        nonlocal calls
        calls += 1
        raise AssertionError("the existing SQL runner must reject unsafe SQL")

    result = ToolExecutor("test.duckdb", sql_execution=run_sql).execute_step(
        _step("DELETE FROM events")
    )

    assert result.success is False
    assert result.error is not None
    assert result.error["type"] == "tool_contract"
    assert calls == 0


@pytest.mark.parametrize(
    ("tool", "arguments", "setup_sql", "source_type"),
    [
        (
            "check_null_rate",
            {"table": "events", "column": "user_id", "threshold": 0.01},
            "CREATE TABLE events (user_id INTEGER); INSERT INTO events VALUES (1), (NULL)",
            "business_data",
        ),
        (
            "check_duplicate_rate",
            {"table": "events", "keys": ["event_id"], "threshold": 0.0},
            "CREATE TABLE events (event_id INTEGER); INSERT INTO events VALUES (1), (1)",
            "business_data",
        ),
        (
            "check_freshness",
            {
                "table": "events",
                "timestamp_column": "event_time",
                "reference_time": "2026-01-30T12:00:00+00:00",
                "max_age": 3600,
            },
            (
                "CREATE TABLE events (event_time TIMESTAMP); "
                "INSERT INTO events VALUES ('2026-01-30 11:55:00')"
            ),
            "business_data",
        ),
        (
            "detect_schema_drift",
            {"table": "events"},
            (
                "CREATE TABLE schema_snapshots (table_name VARCHAR, version INTEGER, "
                "schema_json VARCHAR, effective_at TIMESTAMP); "
                "INSERT INTO schema_snapshots VALUES "
                "('events', 1, '{\"app_build_number\": \"BIGINT\"}', '2026-01-29'), "
                "('events', 2, '{\"app_build_number\": \"VARCHAR\"}', '2026-01-30')"
            ),
            "schema_metadata",
        ),
        (
            "detect_distribution_drift",
            {
                "table": "events",
                "column": "event_name",
                "time_column": "event_time",
                "baseline_start": "2026-01-29T00:00:00+00:00",
                "baseline_end": "2026-01-30T00:00:00+00:00",
                "current_start": "2026-01-30T00:00:00+00:00",
                "current_end": "2026-01-31T00:00:00+00:00",
                "threshold": 0.1,
            },
            (
                "CREATE TABLE events (event_time TIMESTAMP, event_name VARCHAR); "
                "INSERT INTO events VALUES "
                "('2026-01-29 12:00:00', 'login'), "
                "('2026-01-29 12:00:00', 'run_ai_task'), "
                "('2026-01-30 12:00:00', 'login'), "
                "('2026-01-30 12:00:00', 'execute_ai_task')"
            ),
            "business_data",
        ),
    ],
)
def test_executor_runs_each_data_quality_tool_and_converts_evidence(
    tool: str,
    arguments: dict[str, Any],
    setup_sql: str,
    source_type: str,
    tmp_path,
) -> None:
    database_path = tmp_path / f"{tool}.duckdb"
    with duckdb.connect(str(database_path)) as connection:
        for statement in setup_sql.split("; "):
            connection.execute(statement)

    result = ToolExecutor(database_path).execute_step(
        _quality_step(tool, arguments), incident_id="INC-DQ-001"
    )

    assert result.success is True
    assert result.query_id
    assert result.error is None
    assert result.result["check_name"] == tool
    assert len(result.evidence) == 1
    assert result.evidence[0].source_type == source_type
    assert result.evidence[0].query_id == result.query_id
    assert result.evidence[0].observation["check_name"] == tool
    assert result.model_dump_json()


def test_executor_rejects_data_quality_contract_errors_before_execution() -> None:
    result = ToolExecutor("unused.duckdb").execute_step(
        _quality_step(
            "check_null_rate",
            {"table": "events", "column": "user_id", "threshold": "0.1"},
        )
    )

    assert result.success is False
    assert result.error is not None
    assert result.error["type"] == "tool_contract"
    assert result.result is None


def test_executor_returns_structured_data_quality_tool_failure(tmp_path) -> None:
    database_path = tmp_path / "missing-schema-table.duckdb"
    with duckdb.connect(str(database_path)):
        pass

    result = ToolExecutor(database_path).execute_step(
        _quality_step("detect_schema_drift", {"table": "events"}),
        incident_id="INC-DQ-FAIL",
    )

    assert result.success is False
    assert result.query_id
    assert result.result["check_name"] == "detect_schema_drift"
    assert result.result["status"] == "error"
    assert result.evidence == []
    assert result.error is not None


def test_data_quality_evidence_id_is_deterministic_for_same_observation() -> None:
    result = DataQualityCheckResult(
        check_name="check_null_rate",
        status="success",
        passed=False,
        table="events",
        column="user_id",
        observed_value=0.5,
        threshold=0.01,
        query_id="Q-DQ-001",
        evidence=[
            DataQualityEvidence(
                finding="events.user_id null rate is 50.00%",
                query_id="Q-DQ-001",
                details={"null_rate": 0.5},
            )
        ],
    )

    first = data_quality_evidence_to_reference(
        result.evidence[0],
        result=result,
        tool_name="check_null_rate",
        incident_id="INC-DQ-001",
        sequence=1,
    )
    second = data_quality_evidence_to_reference(
        result.evidence[0],
        result=result,
        tool_name="check_null_rate",
        incident_id="INC-DQ-001",
        sequence=1,
    )

    assert first == second
    assert first.evidence_id.startswith("dq-")
    assert first.query_id == "Q-DQ-001"
    assert first.observation["details"] == {"null_rate": 0.5}
