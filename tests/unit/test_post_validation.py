from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import duckdb
import pytest

from config.faults import EvidenceSourceType
from harness.post_validation import PostRepairValidationError, PostRepairValidator
from harness.repair import (
    RepairAction,
    RepairEvidence,
    RepairProposal,
    RepairRisk,
    SandboxRun,
    SandboxRunStatus,
)

NOW = datetime(2026, 8, 27, 10, tzinfo=UTC)
TARGET_DATE = date(2026, 8, 12)


def _write_events_database(path: Path, rows: list[tuple[int, int, str, str]]) -> None:
    with duckdb.connect(str(path)) as connection:
        connection.execute(
            "CREATE TABLE events (event_id INTEGER, user_id INTEGER, event_time TIMESTAMP, event_name VARCHAR)"
        )
        connection.executemany("INSERT INTO events VALUES (?, ?, ?, ?)", rows)


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
        rationale="Restore missing source rows in a sandbox.",
        created_at=NOW,
        valid_until=NOW + timedelta(hours=1),
    )


def _run(proposal: RepairProposal, sandbox_path: Path) -> SandboxRun:
    return SandboxRun(
        run_id="SR-001",
        incident_id=proposal.incident_id,
        proposal_id=proposal.proposal_id,
        proposal_hash=proposal.proposal_hash,
        approval_decision_id="AD-001",
        action=proposal.action,
        sandbox_path=str(sandbox_path),
        status=SandboxRunStatus.SUCCEEDED,
        started_at=NOW,
        finished_at=NOW + timedelta(minutes=1),
    )


def test_post_validation_passes_when_target_metric_recovers(tmp_path: Path) -> None:
    source = tmp_path / "source.duckdb"
    sandbox = tmp_path / "sandbox.duckdb"
    _write_events_database(source, [(1, 1, "2026-08-12 10:00:00", "login")])
    _write_events_database(
        sandbox,
        [
            (1, 1, "2026-08-12 10:00:00", "login"),
            (2, 2, "2026-08-12 11:00:00", "login"),
            (3, 3, "2026-08-12 12:00:00", "run_ai_task"),
        ],
    )
    proposal = _proposal()

    result = PostRepairValidator(source).validate(
        proposal,
        _run(proposal, sandbox),
        metric_id="daily_active_users",
        metric_date=TARGET_DATE,
        expected_value=3,
        validation_id="PV-001",
    )

    assert result.status.value == "passed"
    assert result.observed_before == 1
    assert result.observed_after == 3
    assert result.regressions == ()


def test_post_validation_fails_when_configured_metric_regresses(tmp_path: Path) -> None:
    source = tmp_path / "source.duckdb"
    sandbox = tmp_path / "sandbox.duckdb"
    _write_events_database(
        source,
        [
            (1, 1, "2026-08-12 10:00:00", "run_ai_task"),
            (2, 1, "2026-08-12 11:00:00", "run_ai_task"),
            (3, 2, "2026-08-12 12:00:00", "run_ai_task"),
            (4, 3, "2026-08-12 13:00:00", "run_ai_task"),
        ],
    )
    _write_events_database(
        sandbox,
        [
            (1, 1, "2026-08-12 10:00:00", "run_ai_task"),
            (2, 2, "2026-08-12 11:00:00", "login"),
            (3, 3, "2026-08-12 12:00:00", "login"),
        ],
    )
    proposal = _proposal()

    result = PostRepairValidator(source).validate(
        proposal,
        _run(proposal, sandbox),
        metric_id="daily_active_users",
        metric_date=TARGET_DATE,
        expected_value=3,
        regression_metric_ids=("ai_task_count",),
        max_regression_ratio=0.2,
        validation_id="PV-002",
    )

    assert result.status.value == "failed"
    assert result.target_met is True
    assert result.regressions[0].startswith("ai_task_count:")


def test_post_validation_rejects_unconfigured_metrics_and_non_successful_runs(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.duckdb"
    sandbox = tmp_path / "sandbox.duckdb"
    _write_events_database(source, [(1, 1, "2026-08-12 10:00:00", "login")])
    _write_events_database(sandbox, [(1, 1, "2026-08-12 10:00:00", "login")])
    proposal = _proposal()
    validator = PostRepairValidator(source)

    with pytest.raises(PostRepairValidationError, match="unknown configured metric"):
        validator.validate(
            proposal,
            _run(proposal, sandbox),
            metric_id="not_a_metric",
            metric_date=TARGET_DATE,
            expected_value=1,
        )

    pending_run = _run(proposal, sandbox).model_copy(
        update={"status": SandboxRunStatus.PENDING, "started_at": None, "finished_at": None}
    )
    with pytest.raises(PostRepairValidationError, match="successful sandbox run"):
        validator.validate(
            proposal,
            pending_run,
            metric_id="daily_active_users",
            metric_date=TARGET_DATE,
            expected_value=1,
        )
