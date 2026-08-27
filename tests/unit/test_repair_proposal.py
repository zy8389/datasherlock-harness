from datetime import UTC, datetime

import pytest

from harness.repair_proposal import RepairProposalBuilder, RepairProposalBuildError
from harness.state import IncidentState, IncidentStatus


def rooted_state() -> IncidentState:
    return IncidentState(
        alert={
            "incident_id": "INC-F01",
            "metric": "daily_active_users",
            "observed_at": "2026-01-30",
            "device_type": "android",
        },
        status=IncidentStatus.ROOT_CAUSE_FOUND,
        root_cause={
            "hypothesis_id": "H01",
            "root_cause_type": "missing_partition",
            "confidence": 0.92,
            "supporting_evidence_ids": ["E01", "E02"],
            "independent_source_types": ["business_data", "operational_metadata"],
        },
        evidence=[
            {
                "evidence_id": "E01",
                "source_type": "business_data",
                "description": "target Android events are absent",
                "observation": {
                    "target_date": "2026-01-30",
                    "observed_row": {"android_event_count": 0},
                },
            },
            {
                "evidence_id": "E02",
                "source_type": "operational_metadata",
                "description": "partition metadata reports missing",
                "observation": {
                    "target_date": "2026-01-30",
                    "observed_row": {
                        "partition_value": "2026-01-30/android",
                        "row_count": 0,
                        "status": "missing",
                    },
                },
            },
        ],
    )


def test_builder_derives_partition_scope_from_structured_runtime_observations() -> None:
    now = datetime(2026, 1, 30, 13, tzinfo=UTC)
    item = RepairProposalBuilder(now=lambda: now).build(rooted_state())

    assert item.parameters == {
        "table": "events",
        "source_table": "events",
        "partition_column": "device_type",
        "partition_value": "2026-01-30/android",
    }
    assert item.supporting_evidence_ids == ("E01", "E02")
    assert item.independent_source_types == (
        "business_data",
        "operational_metadata",
    )
    assert item.proposal_hash == item.content_hash()


@pytest.mark.parametrize(
    "mutation",
    [
        lambda state: state.evidence[0].pop("observation"),
        lambda state: state.evidence[1]["observation"]["observed_row"].update(
            {"status": "ready"}
        ),
        lambda state: state.evidence[1]["observation"]["observed_row"].update(
            {"partition_value": "2026-01-31/android"}
        ),
    ],
)
def test_builder_fails_closed_without_provable_f01_scope(mutation) -> None:
    state = rooted_state()
    mutation(state)
    with pytest.raises(RepairProposalBuildError):
        RepairProposalBuilder().build(state)


def test_builder_requires_confirmed_root_cause_state() -> None:
    state = rooted_state()
    state.status = IncidentStatus.AWAITING_APPROVAL
    with pytest.raises(RepairProposalBuildError, match="ROOT_CAUSE_FOUND"):
        RepairProposalBuilder().build(state)
