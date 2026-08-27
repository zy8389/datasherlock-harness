from datetime import UTC, datetime
from pathlib import Path

import duckdb
import pytest

from config.faults import EvidenceSourceType
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
from harness.sandbox_repair import SandboxRepairError, SandboxRepairExecutor


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


def write_database(path: Path, *, include_target: bool, include_metadata: bool = True) -> None:
    with duckdb.connect(str(path)) as connection:
        connection.execute(
            "CREATE TABLE events (event_id INTEGER, user_id INTEGER, "
            "event_time TIMESTAMP, event_name VARCHAR, device_type VARCHAR)"
        )
        rows = [(1, 1, "2026-01-30 10:00:00", "login", "ios")]
        if include_target:
            rows.append((2, 2, "2026-01-30 11:00:00", "login", "android"))
        connection.executemany("INSERT INTO events VALUES (?, ?, ?, ?, ?)", rows)
        if include_metadata:
            connection.execute(
                "CREATE TABLE partition_metadata (table_name VARCHAR, "
                "partition_value VARCHAR, row_count BIGINT, status VARCHAR)"
            )
            connection.execute(
                "INSERT INTO partition_metadata VALUES "
                "('events', '2026-01-30/ios', 1, 'ready'), "
                "('events', '2026-01-30/android', ?, ?)",
                [1 if include_target else 0, "ready" if include_target else "missing"],
            )


def bound_run(executor: SandboxRepairExecutor, item) -> object:
    decision = ApprovalDecision.for_proposal(
        item,
        decision_id="AD-001",
        reviewer="reviewer",
        outcome=ApprovalOutcome.APPROVED,
        decided_at=item.created_at,
    )
    run_id = "SR-001"
    expected = executor.sandbox_path_for(
        SandboxRun(
            run_id=run_id,
            incident_id=item.incident_id,
            proposal_id=item.proposal_id,
            proposal_hash=item.proposal_hash,
            approval_decision_id=decision.decision_id,
            action=item.action,
            sandbox_path="placeholder",
        )
    )
    return SandboxRun.for_approved_proposal(
        item,
        decision,
        run_id=run_id,
        sandbox_path=str(expected),
    )


def test_f01_repair_isolated_and_exactly_once(tmp_path: Path) -> None:
    source = tmp_path / "faulty.duckdb"
    trusted = tmp_path / "trusted.duckdb"
    write_database(source, include_target=False)
    write_database(trusted, include_target=True)
    before = source.read_bytes()
    executor = SandboxRepairExecutor(
        source,
        tmp_path / "sandboxes",
        repair_source_database_path=trusted,
    )
    item = proposal()
    run = bound_run(executor, item)

    result = executor.execute(item, run)

    assert result.status is SandboxRunStatus.SUCCEEDED
    assert result.handler_invocation_count == 1
    assert source.read_bytes() == before
    with duckdb.connect(result.sandbox_path, read_only=True) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM events WHERE device_type = 'android'"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT row_count, status FROM partition_metadata "
            "WHERE partition_value = '2026-01-30/android'"
        ).fetchone() == (1, "ready")
    assert (Path(result.sandbox_path).parent / "repair-invocation.json").is_file()

    with pytest.raises(SandboxRepairError, match="pending"):
        executor.execute(item, result)


def test_sandbox_path_traversal_and_existing_run_fail_before_write(tmp_path: Path) -> None:
    source = tmp_path / "faulty.duckdb"
    trusted = tmp_path / "trusted.duckdb"
    write_database(source, include_target=False)
    write_database(trusted, include_target=True)
    executor = SandboxRepairExecutor(
        source,
        tmp_path / "sandboxes",
        repair_source_database_path=trusted,
    )
    item = proposal()
    run = bound_run(executor, item).model_copy(
        update={"sandbox_path": str(tmp_path / "sandboxes" / "INC-001" / ".." / "outside" / "x.duckdb")}
    )
    with pytest.raises(SandboxRepairError, match="derived|traversal"):
        executor.execute(item, run)
    assert not (tmp_path / "outside").exists()


def test_later_metadata_failure_rolls_back_event_changes(tmp_path: Path) -> None:
    source = tmp_path / "faulty.duckdb"
    trusted = tmp_path / "trusted.duckdb"
    write_database(source, include_target=False, include_metadata=True)
    write_database(trusted, include_target=True, include_metadata=False)
    executor = SandboxRepairExecutor(
        source,
        tmp_path / "sandboxes",
        repair_source_database_path=trusted,
    )
    item = proposal()
    result = executor.execute(item, bound_run(executor, item))

    assert result.status is SandboxRunStatus.FAILED
    with duckdb.connect(result.sandbox_path, read_only=True) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM events WHERE device_type = 'android'"
        ).fetchone()[0] == 0
