from datetime import UTC, datetime, timedelta
from pathlib import Path

import duckdb
import pytest

from config.faults import EvidenceSourceType
from harness.approval import ApprovalFlow
from harness.post_validation import PostRepairValidator
from harness.repair import (
    ApprovalDecision,
    ApprovalOutcome,
    RepairAction,
    RepairEvidence,
    RepairProposal,
    RepairRisk,
)
from harness.repair_workflow import RepairWorkflowError, RepairWorkflowService
from harness.sandbox_repair import SandboxRepairExecutor
from harness.state import IncidentState, IncidentStatus

NOW = datetime.now(UTC)


def _write_database(path: Path, rows: list[tuple[int, int, str, str, str]]) -> None:
    with duckdb.connect(str(path)) as connection:
        connection.execute(
            "CREATE TABLE events (event_id INTEGER, user_id INTEGER, event_time TIMESTAMP, event_name VARCHAR, device_type VARCHAR)"
        )
        connection.executemany("INSERT INTO events VALUES (?, ?, ?, ?, ?)", rows)


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
        parameters={
            "table": "events",
            "source_table": "events",
            "partition_column": "device_type",
            "partition_value": "android",
        },
        risk=RepairRisk.MEDIUM,
        rationale="Rebuild the missing Android partition in an isolated database.",
        created_at=NOW,
        valid_until=NOW + timedelta(hours=1),
    )


def _approved_state(proposal: RepairProposal) -> IncidentState:
    state = IncidentState(
        alert={
            "incident_id": "INC-001",
            "metric": "daily_active_users",
            "observed_at": "2026-08-12",
            "expected_value": 3,
        },
        root_cause={"root_cause_type": "missing_partition"},
        status=IncidentStatus.ROOT_CAUSE_FOUND,
    )
    flow = ApprovalFlow()
    flow.propose(state, proposal)
    flow.request_approval(state)
    flow.record_decision(
        state,
        ApprovalDecision.for_proposal(
            proposal,
            decision_id="AD-001",
            outcome=ApprovalOutcome.APPROVED,
            reviewer="data-engineer",
        ),
    )
    return state


def test_repair_workflow_executes_sandbox_and_resolves_incident(tmp_path: Path) -> None:
    source = tmp_path / "faulty.duckdb"
    repair_source = tmp_path / "baseline.duckdb"
    _write_database(source, [(1, 1, "2026-08-12 10:00:00", "login", "ios")])
    _write_database(
        repair_source,
        [
            (1, 1, "2026-08-12 10:00:00", "login", "ios"),
            (2, 2, "2026-08-12 11:00:00", "login", "android"),
            (3, 3, "2026-08-12 12:00:00", "run_ai_task", "android"),
        ],
    )
    proposal = _proposal()
    state = _approved_state(proposal)
    service = RepairWorkflowService(
        SandboxRepairExecutor(
            source,
            tmp_path / "sandboxes",
            repair_source_database_path=repair_source,
        ),
        PostRepairValidator(source),
    )

    result = service.execute_approved_repair(state, run_id="SR-001", validation_id="PV-001")

    assert result is state
    assert state.status is IncidentStatus.RESOLVED
    assert state.final_status is IncidentStatus.RESOLVED
    assert state.sandbox_run is not None
    assert state.repair_result is not None
    assert state.repair_result.status.value == "passed"
    assert [trace["tool"] for trace in state.tool_trace] == [
        "sandbox_repair_executor",
        "post_repair_validator",
    ]
    with duckdb.connect(str(source), read_only=True) as connection:
        assert connection.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1


def test_repair_workflow_rejects_invalid_alert_before_creating_run(tmp_path: Path) -> None:
    source = tmp_path / "faulty.duckdb"
    repair_source = tmp_path / "baseline.duckdb"
    _write_database(source, [(1, 1, "2026-08-12 10:00:00", "login", "ios")])
    _write_database(repair_source, [(1, 1, "2026-08-12 10:00:00", "login", "ios")])
    proposal = _proposal()
    state = _approved_state(proposal)
    state.alert.pop("expected_value")
    service = RepairWorkflowService(
        SandboxRepairExecutor(
            source,
            tmp_path / "sandboxes",
            repair_source_database_path=repair_source,
        ),
        PostRepairValidator(source),
    )

    with pytest.raises(RepairWorkflowError, match="expected_value"):
        service.execute_approved_repair(state)

    assert state.status is IncidentStatus.SANDBOX_REPAIR
    assert state.sandbox_run is None
