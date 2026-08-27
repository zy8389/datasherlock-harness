from datetime import UTC, date, datetime
from pathlib import Path

import duckdb

from config.faults import EvidenceSourceType
from harness.post_validation import PostRepairValidator
from harness.repair import (
    ApprovalDecision,
    ApprovalOutcome,
    RepairAction,
    RepairEvidence,
    RepairProposal,
    RepairRisk,
    SandboxRun,
    SandboxRunStatus,
)


def proposal() -> RepairProposal:
    return RepairProposal(
        proposal_id="RP-PV",
        incident_id="INC-PV",
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
        rationale="repair in sandbox",
        created_at=datetime(2026, 1, 30, 12, tzinfo=UTC),
        valid_until=datetime(2026, 1, 30, 13, tzinfo=UTC),
    )


def write_database(path: Path, *, repaired: bool) -> None:
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


def successful_run(item: RepairProposal, sandbox: Path) -> SandboxRun:
    decision = ApprovalDecision.for_proposal(
        item,
        decision_id="AD-PV",
        reviewer="reviewer",
        outcome=ApprovalOutcome.APPROVED,
        decided_at=item.created_at,
    )
    return SandboxRun(
        run_id="SR-PV",
        incident_id=item.incident_id,
        proposal_id=item.proposal_id,
        proposal_hash=item.proposal_hash,
        approval_decision_id=decision.decision_id,
        action=item.action,
        sandbox_path=str(sandbox),
        status=SandboxRunStatus.SUCCEEDED,
        handler_invocation_count=1,
        started_at=item.created_at,
        finished_at=item.created_at,
    )


def test_post_validation_checks_partition_and_metric(tmp_path: Path) -> None:
    source = tmp_path / "source.duckdb"
    sandbox = tmp_path / "sandbox.duckdb"
    write_database(source, repaired=False)
    write_database(sandbox, repaired=True)
    item = proposal()

    result = PostRepairValidator(source).validate(
        item,
        successful_run(item, sandbox),
        metric_id="daily_active_users",
        metric_date=date(2026, 1, 30),
        expected_value=2,
        validation_id="PV-001",
    )

    assert result.status.value == "passed"
    assert result.target_met is True
    assert result.observed_before == 1
    assert result.observed_after == 2
    assert result.regressions == ()


def test_mechanical_repair_can_fail_post_validation(tmp_path: Path) -> None:
    source = tmp_path / "source.duckdb"
    sandbox = tmp_path / "sandbox.duckdb"
    write_database(source, repaired=False)
    write_database(sandbox, repaired=True)
    item = proposal()

    result = PostRepairValidator(source).validate(
        item,
        successful_run(item, sandbox),
        metric_id="daily_active_users",
        metric_date=date(2026, 1, 30),
        expected_value=99,
        validation_id="PV-002",
    )

    assert result.status.value == "failed"
    assert result.target_met is False
