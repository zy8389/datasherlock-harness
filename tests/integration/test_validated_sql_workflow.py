from pathlib import Path

import duckdb

from harness.sql_investigation import execute_validated_sql
from harness.state import IncidentState
from validators.sql_result import SqlResultExpectation


def _database_path(tmp_path: Path) -> Path:
    database_path = tmp_path / "investigation.duckdb"
    with duckdb.connect(str(database_path)) as connection:
        connection.execute(
            "CREATE TABLE events (event_id INTEGER, user_id INTEGER, event_time TIMESTAMP)"
        )
        connection.execute(
            "INSERT INTO events VALUES "
            "(1, 10, '2026-08-12 10:00:00'), "
            "(2, 20, '2026-08-12 11:00:00')"
        )
        connection.execute(
            "CREATE TABLE assignments (user_id INTEGER, variant VARCHAR)"
        )
        connection.execute(
            "INSERT INTO assignments VALUES "
            "(10, 'a'), (10, 'b'), (20, 'a'), (20, 'b')"
        )
    return database_path


def test_validated_sql_workflow_records_only_usable_evidence(tmp_path: Path) -> None:
    database_path = _database_path(tmp_path)
    state = IncidentState(alert={"incident_id": "INC-001"})

    normal = execute_validated_sql(
        state,
        database_path,
        (
            "SELECT CAST(event_time AS DATE) AS metric_date, "
            "COUNT(DISTINCT user_id) AS daily_active_users "
            "FROM events GROUP BY metric_date"
        ),
        metric_id="daily_active_users",
        finding="Daily active users were measured for the target day.",
    )
    empty = execute_validated_sql(
        state,
        database_path,
        "SELECT event_id FROM events WHERE 1 = 0",
        expectation=SqlResultExpectation(
            expected_column_types={"event_id": "INTEGER"}
        ),
    )
    missing_column = execute_validated_sql(
        state,
        database_path,
        "SELECT event_id AS wrong_name FROM events LIMIT 1",
        expectation=SqlResultExpectation(required_columns=["event_id"]),
    )
    expanded_join = execute_validated_sql(
        state,
        database_path,
        (
            "SELECT events.event_id, assignments.variant FROM events "
            "JOIN assignments ON events.user_id = assignments.user_id"
        ),
        expectation=SqlResultExpectation(
            expected_column_types={"event_id": "INTEGER", "variant": "VARCHAR"},
            max_result_rows=2,
        ),
    )

    assert normal.response.status == "success"
    assert normal.validation.passed is True
    assert empty.validation.reason == "empty_result"
    assert missing_column.validation.reason == "missing_required_columns"
    assert expanded_join.validation.reason == "result_row_count_exceeded"
    assert len(state.tool_trace) == 4
    assert len(state.evidence) == 1
    assert state.evidence[0]["finding"] == (
        "Daily active users were measured for the target day."
    )
    assert state.evidence[0]["query_id"] == normal.response.query_id

    restored = IncidentState.from_json(state.to_json())
    assert restored == state
    assert restored.tool_trace[3]["validation"]["reason"] == "result_row_count_exceeded"
