from __future__ import annotations

import json
from pathlib import Path

import pytest

from demo.service import (
    DemoCaseNotFoundError,
    DemoIncidentConflictError,
    DemoService,
)


def _assert_no_internal_paths(payload: object, workdir: Path) -> None:
    serialized = json.dumps(payload, sort_keys=True)
    assert str(workdir.resolve()) not in serialized
    assert "sandbox_path" not in serialized
    assert "database_path" not in serialized


def test_case_library_has_all_cases_without_ground_truth_answers(tmp_path: Path) -> None:
    service = DemoService(workdir=tmp_path / "demo")

    payload = service.list_cases().model_dump(mode="json")

    assert len(payload["cases"]) == 60
    assert any(item["case_id"] == "F01-001" for item in payload["cases"])
    serialized = json.dumps(payload)
    assert "expected_root_cause" not in serialized
    assert "root_cause_type" not in serialized
    assert "expected_evidence" not in serialized
    assert "source_seed_case_id" not in serialized
    assert "injection" not in serialized
    with pytest.raises(DemoCaseNotFoundError):
        service.start_incident("F99-999")


@pytest.mark.parametrize("case_id", [f"F01-{index:03d}" for index in range(1, 6)])
def test_validated_f01_variants_reach_authorized_approval_state(
    tmp_path: Path,
    case_id: str,
) -> None:
    service = DemoService(workdir=tmp_path / "demo")

    incident = service.start_incident(case_id)

    assert incident.status == "AWAITING_APPROVAL"
    assert incident.root_cause is not None
    assert incident.root_cause.root_cause_type == "missing_partition"
    assert incident.repair_proposal is not None
    assert incident.approval is None
    assert incident.repair is None
    assert incident.post_validation is None
    assert incident.model_calls == 0


def test_approve_resolves_once_persists_and_redacts_paths(tmp_path: Path) -> None:
    workdir = tmp_path / "demo"
    service = DemoService(workdir=workdir)
    started = service.start_incident("F01-001")

    resolved = service.decide_incident(
        started.incident_id,
        reviewer="demo-reviewer",
        outcome="approved",
        comment="Validated for the isolated demo.",
    )

    assert resolved.status == "RESOLVED"
    assert resolved.final_status == "RESOLVED"
    assert resolved.repair is not None
    assert resolved.repair.handler_invocation_count == 1
    assert resolved.post_validation is not None
    assert resolved.post_validation.status == "passed"
    assert resolved.final_report is not None
    _assert_no_internal_paths(resolved.model_dump(mode="json"), workdir)

    with pytest.raises(DemoIncidentConflictError):
        service.decide_incident(
            started.incident_id,
            reviewer="demo-reviewer",
            outcome="approved",
        )
    reloaded = DemoService(workdir=workdir).get_incident(started.incident_id)
    assert reloaded.status == "RESOLVED"
    assert reloaded.repair is not None
    assert reloaded.repair.handler_invocation_count == 1


def test_reject_is_terminal_without_creating_a_sandbox(tmp_path: Path) -> None:
    workdir = tmp_path / "demo"
    service = DemoService(workdir=workdir)
    started = service.start_incident("F01-001")

    rejected = service.decide_incident(
        started.incident_id,
        reviewer="demo-reviewer",
        outcome="rejected",
        comment="Needs manual review.",
    )

    assert rejected.status == "REJECTED"
    assert rejected.final_status == "REJECTED"
    assert rejected.approval is not None
    assert rejected.approval.outcome == "rejected"
    assert rejected.repair is None
    assert rejected.post_validation is None
    assert not (workdir / "incidents" / started.incident_id / "sandbox").exists()


def test_frozen_benchmark_is_loaded_from_committed_report(tmp_path: Path) -> None:
    snapshot = DemoService(workdir=tmp_path / "demo").benchmark_snapshot()

    assert snapshot.run_id == "full-60-4arch-post-pr14-20260831"
    assert snapshot.historical is True
    assert snapshot.post_pr20_rerun is False
    assert [row.variant for row in snapshot.rows] == [
        "single_prompt",
        "react",
        "state_graph_no_validator",
        "full_harness",
    ]
