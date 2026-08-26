import pytest

from harness.evidence import bind_sql_validation_to_incident
from harness.state import IncidentState
from tools.sql_runner import SqlExecutionResponse
from validators.sql_result import SqlResultExpectation, validate_sql_result


def _response(**overrides: object) -> SqlExecutionResponse:
    payload: dict[str, object] = {
        "query_id": "query-123",
        "status": "success",
        "statement_type": "SELECT",
        "columns": ["daily_active_users"],
        "column_types": ["INTEGER"],
        "rows": [[7600]],
        "row_count": 1,
        "truncated": False,
        "duration_ms": 4.2,
    }
    payload.update(overrides)
    return SqlExecutionResponse.model_validate(payload)


def test_usable_sql_result_is_traced_and_promoted_to_incident_evidence() -> None:
    state = IncidentState()
    response = _response()
    validation = validate_sql_result(
        response,
        SqlResultExpectation(required_columns=["daily_active_users"]),
    )

    returned = bind_sql_validation_to_incident(
        state,
        response,
        validation,
        finding="Daily active users are below the expected range.",
    )

    assert returned is state
    assert state.tool_trace == [
        {
            "trace_id": "sql:query-123",
            "tool": "sql_runner",
            "query_id": "query-123",
            "response": response.model_dump(mode="json"),
            "validation": validation.model_dump(mode="json"),
        }
    ]
    assert state.evidence == [
        {
            "evidence_id": "sql:query-123",
            "source_type": "sql_query",
            "query_id": "query-123",
            "tool_trace_id": "sql:query-123",
            "finding": "Daily active users are below the expected range.",
            "validation": validation.evidence.model_dump(mode="json"),
        }
    ]


def test_unusable_sql_result_is_traced_but_not_promoted_to_evidence() -> None:
    state = IncidentState()
    response = _response(rows=[], row_count=0)
    validation = validate_sql_result(response)

    bind_sql_validation_to_incident(state, response, validation)

    assert len(state.tool_trace) == 1
    assert state.tool_trace[0]["validation"]["reason"] == "empty_result"
    assert state.evidence == []


def test_binding_same_query_is_idempotent_after_incident_replay() -> None:
    state = IncidentState()
    response = _response()
    validation = validate_sql_result(response)

    bind_sql_validation_to_incident(state, response, validation)
    bind_sql_validation_to_incident(state, response, validation)

    assert len(state.tool_trace) == 1
    assert len(state.evidence) == 1


def test_binding_rejects_response_from_a_different_query() -> None:
    response = _response()
    validation = validate_sql_result(response)
    other_response = _response(query_id="query-456")

    with pytest.raises(ValueError, match="same query_id"):
        bind_sql_validation_to_incident(IncidentState(), other_response, validation)
