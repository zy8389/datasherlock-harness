"""Durable checkpoints and deterministic interruption recovery.

The checkpoint layer persists the existing ``IncidentState`` as the source of
truth and adds only the metadata needed to resume a plan safely.  It does not
execute planners or tools while restoring a snapshot.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    ValidationError,
    model_validator,
)

from agents.planner import InvestigationStep
from harness.guardrails import fingerprint_step
from harness.state import IncidentState, IncidentStatus

CHECKPOINT_SCHEMA_VERSION = 1
_CHECKSUM_LENGTH = 64
_INCIDENT_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]+$")


class CheckpointError(RuntimeError):
    """Base error for checkpoint persistence or recovery failures."""


class CheckpointIntegrityError(CheckpointError):
    """Raised when a checkpoint is malformed or its checksum does not match."""


class CheckpointVersionError(CheckpointError):
    """Raised when a checkpoint uses an unsupported schema version."""


class CheckpointNotFoundError(CheckpointError):
    """Raised when a requested incident or checkpoint does not exist."""


class ResumeError(CheckpointError):
    """Raised when a restored snapshot cannot produce a safe resume plan."""


class ResumeIntegrityError(ResumeError):
    """Raised when resume metadata no longer matches the persisted plan."""


class ResumeAction(StrEnum):
    CONTINUE_TRIAGE = "CONTINUE_TRIAGE"
    CONTINUE_PLANNING = "CONTINUE_PLANNING"
    ENTER_EXECUTING = "ENTER_EXECUTING"
    EXECUTE_NEXT_TOOL = "EXECUTE_NEXT_TOOL"
    CONTINUE_VALIDATION = "CONTINUE_VALIDATION"
    CONTINUE_HYPOTHESIS_TESTING = "CONTINUE_HYPOTHESIS_TESTING"
    CONTINUE_POST_ROOT_CAUSE_FLOW = "CONTINUE_POST_ROOT_CAUSE_FLOW"
    TERMINAL = "TERMINAL"


class ResumeMetadata(BaseModel):
    """Typed cursor and idempotency metadata for one checkpoint."""

    model_config = ConfigDict(extra="forbid", strict=True)

    completed_step_ids: list[str] = Field(default_factory=list)
    # This ordered list is convenient for audit output.  The step-indexed map
    # below binds every fingerprint to the plan step that produced it.
    completed_tool_fingerprints: list[str] = Field(default_factory=list)
    completed_step_fingerprints: dict[str, str] = Field(default_factory=dict)
    completed_tool_call_ids: dict[str, str] = Field(default_factory=dict)
    last_completed_step_id: str | None = None
    next_step_index: int = Field(default=0, ge=0)
    replay_step_id: str | None = None
    resume_action: ResumeAction = ResumeAction.EXECUTE_NEXT_TOOL

    @model_validator(mode="after")
    def validate_consistency(self) -> ResumeMetadata:
        if len(self.completed_step_ids) != len(set(self.completed_step_ids)):
            raise ValueError("completed_step_ids must be unique")
        if len(self.completed_tool_fingerprints) != len(
            set(self.completed_tool_fingerprints)
        ):
            raise ValueError("completed_tool_fingerprints must be unique")
        unknown_fingerprint_steps = set(self.completed_step_fingerprints) - set(
            self.completed_step_ids
        )
        if unknown_fingerprint_steps:
            raise ValueError(
                "completed_step_fingerprints reference unknown completed steps: "
                + ", ".join(sorted(unknown_fingerprint_steps))
            )
        unknown_call_id_steps = set(self.completed_tool_call_ids) - set(
            self.completed_step_ids
        )
        if unknown_call_id_steps:
            raise ValueError(
                "completed_tool_call_ids reference unknown completed steps: "
                + ", ".join(sorted(unknown_call_id_steps))
            )
        if self.last_completed_step_id is not None and self.last_completed_step_id not in self.completed_step_ids:
            raise ValueError("last_completed_step_id must be completed")
        if self.replay_step_id is not None and self.replay_step_id not in self.completed_step_ids:
            raise ValueError("replay_step_id must reference a completed step")
        return self


class CheckpointEnvelope(BaseModel):
    """Versioned, checksummed snapshot written by a checkpoint store."""

    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: int = CHECKPOINT_SCHEMA_VERSION
    checkpoint_id: str = Field(min_length=1)
    sequence: int = Field(default=1, ge=1)
    incident_id: str = Field(min_length=1)
    created_at: datetime
    reason: str = Field(min_length=1)
    state: IncidentState
    resume: ResumeMetadata
    integrity_sha256: str = ""

    @classmethod
    def create(
        cls,
        state: IncidentState,
        *,
        reason: str,
        resume: ResumeMetadata,
        sequence: int,
        created_at: datetime | None = None,
    ) -> CheckpointEnvelope:
        incident_id = _incident_id_from_state(state)
        checkpoint_id = deterministic_checkpoint_id(
            incident_id,
            sequence=sequence,
            status=state.status,
            next_step_index=resume.next_step_index,
        )
        envelope = cls(
            schema_version=CHECKPOINT_SCHEMA_VERSION,
            checkpoint_id=checkpoint_id,
            sequence=sequence,
            incident_id=incident_id,
            created_at=created_at or datetime.now(UTC),
            reason=reason,
            state=state.model_copy(deep=True),
            resume=resume.model_copy(deep=True),
        )
        return envelope.with_integrity()

    def with_integrity(self) -> CheckpointEnvelope:
        checksum = _checksum_for_payload(self.model_dump(mode="json", exclude={"integrity_sha256"}))
        return self.model_copy(update={"integrity_sha256": checksum})

    def verify_integrity(self) -> None:
        if self.schema_version != CHECKPOINT_SCHEMA_VERSION:
            raise CheckpointVersionError(
                f"unsupported checkpoint schema version: {self.schema_version}"
            )
        if (
            len(self.integrity_sha256) != _CHECKSUM_LENGTH
            or not re.fullmatch(r"[0-9a-f]{64}", self.integrity_sha256)
        ):
            raise CheckpointIntegrityError("checkpoint checksum is missing or malformed")
        expected = _checksum_for_payload(
            self.model_dump(mode="json", exclude={"integrity_sha256"})
        )
        if not _constant_time_equal(expected, self.integrity_sha256):
            raise CheckpointIntegrityError(
                f"checkpoint checksum mismatch: {self.checkpoint_id}"
            )

    @classmethod
    def from_json(cls, payload: str | bytes | bytearray) -> CheckpointEnvelope:
        try:
            raw = json.loads(payload)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise CheckpointIntegrityError("checkpoint is not valid JSON") from exc
        if not isinstance(raw, Mapping):
            raise CheckpointIntegrityError("checkpoint JSON must contain an object")
        version = raw.get("schema_version")
        if version != CHECKPOINT_SCHEMA_VERSION:
            raise CheckpointVersionError(f"unsupported checkpoint schema version: {version!r}")
        try:
            checkpoint = cls.model_validate(raw, strict=False)
        except ValidationError as exc:
            raise CheckpointIntegrityError("checkpoint payload failed schema validation") from exc
        checkpoint.verify_integrity()
        return checkpoint

    def to_json(self) -> str:
        self.verify_integrity()
        return self.model_dump_json()


class RestoredCheckpoint(BaseModel):
    """Safe restore result returned without executing any runtime action."""

    model_config = ConfigDict(extra="forbid", strict=True)

    checkpoint_id: str = Field(min_length=1)
    incident_id: str = Field(min_length=1)
    state: IncidentState
    resume: ResumeMetadata
    reason: str = Field(min_length=1)
    created_at: datetime


class ResumePlan(BaseModel):
    """Inspectable action returned by resume planning."""

    model_config = ConfigDict(extra="forbid", strict=True)

    action: ResumeAction
    next_step_id: str | None = None
    next_step_index: int = Field(ge=0)
    terminal: bool
    reason: str = Field(min_length=1)


class CheckpointStore(Protocol):
    """Replaceable persistence boundary for checkpoint envelopes."""

    def save(self, checkpoint: CheckpointEnvelope) -> CheckpointEnvelope: ...

    def load(self, checkpoint_id: str) -> CheckpointEnvelope: ...

    def list(self, incident_id: str) -> tuple[CheckpointEnvelope, ...]: ...

    def load_latest(self, incident_id: str) -> CheckpointEnvelope: ...

    def load_latest_valid(self, incident_id: str) -> CheckpointEnvelope: ...

    def next_sequence(self, incident_id: str) -> int: ...


class FileCheckpointStore:
    """Atomic JSON-file checkpoint store with incident isolation."""

    def __init__(self, root_dir: str | Path) -> None:
        self.root_dir = Path(root_dir)

    def save(self, checkpoint: CheckpointEnvelope) -> CheckpointEnvelope:
        checkpoint.verify_integrity()
        incident_dir = self._incident_dir(checkpoint.incident_id)
        incident_dir.mkdir(parents=True, exist_ok=True)
        final_path = incident_dir / self._filename(checkpoint)
        payload = checkpoint.to_json().encode("utf-8")
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=incident_dir,
                prefix=f".{final_path.name}.",
                suffix=".tmp",
                delete=False,
            ) as temporary:
                temporary_path = Path(temporary.name)
                temporary.write(payload)
                temporary.flush()
                os.fsync(temporary.fileno())
            os.replace(temporary_path, final_path)
            temporary_path = None
            _fsync_directory(incident_dir)
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
        return checkpoint

    def load(self, checkpoint_id: str) -> CheckpointEnvelope:
        matches = [
            path
            for path in self.root_dir.rglob("*.json")
            if checkpoint_id in path.stem
        ] if self.root_dir.exists() else []
        if not matches:
            raise CheckpointNotFoundError(f"checkpoint not found: {checkpoint_id}")
        if len(matches) > 1:
            raise CheckpointIntegrityError(f"checkpoint id is not unique: {checkpoint_id}")
        return self._read(matches[0])

    def list(self, incident_id: str) -> tuple[CheckpointEnvelope, ...]:
        paths = sorted(self._incident_dir(incident_id).glob("*.json"), key=self._path_sort_key)
        checkpoints = tuple(self._read(path) for path in paths)
        return tuple(sorted(checkpoints, key=lambda item: (item.sequence, item.created_at, item.checkpoint_id)))

    def next_sequence(self, incident_id: str) -> int:
        """Return the next filename sequence without parsing checkpoint JSON."""

        paths = self._incident_dir(incident_id).glob("*.json")
        max_sequence = max(
            (self._sequence_from_filename(path) for path in paths),
            default=0,
        )
        return max_sequence + 1

    def load_latest(self, incident_id: str) -> CheckpointEnvelope:
        return self.load_latest_valid(incident_id)

    def load_latest_valid(self, incident_id: str) -> CheckpointEnvelope:
        paths = sorted(self._incident_dir(incident_id).glob("*.json"), key=self._path_sort_key, reverse=True)
        if not paths:
            raise CheckpointNotFoundError(f"no checkpoint exists for incident: {incident_id}")
        errors: list[CheckpointError] = []
        for path in paths:
            try:
                return self._read(path)
            except CheckpointVersionError:
                raise
            except CheckpointIntegrityError as exc:
                errors.append(exc)
        raise errors[-1] if errors else CheckpointNotFoundError(
            f"no valid checkpoint exists for incident: {incident_id}"
        )

    def _read(self, path: Path) -> CheckpointEnvelope:
        try:
            payload = path.read_bytes()
        except OSError as exc:
            raise CheckpointIntegrityError(f"could not read checkpoint: {path}") from exc
        try:
            return CheckpointEnvelope.from_json(payload)
        except CheckpointError:
            raise
        except Exception as exc:
            raise CheckpointIntegrityError(f"could not parse checkpoint: {path}") from exc

    def _incident_dir(self, incident_id: str) -> Path:
        if not isinstance(incident_id, str) or not incident_id.strip():
            raise CheckpointNotFoundError("incident_id must be a non-empty string")
        directory_name = (
            incident_id
            if _INCIDENT_ID_PATTERN.fullmatch(incident_id)
            else "incident-" + hashlib.sha256(incident_id.encode("utf-8")).hexdigest()[:24]
        )
        return self.root_dir / directory_name

    @staticmethod
    def _filename(checkpoint: CheckpointEnvelope) -> str:
        return f"{checkpoint.sequence:06d}-{checkpoint.state.status.value}-{checkpoint.checkpoint_id}.json"

    @staticmethod
    def _path_sort_key(path: Path) -> tuple[int, str]:
        return (FileCheckpointStore._sequence_from_filename(path), path.name)

    @staticmethod
    def _sequence_from_filename(path: Path) -> int:
        match = re.match(r"^(\d+)-", path.name)
        return int(match.group(1)) if match else -1


class CheckpointManager:
    """Create, persist, validate, and restore checkpoint envelopes."""

    def __init__(self, store: CheckpointStore) -> None:
        self.store = store

    def save(
        self,
        state: IncidentState,
        *,
        reason: str,
        resume: ResumeMetadata | None = None,
    ) -> CheckpointEnvelope:
        metadata = resume or ResumeMetadata(
            resume_action=resume_action_for_status(
                state.status,
                plan_persisted=bool(state.plan),
            )
        )
        incident_id = _incident_id_from_state(state)
        sequence = self.store.next_sequence(incident_id)
        checkpoint = CheckpointEnvelope.create(
            state,
            reason=reason,
            resume=metadata,
            sequence=sequence,
        )
        return self.store.save(checkpoint)

    checkpoint = save

    def restore_latest(self, incident_id: str) -> RestoredCheckpoint:
        return self.restore(self.store.load_latest_valid(incident_id))

    def restore(self, checkpoint: CheckpointEnvelope) -> RestoredCheckpoint:
        checkpoint.verify_integrity()
        validate_resume_metadata(checkpoint.state, checkpoint.resume)
        return RestoredCheckpoint(
            checkpoint_id=checkpoint.checkpoint_id,
            incident_id=checkpoint.incident_id,
            state=checkpoint.state.model_copy(deep=True),
            resume=checkpoint.resume.model_copy(deep=True),
            reason=checkpoint.reason,
            created_at=checkpoint.created_at,
        )

    @staticmethod
    def resume_plan(state: IncidentState, resume: ResumeMetadata) -> ResumePlan:
        validate_resume_metadata(state, resume)
        return build_resume_plan(state, resume)


def deterministic_checkpoint_id(
    incident_id: str,
    *,
    sequence: int,
    status: IncidentStatus,
    next_step_index: int,
) -> str:
    material = {
        "incident_id": incident_id,
        "sequence": sequence,
        "status": status.value,
        "next_step_index": next_step_index,
    }
    digest = hashlib.sha256(
        json.dumps(material, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:24]
    return f"cp-{digest}"


def deterministic_tool_call_id(
    incident_id: str,
    step_id: str,
    fingerprint: str,
) -> str:
    material = {
        "incident_id": incident_id,
        "step_id": step_id,
        "fingerprint": fingerprint,
    }
    digest = hashlib.sha256(
        json.dumps(material, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:24]
    return f"tc-{digest}"


def validate_resume_metadata(state: IncidentState, resume: ResumeMetadata) -> None:
    """Fail closed if the resume cursor no longer matches the persisted plan."""

    plan_steps: list[InvestigationStep] = []
    for raw_step in state.plan:
        try:
            plan_steps.append(InvestigationStep.model_validate(raw_step))
        except (TypeError, ValueError) as exc:
            raise ResumeIntegrityError("checkpoint plan contains an invalid step") from exc
    by_id = {step.step_id: step for step in plan_steps}
    if len(by_id) != len(plan_steps):
        raise ResumeIntegrityError("checkpoint plan contains duplicate step ids")
    for step_id in resume.completed_step_ids:
        step = by_id.get(step_id)
        if step is None:
            raise ResumeIntegrityError(f"completed step is absent from plan: {step_id}")
        expected_fingerprint = fingerprint_step(step)
        actual_fingerprint = resume.completed_step_fingerprints.get(step_id)
        if actual_fingerprint != expected_fingerprint:
            raise ResumeIntegrityError(
                f"completed step fingerprint does not match plan: {step_id}"
            )
        if expected_fingerprint not in resume.completed_tool_fingerprints:
            raise ResumeIntegrityError(
                f"completed fingerprint list is missing step: {step_id}"
            )
        expected_call_id = deterministic_tool_call_id(
            state.alert.get("incident_id", "unknown"),
            step_id,
            expected_fingerprint,
        )
        actual_call_id = resume.completed_tool_call_ids.get(step_id)
        if actual_call_id != expected_call_id:
            raise ResumeIntegrityError(
                f"completed tool call id does not match plan: {step_id}"
            )
    if resume.next_step_index > len(plan_steps):
        raise ResumeIntegrityError("next_step_index is outside the persisted plan")
    if resume.replay_step_id is not None and resume.replay_step_id not in by_id:
        raise ResumeIntegrityError("replay_step_id is absent from the persisted plan")
    if state.status.is_terminal and resume.resume_action is not ResumeAction.TERMINAL:
        raise ResumeIntegrityError("terminal checkpoint must have TERMINAL resume action")


def build_resume_plan(state: IncidentState, resume: ResumeMetadata) -> ResumePlan:
    if state.status.is_terminal:
        return ResumePlan(
            action=ResumeAction.TERMINAL,
            next_step_index=resume.next_step_index,
            terminal=True,
            reason=f"incident is already terminal: {state.status.value}",
        )
    if state.status is IncidentStatus.EXECUTING:
        for index, raw_step in enumerate(state.plan):
            step_id = _step_id(raw_step)
            if step_id not in resume.completed_step_ids:
                return ResumePlan(
                    action=ResumeAction.EXECUTE_NEXT_TOOL,
                    next_step_id=step_id,
                    next_step_index=index,
                    terminal=False,
                    reason="next pending tool step",
                )
        if resume.replay_step_id is not None:
            index = _step_index(state.plan, resume.replay_step_id)
            return ResumePlan(
                action=ResumeAction.EXECUTE_NEXT_TOOL,
                next_step_id=resume.replay_step_id,
                next_step_index=index,
                terminal=False,
                reason="explicit retry replay is pending",
            )
        return ResumePlan(
            action=ResumeAction.CONTINUE_VALIDATION,
            next_step_index=len(state.plan),
            terminal=False,
            reason="all planned tools are complete",
        )
    if state.status in {IncidentStatus.RECEIVED, IncidentStatus.TRIAGE}:
        action = ResumeAction.CONTINUE_TRIAGE
    elif state.status is IncidentStatus.PLANNING:
        action = (
            ResumeAction.ENTER_EXECUTING
            if state.plan
            else ResumeAction.CONTINUE_PLANNING
        )
    elif state.status is IncidentStatus.VALIDATING:
        action = ResumeAction.CONTINUE_VALIDATION
    elif state.status is IncidentStatus.HYPOTHESIS_TESTING:
        action = ResumeAction.CONTINUE_HYPOTHESIS_TESTING
    else:
        action = ResumeAction.CONTINUE_POST_ROOT_CAUSE_FLOW
    return ResumePlan(
        action=action,
        next_step_index=resume.next_step_index,
        terminal=False,
        reason=f"resume from {state.status.value}",
    )


def resume_action_for_status(
    status: IncidentStatus,
    *,
    plan_persisted: bool = False,
) -> ResumeAction:
    if status.is_terminal:
        return ResumeAction.TERMINAL
    if status in {IncidentStatus.RECEIVED, IncidentStatus.TRIAGE}:
        return ResumeAction.CONTINUE_TRIAGE
    if status is IncidentStatus.PLANNING:
        return (
            ResumeAction.ENTER_EXECUTING
            if plan_persisted
            else ResumeAction.CONTINUE_PLANNING
        )
    if status is IncidentStatus.EXECUTING:
        return ResumeAction.EXECUTE_NEXT_TOOL
    if status is IncidentStatus.VALIDATING:
        return ResumeAction.CONTINUE_VALIDATION
    if status is IncidentStatus.HYPOTHESIS_TESTING:
        return ResumeAction.CONTINUE_HYPOTHESIS_TESTING
    return ResumeAction.CONTINUE_POST_ROOT_CAUSE_FLOW


def _incident_id_from_state(state: IncidentState) -> str:
    incident_id = state.alert.get("incident_id")
    if not isinstance(incident_id, str) or not incident_id.strip():
        raise CheckpointError("IncidentState.alert.incident_id is required for checkpointing")
    return incident_id


def _checksum_for_payload(payload: Mapping[str, JsonValue]) -> str:
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _constant_time_equal(left: str, right: str) -> bool:
    return hashlib.sha256(left.encode()).digest() == hashlib.sha256(right.encode()).digest()


def _fsync_directory(directory: Path) -> None:
    if os.name == "nt":
        return
    try:
        descriptor = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _step_id(raw_step: Mapping[str, JsonValue]) -> str:
    value = raw_step.get("step_id")
    if not isinstance(value, str) or not value:
        raise ResumeIntegrityError("plan step is missing a valid step_id")
    return value


def _step_index(plan: Sequence[Mapping[str, JsonValue]], step_id: str) -> int:
    for index, raw_step in enumerate(plan):
        if _step_id(raw_step) == step_id:
            return index
    raise ResumeIntegrityError(f"step is absent from plan: {step_id}")


__all__ = [
    "CHECKPOINT_SCHEMA_VERSION",
    "CheckpointEnvelope",
    "CheckpointError",
    "CheckpointIntegrityError",
    "CheckpointManager",
    "CheckpointNotFoundError",
    "CheckpointStore",
    "CheckpointVersionError",
    "FileCheckpointStore",
    "RestoredCheckpoint",
    "ResumeAction",
    "ResumeError",
    "ResumeIntegrityError",
    "ResumeMetadata",
    "ResumePlan",
    "build_resume_plan",
    "deterministic_checkpoint_id",
    "deterministic_tool_call_id",
    "resume_action_for_status",
    "validate_resume_metadata",
]
