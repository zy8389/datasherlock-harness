import json
from pathlib import Path

import pytest

from harness.checkpoint import (
    CheckpointConflictError,
    IncidentAlreadyExistsError,
    IncidentAuditEvent,
    IncidentCheckpointStore,
    IncidentNotFoundError,
)
from harness.state import IncidentState, IncidentStatus


def _state() -> IncidentState:
    return IncidentState(
        alert={"incident_id": "INC-001", "metric": "daily_active_users"},
        status=IncidentStatus.TRIAGE,
    )


def test_checkpoint_store_creates_loads_and_atomically_revises_state(tmp_path: Path) -> None:
    store = IncidentCheckpointStore(tmp_path / "checkpoints")
    state = _state()

    created = store.create(state)
    assert created.revision == 1
    assert store.load("INC-001") == created

    state.current_conclusion = "Investigating missing data."
    saved = store.save(state, expected_revision=created.revision)

    assert saved.revision == 2
    assert store.load("INC-001").state.current_conclusion == "Investigating missing data."
    assert not list((tmp_path / "checkpoints" / "INC-001").glob("*.tmp"))


def test_checkpoint_store_rejects_duplicate_creation_and_stale_writes(tmp_path: Path) -> None:
    store = IncidentCheckpointStore(tmp_path / "checkpoints")
    state = _state()
    created = store.create(state)

    with pytest.raises(IncidentAlreadyExistsError):
        store.create(state)

    store.save(state, expected_revision=created.revision)
    with pytest.raises(CheckpointConflictError, match="current revision is 2"):
        store.save(state, expected_revision=created.revision)


def test_checkpoint_store_appends_auditable_jsonl_events(tmp_path: Path) -> None:
    store = IncidentCheckpointStore(tmp_path / "checkpoints")
    checkpoint = store.create(_state())
    event = IncidentAuditEvent(
        incident_id="INC-001",
        event_type="repair_approved",
        revision=checkpoint.revision,
        details={"proposal_id": "RP-001", "reviewer": "data-engineer"},
    )

    returned = store.append_event(event)

    audit_path = tmp_path / "checkpoints" / "INC-001" / "audit.jsonl"
    assert returned == event
    assert json.loads(audit_path.read_text(encoding="utf-8")) == event.model_dump(
        mode="json"
    )


def test_checkpoint_store_rejects_unknown_and_unsafe_incident_ids(tmp_path: Path) -> None:
    store = IncidentCheckpointStore(tmp_path / "checkpoints")

    with pytest.raises(IncidentNotFoundError):
        store.load("INC-001")
    with pytest.raises(Exception, match="unsafe"):
        store.load("../escape")
    assert not (tmp_path / "escape").exists()
