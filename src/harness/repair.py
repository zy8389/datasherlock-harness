"""Immutable contracts for approval-gated, sandbox-only repairs.

The repair layer deliberately describes artifacts and their bindings.  It does
not own incident transitions; those remain the responsibility of
``HarnessGraph``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
from datetime import UTC, datetime
from enum import StrEnum
from typing import Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    field_validator,
    model_validator,
)

from config.faults import EvidenceSourceType


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _aware_utc(value: datetime, *, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


class RepairAction(StrEnum):
    """The currently implemented deterministic repair handler."""

    RERUN_PARTITION = "rerun_partition"


class RepairRisk(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ApprovalOutcome(StrEnum):
    APPROVED = "approved"
    REJECTED = "rejected"


class SandboxRunStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class PostValidationStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"


class RepairEvidence(BaseModel):
    """One supporting evidence item copied from the live runtime state."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
    )

    evidence_id: str = Field(min_length=1)
    source_type: EvidenceSourceType
    asset: str = Field(min_length=1)
    finding: str = Field(min_length=1)
    observation: dict[str, JsonValue] = Field(default_factory=dict)


class RepairProposal(BaseModel):
    """An immutable, hash-bound request for one known repair handler."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
    )

    proposal_id: str = Field(min_length=1)
    incident_id: str = Field(min_length=1)
    root_cause_type: str = Field(min_length=1)
    root_cause_confidence: float = Field(ge=0, le=1)
    evidence: tuple[RepairEvidence, ...] = Field(min_length=2)
    supporting_evidence_ids: tuple[str, ...] = Field(default_factory=tuple)
    independent_source_types: tuple[EvidenceSourceType, ...] = Field(default_factory=tuple)
    affected_assets: tuple[str, ...] = Field(min_length=1)
    action: RepairAction
    parameters: dict[str, JsonValue]
    risk: RepairRisk
    rationale: str = Field(min_length=1)
    created_at: datetime = Field(default_factory=_utc_now)
    valid_until: datetime
    proposal_hash: str = Field(default="", repr=False)

    @field_validator("created_at", "valid_until")
    @classmethod
    def normalize_timestamps(cls, value: datetime, info: object) -> datetime:
        name = getattr(info, "field_name", "timestamp")
        return _aware_utc(value, name=name)

    @field_validator("supporting_evidence_ids")
    @classmethod
    def unique_evidence_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not item.strip() for item in value):
            raise ValueError("supporting_evidence_ids must not contain blanks")
        if len(value) != len(set(value)):
            raise ValueError("supporting_evidence_ids must be unique")
        return value

    @field_validator("independent_source_types")
    @classmethod
    def unique_source_types(
        cls, value: tuple[EvidenceSourceType, ...]
    ) -> tuple[EvidenceSourceType, ...]:
        if len(value) != len(set(value)):
            raise ValueError("independent_source_types must be unique")
        return value

    @field_validator("affected_assets")
    @classmethod
    def unique_assets(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not item.strip() for item in value):
            raise ValueError("affected_assets must not contain blanks")
        if len(value) != len(set(value)):
            raise ValueError("affected_assets must be unique")
        return value

    @model_validator(mode="after")
    def validate_contract_and_hash(self) -> Self:
        evidence_ids = tuple(item.evidence_id for item in self.evidence)
        evidence_sources = tuple(item.source_type for item in self.evidence)
        if len(evidence_ids) != len(set(evidence_ids)):
            raise ValueError("repair evidence ids must be unique")
        if len(set(evidence_sources)) < 2:
            raise ValueError("repair proposal requires two independent evidence sources")
        if not self.supporting_evidence_ids:
            object.__setattr__(self, "supporting_evidence_ids", evidence_ids)
        if set(self.supporting_evidence_ids) != set(evidence_ids):
            raise ValueError("supporting_evidence_ids must match proposal evidence")
        expected_sources = tuple(dict.fromkeys(evidence_sources))
        if not self.independent_source_types:
            object.__setattr__(self, "independent_source_types", expected_sources)
        if set(self.independent_source_types) != set(expected_sources):
            raise ValueError("independent_source_types must match proposal evidence")
        if any(item.asset not in self.affected_assets for item in self.evidence):
            raise ValueError("repair evidence assets must be affected assets")
        if self.valid_until <= self.created_at:
            raise ValueError("valid_until must be later than created_at")
        if self.action is not RepairAction.RERUN_PARTITION:
            raise ValueError("repair action has no available deterministic handler")
        required_parameters = {
            "table",
            "source_table",
            "partition_column",
            "partition_value",
        }
        if set(self.parameters) != required_parameters:
            raise ValueError(
                "rerun_partition parameters must be exactly: "
                + ", ".join(sorted(required_parameters))
            )
        expected_hash = self.content_hash()
        if self.proposal_hash and not hmac.compare_digest(
            self.proposal_hash, expected_hash
        ):
            raise ValueError("proposal_hash does not match proposal content")
        object.__setattr__(self, "proposal_hash", expected_hash)
        return self

    def content_hash(self) -> str:
        """Hash every approval-relevant field except ``proposal_hash``."""

        payload = self.model_dump(mode="json", exclude={"proposal_hash"})
        canonical = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class ApprovalDecision(BaseModel):
    """An immutable reviewer decision bound to one proposal digest."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
    )

    decision_id: str = Field(min_length=1)
    incident_id: str = Field(min_length=1)
    proposal_id: str = Field(min_length=1)
    proposal_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    reviewer: str = Field(min_length=1)
    outcome: ApprovalOutcome
    comment: str | None = None
    decided_at: datetime = Field(default_factory=_utc_now)

    @field_validator("decided_at")
    @classmethod
    def normalize_decided_at(cls, value: datetime) -> datetime:
        return _aware_utc(value, name="decided_at")

    @model_validator(mode="after")
    def require_rejection_comment(self) -> Self:
        if self.outcome is ApprovalOutcome.REJECTED and not self.comment:
            raise ValueError("rejected approval requires a non-empty comment")
        return self

    @classmethod
    def for_proposal(
        cls,
        proposal: RepairProposal,
        *,
        decision_id: str,
        reviewer: str,
        outcome: ApprovalOutcome,
        comment: str | None = None,
        decided_at: datetime | None = None,
    ) -> Self:
        timestamp = _aware_utc(decided_at or _utc_now(), name="decided_at")
        if timestamp < proposal.created_at or timestamp > proposal.valid_until:
            raise ValueError("cannot decide outside the repair proposal validity window")
        return cls(
            decision_id=decision_id,
            incident_id=proposal.incident_id,
            proposal_id=proposal.proposal_id,
            proposal_hash=proposal.proposal_hash,
            reviewer=reviewer,
            outcome=outcome,
            comment=comment,
            decided_at=timestamp,
        )


class SandboxRun(BaseModel):
    """Checkpoint-safe execution and audit record for one repair attempt."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
    )

    run_id: str = Field(min_length=1)
    incident_id: str = Field(min_length=1)
    proposal_id: str = Field(min_length=1)
    proposal_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    approval_decision_id: str = Field(min_length=1)
    action: RepairAction
    sandbox_path: str = Field(min_length=1)
    status: SandboxRunStatus = SandboxRunStatus.PENDING
    source_hash_before: str | None = None
    source_hash_after: str | None = None
    sandbox_hash_before: str | None = None
    sandbox_hash_after: str | None = None
    handler_invocation_count: int = Field(default=0, ge=0)
    changed_row_counts: dict[str, int] = Field(default_factory=dict)
    operation_details: dict[str, JsonValue] = Field(default_factory=dict)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error: str | None = None

    @field_validator("started_at", "finished_at")
    @classmethod
    def normalize_optional_timestamps(cls, value: datetime | None, info: object) -> datetime | None:
        if value is None:
            return None
        return _aware_utc(value, name=getattr(info, "field_name", "timestamp"))

    @model_validator(mode="after")
    def validate_timing(self) -> Self:
        if self.finished_at is not None and self.started_at is None:
            raise ValueError("finished sandbox runs require started_at")
        if self.started_at and self.finished_at and self.finished_at < self.started_at:
            raise ValueError("finished_at cannot precede started_at")
        if self.status in {
            SandboxRunStatus.FAILED,
            SandboxRunStatus.CANCELLED,
        } and not self.error:
            raise ValueError("failed sandbox runs require an error")
        if self.status in {
            SandboxRunStatus.FAILED,
            SandboxRunStatus.CANCELLED,
            SandboxRunStatus.SUCCEEDED,
        } and self.finished_at is None:
            raise ValueError("terminal sandbox runs require finished_at")
        if self.status is SandboxRunStatus.SUCCEEDED and self.handler_invocation_count != 1:
            raise ValueError("successful sandbox runs require one handler invocation")
        return self

    @classmethod
    def for_approved_proposal(
        cls,
        proposal: RepairProposal,
        decision: ApprovalDecision,
        *,
        run_id: str,
        sandbox_path: str,
    ) -> Self:
        if decision.outcome is not ApprovalOutcome.APPROVED:
            raise ValueError("only approved proposals may create sandbox runs")
        if (
            decision.incident_id != proposal.incident_id
            or decision.proposal_id != proposal.proposal_id
            or not hmac.compare_digest(decision.proposal_hash, proposal.proposal_hash)
        ):
            raise ValueError("approval decision does not bind to repair proposal")
        return cls(
            run_id=run_id,
            incident_id=proposal.incident_id,
            proposal_id=proposal.proposal_id,
            proposal_hash=proposal.proposal_hash,
            approval_decision_id=decision.decision_id,
            action=proposal.action,
            sandbox_path=sandbox_path,
        )


class PostValidationResult(BaseModel):
    """Typed outcome of validating a completed sandbox repair."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
    )

    validation_id: str = Field(min_length=1)
    incident_id: str = Field(min_length=1)
    sandbox_run_id: str = Field(min_length=1)
    proposal_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    metric_id: str = Field(min_length=1)
    observed_before: float
    observed_after: float
    target_met: bool
    regressions: tuple[str, ...] = Field(default_factory=tuple)
    status: PostValidationStatus
    summary: str = Field(min_length=1)
    validated_at: datetime = Field(default_factory=_utc_now)

    @field_validator("observed_before", "observed_after")
    @classmethod
    def require_finite_values(cls, value: float) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError("observed metric values must be finite numbers")
        return float(value)

    @field_validator("regressions")
    @classmethod
    def validate_regressions(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not item.strip() for item in value):
            raise ValueError("regressions must not contain blanks")
        if len(value) != len(set(value)):
            raise ValueError("regressions must be unique")
        return value

    @field_validator("validated_at")
    @classmethod
    def normalize_validated_at(cls, value: datetime) -> datetime:
        return _aware_utc(value, name="validated_at")

    @model_validator(mode="after")
    def validate_outcome(self) -> Self:
        expected_pass = self.target_met and not self.regressions
        if (self.status is PostValidationStatus.PASSED) != expected_pass:
            raise ValueError(
                "post-validation passes only when the target is met without regressions"
            )
        return self


def proposal_is_intact(proposal: RepairProposal) -> bool:
    """Validate a potentially ``model_copy(update=...)``-tampered proposal."""

    return hmac.compare_digest(proposal.content_hash(), proposal.proposal_hash)


__all__ = [
    "ApprovalDecision",
    "ApprovalOutcome",
    "PostValidationResult",
    "PostValidationStatus",
    "RepairAction",
    "RepairEvidence",
    "RepairProposal",
    "RepairRisk",
    "SandboxRun",
    "SandboxRunStatus",
    "proposal_is_intact",
]
