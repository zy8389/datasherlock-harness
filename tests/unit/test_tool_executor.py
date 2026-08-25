from typing import Any

from tools.executor import ToolExecutor
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
