from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import duckdb
import pytest

from config.faults import EvidenceSourceType
from harness.graph import HarnessGraph, HarnessTransitionError
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
from harness.repair_recovery import RepairRecoveryError, resume_approved_repair
from harness.sandbox_repair import SandboxRepairError, SandboxRepairExecutor
from harness.state import IncidentState, IncidentStatus


def _write_database(path: Path, *, repaired: bool) -> None:
    with duckdb.connect(str(path)) as connection:
        connection.execute(
            "CREATE TABLE events (event_id INTEGER, user_id INTEGER, "
            "event_time TIMESTAMP, event_name VARCHAR, device_type VARCHAR)"
        )
        rows = [(1, 1, "2026-01-30 10:00:00", "login", "ios")]
        if repaired:
            rows.append((2, 2, "2026-01-30 11:00:00", "login", "android"))
        connection.executemany("INSERT INTO events VALUES (?, ?, ?, ?, ?)", rows)
        connection.execute(
            "CREATE TABLE partition_metadata (table_name VARCHAR, "
            "partition_value VARCHAR, row_count BIGINT, status VARCHAR)"
        )
        connection.execute(
            "INSERT INTO partition_metadata VALUES "
            "('events', '2026-01-30/android', ?, ?)",
            [1 if repaired else 0, "ready" if repaired else "missing"],
        )


def _proposal(now: datetime) -> RepairProposal:
    return RepairProposal(
        proposal_id="RP-RECOVERY",
        incident_id="INC-RECOVERY",
        root_cause_type="missing_partition",
        root_cause_confidence=0.9,
        evidence=(
            RepairEvidence(
                evidence_id="E1",
                source_type=EvidenceSourceType.BUSINESS_DATA,
                asset="events",
                finding="target partition is empty",
                observation={"target_date": "2026-01-30", "event_count": 0},
            ),
            RepairEvidence(
                evidence_id="E2",
                source_type=EvidenceSourceType.OPERATIONAL_METADATA,
                asset="partition_metadata",
                finding="target partition is missing",
                observation={"partition_value": "2026-01-30/android", "status": "missing"},
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
        rationale="restore the confirmed partition in a sandbox",
        created_at=now,
        valid_until=now + timedelta(hours=1),
    )


class _CountingExecutor(SandboxRepairExecutor):
    def __init__(self, *args: object, **kwargs: object) -> None:
        self.handler_calls = 0
        super().__init__(*args, **kwargs)

    def _execute_handler(self, proposal, sandbox_path):  # type: ignore[no-untyped-def]
        self.handler_calls += 1
        return super()._execute_handler(proposal, sandbox_path)


def _context(tmp_path: Path) -> tuple[
    HarnessGraph,
    IncidentState,
    RepairProposal,
    ApprovalDecision,
    SandboxRun,
    _CountingExecutor,
]:
    now = datetime.now(UTC)
    source = tmp_path / "faulty.duckdb"
    trusted = tmp_path / "trusted.duckdb"
    _write_database(source, repaired=False)
    _write_database(trusted, repaired=True)
    item = _proposal(now)
    state = IncidentState(
        alert={"incident_id": item.incident_id, "metric": "daily_active_users", "observed_at": "2026-01-30"},
        root_cause={
            "root_cause_type": item.root_cause_type,
            "supporting_evidence_ids": ["E1", "E2"],
            "independent_source_types": ["business_data", "operational_metadata"],
        },
        evidence=[
            {"evidence_id": "E1", "source_type": "business_data", "description": "empty"},
            {"evidence_id": "E2", "source_type": "operational_metadata", "description": "missing"},
        ],
        status=IncidentStatus.ROOT_CAUSE_FOUND,
    )
    graph = HarnessGraph()
    graph.propose_fix(state, item.model_dump(mode="json"))
    decision = ApprovalDecision.for_proposal(
        item,
        decision_id="AD-RECOVERY",
        reviewer="reviewer",
        outcome=ApprovalOutcome.APPROVED,
        decided_at=now,
    )
    graph.record_approval(
        state,
        approved=True,
        metadata=decision.model_dump(mode="json"),
        now=now,
    )
    executor = _CountingExecutor(
        source,
        tmp_path / "sandboxes",
        repair_source_database_path=trusted,
    )
    placeholder = SandboxRun(
        run_id="SR-RECOVERY",
        incident_id=item.incident_id,
        proposal_id=item.proposal_id,
        proposal_hash=item.proposal_hash,
        approval_decision_id=decision.decision_id,
        action=item.action,
        sandbox_path="placeholder",
    )
    pending = SandboxRun.for_approved_proposal(
        item,
        decision,
        run_id=placeholder.run_id,
        sandbox_path=str(executor.sandbox_path_for(placeholder)),
    )
    graph.record_pending_repair_run(state, pending.model_dump(mode="json"))
    return graph, state, item, decision, pending, executor


def test_typed_graph_cannot_bypass_approval_repair_or_post_validation(tmp_path: Path) -> None:
    graph, state, item, decision, pending, executor = _context(tmp_path)

    # Rebuild the approval checkpoint so the boolean-only call is tested before approval.
    graph = HarnessGraph()
    state.status = IncidentStatus.AWAITING_APPROVAL
    state.approval = None
    state.repair_result = None
    with pytest.raises(HarnessTransitionError, match="ApprovalDecision"):
        graph.record_approval(state, approved=True)

    graph.record_approval(
        state,
        approved=True,
        metadata=decision.model_dump(mode="json"),
        now=decision.decided_at,
    )
    graph.record_pending_repair_run(state, pending.model_dump(mode="json"))
    with pytest.raises(HarnessTransitionError, match="SandboxRun"):
        graph.record_repair_result(state, success=True)

    completed = executor.execute(item, pending)
    with pytest.raises(HarnessTransitionError, match="conflicts"):
        graph.record_repair_result(
            state,
            success=False,
            result=completed.model_dump(mode="json"),
        )
    graph.record_repair_result(state, result=completed.model_dump(mode="json"))
    with pytest.raises(HarnessTransitionError, match="PostValidationResult"):
        graph.record_post_validation_result(state, validated=True)

    validation = PostValidationResult(
        validation_id="PV-RECOVERY",
        incident_id=item.incident_id,
        sandbox_run_id=completed.run_id,
        proposal_hash=item.proposal_hash,
        metric_id="daily_active_users",
        observed_before=1,
        observed_after=2,
        target_met=True,
        status=PostValidationStatus.PASSED,
        summary="all checks passed",
    )
    graph.record_post_validation_result(
        state,
        validated=True,
        result=validation.model_dump(mode="json"),
    )
    assert state.status is IncidentStatus.RESOLVED


def test_typed_approval_outcome_must_match_boolean_argument(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    item = _proposal(now)
    state = IncidentState(
        alert={"incident_id": item.incident_id},
        root_cause={
            "root_cause_type": item.root_cause_type,
            "supporting_evidence_ids": ["E1", "E2"],
            "independent_source_types": ["business_data", "operational_metadata"],
        },
        evidence=[
            {"evidence_id": "E1", "source_type": "business_data", "description": "empty"},
            {"evidence_id": "E2", "source_type": "operational_metadata", "description": "missing"},
        ],
        status=IncidentStatus.ROOT_CAUSE_FOUND,
    )
    graph = HarnessGraph()
    graph.propose_fix(state, item.model_dump(mode="json"))
    decision = ApprovalDecision.for_proposal(
        item,
        decision_id="AD-REJECTED",
        reviewer="reviewer",
        outcome=ApprovalOutcome.REJECTED,
        comment="needs review",
        decided_at=now,
    )
    with pytest.raises(HarnessTransitionError, match="conflicts"):
        graph.record_approval(
            state,
            approved=True,
            metadata=decision.model_dump(mode="json"),
            now=now,
        )
    assert state.status is IncidentStatus.AWAITING_APPROVAL


@pytest.mark.parametrize("operation", ["propose", "approval", "pending", "repair", "post"])
def test_malformed_typed_proposal_never_falls_back_to_legacy(operation: str) -> None:
    graph = HarnessGraph()
    invalid_proposal = {"proposal_id": "RP-MALFORMED", "action": "rerun_partition"}
    if operation == "propose":
        state = IncidentState(
            alert={"incident_id": "INC-MALFORMED"},
            root_cause={
                "root_cause_type": "missing_partition",
                "supporting_evidence_ids": ["E1", "E2"],
                "independent_source_types": ["business_data", "operational_metadata"],
            },
            status=IncidentStatus.ROOT_CAUSE_FOUND,
        )
        call = lambda: graph.propose_fix(state, invalid_proposal)
    elif operation == "approval":
        state = IncidentState(
            alert={"incident_id": "INC-MALFORMED"},
            fix_proposal=invalid_proposal,
            status=IncidentStatus.AWAITING_APPROVAL,
        )
        call = lambda: graph.record_approval(state, approved=True)
    elif operation == "pending":
        state = IncidentState(
            alert={"incident_id": "INC-MALFORMED"},
            fix_proposal=invalid_proposal,
            approval={"status": "approved"},
            status=IncidentStatus.SANDBOX_REPAIR,
        )
        call = lambda: graph.record_pending_repair_run(state, {})
    elif operation == "repair":
        state = IncidentState(
            alert={"incident_id": "INC-MALFORMED"},
            fix_proposal=invalid_proposal,
            status=IncidentStatus.SANDBOX_REPAIR,
        )
        call = lambda: graph.record_repair_result(state, success=True)
    else:
        state = IncidentState(
            alert={"incident_id": "INC-MALFORMED"},
            fix_proposal=invalid_proposal,
            status=IncidentStatus.POST_VALIDATION,
        )
        call = lambda: graph.record_post_validation_result(state, validated=True)

    with pytest.raises(HarnessTransitionError, match="typed repair proposal"):
        call()
    assert state.status is not IncidentStatus.RESOLVED


def test_typed_failed_run_cannot_enter_post_validation(tmp_path: Path) -> None:
    graph, state, _item, _decision, pending, _executor = _context(tmp_path)
    # Construct a valid terminal failed artifact without requiring sandbox hashes.
    failed_values = pending.model_dump()
    failed_values.update(
        {
            "status": SandboxRunStatus.FAILED,
            "handler_invocation_count": 1,
            "started_at": datetime.now(UTC),
            "finished_at": datetime.now(UTC),
            "error": "handler failed",
        }
    )
    failed = SandboxRun(
        **failed_values,
    )
    graph.record_repair_result(state, result=failed.model_dump(mode="json"))
    assert state.status is IncidentStatus.TOOL_FAILED
    assert state.final_status is IncidentStatus.TOOL_FAILED


def test_durable_terminal_result_recovers_without_second_handler_invocation(tmp_path: Path) -> None:
    graph, state, item, decision, pending, executor = _context(tmp_path)
    completed = executor.execute(item, pending)
    assert executor.handler_calls == 1
    result_path = Path(completed.sandbox_path).parent / "repair-result.json"
    assert result_path.is_file()
    assert json.loads(result_path.read_text(encoding="utf-8"))["run_id"] == pending.run_id

    recovered = resume_approved_repair(
        state, item, decision, pending, executor, graph
    )
    assert recovered.status is SandboxRunStatus.SUCCEEDED
    assert executor.handler_calls == 1
    assert state.status is IncidentStatus.POST_VALIDATION


def test_invocation_marker_without_terminal_result_fails_closed(tmp_path: Path) -> None:
    graph, state, item, decision, pending, executor = _context(tmp_path)
    run_directory = executor.sandbox_path_for(pending).parent
    run_directory.mkdir(parents=True)
    (run_directory / "repair-invocation.json").write_text(
        json.dumps(
            {
                "handler_invocation_count": 1,
                "run_id": pending.run_id,
                "proposal_id": item.proposal_id,
                "proposal_hash": item.proposal_hash,
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(RepairRecoveryError, match="indeterminate"):
        resume_approved_repair(state, item, decision, pending, executor, graph)
    assert executor.handler_calls == 0
    assert state.status is IncidentStatus.SANDBOX_REPAIR


@pytest.mark.parametrize("tamper", ["source", "sandbox", "proposal_hash", "approval_decision_id", "run_id"])
def test_durable_result_binding_and_hash_tampering_fails_closed(
    tmp_path: Path, tamper: str
) -> None:
    graph, state, item, decision, pending, executor = _context(tmp_path)
    completed = executor.execute(item, pending)
    result_path = Path(completed.sandbox_path).parent / "repair-result.json"
    if tamper == "source":
        with duckdb.connect(str(Path(executor._source_database_path))) as connection:
            connection.execute("CREATE TABLE external_change (value INTEGER)")
    elif tamper == "sandbox":
        with duckdb.connect(completed.sandbox_path) as connection:
            connection.execute("INSERT INTO events VALUES (9, 9, '2026-01-30 12:00:00', 'x', 'android')")
    else:
        payload = json.loads(result_path.read_text(encoding="utf-8"))
        payload[tamper] = "wrong"
        result_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises((RepairRecoveryError, SandboxRepairError)):
        resume_approved_repair(state, item, decision, pending, executor, graph)
    assert executor.handler_calls == 1
    assert state.status is IncidentStatus.SANDBOX_REPAIR


def test_malformed_terminal_result_fails_closed(tmp_path: Path) -> None:
    graph, state, item, decision, pending, executor = _context(tmp_path)
    run_directory = executor.sandbox_path_for(pending).parent
    run_directory.mkdir(parents=True)
    (run_directory / "repair-result.json").write_text("{", encoding="utf-8")
    with pytest.raises(RepairRecoveryError, match="malformed"):
        resume_approved_repair(state, item, decision, pending, executor, graph)
    assert executor.handler_calls == 0
