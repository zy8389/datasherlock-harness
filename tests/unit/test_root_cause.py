import pytest

from harness.root_cause import RootCauseConfirmationError, RootCauseConfirmationService
from harness.state import IncidentState, IncidentStatus


def _diagnostic_incident() -> IncidentState:
    return IncidentState(
        alert={"incident_id": "INC-001", "metric": "daily_active_users"},
        status=IncidentStatus.HYPOTHESIS_TESTING,
        tool_trace=[
            {
                "trace_id": "sql:Q01",
                "tool": "sql_runner",
                "query_id": "Q01",
                "response": {"status": "success"},
                "validation": {"evidence": {"usable": True}},
            },
            {
                "trace_id": "sql:Q02",
                "tool": "sql_runner",
                "query_id": "Q02",
                "response": {"status": "success"},
                "validation": {"evidence": {"usable": True}},
            },
        ],
        evidence=[
            {
                "evidence_id": "sql:Q01",
                "query_id": "Q01",
                "tool_trace_id": "sql:Q01",
                "root_cause_type": "missing_partition",
                "source_type": "business_data",
                "asset": "events",
                "repair_context": {"partition_value": "android"},
                "finding": "Android events are absent.",
            },
            {
                "evidence_id": "sql:Q02",
                "query_id": "Q02",
                "tool_trace_id": "sql:Q02",
                "root_cause_type": "missing_partition",
                "source_type": "operational_metadata",
                "asset": "partition_metadata",
                "repair_context": {"partition_value": "android"},
                "finding": "The partition is marked missing.",
            },
        ],
    )


def test_confirmation_derives_a_root_cause_from_trace_bound_evidence() -> None:
    state = _diagnostic_incident()

    RootCauseConfirmationService().confirm(state)

    assert state.status is IncidentStatus.ROOT_CAUSE_FOUND
    assert state.root_cause == {
        "root_cause_type": "missing_partition",
        "confidence": 0.8,
        "affected_assets": ["events", "partition_metadata", "pipeline_runs"],
        "repair_context": {"partition_value": "android"},
    }
    assert [item["source_type"] for item in state.evidence] == [
        "business_data",
        "operational_metadata",
    ]


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda state: state.tool_trace[0]["validation"]["evidence"].update(
                {"usable": False}
            ),
            "usable tool result",
        ),
        (
            lambda state: state.evidence[1].update({"source_type": "business_data"}),
            "no trace-bound evidence",
        ),
        (
            lambda state: state.evidence[1].update({"repair_context": {}}),
            "no trace-bound evidence",
        ),
    ],
)
def test_confirmation_rejects_untrusted_or_incomplete_diagnostics(
    mutate, message: str
) -> None:
    state = _diagnostic_incident()
    mutate(state)

    with pytest.raises(RootCauseConfirmationError, match=message):
        RootCauseConfirmationService().confirm(state)
