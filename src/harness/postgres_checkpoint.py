"""Transactional PostgreSQL storage for incident checkpoints and audit events."""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import quote

import psycopg

from harness.checkpoint import (
    CheckpointConflictError,
    CheckpointStoreError,
    IncidentAlreadyExistsError,
    IncidentAuditEvent,
    IncidentCheckpoint,
    IncidentNotFoundError,
)
from harness.state import IncidentState

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS incident_checkpoints (
    incident_id TEXT PRIMARY KEY,
    revision INTEGER NOT NULL CHECK (revision >= 1),
    saved_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    state JSONB NOT NULL
);

CREATE TABLE IF NOT EXISTS incident_audit_events (
    event_id TEXT PRIMARY KEY,
    incident_id TEXT NOT NULL REFERENCES incident_checkpoints (incident_id),
    event_type TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK (revision >= 1),
    occurred_at TIMESTAMPTZ NOT NULL,
    details JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS incident_audit_events_incident_order_idx
    ON incident_audit_events (incident_id, revision, occurred_at, event_id);
"""


class PostgresCheckpointStore:
    """Checkpoint repository with database transactions and optimistic locking.

    ``save`` updates only when the caller's revision is current. PostgreSQL
    obtains the required row lock for the conditional ``UPDATE``; concurrent
    writers therefore cannot overwrite a newer state snapshot.
    """

    def __init__(
        self,
        database_url: str,
        *,
        audit_retention_days: int = 365,
        connection_factory: Callable[[], Any] | None = None,
        initialize_schema: bool = True,
    ) -> None:
        if not database_url.strip():
            raise ValueError("database_url must not be blank")
        if audit_retention_days < 1:
            raise ValueError("audit_retention_days must be at least one day")
        self._database_url = database_url
        self._audit_retention_days = audit_retention_days
        self._connection_factory = connection_factory or (
            lambda: psycopg.connect(self._database_url)
        )
        if initialize_schema:
            self.initialize_schema()

    @classmethod
    def from_environment(
        cls, environment: Mapping[str, str] | None = None
    ) -> PostgresCheckpointStore:
        values = environment if environment is not None else os.environ
        database_url = values.get("INCIDENT_DATABASE_URL") or _database_url(values)
        try:
            retention_days = int(values.get("INCIDENT_AUDIT_RETENTION_DAYS", "365"))
        except ValueError as exc:
            raise ValueError(
                "INCIDENT_AUDIT_RETENTION_DAYS must be an integer"
            ) from exc
        return cls(database_url, audit_retention_days=retention_days)

    def initialize_schema(self) -> None:
        """Apply idempotent schema initialization before serving incident traffic."""

        try:
            with self._connection_factory() as connection:
                connection.execute(_SCHEMA_SQL)
        except Exception as exc:
            raise CheckpointStoreError(
                "could not initialize incident checkpoint schema"
            ) from exc

    def create(self, state: IncidentState) -> IncidentCheckpoint:
        """Create initial revision one in a single database transaction."""

        incident_id = _incident_id_from_state(state)
        try:
            with (
                self._connection_factory() as connection,
                connection.cursor() as cursor,
            ):
                cursor.execute(
                    """
                        INSERT INTO incident_checkpoints (incident_id, revision, state)
                        VALUES (%s, 1, %s::jsonb)
                        ON CONFLICT (incident_id) DO NOTHING
                        RETURNING revision, saved_at
                        """,
                    (incident_id, _json_payload(state.to_dict())),
                )
                row = cursor.fetchone()
                if row is None:
                    raise IncidentAlreadyExistsError(
                        f"incident already exists: {incident_id}"
                    )
        except IncidentAlreadyExistsError:
            raise
        except Exception as exc:
            raise CheckpointStoreError(
                f"could not create checkpoint for incident {incident_id}"
            ) from exc
        return IncidentCheckpoint(
            incident_id=incident_id,
            revision=row[0],
            saved_at=row[1],
            state=state,
        )

    def load(self, incident_id: str) -> IncidentCheckpoint:
        """Load and validate the latest state snapshot for an incident."""

        try:
            with (
                self._connection_factory() as connection,
                connection.cursor() as cursor,
            ):
                cursor.execute(
                    """
                        SELECT revision, saved_at, state
                        FROM incident_checkpoints
                        WHERE incident_id = %s
                        """,
                    (incident_id,),
                )
                row = cursor.fetchone()
        except Exception as exc:
            raise CheckpointStoreError(
                f"could not load checkpoint for incident {incident_id}"
            ) from exc
        if row is None:
            raise IncidentNotFoundError(f"incident not found: {incident_id}")
        return _checkpoint_from_row(incident_id, row)

    def save(
        self, state: IncidentState, *, expected_revision: int
    ) -> IncidentCheckpoint:
        """Atomically advance one checkpoint revision or report a conflict."""

        incident_id = _incident_id_from_state(state)
        try:
            with (
                self._connection_factory() as connection,
                connection.cursor() as cursor,
            ):
                cursor.execute(
                    """
                        UPDATE incident_checkpoints
                        SET revision = revision + 1, saved_at = NOW(), state = %s::jsonb
                        WHERE incident_id = %s AND revision = %s
                        RETURNING revision, saved_at
                        """,
                    (_json_payload(state.to_dict()), incident_id, expected_revision),
                )
                row = cursor.fetchone()
                if row is None:
                    cursor.execute(
                        "SELECT revision FROM incident_checkpoints WHERE incident_id = %s",
                        (incident_id,),
                    )
                    current = cursor.fetchone()
                    if current is None:
                        raise IncidentNotFoundError(
                            f"incident not found: {incident_id}"
                        )
                    raise CheckpointConflictError(
                        f"expected revision {expected_revision}, current revision is {current[0]}"
                    )
        except (CheckpointConflictError, IncidentNotFoundError):
            raise
        except Exception as exc:
            raise CheckpointStoreError(
                f"could not save checkpoint for incident {incident_id}"
            ) from exc
        return IncidentCheckpoint(
            incident_id=incident_id,
            revision=row[0],
            saved_at=row[1],
            state=state,
        )

    def append_event(self, event: IncidentAuditEvent) -> IncidentAuditEvent:
        """Append an audit event and enforce the configured retention window."""

        try:
            with (
                self._connection_factory() as connection,
                connection.cursor() as cursor,
            ):
                cursor.execute(
                    """
                        INSERT INTO incident_audit_events (
                            event_id, incident_id, event_type, revision, occurred_at, details
                        )
                        SELECT %s, %s, %s, %s, %s, %s::jsonb
                        WHERE EXISTS (
                            SELECT 1 FROM incident_checkpoints WHERE incident_id = %s
                        )
                        RETURNING event_id
                        """,
                    (
                        event.event_id,
                        event.incident_id,
                        event.event_type,
                        event.revision,
                        event.occurred_at,
                        _json_payload(event.details),
                        event.incident_id,
                    ),
                )
                if cursor.fetchone() is None:
                    raise IncidentNotFoundError(
                        f"incident not found: {event.incident_id}"
                    )
                cursor.execute(
                    """
                        DELETE FROM incident_audit_events
                        WHERE occurred_at < NOW() - make_interval(days => %s)
                        """,
                    (self._audit_retention_days,),
                )
        except IncidentNotFoundError:
            raise
        except Exception as exc:
            raise CheckpointStoreError(
                f"could not append audit event for incident {event.incident_id}"
            ) from exc
        return event

    def read_events(self, incident_id: str) -> list[IncidentAuditEvent]:
        """Return one incident's retained audit stream in deterministic order."""

        try:
            with (
                self._connection_factory() as connection,
                connection.cursor() as cursor,
            ):
                cursor.execute(
                    "SELECT 1 FROM incident_checkpoints WHERE incident_id = %s",
                    (incident_id,),
                )
                if cursor.fetchone() is None:
                    raise IncidentNotFoundError(f"incident not found: {incident_id}")
                cursor.execute(
                    """
                        SELECT event_id, event_type, revision, occurred_at, details
                        FROM incident_audit_events
                        WHERE incident_id = %s
                        ORDER BY revision, occurred_at, event_id
                        """,
                    (incident_id,),
                )
                rows = cursor.fetchall()
        except IncidentNotFoundError:
            raise
        except Exception as exc:
            raise CheckpointStoreError(
                f"could not read audit events for incident {incident_id}"
            ) from exc
        return [
            IncidentAuditEvent(
                event_id=row[0],
                incident_id=incident_id,
                event_type=row[1],
                revision=row[2],
                occurred_at=row[3],
                details=_json_object(row[4]),
            )
            for row in rows
        ]


def _database_url(values: Mapping[str, str]) -> str:
    host = values.get("POSTGRES_HOST", "localhost")
    port = values.get("POSTGRES_PORT", "5432")
    database = values.get("POSTGRES_DB", "datasherlock")
    user = quote(values.get("POSTGRES_USER", "datasherlock"), safe="")
    password = quote(values.get("POSTGRES_PASSWORD", "change-me"), safe="")
    return f"postgresql://{user}:{password}@{host}:{port}/{database}"


def _incident_id_from_state(state: IncidentState) -> str:
    incident_id = state.alert.get("incident_id")
    if not isinstance(incident_id, str) or not incident_id.strip():
        raise CheckpointStoreError("state alert must contain a safe incident_id")
    return incident_id


def _checkpoint_from_row(incident_id: str, row: tuple[Any, ...]) -> IncidentCheckpoint:
    try:
        return IncidentCheckpoint(
            incident_id=incident_id,
            revision=row[0],
            saved_at=row[1],
            state=IncidentState.from_dict(_json_object(row[2])),
        )
    except (TypeError, ValueError) as exc:
        raise CheckpointStoreError(
            f"invalid checkpoint for incident {incident_id}"
        ) from exc


def _json_payload(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"))


def _json_object(value: object) -> dict[str, Any]:
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, dict):
        raise TypeError("database JSON value must be an object")
    return value


__all__ = ["PostgresCheckpointStore"]
