from datetime import UTC, datetime
from pathlib import Path

from harness.approval import record_approval
from harness.checkpoint import (
    CheckpointManager,
    FileCheckpointStore,
    ResumeAction,
)
from harness.graph import HarnessGraph
from harness.repair import (
    ApprovalDecision,
    ApprovalOutcome,
    PostValidationResult,
    PostValidationStatus,
    SandboxRun,
)
from harness.repair_proposal import RepairProposalBuilder
from harness.state import IncidentState, IncidentStatus


def test_repair_artifacts_round_trip_and_resume_without_execution(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    state = IncidentState(
        alert={
            "incident_id": "INC-CP",
            "metric": "daily_active_users",
            "observed_at": "2026-01-30",
        },
        root_cause={
            "root_cause_type": "missing_partition",
            "confidence": 0.9,
            "supporting_evidence_ids": ["E1", "E2"],
            "independent_source_types": ["business_data", "operational_metadata"],
        },
        evidence=[
            {
                "evidence_id": "E1",
                "source_type": "business_data",
                "description": "empty",
                "observation": {
                    "target_date": "2026-01-30",
                    "observed_row": {"event_count": 0},
                },
            },
            {
                "evidence_id": "E2",
                "source_type": "operational_metadata",
                "description": "missing",
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
        status=IncidentStatus.ROOT_CAUSE_FOUND,
    )
    item = RepairProposalBuilder(now=lambda: now).build(state)
    manager = CheckpointManager(FileCheckpointStore(tmp_path / "checkpoints"))
    graph = HarnessGraph(checkpoint_manager=manager)
    graph.propose_fix(state, item.model_dump(mode="json"))
    decision = ApprovalDecision.for_proposal(
        item,
        decision_id="AD-CP",
        reviewer="reviewer",
        outcome=ApprovalOutcome.APPROVED,
        decided_at=item.created_at,
    )
    record_approval(graph, state, item, decision)
    run = SandboxRun(
        run_id="SR-CP",
        incident_id=item.incident_id,
        proposal_id=item.proposal_id,
        proposal_hash=item.proposal_hash,
        approval_decision_id=decision.decision_id,
        action=item.action,
        sandbox_path=str(tmp_path / "sandboxes" / item.incident_id / "SR-CP" / "datasherlock.duckdb"),
    )
    graph.record_pending_repair_run(state, run.model_dump(mode="json"))

    restored, resume = graph.resume_latest(item.incident_id)

    assert restored.status is IncidentStatus.SANDBOX_REPAIR
    assert restored.fix_proposal is not None
    assert restored.fix_proposal["proposal_hash"] == item.proposal_hash
    assert restored.repair_result is not None
    assert restored.repair_result["sandbox_run"]["status"] == "pending"
    assert resume.action is ResumeAction.CONTINUE_POST_ROOT_CAUSE_FLOW

    resumed_graph = HarnessGraph(checkpoint_manager=manager)
    resumed_graph.restore_runtime(restored)
    resumed_graph.record_repair_result(
        restored,
        result={
            "run_id": run.run_id,
            "incident_id": run.incident_id,
            "proposal_id": run.proposal_id,
            "proposal_hash": run.proposal_hash,
            "approval_decision_id": run.approval_decision_id,
            "action": run.action.value,
            "sandbox_path": run.sandbox_path,
            "status": "succeeded",
            "source_hash_before": "a" * 64,
            "source_hash_after": "a" * 64,
            "sandbox_hash_before": "b" * 64,
            "sandbox_hash_after": "c" * 64,
            "handler_invocation_count": 1,
            "started_at": now.isoformat(),
            "finished_at": now.isoformat(),
        },
    )
    assert restored.status is IncidentStatus.POST_VALIDATION
    after_repair, post_resume = resumed_graph.resume_latest(item.incident_id)
    assert after_repair.status is IncidentStatus.POST_VALIDATION
    assert post_resume.action is ResumeAction.CONTINUE_POST_ROOT_CAUSE_FLOW
    validation = PostValidationResult(
        validation_id="PV-CP",
        incident_id=item.incident_id,
        sandbox_run_id=run.run_id,
        proposal_hash=item.proposal_hash,
        metric_id="daily_active_users",
        observed_before=1,
        observed_after=2,
        target_met=True,
        status=PostValidationStatus.PASSED,
        summary="all checks passed",
    )
    resumed_graph.record_post_validation_result(
        restored,
        validated=True,
        result=validation.model_dump(mode="json"),
    )
    final, _ = resumed_graph.resume_latest(item.incident_id)
    assert final.repair_result is not None
    assert final.repair_result["sandbox_run"]["run_id"] == run.run_id
    assert final.repair_result["post_validation"]["validation_id"] == "PV-CP"
