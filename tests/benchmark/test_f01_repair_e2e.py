from datetime import UTC, datetime, timedelta
from pathlib import Path

from benchmark.case_generator import load_case_manifest, materialize_case
from benchmark.runner import (
    BenchmarkRunConfig,
    _smoke_model_client_factory,
    build_harness_executor,
    build_runtime_input,
)
from config.faults import EvidenceSourceType
from data.generator import generate_dataset, write_outputs
from harness.approval import record_approval
from harness.graph import HarnessGraph
from harness.hypothesis import HypothesisManager
from harness.post_validation import PostRepairValidator
from harness.repair import (
    ApprovalDecision,
    ApprovalOutcome,
    RepairAction,
    RepairEvidence,
    RepairProposal,
    RepairRisk,
    SandboxRun,
)
from harness.repair_proposal import RepairProposalBuilder
from harness.sandbox_repair import SandboxRepairExecutor
from harness.state import IncidentState, IncidentStatus

ROOT = Path(__file__).parents[2]
CASES = ROOT / "benchmark" / "cases"


def test_f01_diagnosis_approval_sandbox_repair_and_post_validation(tmp_path: Path) -> None:
    manifest = load_case_manifest("F01-001", directory=CASES)
    baseline = generate_dataset(
        manifest.baseline_user_count,
        manifest.baseline_days,
        manifest.baseline_event_count,
        manifest.baseline_seed,
        datetime.combine(manifest.baseline_start_date, datetime.min.time()),
    )
    fault = materialize_case(manifest, baseline_tables=baseline)
    fault_dir = tmp_path / "fault"
    baseline_dir = tmp_path / "baseline"
    write_outputs(fault_dir, fault.tables)
    write_outputs(baseline_dir, baseline)

    config = BenchmarkRunConfig(
        case_ids=["F01-001"],
        harness_version="current-main",
        model_name="deterministic-smoke",
        output_dir=tmp_path / "runner-output",
    )
    runtime_input = build_runtime_input(
        manifest,
        fault_dir / "datasherlock.duckdb",
        run_id="INC-runtime-f01",
        config=config,
    )
    diagnosis = build_harness_executor(
        config,
        model_client_factory=_smoke_model_client_factory,
    ).execute(runtime_input)
    state = IncidentState.from_dict(diagnosis.trace_payload["state"])
    assert state.status is IncidentStatus.ROOT_CAUSE_FOUND
    assert state.root_cause is not None
    assert state.root_cause["root_cause_type"] == "missing_partition"

    manager = HypothesisManager()
    graph = HarnessGraph(hypothesis_manager=manager)
    graph.restore_runtime(state)
    proposal = RepairProposalBuilder(
        hypothesis_manager=manager,
        now=lambda: datetime.now(UTC),
    ).build(state)
    assert proposal.proposal_hash == proposal.content_hash()
    graph.propose_fix(state, proposal.model_dump(mode="json"))
    assert state.status is IncidentStatus.AWAITING_APPROVAL

    decision = ApprovalDecision.for_proposal(
        proposal,
        decision_id="AD-F01",
        reviewer="data-engineer",
        outcome=ApprovalOutcome.APPROVED,
    )
    record_approval(graph, state, proposal, decision)
    assert state.status is IncidentStatus.SANDBOX_REPAIR

    executor = SandboxRepairExecutor(
        fault_dir / "datasherlock.duckdb",
        tmp_path / "sandboxes",
        repair_source_database_path=baseline_dir / "datasherlock.duckdb",
    )
    placeholder = SandboxRun(
        run_id="SR-F01",
        incident_id=proposal.incident_id,
        proposal_id=proposal.proposal_id,
        proposal_hash=proposal.proposal_hash,
        approval_decision_id=decision.decision_id,
        action=proposal.action,
        sandbox_path="placeholder",
    )
    run = SandboxRun.for_approved_proposal(
        proposal,
        decision,
        run_id=placeholder.run_id,
        sandbox_path=str(executor.sandbox_path_for(placeholder)),
    )
    graph.record_pending_repair_run(state, run.model_dump(mode="json"))
    repaired = executor.execute(proposal, run)
    graph.record_repair_result(state, result=repaired.model_dump(mode="json"))
    assert state.status is IncidentStatus.POST_VALIDATION
    assert repaired.handler_invocation_count == 1

    validation = PostRepairValidator(fault_dir / "datasherlock.duckdb").validate(
        proposal,
        repaired,
        metric_id=runtime_input.alert.metric,
        metric_date=manifest.metric_date,
        expected_value=runtime_input.alert.expected_value,
        validation_id="PV-F01",
    )
    graph.record_post_validation_result(
        state,
        validated=validation.status.value == "passed",
        result=validation.model_dump(mode="json"),
    )
    assert validation.status.value == "passed"
    assert state.status is IncidentStatus.RESOLVED
    assert state.final_status is IncidentStatus.RESOLVED
    assert state.repair_result is not None
    assert state.repair_result["post_validation"]["sandbox_run_id"] == run.run_id
    assert (Path(repaired.sandbox_path).parent / "repair-invocation.json").is_file()


def test_f01_rejected_approval_does_not_create_sandbox(tmp_path: Path) -> None:
    item = RepairProposal(
        proposal_id="RP-REJECT",
        incident_id="INC-REJECT",
        root_cause_type="missing_partition",
        root_cause_confidence=0.9,
        evidence=(
            RepairEvidence(
                evidence_id="E1",
                source_type=EvidenceSourceType.BUSINESS_DATA,
                asset="events",
                finding="empty",
                observation={"target_date": "2026-01-30", "observed_row": {"event_count": 0}},
            ),
            RepairEvidence(
                evidence_id="E2",
                source_type=EvidenceSourceType.OPERATIONAL_METADATA,
                asset="partition_metadata",
                finding="missing",
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
        rationale="sandbox-only repair",
        created_at=datetime.now(UTC),
        valid_until=datetime.now(UTC) + timedelta(hours=1),
    )
    state = IncidentState(
        alert={"incident_id": item.incident_id},
        root_cause={
            "root_cause_type": item.root_cause_type,
            "supporting_evidence_ids": ["E1", "E2"],
            "independent_source_types": ["business_data", "operational_metadata"],
        },
        evidence=[
            {
                "evidence_id": "E1",
                "source_type": "business_data",
                "description": "empty",
            },
            {
                "evidence_id": "E2",
                "source_type": "operational_metadata",
                "description": "missing",
            },
        ],
        status=IncidentStatus.ROOT_CAUSE_FOUND,
    )
    graph = HarnessGraph()
    graph.propose_fix(state, item.model_dump(mode="json"))
    decision = ApprovalDecision.for_proposal(
        item,
        decision_id="AD-REJECT",
        reviewer="data-engineer",
        outcome=ApprovalOutcome.REJECTED,
        comment="The proposed repair needs review.",
    )
    record_approval(graph, state, item, decision)

    assert state.status is IncidentStatus.REJECTED
    assert state.final_status is IncidentStatus.REJECTED
    assert not (tmp_path / "sandboxes").exists()
