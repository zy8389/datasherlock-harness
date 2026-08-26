from datetime import UTC, datetime
from typing import Self

import pytest

from harness.checkpoint import CheckpointConflictError, IncidentAuditEvent
from harness.postgres_checkpoint import PostgresCheckpointStore
from harness.state import IncidentState, IncidentStatus


class _Cursor:
    def __init__(self, responses: list[object]) -> None:
        self.calls: list[tuple[str, tuple[object, ...] | None]] = []
        self._responses = responses

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def execute(
        self, statement: str, parameters: tuple[object, ...] | None = None
    ) -> None:
        self.calls.append((statement, parameters))

    def fetchone(self) -> object:
        return self._responses.pop(0)

    def fetchall(self) -> list[object]:
        return []


class _Connection:
    def __init__(self, cursor: _Cursor) -> None:
        self._cursor = cursor

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def cursor(self) -> _Cursor:
        return self._cursor


def _state() -> IncidentState:
    return IncidentState(
        alert={"incident_id": "INC-001", "metric": "daily_active_users"},
        status=IncidentStatus.TRIAGE,
    )


def test_postgres_store_uses_revision_guarded_update_for_concurrent_writers() -> None:
    now = datetime(2026, 8, 27, tzinfo=UTC)
    cursor = _Cursor(responses=[(2, now)])
    store = PostgresCheckpointStore(
        "postgresql://unused",
        connection_factory=lambda: _Connection(cursor),
        initialize_schema=False,
    )
    state = _state()

    saved = store.save(state, expected_revision=1)

    assert saved.revision == 2
    statement, parameters = cursor.calls[0]
    assert "WHERE incident_id = %s AND revision = %s" in statement
    assert parameters is not None
    assert parameters[-2:] == ("INC-001", 1)


def test_postgres_store_reports_the_current_revision_after_a_write_conflict() -> None:
    cursor = _Cursor(responses=[None, (3,)])
    store = PostgresCheckpointStore(
        "postgresql://unused",
        connection_factory=lambda: _Connection(cursor),
        initialize_schema=False,
    )

    with pytest.raises(CheckpointConflictError, match="current revision is 3"):
        store.save(_state(), expected_revision=2)


def test_postgres_store_prunes_expired_audit_events_in_the_append_transaction() -> None:
    cursor = _Cursor(responses=[("EV-001",)])
    store = PostgresCheckpointStore(
        "postgresql://unused",
        audit_retention_days=90,
        connection_factory=lambda: _Connection(cursor),
        initialize_schema=False,
    )
    event = IncidentAuditEvent(
        event_id="EV-001",
        incident_id="INC-001",
        event_type="incident_created",
        revision=1,
    )

    store.append_event(event)

    statements = [statement for statement, _ in cursor.calls]
    assert "INSERT INTO incident_audit_events" in statements[0]
    assert "DELETE FROM incident_audit_events" in statements[1]
    assert cursor.calls[1][1] == (90,)
