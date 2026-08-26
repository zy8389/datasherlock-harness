"""Deterministic construction of approval-gated repair proposals."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from config.faults import DEFAULT_FAULT_CATALOG_PATH, load_fault_catalog
from harness.repair import RepairAction, RepairEvidence, RepairProposal, RepairRisk
from harness.state import IncidentState, IncidentStatus


class RepairProposalBuildError(ValueError):
    """Raised when confirmed diagnostic state cannot safely produce a repair."""


class MissingPartitionContext(BaseModel):
    """Trusted diagnostic details required for the F01 repair handler."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    partition_value: str = Field(min_length=1)


class MissingPartitionRootCause(BaseModel):
    """The portion of an F01 conclusion that is sufficient to plan a repair."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    root_cause_type: Literal["missing_partition"]
    confidence: float = Field(ge=0, le=1)
    affected_assets: tuple[str, ...] = Field(min_length=1)
    repair_context: MissingPartitionContext


class RepairProposalBuilder:
    """Build fixed repairs from persisted, confirmed diagnostic evidence."""

    def __init__(
        self,
        *,
        proposal_ttl: timedelta = timedelta(hours=1),
        now: Callable[[], datetime] | None = None,
        fault_catalog_path: str = str(DEFAULT_FAULT_CATALOG_PATH),
    ) -> None:
        if proposal_ttl <= timedelta(0):
            raise ValueError("proposal_ttl must be positive")
        self._proposal_ttl = proposal_ttl
        self._now = now or (lambda: datetime.now(UTC))
        catalog = load_fault_catalog(fault_catalog_path)
        self._faults = {fault.root_cause_type: fault for fault in catalog.faults}

    def build(self, state: IncidentState) -> RepairProposal:
        """Return the only allowed proposal for a supported confirmed root cause."""

        if state.status is not IncidentStatus.ROOT_CAUSE_FOUND:
            raise RepairProposalBuildError(
                "repair proposals require ROOT_CAUSE_FOUND incident status"
            )
        incident_id = state.alert.get("incident_id")
        if not isinstance(incident_id, str) or not incident_id.strip():
            raise RepairProposalBuildError("incident alert must contain incident_id")
        if state.root_cause is None:
            raise RepairProposalBuildError("incident has no confirmed root cause")

        root_cause_type = state.root_cause.get("root_cause_type")
        if root_cause_type != "missing_partition":
            raise RepairProposalBuildError(
                f"no deterministic repair proposal is implemented for {root_cause_type!r}"
            )
        try:
            root_cause = MissingPartitionRootCause.model_validate(state.root_cause)
            evidence = tuple(
                RepairEvidence.model_validate(item) for item in state.evidence
            )
        except ValidationError as exc:
            raise RepairProposalBuildError(
                "confirmed root cause or evidence does not meet the repair contract"
            ) from exc
        if len(evidence) < 2 or len({item.source_type for item in evidence}) < 2:
            raise RepairProposalBuildError(
                "confirmed evidence must contain two independent sources"
            )

        catalog_fault = self._faults[root_cause.root_cause_type]
        if set(root_cause.affected_assets) != set(catalog_fault.affected_assets):
            raise RepairProposalBuildError(
                "root cause affected_assets does not match the fault catalog"
            )
        now = self._normalized_now()
        try:
            return RepairProposal(
                proposal_id=f"RP-{uuid4()}",
                incident_id=incident_id,
                root_cause_type=root_cause.root_cause_type,
                root_cause_confidence=root_cause.confidence,
                evidence=evidence,
                affected_assets=root_cause.affected_assets,
                action=RepairAction.RERUN_PARTITION,
                parameters={
                    "table": "events",
                    "source_table": "events",
                    "partition_column": "device_type",
                    "partition_value": root_cause.repair_context.partition_value,
                },
                risk=RepairRisk.MEDIUM,
                rationale=(
                    "Restore the confirmed missing events partition from the "
                    "configured trusted repair source in an isolated sandbox."
                ),
                created_at=now,
                valid_until=now + self._proposal_ttl,
            )
        except ValidationError as exc:
            raise RepairProposalBuildError("generated repair proposal is invalid") from exc

    def _normalized_now(self) -> datetime:
        value = self._now()
        if value.tzinfo is None or value.utcoffset() is None:
            raise RepairProposalBuildError("clock must return a timezone-aware datetime")
        return value.astimezone(UTC)


__all__ = [
    "MissingPartitionContext",
    "MissingPartitionRootCause",
    "RepairProposalBuildError",
    "RepairProposalBuilder",
]
