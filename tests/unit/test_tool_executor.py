from typing import Any

import duckdb
import pytest

from tools.data_quality import DataQualityCheckResult, DataQualityEvidence
from tools.executor import ToolExecutor, data_quality_evidence_to_reference
from tools.sql_runner import SqlExecutionResponse
from validators.sql_result import SqlResultFailureReason


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
            column_types=["INTEGER"],
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


def test_executor_forwards_runtime_timeout_and_row_limit_to_sql_adapter() -> None:
    calls: list[dict[str, object]] = []

    def run_sql(_: str, __: str, **kwargs: object) -> SqlExecutionResponse:
        calls.append(kwargs)
        return SqlExecutionResponse(
            query_id="Q-LIMITED",
            status="success",
            statement_type="SELECT",
            columns=["answer"],
            column_types=["INTEGER"],
            rows=[[1]],
            row_count=1,
        )

    result = ToolExecutor("test.duckdb", sql_execution=run_sql).execute_step(
        _step(), timeout_seconds=2.5, max_rows=7
    )

    assert result.success is True
    assert calls == [
        {
            "incident_id": None,
            "trace_id": None,
            "audit_path": None,
            "timeout_seconds": 2.5,
            "max_rows": 7,
        }
    ]


def test_executor_attaches_pure_sql_validation_without_evidence() -> None:
    def run_sql(_: str, __: str, **_kwargs: object) -> SqlExecutionResponse:
        return SqlExecutionResponse(
            query_id="Q-VALIDATED",
            status="success",
            statement_type="SELECT",
            columns=["answer"],
            column_types=["INTEGER"],
            rows=[[1]],
            row_count=1,
        )

    result = ToolExecutor("test.duckdb", sql_execution=run_sql).execute_step(_step())

    assert result.success is True
    assert result.sql_validation is not None
    assert result.sql_validation.passed is True
    assert result.evidence == []


def test_executor_keeps_sql_success_separate_from_empty_result_validation() -> None:
    def run_sql(_: str, __: str, **_kwargs: object) -> SqlExecutionResponse:
        return SqlExecutionResponse(
            query_id="Q-EMPTY",
            status="success",
            statement_type="SELECT",
            columns=["answer"],
            column_types=["INTEGER"],
            rows=[],
            row_count=0,
        )

    result = ToolExecutor("test.duckdb", sql_execution=run_sql).execute_step(_step())

    assert result.success is True
    assert result.sql_validation is not None
    assert result.sql_validation.reason is SqlResultFailureReason.EMPTY_RESULT
    assert result.evidence == []


def test_executor_keeps_sql_execution_failure_separate_from_validation() -> None:
    def run_sql(_: str, __: str, **_kwargs: object) -> SqlExecutionResponse:
        return SqlExecutionResponse(
            query_id="Q-ERROR",
            status="error",
            error={"type": "execution", "message": "missing table"},
        )

    result = ToolExecutor("test.duckdb", sql_execution=run_sql).execute_step(_step())

    assert result.success is False
    assert result.sql_validation is not None
    assert result.sql_validation.reason is SqlResultFailureReason.SQL_EXECUTION_FAILED
    assert result.evidence == []


def test_executor_applies_metric_policy_only_when_metric_output_is_present() -> None:
    def run_sql(_: str, __: str, **_kwargs: object) -> SqlExecutionResponse:
        return SqlExecutionResponse(
            query_id="Q-METRIC",
            status="success",
            statement_type="SELECT",
            columns=["metric_date", "daily_active_users"],
            column_types=["DATE", "BIGINT"],
            rows=[["2026-08-12", 10]],
            row_count=1,
        )

    metric_sql = (
        "SELECT CAST('2026-08-12' AS DATE) AS metric_date, "
        "COUNT(DISTINCT 1) AS daily_active_users"
    )
    result = ToolExecutor("test.duckdb", sql_execution=run_sql).execute_step(
        _step(metric_sql), metric_id="daily_active_users"
    )

    assert result.success is True
    assert result.sql_validation is not None
    assert result.sql_validation.passed is True
    assert result.sql_validation.evidence.ast_validated is True


def test_executor_metric_policy_is_order_insensitive_for_exact_metric_columns() -> None:
    def run_sql(_: str, __: str, **_kwargs: object) -> SqlExecutionResponse:
        return SqlExecutionResponse(
            query_id="Q-METRIC-REORDERED",
            status="success",
            statement_type="SELECT",
            columns=["daily_active_users", "metric_date"],
            column_types=["BIGINT", "DATE"],
            rows=[[10, "2026-08-12"]],
            row_count=1,
        )

    metric_sql = (
        "SELECT COUNT(DISTINCT 1) AS daily_active_users, "
        "CAST('2026-08-12' AS DATE) AS metric_date"
    )
    result = ToolExecutor("test.duckdb", sql_execution=run_sql).execute_step(
        _step(metric_sql), metric_id="daily_active_users"
    )

    assert result.success is True
    assert result.sql_validation is not None
    assert result.sql_validation.passed is True
    assert result.sql_validation.evidence.ast_validated is True


def test_executor_does_not_apply_metric_policy_to_diagnostic_comparison_output() -> None:
    def run_sql(_: str, __: str, **_kwargs: object) -> SqlExecutionResponse:
        return SqlExecutionResponse(
            query_id="Q-DIAGNOSTIC",
            status="success",
            statement_type="SELECT",
            columns=["raw_event_count", "raw_user_count", "daily_active_users"],
            column_types=["BIGINT", "BIGINT", "BIGINT"],
            rows=[[373, 111, 59]],
            row_count=1,
        )

    diagnostic_sql = (
        "SELECT COUNT(*) AS raw_event_count, "
        "COUNT(DISTINCT user_id) AS raw_user_count, "
        "59 AS daily_active_users FROM events"
    )
    result = ToolExecutor("test.duckdb", sql_execution=run_sql).execute_step(
        _step(diagnostic_sql), metric_id="daily_active_users"
    )

    assert result.success is True
    assert result.sql_validation is not None
    assert result.sql_validation.passed is True
    assert result.sql_validation.missing_columns == []
    assert result.sql_validation.evidence.ast_validated is True


def test_executor_forwards_runtime_timeout_to_data_quality_adapter() -> None:
    calls: list[dict[str, object]] = []

    def fake_check_null_rate(
        _: str,
        table: str,
        column: str,
        **kwargs: object,
    ) -> DataQualityCheckResult:
        calls.append(kwargs)
        return DataQualityCheckResult(
            check_name="check_null_rate",
            status="success",
            passed=True,
            table=table,
            column=column,
            observed_value=0.0,
            threshold=0.01,
            query_id="Q-DQ-LIMITED",
        )

    result = ToolExecutor(
        "test.duckdb",
        data_quality_execution={"check_null_rate": fake_check_null_rate},
    ).execute_step(
        _quality_step(
            "check_null_rate",
            {"table": "events", "column": "user_id"},
        ),
        timeout_seconds=3.5,
    )

    assert result.success is True
    assert calls == [
        {
            "incident_id": None,
            "trace_id": None,
            "audit_path": None,
            "timeout_seconds": 3.5,
        }
    ]


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


def test_executor_empty_data_quality_mapping_disables_all_quality_adapters() -> None:
    result = ToolExecutor(
        "unused.duckdb",
        data_quality_execution={},
    ).execute_step(
        _quality_step(
            "check_null_rate",
            {"table": "events", "column": "user_id"},
        )
    )

    assert result.success is False
    assert result.error == {
        "type": "unsupported_tool",
        "message": "no execution adapter is registered for tool: check_null_rate",
    }


def test_executor_custom_data_quality_mapping_only_enables_injected_adapters() -> None:
    calls: list[tuple[str, str, str]] = []

    def fake_check_null_rate(
        database_path: str,
        table: str,
        column: str,
        **_: object,
    ) -> DataQualityCheckResult:
        calls.append((database_path, table, column))
        return DataQualityCheckResult(
            check_name="check_null_rate",
            status="success",
            passed=True,
            table=table,
            column=column,
            observed_value=0.0,
            threshold=0.01,
            query_id="Q-CUSTOM-001",
        )

    executor = ToolExecutor(
        "injected.duckdb",
        data_quality_execution={"check_null_rate": fake_check_null_rate},
    )
    enabled = executor.execute_step(
        _quality_step(
            "check_null_rate",
            {"table": "events", "column": "user_id"},
        )
    )
    disabled = executor.execute_step(
        _quality_step(
            "check_duplicate_rate",
            {"table": "events", "keys": ["event_id"]},
        )
    )

    assert enabled.success is True
    assert enabled.query_id == "Q-CUSTOM-001"
    assert calls == [("injected.duckdb", "events", "user_id")]
    assert disabled.success is False
    assert disabled.error == {
        "type": "unsupported_tool",
        "message": "no execution adapter is registered for tool: check_duplicate_rate",
    }


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


def test_executor_treats_insufficient_schema_history_as_success(tmp_path) -> None:
    database_path = tmp_path / "insufficient-schema-history.duckdb"
    with duckdb.connect(str(database_path)) as connection:
        connection.execute(
            """
            CREATE TABLE schema_snapshots (
                table_name VARCHAR,
                version INTEGER,
                schema_json VARCHAR,
                effective_at TIMESTAMP
            )
            """
        )

    result = ToolExecutor(database_path).execute_step(
        _quality_step("detect_schema_drift", {"table": "events"}),
        incident_id="INC-DQ-INCONCLUSIVE",
    )

    assert result.success is True
    assert result.error is None
    assert result.result["status"] == "success"
    assert result.result["passed"] is None
    assert result.result["error"] is None
    assert result.evidence[0].observation["passed"] is None
    assert result.evidence[0].observation["details"] == {
        "assessment": "insufficient_history",
        "snapshot_count": 0,
        "required_snapshot_count": 2,
    }
    assert result.model_validate_json(result.model_dump_json()) == result


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


@pytest.mark.parametrize(
    ("tool_name", "table", "expected_source"),
    [
        ("detect_schema_drift", "events", "schema_metadata"),
        ("check_null_rate", "partition_metadata", "operational_metadata"),
        ("check_null_rate", "pipeline_runs", "operational_metadata"),
        ("check_null_rate", "metric_versions", "metric_version"),
        ("check_null_rate", "experiment_configs", "experiment_config"),
        ("check_null_rate", "events", "business_data"),
    ],
)
def test_data_quality_provenance_uses_the_canonical_asset_policy(
    tool_name: str,
    table: str,
    expected_source: str,
) -> None:
    evidence = DataQualityEvidence(
        finding="one deterministic finding",
        query_id="Q-PROVENANCE",
        details={},
    )
    result = DataQualityCheckResult(
        check_name=tool_name,
        status="success",
        passed=False,
        table=table,
        query_id="Q-PROVENANCE",
        evidence=[evidence],
    )

    reference = data_quality_evidence_to_reference(
        evidence,
        result=result,
        tool_name=tool_name,
        incident_id="INC-PROVENANCE",
        sequence=1,
    )

    assert reference.source_type == expected_source


def test_data_quality_provenance_rejects_unknown_assets() -> None:
    evidence = DataQualityEvidence(
        finding="one deterministic finding",
        query_id="Q-UNKNOWN",
        details={},
    )
    result = DataQualityCheckResult(
        check_name="check_null_rate",
        status="success",
        passed=False,
        table="unknown_asset",
        query_id="Q-UNKNOWN",
        evidence=[evidence],
    )

    with pytest.raises(ValueError, match="unknown source asset"):
        data_quality_evidence_to_reference(
            evidence,
            result=result,
            tool_name="check_null_rate",
            incident_id="INC-PROVENANCE",
            sequence=1,
        )


def test_executor_normalizes_unknown_data_quality_asset_as_contract_failure() -> None:
    def fake_check_null_rate(
        _: str,
        table: str,
        column: str,
        **__: object,
    ) -> DataQualityCheckResult:
        return DataQualityCheckResult(
            check_name="check_null_rate",
            status="success",
            passed=False,
            table="unknown_asset",
            column=column,
            query_id="Q-UNKNOWN-ASSET",
            evidence=[
                DataQualityEvidence(
                    finding=f"unexpected provenance for {table}",
                    query_id="Q-UNKNOWN-ASSET",
                    details={},
                )
            ],
        )

    result = ToolExecutor(
        "unused.duckdb",
        data_quality_execution={"check_null_rate": fake_check_null_rate},
    ).execute_step(
        _quality_step(
            "check_null_rate",
            {"table": "events", "column": "user_id"},
        )
    )

    assert result.success is False
    assert result.query_id == "Q-UNKNOWN-ASSET"
    assert result.error is not None
    assert result.error["type"] == "tool_contract"
    assert "unknown source asset" in result.error["message"]
    assert result.evidence == []
