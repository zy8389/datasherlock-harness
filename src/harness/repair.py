"""Typed, approval-gated contracts for sandbox-only repair work."""

from __future__ import annotations

import hashlib
import hmac
import json
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


def _require_aware_utc(value: datetime, *, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(UTC)


class RepairAction(StrEnum):
    """The complete allowlist of repair handlers that a sandbox may invoke."""

    RERUN_PARTITION = "rerun_partition"
    DEDUPLICATE_BATCH = "deduplicate_batch"
    RELOAD_SOURCE_DATA = "reload_source_data"
    RESTORE_METRIC_DEFINITION = "restore_metric_definition"
    UPDATE_TIMEZONE_CONFIGURATION = "update_timezone_configuration"
    NORMALIZE_DURATION_UNIT = "normalize_duration_unit"
    UPDATE_FIELD_MAPPING = "update_field_mapping"
    APPLY_SCHEMA_COMPATIBILITY = "apply_schema_compatibility"
    REBALANCE_EXPERIMENT_ALLOCATION = "rebalance_experiment_allocation"


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
    """One traceable evidence item supporting a repair proposal."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    evidence_id: str = Field(min_length=1)
    source_type: EvidenceSourceType
    asset: str = Field(min_length=1)
    finding: str = Field(min_length=1)


class RepairProposal(BaseModel):
    """A deterministic repair request that must be approved before execution.

    The content hash is generated from all material proposal fields. It is the
    value an approval decision and sandbox run bind to, preventing a reviewer
    from approving one action while a later-mutated proposal is executed.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    proposal_id: str = Field(min_length=1)
    incident_id: str = Field(min_length=1)
    root_cause_type: str = Field(min_length=1)
    root_cause_confidence: float = Field(ge=0, le=1)
    evidence: tuple[RepairEvidence, ...] = Field(min_length=2)
    affected_assets: tuple[str, ...] = Field(min_length=1)
    action: RepairAction
    parameters: dict[str, JsonValue] = Field(default_factory=dict)
    risk: RepairRisk
    rationale: str = Field(min_length=1)
    created_at: datetime = Field(default_factory=_utc_now)
    valid_until: datetime
    proposal_hash: str = Field(default="", repr=False)

    @field_validator("affected_assets")
    @classmethod
    def validate_affected_assets(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not asset.strip() for asset in value):
            raise ValueError("affected_assets must not contain blanks")
        if len(value) != len(set(value)):
            raise ValueError("affected_assets must be unique")
        return value

    @field_validator("created_at", "valid_until")
    @classmethod
    def normalize_timestamps(cls, value: datetime, info) -> datetime:
        return _require_aware_utc(value, field_name=info.field_name)

    @model_validator(mode="after")
    def validate_and_hash(self) -> Self:
        evidence_ids = [item.evidence_id for item in self.evidence]
        if len(evidence_ids) != len(set(evidence_ids)):
            raise ValueError("repair evidence ids must be unique")
        if len({item.source_type for item in self.evidence}) < 2:
            raise ValueError("repair proposal requires two independent evidence sources")
        unknown_assets = {item.asset for item in self.evidence}.difference(
            self.affected_assets
        )
        if unknown_assets:
            raise ValueError(
                "repair evidence assets must be affected assets: "
                + ", ".join(sorted(unknown_assets))
            )
        if self.valid_until <= self.created_at:
            raise ValueError("valid_until must be later than created_at")

        expected_hash = self.content_hash()
        if self.proposal_hash and not hmac.compare_digest(
            self.proposal_hash, expected_hash
        ):
            raise ValueError("proposal_hash does not match proposal content")
        object.__setattr__(self, "proposal_hash", expected_hash)
        return self

    def content_hash(self) -> str:
        """Return a stable SHA-256 digest of approval-relevant proposal content."""

        payload = self.model_dump(mode="json", exclude={"proposal_hash"})
        canonical_json = json.dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


class ApprovalDecision(BaseModel):
    """A reviewer's immutable decision for exactly one proposal digest."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    decision_id: str = Field(min_length=1)
    incident_id: str = Field(min_length=1)
    proposal_id: str = Field(min_length=1)
    proposal_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    outcome: ApprovalOutcome
    reviewer: str = Field(min_length=1)
    comment: str | None = None
    decided_at: datetime = Field(default_factory=_utc_now)

    @field_validator("decided_at")
    @classmethod
    def normalize_decided_at(cls, value: datetime) -> datetime:
        return _require_aware_utc(value, field_name="decided_at")

    @model_validator(mode="after")
    def require_rejection_reason(self) -> Self:
        if self.outcome is ApprovalOutcome.REJECTED and not self.comment:
            raise ValueError("a rejected proposal requires a comment")
        return self

    @classmethod
    def for_proposal(
        cls,
        proposal: RepairProposal,
        *,
        decision_id: str,
        outcome: ApprovalOutcome,
        reviewer: str,
        comment: str | None = None,
        decided_at: datetime | None = None,
    ) -> Self:
        timestamp = decided_at or _utc_now()
        normalized_timestamp = _require_aware_utc(timestamp, field_name="decided_at")
        if normalized_timestamp > proposal.valid_until:
            raise ValueError("cannot decide on an expired repair proposal")
        return cls(
            decision_id=decision_id,
            incident_id=proposal.incident_id,
            proposal_id=proposal.proposal_id,
            proposal_hash=proposal.proposal_hash,
            outcome=outcome,
            reviewer=reviewer,
            comment=comment,
            decided_at=normalized_timestamp,
        )


class SandboxRun(BaseModel):
    """The isolated execution record created from an approved proposal."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    run_id: str = Field(min_length=1)
    incident_id: str = Field(min_length=1)
    proposal_id: str = Field(min_length=1)
    proposal_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    approval_decision_id: str = Field(min_length=1)
    action: RepairAction
    sandbox_path: str = Field(min_length=1)
    status: SandboxRunStatus = SandboxRunStatus.PENDING
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error: str | None = None

    @field_validator("started_at", "finished_at")
    @classmethod
    def normalize_optional_timestamps(cls, value: datetime | None, info) -> datetime | None:
        if value is None:
            return None
        return _require_aware_utc(value, field_name=info.field_name)

    @model_validator(mode="after")
    def validate_run_timing(self) -> Self:
        if self.finished_at is not None and self.started_at is None:
            raise ValueError("finished sandbox runs require started_at")
        if self.started_at and self.finished_at and self.finished_at < self.started_at:
            raise ValueError("finished_at cannot precede started_at")
        if self.status is SandboxRunStatus.FAILED and not self.error:
            raise ValueError("failed sandbox runs require an error")
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
            raise ValueError("approval decision does not bind to the repair proposal")
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
    """The outcome of validating a completed sandbox repair."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

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

    @field_validator("regressions")
    @classmethod
    def validate_regressions(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not regression.strip() for regression in value):
            raise ValueError("regressions must not contain blanks")
        if len(value) != len(set(value)):
            raise ValueError("regressions must be unique")
        return value

    @field_validator("validated_at")
    @classmethod
    def normalize_validated_at(cls, value: datetime) -> datetime:
        return _require_aware_utc(value, field_name="validated_at")

    @model_validator(mode="after")
    def validate_outcome(self) -> Self:
        passed = self.target_met and not self.regressions
        if (self.status is PostValidationStatus.PASSED) != passed:
            raise ValueError(
                "post-validation passes only when the target is met without regressions"
            )
        return self


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
]
