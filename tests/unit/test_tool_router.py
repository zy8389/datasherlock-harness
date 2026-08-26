from pathlib import Path

import duckdb
import pytest

from harness.state import IncidentState
from harness.tool_router import InvestigationToolRouter
from tools.registry import ToolArgumentsError
from validators.sql_result import SqlResultExpectation


def _database_path(tmp_path: Path) -> Path:
    database_path = tmp_path / "router.duckdb"
    with duckdb.connect(str(database_path)) as connection:
        connection.execute("CREATE TABLE events (event_id INTEGER)")
        connection.execute("INSERT INTO events VALUES (1)")
    return database_path


def test_sql_query_route_cannot_bypass_result_validation(tmp_path: Path) -> None:
    state = IncidentState()

    outcome = InvestigationToolRouter().execute(
        state,
        "sql_query",
        {"sql": "SELECT event_id FROM events"},
        database_path=_database_path(tmp_path),
        expectation=SqlResultExpectation(required_columns=["event_id"]),
    )

    assert outcome.response.status == "success"
    assert outcome.validation.passed is True
    assert len(state.tool_trace) == 1
    assert len(state.evidence) == 1
    assert state.evidence[0]["query_id"] == outcome.response.query_id


def test_sql_query_route_records_failed_validation_without_evidence(tmp_path: Path) -> None:
    state = IncidentState()

    outcome = InvestigationToolRouter().execute(
        state,
        "sql_query",
        {"sql": "SELECT event_id FROM events WHERE 1 = 0"},
        database_path=_database_path(tmp_path),
    )

    assert outcome.validation.reason == "empty_result"
    assert len(state.tool_trace) == 1
    assert state.evidence == []


def test_sql_query_route_validates_registered_arguments_before_execution(
    tmp_path: Path,
) -> None:
    with pytest.raises(ToolArgumentsError, match="unknown field"):
        InvestigationToolRouter().execute(
            IncidentState(),
            "sql_query",
            {"query": "SELECT event_id FROM events"},
            database_path=_database_path(tmp_path),
        )
