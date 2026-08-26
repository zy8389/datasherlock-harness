import pytest
from pydantic import BaseModel, ValidationError

from harness.state import IncidentState, IncidentStatus


def test_incident_state_defaults_do_not_share_mutable_values() -> None:
    first = IncidentState()
    second = IncidentState()

    first.alert["incident_id"] = "INC-001"
    first.plan.append({"step": "inspect_events"})

    assert second.alert == {}
    assert second.plan == []
    assert isinstance(first, BaseModel)
    assert first.status is IncidentStatus.RECEIVED
    assert first.final_status is None


def test_incident_state_json_round_trip_preserves_checkpoint() -> None:
    state = IncidentState(
        alert={
            "incident_id": "INC-001",
            "metric": "daily_active_users",
            "severity": "high",
        },
        plan=[{"step_id": "P01", "action": "inspect_event_volume"}],
        hypotheses=[
            {"hypothesis_id": "H01", "root_cause_type": "missing_partition"}
        ],
        evidence=[{"evidence_id": "E01", "finding": "mobile events are missing"}],
        tool_trace=[
            {
                "tool": "sql_runner",
                "query": "SELECT COUNT(*) FROM events",
                "status": "ok",
            }
        ],
        root_cause={
            "root_cause_type": "missing_partition",
            "affected_asset": "events_mobile_20260812",
            "confidence": 0.94,
        },
        status=IncidentStatus.RESOLVED,
        final_status=IncidentStatus.RESOLVED,
        rejected_hypotheses=[{"hypothesis_id": "H02", "reason": "data is fresh"}],
        retry_count=1,
        token_cost=1250.5,
        current_conclusion="The mobile event partition was not written.",
    )

    serialized = state.to_json()
    restored = IncidentState.from_json(serialized)

    assert restored == state
    assert restored.status is IncidentStatus.RESOLVED
    assert restored.final_status is IncidentStatus.RESOLVED

    restored.evidence[0]["finding"] = "changed after restore"
    assert restored != state
    assert state.evidence[0]["finding"] == "mobile events are missing"


def test_incident_state_validates_checkpoint_fields_and_assignments() -> None:
    with pytest.raises(ValidationError):
        IncidentState(alert={"unsupported": object()})

    with pytest.raises(ValidationError):
        IncidentState.from_dict({"unknown_field": "value"})

    state = IncidentState(status="EXECUTING")
    assert state.status is IncidentStatus.EXECUTING

    with pytest.raises(ValidationError):
        state.retry_count = -1

    with pytest.raises(ValidationError):
        state.token_cost = -0.1


def test_incident_status_identifies_terminal_states() -> None:
    assert {status.value for status in IncidentStatus} == {
        "RECEIVED",
        "TRIAGE",
        "PLANNING",
        "EXECUTING",
        "VALIDATING",
        "HYPOTHESIS_TESTING",
        "ROOT_CAUSE_FOUND",
        "FIX_PROPOSED",
        "AWAITING_APPROVAL",
        "SANDBOX_REPAIR",
        "POST_VALIDATION",
        "RESOLVED",
        "REJECTED",
        "UNRESOLVED",
        "BUDGET_EXCEEDED",
        "TOOL_FAILED",
        "VALIDATION_FAILED",
    }
    assert IncidentStatus.RESOLVED.is_terminal
    assert IncidentStatus.UNRESOLVED.is_terminal
    assert not IncidentStatus.EXECUTING.is_terminal
