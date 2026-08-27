from datetime import UTC, datetime, timedelta

import pytest

from config.faults import EvidenceSourceType
from harness.approval import (
    ApprovalValidationError,
    validate_approval_decision,
)
from harness.repair import (
    ApprovalDecision,
    ApprovalOutcome,
    RepairAction,
    RepairEvidence,
    RepairProposal,
    RepairRisk,
)


def proposal() -> RepairProposal:
    return RepairProposal(
        proposal_id="RP-001",
        incident_id="INC-001",
        root_cause_type="missing_partition",
        root_cause_confidence=0.93,
        evidence=(
            RepairEvidence(
                evidence_id="E01",
                source_type=EvidenceSourceType.BUSINESS_DATA,
                asset="events",
                finding="events are empty",
                observation={"target_date": "2026-01-30", "observed_row": {"event_count": 0}},
            ),
            RepairEvidence(
                evidence_id="E02",
                source_type=EvidenceSourceType.OPERATIONAL_METADATA,
                asset="partition_metadata",
                finding="partition is missing",
                observation={
                    "target_date": "2026-01-30",
                    "observed_row": {
                        "partition_value": "2026-01-30/android",
                        "row_count": 0,
                        "status": "missing",
                    },
                },
            ),
        ),
        affected_assets=("events", "partition_metadata"),
        action=RepairAction.RERUN_PARTITION,
        parameters={
            "table": "events",
            "source_table": "events",
            "partition_column": "device_type",
            "partition_value": "2026-01-30/android",
        },
        risk=RepairRisk.MEDIUM,
        rationale="restore the partition in a sandbox",
        created_at=datetime(2026, 1, 30, 12, tzinfo=UTC),
        valid_until=datetime(2026, 1, 30, 13, tzinfo=UTC),
    )


def test_approval_validation_rejects_wrong_bindings_and_expiry() -> None:
    item = proposal()
    decision = ApprovalDecision(
        decision_id="AD-001",
        incident_id="OTHER",
        proposal_id=item.proposal_id,
        proposal_hash=item.proposal_hash,
        reviewer="reviewer",
        outcome=ApprovalOutcome.APPROVED,
        decided_at=datetime(2026, 1, 30, 12, 1, tzinfo=UTC),
    )
    with pytest.raises(ApprovalValidationError, match="bind"):
        validate_approval_decision(item, decision, now=datetime(2026, 1, 30, 12, 2, tzinfo=UTC))
    valid = ApprovalDecision.for_proposal(
        item,
        decision_id="AD-002",
        reviewer="reviewer",
        outcome=ApprovalOutcome.APPROVED,
        decided_at=item.created_at + timedelta(minutes=1),
    )
    with pytest.raises(ApprovalValidationError, match="expired"):
        validate_approval_decision(
            item,
            valid,
            now=item.valid_until + timedelta(seconds=1),
        )
