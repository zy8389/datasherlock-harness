"""Atomic incident checkpoints and append-only local audit events."""

from __future__ import annotations

import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from typing import Final, Protocol
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator

from harness.state import IncidentState

_INCIDENT_ID_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]*$"
)


class CheckpointStoreError(RuntimeError):
    """Base error for durable incident checkpoint operations."""


class IncidentNotFoundError(CheckpointStoreError):
    """Raised when a requested incident checkpoint has not been created."""


class IncidentAlreadyExistsError(CheckpointStoreError):
    """Raised when callers try to create a second checkpoint for one incident."""


class CheckpointConflictError(CheckpointStoreError):
    """Raised when a stale revision attempts to overwrite a newer checkpoint."""


class IncidentCheckpoint(BaseModel):
    """Versioned, restorable incident state persisted by the checkpoint store."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    incident_id: str = Field(min_length=1)
    revision: int = Field(ge=1)
    saved_at: datetime
    state: IncidentState

    @field_validator("incident_id")
    @classmethod
    def validate_incident_id(cls, value: str) -> str:
        if not _INCIDENT_ID_PATTERN.fullmatch(value):
            raise ValueError("incident_id contains unsafe path characters")
        return value

    @field_validator("saved_at")
    @classmethod
    def normalize_saved_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("saved_at must be timezone-aware")
        return value.astimezone(UTC)


class IncidentAuditEvent(BaseModel):
    """One append-only state transition or execution event."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    event_id: str = Field(default_factory=lambda: f"EV-{uuid4()}", min_length=1)
    incident_id: str = Field(min_length=1)
    event_type: str = Field(min_length=1)
    revision: int = Field(ge=1)
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    details: dict[str, JsonValue] = Field(default_factory=dict)

    @field_validator("incident_id")
    @classmethod
    def validate_incident_id(cls, value: str) -> str:
        if not _INCIDENT_ID_PATTERN.fullmatch(value):
            raise ValueError("incident_id contains unsafe path characters")
        return value

    @field_validator("occurred_at")
    @classmethod
    def normalize_occurred_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("occurred_at must be timezone-aware")
        return value.astimezone(UTC)


class IncidentCheckpointRepository(Protocol):
    """Durable state store contract used by the incident API and workflow."""

    def create(self, state: IncidentState) -> IncidentCheckpoint: ...

    def load(self, incident_id: str) -> IncidentCheckpoint: ...

    def save(
        self, state: IncidentState, *, expected_revision: int
    ) -> IncidentCheckpoint: ...

    def append_event(self, event: IncidentAuditEvent) -> IncidentAuditEvent: ...

    def read_events(self, incident_id: str) -> list[IncidentAuditEvent]: ...


class IncidentCheckpointStore:
    """Filesystem-backed checkpoints with atomic writes and process-local locking.

    The store is suitable for the local MVP. It prevents accidental stale writes
    through a caller-supplied revision, but distributed deployments should use a
    database transaction or distributed lock around the same revision contract.
    """

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root).resolve()
        self._root.mkdir(parents=True, exist_ok=True)
        if not self._root.is_dir():
            raise CheckpointStoreError("checkpoint root must be a directory")
        self._lock = RLock()

    def create(self, state: IncidentState) -> IncidentCheckpoint:
        """Persist the first revision of an incident and reject replacement."""

        incident_id = self._incident_id_from_state(state)
        with self._lock:
            checkpoint_path = self._checkpoint_path(incident_id)
            if checkpoint_path.exists():
                raise IncidentAlreadyExistsError(f"incident already exists: {incident_id}")
            checkpoint = IncidentCheckpoint(
                incident_id=incident_id,
                revision=1,
                saved_at=datetime.now(UTC),
                state=state,
            )
            self._write_checkpoint(checkpoint)
            return checkpoint

    def load(self, incident_id: str) -> IncidentCheckpoint:
        """Load and validate the latest checkpoint for one incident."""

        with self._lock:
            checkpoint_path = self._checkpoint_path(incident_id)
            if not checkpoint_path.is_file():
                raise IncidentNotFoundError(f"incident not found: {incident_id}")
            try:
                payload = json.loads(checkpoint_path.read_text(encoding="utf-8"))
                checkpoint = IncidentCheckpoint.model_validate(payload)
            except (OSError, json.JSONDecodeError, ValueError) as exc:
                raise CheckpointStoreError(
                    f"invalid checkpoint for incident {incident_id}"
                ) from exc
            if checkpoint.incident_id != incident_id:
                raise CheckpointStoreError("checkpoint incident_id does not match its path")
            return checkpoint

    def save(self, state: IncidentState, *, expected_revision: int) -> IncidentCheckpoint:
        """Atomically save the next revision when the caller has the latest state."""

        incident_id = self._incident_id_from_state(state)
        with self._lock:
            current = self.load(incident_id)
            if expected_revision != current.revision:
                raise CheckpointConflictError(
                    f"expected revision {expected_revision}, current revision is {current.revision}"
                )
            checkpoint = IncidentCheckpoint(
                incident_id=incident_id,
                revision=current.revision + 1,
                saved_at=datetime.now(UTC),
                state=state,
            )
            self._write_checkpoint(checkpoint)
            return checkpoint

    def append_event(self, event: IncidentAuditEvent) -> IncidentAuditEvent:
        """Durably append a JSONL audit event for an existing incident."""

        with self._lock:
            self.load(event.incident_id)
            audit_path = self._audit_path(event.incident_id)
            line = json.dumps(
                event.model_dump(mode="json"), ensure_ascii=True, separators=(",", ":")
            )
            try:
                with audit_path.open("a", encoding="utf-8") as file:
                    file.write(line + "\n")
                    file.flush()
                    os.fsync(file.fileno())
            except OSError as exc:
                raise CheckpointStoreError(
                    f"could not append audit event for incident {event.incident_id}"
                ) from exc
            return event

    def read_events(self, incident_id: str) -> list[IncidentAuditEvent]:
        """Return the validated append-only audit sequence for one incident."""

        with self._lock:
            self.load(incident_id)
            audit_path = self._audit_path(incident_id)
            if not audit_path.is_file():
                return []
            try:
                return [
                    IncidentAuditEvent.model_validate(json.loads(line))
                    for line in audit_path.read_text(encoding="utf-8").splitlines()
                    if line.strip()
                ]
            except (OSError, json.JSONDecodeError, ValueError) as exc:
                raise CheckpointStoreError(
                    f"invalid audit log for incident {incident_id}"
                ) from exc

    def _write_checkpoint(self, checkpoint: IncidentCheckpoint) -> None:
        checkpoint_path = self._checkpoint_path(checkpoint.incident_id)
        checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = checkpoint_path.with_name(
            f".{checkpoint_path.name}.{uuid4()}.tmp"
        )
        payload = json.dumps(
            checkpoint.model_dump(mode="json"), ensure_ascii=True, separators=(",", ":")
        )
        try:
            with temporary_path.open("x", encoding="utf-8") as file:
                file.write(payload)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary_path, checkpoint_path)
        except OSError as exc:
            raise CheckpointStoreError(
                f"could not save checkpoint for incident {checkpoint.incident_id}"
            ) from exc
        finally:
            if temporary_path.exists():
                temporary_path.unlink(missing_ok=True)

    def _checkpoint_path(self, incident_id: str) -> Path:
        return self._incident_directory(incident_id) / "checkpoint.json"

    def _audit_path(self, incident_id: str) -> Path:
        directory = self._incident_directory(incident_id)
        directory.mkdir(parents=True, exist_ok=True)
        return directory / "audit.jsonl"

    def _incident_directory(self, incident_id: str) -> Path:
        if not _INCIDENT_ID_PATTERN.fullmatch(incident_id):
            raise CheckpointStoreError("incident_id contains unsafe path characters")
        directory = (self._root / incident_id).resolve()
        if not directory.is_relative_to(self._root):
            raise CheckpointStoreError("incident directory escapes checkpoint root")
        return directory

    @staticmethod
    def _incident_id_from_state(state: IncidentState) -> str:
        incident_id = state.alert.get("incident_id")
        if not isinstance(incident_id, str) or not _INCIDENT_ID_PATTERN.fullmatch(
            incident_id
        ):
            raise CheckpointStoreError("state alert must contain a safe incident_id")
        return incident_id


__all__ = [
    "CheckpointConflictError",
    "CheckpointStoreError",
    "IncidentAlreadyExistsError",
    "IncidentAuditEvent",
    "IncidentCheckpoint",
    "IncidentCheckpointRepository",
    "IncidentCheckpointStore",
    "IncidentNotFoundError",
]
