"""Bind validated tool outputs to the persisted incident investigation state."""

from __future__ import annotations

from harness.state import IncidentState
from tools.sql_runner import SqlExecutionResponse
from validators.sql_result import SqlResultValidation


def bind_sql_validation_to_incident(
    state: IncidentState,
    response: SqlExecutionResponse,
    validation: SqlResultValidation,
    *,
    finding: str | None = None,
) -> IncidentState:
    """Record a SQL validation and promote only usable results to evidence.

    The operation is idempotent for one query id so a resumed incident does not
    duplicate its trace or evidence after replaying the same tool result.
    """

    if response.query_id != validation.evidence.query_id:
        raise ValueError("response and validation must have the same query_id")

    trace_id = f"sql:{response.query_id}"
    if not any(entry.get("trace_id") == trace_id for entry in state.tool_trace):
        state.tool_trace.append(
            {
                "trace_id": trace_id,
                "tool": "sql_runner",
                "query_id": response.query_id,
                "response": response.model_dump(mode="json"),
                "validation": validation.model_dump(mode="json"),
            }
        )

    evidence_id = f"sql:{response.query_id}"
    if validation.evidence.usable and not any(
        entry.get("evidence_id") == evidence_id for entry in state.evidence
    ):
        state.evidence.append(
            {
                "evidence_id": evidence_id,
                "source_type": "sql_query",
                "query_id": response.query_id,
                "tool_trace_id": trace_id,
                "finding": finding
                or (
                    f"SQL result {response.query_id} passed validation with "
                    f"{response.row_count} returned row(s)."
                ),
                "validation": validation.evidence.model_dump(mode="json"),
            }
        )
    return state
