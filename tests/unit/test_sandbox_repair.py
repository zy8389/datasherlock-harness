from datetime import UTC, datetime, timedelta
from pathlib import Path

import duckdb
import pytest

from config.faults import EvidenceSourceType
from harness.repair import (
    RepairAction,
    RepairEvidence,
    RepairProposal,
    RepairRisk,
    SandboxRun,
    SandboxRunStatus,
)
from harness.sandbox_repair import SandboxRepairError, SandboxRepairExecutor

NOW = datetime(2026, 8, 27, 9, tzinfo=UTC)


def _write_database(path: Path, rows: list[tuple[int, str, int]]) -> None:
    with duckdb.connect(str(path)) as connection:
        connection.execute(
            "CREATE TABLE events (event_id INTEGER, device_type VARCHAR, value INTEGER)"
        )
        connection.executemany("INSERT INTO events VALUES (?, ?, ?)", rows)


def _count_partition(path: Path, partition: str) -> int:
    with duckdb.connect(str(path), read_only=True) as connection:
        return int(
            connection.execute(
                "SELECT COUNT(*) FROM events WHERE device_type = ?", [partition]
            ).fetchone()[0]
        )


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
                finding="Android events are absent.",
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
        "parameters": {
            "table": "events",
            "source_table": "events",
            "partition_column": "device_type",
            "partition_value": "android",
        },
        "risk": RepairRisk.MEDIUM,
        "rationale": "Restore the missing partition only in an isolated copy.",
        "created_at": NOW,
        "valid_until": NOW + timedelta(hours=2),
    }
    values.update(overrides)
    return RepairProposal.model_validate(values)


def _run(proposal: RepairProposal, *, run_id: str = "SR-001") -> SandboxRun:
    return SandboxRun(
        run_id=run_id,
        incident_id=proposal.incident_id,
        proposal_id=proposal.proposal_id,
        proposal_hash=proposal.proposal_hash,
        approval_decision_id="AD-001",
        action=proposal.action,
        sandbox_path="untrusted/caller/path.duckdb",
    )


def test_rerun_partition_uses_isolated_copy_and_trusted_repair_source(
    tmp_path: Path,
) -> None:
    source_database = tmp_path / "faulty.duckdb"
    repair_source_database = tmp_path / "baseline.duckdb"
    _write_database(source_database, [(1, "ios", 10)])
    _write_database(
        repair_source_database,
        [(1, "ios", 10), (2, "android", 20), (3, "android", 30)],
    )
    proposal = _proposal()
    executor = SandboxRepairExecutor(
        source_database,
        tmp_path / "sandboxes",
        repair_source_database_path=repair_source_database,
    )

    result = executor.execute(proposal, _run(proposal))

    assert result.status is SandboxRunStatus.SUCCEEDED
    assert Path(result.sandbox_path).is_file()
    assert Path(result.sandbox_path).is_relative_to(tmp_path / "sandboxes")
    assert _count_partition(source_database, "android") == 0
    assert _count_partition(Path(result.sandbox_path), "android") == 2


def test_executor_rejects_sql_parameters_without_executing_them(tmp_path: Path) -> None:
    source_database = tmp_path / "faulty.duckdb"
    repair_source_database = tmp_path / "baseline.duckdb"
    _write_database(source_database, [(1, "ios", 10)])
    _write_database(repair_source_database, [(1, "ios", 10)])
    proposal = _proposal(
        parameters={
            "table": "events",
            "source_table": "events",
            "partition_column": "device_type",
            "partition_value": "android",
            "sql": "DROP TABLE events",
        }
    )
    executor = SandboxRepairExecutor(
        source_database,
        tmp_path / "sandboxes",
        repair_source_database_path=repair_source_database,
    )

    result = executor.execute(proposal, _run(proposal))

    assert result.status is SandboxRunStatus.FAILED
    assert "parameters must be exactly" in (result.error or "")
    assert _count_partition(source_database, "ios") == 1
    with duckdb.connect(str(result.sandbox_path), read_only=True) as connection:
        assert connection.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1


def test_unimplemented_allowlisted_action_is_rejected_without_source_mutation(
    tmp_path: Path,
) -> None:
    source_database = tmp_path / "faulty.duckdb"
    _write_database(source_database, [(1, "ios", 10)])
    proposal = _proposal(
        action=RepairAction.DEDUPLICATE_BATCH,
        parameters={},
    )
    executor = SandboxRepairExecutor(source_database, tmp_path / "sandboxes")

    result = executor.execute(proposal, _run(proposal))

    assert result.status is SandboxRunStatus.FAILED
    assert "no deterministic sandbox handler" in (result.error or "")
    assert _count_partition(source_database, "ios") == 1


def test_executor_rejects_path_traversal_before_creating_sandbox(tmp_path: Path) -> None:
    source_database = tmp_path / "faulty.duckdb"
    repair_source_database = tmp_path / "baseline.duckdb"
    _write_database(source_database, [(1, "ios", 10)])
    _write_database(repair_source_database, [(1, "ios", 10)])
    proposal = _proposal()
    executor = SandboxRepairExecutor(
        source_database,
        tmp_path / "sandboxes",
        repair_source_database_path=repair_source_database,
    )
    unsafe_run = _run(proposal, run_id="../outside")

    with pytest.raises(SandboxRepairError, match="run_id"):
        executor.execute(proposal, unsafe_run)

    assert not (tmp_path / "outside").exists()
