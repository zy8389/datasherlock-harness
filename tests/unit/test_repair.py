from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from config.faults import EvidenceSourceType
from harness.repair import (
    ApprovalDecision,
    ApprovalOutcome,
    PostValidationResult,
    PostValidationStatus,
    RepairAction,
    RepairEvidence,
    RepairProposal,
    RepairRisk,
    SandboxRun,
)

CREATED_AT = datetime(2026, 8, 26, 12, tzinfo=UTC)


def _proposal(**overrides: object) -> RepairProposal:
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
                finding="Android events are absent on the target date.",
            ),
            RepairEvidence(
                evidence_id="E02",
                source_type=EvidenceSourceType.OPERATIONAL_METADATA,
                asset="partition_metadata",
                finding="The Android partition is marked missing.",
            ),
        ),
        "affected_assets": ("events", "partition_metadata"),
        "action": RepairAction.RERUN_PARTITION,
        "parameters": {"table": "events", "partition": "2026-08-12/android"},
        "risk": RepairRisk.MEDIUM,
        "rationale": "Rerunning the missing source partition is reversible in a sandbox.",
        "created_at": CREATED_AT,
        "valid_until": CREATED_AT + timedelta(hours=4),
    }
    values.update(overrides)
    return RepairProposal.model_validate(values)


def test_repair_proposal_binds_independent_evidence_and_stable_hash() -> None:
    first = _proposal()
    second = _proposal()

    assert len(first.proposal_hash) == 64
    assert first.proposal_hash == second.proposal_hash
    assert first.content_hash() == first.proposal_hash


def test_repair_proposal_rejects_non_independent_evidence_and_unbound_assets() -> None:
    duplicate_source = (
        RepairEvidence(
            evidence_id="E01",
            source_type=EvidenceSourceType.BUSINESS_DATA,
            asset="events",
            finding="Event volume declined.",
        ),
        RepairEvidence(
            evidence_id="E02",
            source_type=EvidenceSourceType.BUSINESS_DATA,
            asset="events",
            finding="Distinct users declined.",
        ),
    )

    with pytest.raises(ValidationError, match="independent evidence"):
        _proposal(evidence=duplicate_source)

    with pytest.raises(ValidationError, match="affected assets"):
        _proposal(
            evidence=(
                RepairEvidence(
                    evidence_id="E01",
                    source_type=EvidenceSourceType.BUSINESS_DATA,
                    asset="users",
                    finding="Users are impacted.",
                ),
                RepairEvidence(
                    evidence_id="E02",
                    source_type=EvidenceSourceType.OPERATIONAL_METADATA,
                    asset="partition_metadata",
                    finding="Partition is missing.",
                ),
            )
        )


def test_repair_proposal_rejects_forged_hash_and_unknown_action() -> None:
    with pytest.raises(ValidationError, match="proposal_hash"):
        _proposal(proposal_hash="0" * 64)

    with pytest.raises(ValidationError):
        _proposal(action="delete_production_table")


def test_approved_proposal_can_create_bound_sandbox_run() -> None:
    proposal = _proposal()
    decision = ApprovalDecision.for_proposal(
        proposal,
        decision_id="AD-001",
        outcome=ApprovalOutcome.APPROVED,
        reviewer="data-engineer",
        decided_at=CREATED_AT + timedelta(minutes=5),
    )

    run = SandboxRun.for_approved_proposal(
        proposal,
        decision,
        run_id="SR-001",
        sandbox_path="runs/INC-001/SR-001/datasherlock.duckdb",
    )

    assert run.action is RepairAction.RERUN_PARTITION
    assert run.proposal_hash == proposal.proposal_hash


def test_rejected_or_expired_proposals_cannot_start_sandbox_runs() -> None:
    proposal = _proposal()
    rejected = ApprovalDecision.for_proposal(
        proposal,
        decision_id="AD-002",
        outcome=ApprovalOutcome.REJECTED,
        reviewer="data-engineer",
        comment="Need a lower-risk action.",
        decided_at=CREATED_AT + timedelta(minutes=5),
    )

    with pytest.raises(ValueError, match="approved"):
        SandboxRun.for_approved_proposal(
            proposal,
            rejected,
            run_id="SR-002",
            sandbox_path="runs/INC-001/SR-002/datasherlock.duckdb",
        )

    with pytest.raises(ValueError, match="expired"):
        ApprovalDecision.for_proposal(
            proposal,
            decision_id="AD-003",
            outcome=ApprovalOutcome.APPROVED,
            reviewer="data-engineer",
            decided_at=proposal.valid_until + timedelta(seconds=1),
        )


def test_post_validation_cannot_pass_with_regressions_or_missed_target() -> None:
    proposal = _proposal()
    values = {
        "validation_id": "PV-001",
        "incident_id": proposal.incident_id,
        "sandbox_run_id": "SR-001",
        "proposal_hash": proposal.proposal_hash,
        "metric_id": "daily_active_users",
        "observed_before": 7600,
        "observed_after": 9950,
        "target_met": True,
        "regressions": (),
        "status": PostValidationStatus.PASSED,
        "summary": "Target metric recovered without regressions.",
        "validated_at": CREATED_AT + timedelta(hours=1),
    }

    assert PostValidationResult.model_validate(values).status is PostValidationStatus.PASSED

    with pytest.raises(ValidationError, match="passes only"):
        PostValidationResult.model_validate(
            {**values, "regressions": ("paid_users declined",)}
        )

    with pytest.raises(ValidationError, match="passes only"):
        PostValidationResult.model_validate({**values, "target_met": False})
