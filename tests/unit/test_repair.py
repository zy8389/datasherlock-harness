from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from config.faults import EvidenceSourceType
from harness.repair import (
    ApprovalDecision,
    ApprovalOutcome,
    RepairAction,
    RepairEvidence,
    RepairProposal,
    RepairRisk,
    SandboxRun,
)

NOW = datetime(2026, 1, 30, 12, tzinfo=UTC)


def proposal(**overrides: object) -> RepairProposal:
    values: dict[str, object] = {
        "proposal_id": "RP-001",
        "incident_id": "INC-001",
        "root_cause_type": "missing_partition",
        "root_cause_confidence": 0.93,
        "evidence": (
            RepairEvidence(
                evidence_id="E01",
                source_type=EvidenceSourceType.BUSINESS_DATA,
                asset="events",
                finding="The target business partition is empty.",
                observation={
                    "target_date": "2026-01-30",
                    "observed_row": {"android_event_count": 0},
                },
            ),
            RepairEvidence(
                evidence_id="E02",
                source_type=EvidenceSourceType.OPERATIONAL_METADATA,
                asset="partition_metadata",
                finding="The target partition is marked missing.",
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
        "affected_assets": ("events", "partition_metadata"),
        "action": RepairAction.RERUN_PARTITION,
        "parameters": {
            "table": "events",
            "source_table": "events",
            "partition_column": "device_type",
            "partition_value": "2026-01-30/android",
        },
        "risk": RepairRisk.MEDIUM,
        "rationale": "Restore only the confirmed partition in a sandbox copy.",
        "created_at": NOW,
        "valid_until": NOW + timedelta(hours=1),
    }
    values.update(overrides)
    return RepairProposal.model_validate(values)


def test_repair_proposal_hash_is_stable_and_tamper_is_detected() -> None:
    first = proposal()
    second = proposal()

    assert first.proposal_hash == second.proposal_hash
    assert first.content_hash() == first.proposal_hash
    tampered = first.model_copy(update={"risk": RepairRisk.HIGH})
    assert tampered.content_hash() != tampered.proposal_hash


def test_contracts_fail_closed_for_unknown_action_and_extra_parameters() -> None:
    with pytest.raises(ValidationError):
        proposal(action="drop_table")
    with pytest.raises(ValidationError, match="exactly"):
        proposal(parameters={"table": "events", "sql": "DROP TABLE events"})


def test_rejected_approval_requires_comment_and_run_binds_hash() -> None:
    item = proposal()
    with pytest.raises(ValidationError, match="comment"):
        ApprovalDecision.for_proposal(
            item,
            decision_id="AD-001",
            reviewer="reviewer",
            outcome=ApprovalOutcome.REJECTED,
            decided_at=NOW + timedelta(minutes=1),
        )
    decision = ApprovalDecision.for_proposal(
        item,
        decision_id="AD-001",
        reviewer="reviewer",
        outcome=ApprovalOutcome.APPROVED,
        decided_at=NOW + timedelta(minutes=1),
    )
    run = SandboxRun.for_approved_proposal(
        item,
        decision,
        run_id="SR-001",
        sandbox_path="C:/sandboxes/INC-001/SR-001/datasherlock.duckdb",
    )
    assert run.proposal_hash == item.proposal_hash


def test_approval_after_expiry_is_rejected() -> None:
    item = proposal()
    with pytest.raises(ValueError, match="validity"):
        ApprovalDecision.for_proposal(
            item,
            decision_id="AD-002",
            reviewer="reviewer",
            outcome=ApprovalOutcome.APPROVED,
            decided_at=item.valid_until + timedelta(seconds=1),
        )
