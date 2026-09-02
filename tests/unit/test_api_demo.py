from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.demo import get_demo_service, router
from demo.service import DemoService


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    service = DemoService(workdir=tmp_path / "demo")
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_demo_service] = lambda: service
    return TestClient(app)


def test_demo_api_runs_f01_approval_e2e(client: TestClient) -> None:
    cases_response = client.get("/demo/cases")
    assert cases_response.status_code == 200
    assert len(cases_response.json()["cases"]) == 60

    start_response = client.post("/demo/incidents", json={"case_id": "F01-001"})
    assert start_response.status_code == 201
    started = start_response.json()
    assert started["status"] == "AWAITING_APPROVAL"
    assert started["root_cause"]["root_cause_type"] == "missing_partition"
    assert started["approval"] is None
    assert started["repair"] is None
    assert started["post_validation"] is None
    assert started["model_calls"] == 0
    serialized = json.dumps(started)
    assert "expected_root_cause" not in serialized
    assert "source_seed_case_id" not in serialized
    assert "target-date Android business events are absent" not in serialized
    assert "partition_metadata reports target Android partition row_count is zero" not in serialized
    assert "sandbox_path" not in serialized

    incident_id = started["incident_id"]
    assert client.get(f"/demo/incidents/{incident_id}").status_code == 200
    incidents = client.get("/demo/incidents")
    assert incidents.status_code == 200
    assert incidents.json()["incidents"][0]["incident_id"] == incident_id

    approve_response = client.post(
        f"/demo/incidents/{incident_id}/approval",
        json={
            "reviewer": "api-reviewer",
            "outcome": "approved",
            "comment": "Approved for sandbox execution.",
        },
    )
    assert approve_response.status_code == 200
    resolved = approve_response.json()
    assert resolved["status"] == "RESOLVED"
    assert resolved["repair"]["handler_invocation_count"] == 1
    assert resolved["post_validation"]["status"] == "passed"

    duplicate = client.post(
        f"/demo/incidents/{incident_id}/approval",
        json={"reviewer": "api-reviewer", "outcome": "approved"},
    )
    assert duplicate.status_code == 409
    persisted = client.get(f"/demo/incidents/{incident_id}").json()
    assert persisted["repair"]["handler_invocation_count"] == 1


def test_demo_api_errors_and_benchmark_contract(client: TestClient) -> None:
    assert client.get("/demo/incidents/not-a-uuid").status_code == 404
    assert client.post("/demo/incidents", json={"case_id": "F02-001"}).status_code == 400
    assert client.post("/demo/incidents", json={"case_id": "F99-999"}).status_code == 404

    invalid_rejection = client.post(
        "/demo/incidents/not-a-uuid/approval",
        json={"reviewer": "api-reviewer", "outcome": "rejected", "comment": ""},
    )
    assert invalid_rejection.status_code == 422

    benchmark = client.get("/demo/benchmark")
    assert benchmark.status_code == 200
    payload = benchmark.json()
    assert payload["historical"] is True
    assert payload["post_pr20_rerun"] is False
    assert len(payload["rows"]) == 4
