from datetime import UTC, datetime, timedelta

import pytest

from harness.repair import RepairAction, RepairRisk
from harness.repair_proposal import RepairProposalBuilder, RepairProposalBuildError
from harness.state import IncidentState, IncidentStatus


def _rooted_incident() -> IncidentState:
    return IncidentState(
        alert={"incident_id": "INC-001"},
        status=IncidentStatus.ROOT_CAUSE_FOUND,
        root_cause={
            "root_cause_type": "missing_partition",
            "confidence": 0.93,
            "affected_assets": ["events", "partition_metadata", "pipeline_runs"],
            "repair_context": {"partition_value": "android"},
        },
        evidence=[
            {
                "evidence_id": "E01",
                "source_type": "business_data",
                "asset": "events",
                "finding": "Android events are absent.",
            },
            {
                "evidence_id": "E02",
                "source_type": "operational_metadata",
                "asset": "partition_metadata",
                "finding": "The partition is marked missing.",
            },
        ],
    )


def test_builder_generates_the_fixed_f01_sandbox_repair() -> None:
    now = datetime(2026, 8, 27, 8, tzinfo=UTC)
    proposal = RepairProposalBuilder(now=lambda: now).build(_rooted_incident())

    assert proposal.action is RepairAction.RERUN_PARTITION
    assert proposal.risk is RepairRisk.MEDIUM
    assert proposal.parameters == {
        "table": "events",
        "source_table": "events",
        "partition_column": "device_type",
        "partition_value": "android",
    }
    assert proposal.created_at == now
    assert proposal.valid_until == now + timedelta(hours=1)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda state: state.root_cause.update({"root_cause_type": "data_delay"}),
            "no deterministic repair proposal",
        ),
        (
            lambda state: state.evidence.pop(),
            "independent sources",
        ),
    ],
)
def test_builder_rejects_unsupported_or_insufficient_diagnostic_state(
    mutate, message: str
) -> None:
    state = _rooted_incident()
    mutate(state)

    with pytest.raises(RepairProposalBuildError, match=message):
        RepairProposalBuilder().build(state)
