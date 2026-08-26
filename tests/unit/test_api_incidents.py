import json
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.auth import ApprovalAuthenticator
from api.incidents import create_incident_router
from harness.checkpoint import IncidentCheckpointStore
from harness.state import IncidentStatus


class _WorkflowStub:
    def __init__(self) -> None:
        self.calls = 0

    def execute_approved_repair(self, state, **kwargs):
        self.calls += 1
        state.status = IncidentStatus.TOOL_FAILED
        state.final_status = IncidentStatus.TOOL_FAILED
        return state


def _client(tmp_path: Path) -> tuple[TestClient, IncidentCheckpointStore, _WorkflowStub]:
    store = IncidentCheckpointStore(tmp_path / "checkpoints")
    workflow = _WorkflowStub()
    app = FastAPI()
    app.include_router(
        create_incident_router(
            store_provider=lambda: store,
            workflow_provider=lambda: workflow,
            approval_authenticator=_approval_authenticator(),
        )
    )
    return TestClient(app), store, workflow


def _approval_authenticator() -> ApprovalAuthenticator:
    return ApprovalAuthenticator.from_environment(
        {
            "INCIDENT_APPROVAL_IDENTITIES": json.dumps(
                [
                    {
                        "token": "approved-test-token",
                        "subject": "data-engineer",
                        "permissions": ["repair:approve"],
                        "identity_source": "test_bearer",
                    },
                    {
                        "token": "observer-test-token",
                        "subject": "observer",
                        "permissions": [],
                        "identity_source": "test_bearer",
                    },
                ]
            )
        }
    )


def _approval_headers(token: str = "approved-test-token") -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _record_diagnostic_evidence(checkpoint) -> None:
    checkpoint.state.status = IncidentStatus.HYPOTHESIS_TESTING
    checkpoint.state.tool_trace = [
        {
            "trace_id": "sql:Q01",
            "tool": "sql_runner",
            "query_id": "Q01",
            "response": {"status": "success"},
            "validation": {"evidence": {"usable": True}},
        },
        {
            "trace_id": "sql:Q02",
            "tool": "sql_runner",
            "query_id": "Q02",
            "response": {"status": "success"},
            "validation": {"evidence": {"usable": True}},
        },
    ]
    checkpoint.state.evidence = [
        {
            "evidence_id": "sql:Q01",
            "query_id": "Q01",
            "tool_trace_id": "sql:Q01",
            "root_cause_type": "missing_partition",
            "source_type": "business_data",
            "asset": "events",
            "repair_context": {"partition_value": "android"},
            "finding": "Android events are absent.",
        },
        {
            "evidence_id": "sql:Q02",
            "query_id": "Q02",
            "tool_trace_id": "sql:Q02",
            "root_cause_type": "missing_partition",
            "source_type": "operational_metadata",
            "asset": "partition_metadata",
            "repair_context": {"partition_value": "android"},
            "finding": "The partition is marked missing.",
        },
    ]


def test_incident_routes_persist_revisioned_approval_and_audit(tmp_path: Path) -> None:
    client, store, workflow = _client(tmp_path)
    created = client.post(
        "/incidents",
        json={
            "alert": {
                "incident_id": "INC-001",
                "metric": "daily_active_users",
                "observed_at": "2026-08-12",
                "expected_value": 3,
            }
        },
    )

    assert created.status_code == 201
    assert created.json()["revision"] == 1
    checkpoint = store.load("INC-001")
    _record_diagnostic_evidence(checkpoint)
    diagnosed = store.save(checkpoint.state, expected_revision=checkpoint.revision)

    rooted = client.post(
        "/incidents/INC-001/root-cause",
        json={"expected_revision": diagnosed.revision},
    )

    assert rooted.status_code == 200
    assert rooted.json()["revision"] == 3
    assert rooted.json()["state"]["status"] == "ROOT_CAUSE_FOUND"

    submitted = client.post(
        "/incidents/INC-001/repair-proposals",
        json={"expected_revision": rooted.json()["revision"]},
    )

    assert submitted.status_code == 200
    assert submitted.json()["revision"] == 4
    assert submitted.json()["state"]["status"] == "AWAITING_APPROVAL"
    proposal = submitted.json()["state"]["repair_proposal"]
    assert proposal["action"] == "rerun_partition"
    assert proposal["risk"] == "medium"
    assert proposal["parameters"] == {
        "table": "events",
        "source_table": "events",
        "partition_column": "device_type",
        "partition_value": "android",
    }
    stale = client.post(
        "/incidents/INC-001/approval",
        json={
            "expected_revision": diagnosed.revision,
            "decision_id": "AD-stale",
            "outcome": "approved",
        },
        headers=_approval_headers(),
    )
    assert stale.status_code == 409

    approved = client.post(
        "/incidents/INC-001/approval",
        json={
            "expected_revision": 4,
            "decision_id": "AD-001",
            "outcome": "approved",
        },
        headers=_approval_headers(),
    )
    assert approved.status_code == 200
    assert approved.json()["revision"] == 5
    assert approved.json()["state"]["status"] == "SANDBOX_REPAIR"

    executed = client.post(
        "/incidents/INC-001/sandbox-repair",
        json={"expected_revision": 5},
    )
    assert executed.status_code == 200
    assert executed.json()["revision"] == 6
    assert executed.json()["state"]["status"] == "TOOL_FAILED"
    assert workflow.calls == 1

    audit = client.get("/incidents/INC-001/audit")
    assert audit.status_code == 200
    assert [event["event_type"] for event in audit.json()] == [
        "incident_created",
        "root_cause_confirmed",
        "repair_proposal_submitted",
        "repair_approval_recorded",
        "sandbox_repair_completed",
    ]
    approval_event = audit.json()[3]
    assert approval_event["details"]["reviewer"] == "data-engineer"
    assert approval_event["details"]["identity_source"] == "test_bearer"
    assert approval_event["details"]["permissions"] == ["repair:approve"]


def test_incident_routes_return_not_found_for_unknown_incident(tmp_path: Path) -> None:
    client, _, _ = _client(tmp_path)

    response = client.get("/incidents/INC-404")

    assert response.status_code == 404


def test_repair_proposal_endpoint_rejects_client_supplied_repair_details(
    tmp_path: Path,
) -> None:
    client, _, _ = _client(tmp_path)
    created = client.post(
        "/incidents",
        json={"alert": {"incident_id": "INC-001", "metric": "daily_active_users"}},
    )

    response = client.post(
        "/incidents/INC-001/repair-proposals",
        json={"expected_revision": created.json()["revision"], "action": "rerun_partition"},
    )

    assert response.status_code == 422


def test_root_cause_endpoint_rejects_client_supplied_conclusion(tmp_path: Path) -> None:
    client, store, _ = _client(tmp_path)
    client.post(
        "/incidents",
        json={"alert": {"incident_id": "INC-001", "metric": "daily_active_users"}},
    )
    checkpoint = store.load("INC-001")
    _record_diagnostic_evidence(checkpoint)
    diagnosed = store.save(checkpoint.state, expected_revision=checkpoint.revision)

    response = client.post(
        "/incidents/INC-001/root-cause",
        json={
            "expected_revision": diagnosed.revision,
            "root_cause_type": "missing_partition",
        },
    )

    assert response.status_code == 422


def test_approval_requires_an_authenticated_authorized_principal(tmp_path: Path) -> None:
    client, _, _ = _client(tmp_path)

    missing_credentials = client.post(
        "/incidents/INC-001/approval",
        json={"expected_revision": 1, "decision_id": "AD-001", "outcome": "approved"},
    )
    insufficient_permissions = client.post(
        "/incidents/INC-001/approval",
        json={"expected_revision": 1, "decision_id": "AD-001", "outcome": "approved"},
        headers=_approval_headers("observer-test-token"),
    )
    forged_reviewer = client.post(
        "/incidents/INC-001/approval",
        json={
            "expected_revision": 1,
            "decision_id": "AD-001",
            "outcome": "approved",
            "reviewer": "admin",
        },
        headers=_approval_headers(),
    )

    assert missing_credentials.status_code == 401
    assert missing_credentials.headers["www-authenticate"] == "Bearer"
    assert insufficient_permissions.status_code == 403
    assert forged_reviewer.status_code == 422
