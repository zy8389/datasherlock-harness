from datetime import UTC, datetime, timedelta

import pytest

from config.faults import EvidenceSourceType
from harness.approval import ApprovalFlow, ApprovalFlowError
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
    SandboxRunStatus,
)
from harness.state import IncidentState, IncidentStatus

NOW = datetime.now(UTC)


def _proposal() -> RepairProposal:
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
                finding="Android events are absent.",
            ),
            RepairEvidence(
                evidence_id="E02",
                source_type=EvidenceSourceType.OPERATIONAL_METADATA,
                asset="partition_metadata",
                finding="The partition is marked missing.",
            ),
        ),
        affected_assets=("events", "partition_metadata"),
        action=RepairAction.RERUN_PARTITION,
        parameters={"partition": "2026-08-12/android"},
        risk=RepairRisk.MEDIUM,
        rationale="The source partition can be safely rebuilt in a sandbox.",
        created_at=NOW,
        valid_until=NOW + timedelta(hours=1),
    )


def _root_cause_state() -> IncidentState:
    return IncidentState(
        alert={"incident_id": "INC-001"},
        root_cause={"root_cause_type": "missing_partition"},
        status=IncidentStatus.ROOT_CAUSE_FOUND,
    )


def test_approval_flow_requires_approved_sandbox_and_passing_validation() -> None:
    flow = ApprovalFlow()
    state = _root_cause_state()
    proposal = _proposal()

    flow.propose(state, proposal)
    assert state.status is IncidentStatus.FIX_PROPOSED
    flow.request_approval(state)
    decision = ApprovalDecision.for_proposal(
        proposal,
        decision_id="AD-001",
        outcome=ApprovalOutcome.APPROVED,
        reviewer="data-engineer",
    )
    flow.record_decision(state, decision)
    assert state.status is IncidentStatus.SANDBOX_REPAIR

    pending_run = flow.create_sandbox_run(
        state,
        run_id="SR-001",
        sandbox_path="runs/INC-001/SR-001/datasherlock.duckdb",
    )
    completed_run = SandboxRun.model_validate(
        {
            **pending_run.model_dump(),
            "status": SandboxRunStatus.SUCCEEDED,
            "started_at": NOW,
            "finished_at": NOW + timedelta(minutes=1),
        }
    )
    flow.record_sandbox_run(state, completed_run)
    assert state.status is IncidentStatus.POST_VALIDATION

    result = PostValidationResult(
        validation_id="PV-001",
        incident_id="INC-001",
        sandbox_run_id=completed_run.run_id,
        proposal_hash=proposal.proposal_hash,
        metric_id="daily_active_users",
        observed_before=7600,
        observed_after=9950,
        target_met=True,
        status=PostValidationStatus.PASSED,
        summary="The target metric recovered without regressions.",
    )
    flow.record_post_validation(state, result)

    assert state.status is IncidentStatus.RESOLVED
    assert state.final_status is IncidentStatus.RESOLVED
    assert IncidentState.from_json(state.to_json()) == state


def test_approval_flow_rejects_inconsistent_proposals_and_decisions() -> None:
    flow = ApprovalFlow()
    state = _root_cause_state()
    proposal = _proposal()

    with pytest.raises(ApprovalFlowError, match="root cause"):
        flow.propose(
            state,
            proposal.model_copy(update={"root_cause_type": "data_delay"}),
        )

    flow.propose(state, proposal)
    with pytest.raises(ApprovalFlowError, match="expected incident status"):
        flow.record_decision(
            state,
            ApprovalDecision.for_proposal(
                proposal,
                decision_id="AD-002",
                outcome=ApprovalOutcome.APPROVED,
                reviewer="data-engineer",
            ),
        )


def test_rejected_approval_is_terminal_and_cannot_create_a_sandbox_run() -> None:
    flow = ApprovalFlow()
    state = _root_cause_state()
    proposal = _proposal()
    flow.propose(state, proposal)
    flow.request_approval(state)
    flow.record_decision(
        state,
        ApprovalDecision.for_proposal(
            proposal,
            decision_id="AD-003",
            outcome=ApprovalOutcome.REJECTED,
            reviewer="data-engineer",
            comment="The proposed action is too broad.",
        ),
    )

    assert state.status is IncidentStatus.REJECTED
    assert state.final_status is IncidentStatus.REJECTED
    with pytest.raises(ApprovalFlowError, match="expected incident status"):
        flow.create_sandbox_run(
            state,
            run_id="SR-002",
            sandbox_path="runs/INC-001/SR-002/datasherlock.duckdb",
        )
