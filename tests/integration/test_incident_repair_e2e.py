"""A full approval-gated repair journey against temporary DuckDB databases."""

import json
from pathlib import Path

import duckdb
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.auth import ApprovalAuthenticator
from api.incidents import create_incident_router
from harness.checkpoint import IncidentCheckpointStore
from harness.post_validation import PostRepairValidator
from harness.repair_workflow import RepairWorkflowService
from harness.root_cause import DiagnosticEvidenceBinding
from harness.sandbox_repair import SandboxRepairExecutor
from harness.state import IncidentStatus
from harness.tool_router import InvestigationToolRouter


def _write_database(path: Path, event_rows: list[tuple[object, ...]]) -> None:
    with duckdb.connect(str(path)) as connection:
        connection.execute(
            "CREATE TABLE events ("
            "event_id INTEGER, user_id INTEGER, event_time TIMESTAMP, "
            "event_name VARCHAR, device_type VARCHAR)"
        )
        connection.execute(
            "CREATE TABLE partition_metadata ("
            "partition_value VARCHAR, row_count INTEGER, status VARCHAR)"
        )
        connection.executemany("INSERT INTO events VALUES (?, ?, ?, ?, ?)", event_rows)
        android_rows = sum(1 for row in event_rows if row[4] == "android")
        metadata_status = "ready" if android_rows else "missing"
        connection.execute(
            "INSERT INTO partition_metadata VALUES (?, ?, ?)",
            ["android", android_rows, metadata_status],
        )


def _approval_authenticator() -> ApprovalAuthenticator:
    return ApprovalAuthenticator.from_environment(
        {
            "INCIDENT_APPROVAL_IDENTITIES": json.dumps(
                [
                    {
                        "token": "e2e-approver-token",
                        "subject": "e2e-approver",
                        "permissions": ["repair:approve"],
                        "identity_source": "integration_test_bearer",
                    }
                ]
            )
        }
    )


def _count_android_events(path: Path) -> int:
    with duckdb.connect(str(path), read_only=True) as connection:
        return int(
            connection.execute(
                "SELECT COUNT(*) FROM events WHERE device_type = 'android'"
            ).fetchone()[0]
        )


def test_incident_repair_lifecycle_from_evidence_to_post_validation(tmp_path: Path) -> None:
    faulty_database = tmp_path / "faulty.duckdb"
    repair_source_database = tmp_path / "baseline.duckdb"
    _write_database(
        faulty_database,
        [(1, 1, "2026-08-12 10:00:00", "login", "ios")],
    )
    _write_database(
        repair_source_database,
        [
            (1, 1, "2026-08-12 10:00:00", "login", "ios"),
            (2, 2, "2026-08-12 11:00:00", "login", "android"),
            (3, 3, "2026-08-12 12:00:00", "run_ai_task", "android"),
        ],
    )
    store = IncidentCheckpointStore(tmp_path / "checkpoints")
    workflow = RepairWorkflowService(
        SandboxRepairExecutor(
            faulty_database,
            tmp_path / "sandboxes",
            repair_source_database_path=repair_source_database,
        ),
        PostRepairValidator(faulty_database),
    )
    app = FastAPI()
    app.include_router(
        create_incident_router(
            store_provider=lambda: store,
            workflow_provider=lambda: workflow,
            approval_authenticator=_approval_authenticator(),
        )
    )
    client = TestClient(app)

    created = client.post(
        "/incidents",
        json={
            "alert": {
                "incident_id": "INC-E2E-001",
                "metric": "daily_active_users",
                "observed_at": "2026-08-12",
                "expected_value": 3,
            }
        },
    )
    assert created.status_code == 201

    checkpoint = store.load("INC-E2E-001")
    checkpoint.state.status = IncidentStatus.HYPOTHESIS_TESTING
    tools = InvestigationToolRouter()
    business_binding = DiagnosticEvidenceBinding(
        root_cause_type="missing_partition",
        source_type="business_data",
        asset="events",
        repair_context={"partition_value": "android"},
    )
    metadata_binding = DiagnosticEvidenceBinding(
        root_cause_type="missing_partition",
        source_type="operational_metadata",
        asset="partition_metadata",
        repair_context={"partition_value": "android"},
    )
    tools.execute(
        checkpoint.state,
        "sql_query",
        {
            "sql": "SELECT COUNT(*) AS android_events FROM events "
            "WHERE device_type = 'android'"
        },
        database_path=faulty_database,
        finding="The Android events partition has zero rows.",
        diagnostic_binding=business_binding,
    )
    tools.execute(
        checkpoint.state,
        "sql_query",
        {
            "sql": "SELECT row_count, status FROM partition_metadata "
            "WHERE partition_value = 'android'"
        },
        database_path=faulty_database,
        finding="The Android partition metadata is marked missing.",
        diagnostic_binding=metadata_binding,
    )
    diagnosed = store.save(checkpoint.state, expected_revision=checkpoint.revision)
    assert len(diagnosed.state.evidence) == 2

    confirmed = client.post(
        "/incidents/INC-E2E-001/root-cause",
        json={"expected_revision": diagnosed.revision},
    )
    assert confirmed.status_code == 200
    assert confirmed.json()["state"]["status"] == "ROOT_CAUSE_FOUND"

    proposed = client.post(
        "/incidents/INC-E2E-001/repair-proposals",
        json={"expected_revision": confirmed.json()["revision"]},
    )
    assert proposed.status_code == 200
    proposal = proposed.json()["state"]["repair_proposal"]
    assert proposal["action"] == "rerun_partition"
    assert proposal["parameters"]["partition_value"] == "android"

    approved = client.post(
        "/incidents/INC-E2E-001/approval",
        json={
            "expected_revision": proposed.json()["revision"],
            "decision_id": "AD-E2E-001",
            "outcome": "approved",
        },
        headers={"Authorization": "Bearer e2e-approver-token"},
    )
    assert approved.status_code == 200
    assert approved.json()["state"]["approval"]["reviewer"] == "e2e-approver"

    completed = client.post(
        "/incidents/INC-E2E-001/sandbox-repair",
        json={"expected_revision": approved.json()["revision"]},
    )
    assert completed.status_code == 200
    state = completed.json()["state"]
    assert state["status"] == "RESOLVED"
    assert state["sandbox_run"]["status"] == "succeeded"
    assert state["repair_result"]["status"] == "passed"
    assert state["repair_result"]["observed_before"] == 1
    assert state["repair_result"]["observed_after"] == 3

    assert _count_android_events(faulty_database) == 0
    assert _count_android_events(Path(state["sandbox_run"]["sandbox_path"])) == 2
    audit = client.get("/incidents/INC-E2E-001/audit")
    assert [event["event_type"] for event in audit.json()] == [
        "incident_created",
        "root_cause_confirmed",
        "repair_proposal_submitted",
        "repair_approval_recorded",
        "sandbox_repair_completed",
    ]
