"""Build deterministic repair proposals from confirmed runtime evidence."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from harness.hypothesis import EvidenceReference, HypothesisManager
from harness.repair import (
    RepairAction,
    RepairEvidence,
    RepairProposal,
    RepairRisk,
)
from harness.state import IncidentState, IncidentStatus


class RepairProposalBuildError(ValueError):
    """Raised when the live diagnostic state cannot prove a safe repair scope."""


class MissingPartitionContext(BaseModel):
    """The only repair scope accepted by the F01 handler."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    partition_value: str = Field(min_length=1)


class MissingPartitionRootCause(BaseModel):
    """Validated fields copied from the authoritative root-cause result."""

    # IncidentState contains additional authoritative validator fields; this
    # helper extracts only the fields needed by the repair builder.
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    root_cause_type: str = Field(pattern=r"^missing_partition$")
    confidence: float = Field(ge=0, le=1)
    supporting_evidence_ids: tuple[str, ...] = Field(min_length=2)
    independent_source_types: tuple[str, ...] = Field(min_length=2)


_PARTITION_VALUE = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2})/(?P<device>[A-Za-z0-9][A-Za-z0-9_.-]*)$"
)


class RepairProposalBuilder:
    """Construct the single currently executable proposal, F01 ``rerun_partition``."""

    def __init__(
        self,
        *,
        proposal_ttl: timedelta = timedelta(hours=1),
        now: Callable[[], datetime] | None = None,
        hypothesis_manager: HypothesisManager | None = None,
    ) -> None:
        if proposal_ttl <= timedelta(0):
            raise ValueError("proposal_ttl must be positive")
        self._proposal_ttl = proposal_ttl
        self._now = now or (lambda: datetime.now(UTC))
        self._hypothesis_manager = hypothesis_manager

    def build(self, state: IncidentState) -> RepairProposal:
        """Build a proposal only from state-bound, structured supporting evidence."""

        if state.status is not IncidentStatus.ROOT_CAUSE_FOUND:
            raise RepairProposalBuildError(
                "repair proposals require ROOT_CAUSE_FOUND incident status"
            )
        incident_id = state.alert.get("incident_id")
        if not isinstance(incident_id, str) or not incident_id.strip():
            raise RepairProposalBuildError("incident alert must contain incident_id")
        if state.root_cause is None:
            raise RepairProposalBuildError("incident has no confirmed root cause")

        try:
            root_cause = MissingPartitionRootCause.model_validate(state.root_cause)
        except ValidationError as exc:
            raise RepairProposalBuildError(
                "confirmed root cause is missing required supporting evidence"
            ) from exc
        if state.root_cause.get("root_cause_type") != root_cause.root_cause_type:
            raise RepairProposalBuildError("root cause type is not internally consistent")

        references = self._references(state, root_cause)
        supporting = self._supporting_references(references, root_cause)
        if len(supporting) < 2:
            raise RepairProposalBuildError(
                "confirmed evidence must contain at least two supporting items"
            )
        source_types = {reference.source_type for reference in supporting}
        if len(source_types) < 2:
            raise RepairProposalBuildError(
                "confirmed evidence must contain two independent sources"
            )
        if set(root_cause.independent_source_types) != source_types:
            raise RepairProposalBuildError(
                "root-cause independent source types do not match live evidence"
            )

        partition_value = self._validate_f01_observations(state, supporting)
        now = self._normalized_now()
        evidence = tuple(self._to_repair_evidence(reference) for reference in supporting)
        affected_assets = tuple(sorted({item.asset for item in evidence}))
        try:
            return RepairProposal(
                proposal_id=f"RP-{uuid4()}",
                incident_id=incident_id,
                root_cause_type=root_cause.root_cause_type,
                root_cause_confidence=root_cause.confidence,
                evidence=evidence,
                supporting_evidence_ids=tuple(item.evidence_id for item in evidence),
                independent_source_types=tuple(
                    sorted({item.source_type for item in evidence}, key=lambda item: item.value)
                ),
                affected_assets=affected_assets,
                action=RepairAction.RERUN_PARTITION,
                parameters={
                    "table": "events",
                    "source_table": "events",
                    "partition_column": "device_type",
                    "partition_value": partition_value,
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

    def _references(
        self,
        state: IncidentState,
        root_cause: MissingPartitionRootCause,
    ) -> dict[str, EvidenceReference]:
        references: dict[str, EvidenceReference] = {}
        for payload in state.evidence:
            try:
                reference = EvidenceReference.model_validate(payload)
            except ValidationError:
                continue
            if reference.evidence_id in references and references[reference.evidence_id] != reference:
                raise RepairProposalBuildError(
                    f"evidence id has conflicting runtime metadata: {reference.evidence_id}"
                )
            references[reference.evidence_id] = reference
        if self._hypothesis_manager is not None:
            managed = {
                item.evidence_id: item for item in self._hypothesis_manager.evidence()
            }
            for evidence_id in root_cause.supporting_evidence_ids:
                if evidence_id not in managed or managed[evidence_id] != references.get(evidence_id):
                    raise RepairProposalBuildError(
                        f"supporting evidence is not bound to HypothesisManager: {evidence_id}"
                    )
        return references

    @staticmethod
    def _supporting_references(
        references: Mapping[str, EvidenceReference],
        root_cause: MissingPartitionRootCause,
    ) -> tuple[EvidenceReference, ...]:
        result: list[EvidenceReference] = []
        for evidence_id in root_cause.supporting_evidence_ids:
            reference = references.get(evidence_id)
            if reference is None:
                raise RepairProposalBuildError(
                    f"supporting evidence is missing from incident state: {evidence_id}"
                )
            result.append(reference)
        return tuple(result)

    @staticmethod
    def _to_repair_evidence(reference: EvidenceReference) -> RepairEvidence:
        if reference.source_type == "business_data":
            asset = "events"
        elif reference.source_type == "operational_metadata":
            asset = "partition_metadata"
        else:
            raise RepairProposalBuildError(
                "F01 repair requires business_data and operational_metadata evidence"
            )
        if not reference.observation:
            raise RepairProposalBuildError(
                f"evidence has no structured observation: {reference.evidence_id}"
            )
        return RepairEvidence(
            evidence_id=reference.evidence_id,
            source_type=reference.source_type,
            asset=asset,
            finding=reference.description,
            observation=reference.observation,
        )

    @classmethod
    def _validate_f01_observations(
        cls,
        state: IncidentState,
        evidence: tuple[EvidenceReference, ...],
    ) -> str:
        target_date = cls._alert_date(state.alert.get("observed_at"))
        partition_value: str | None = None
        business_seen = False
        metadata_seen = False
        for reference in evidence:
            observation = reference.observation
            observed_date = observation.get("target_date")
            if observed_date != target_date.isoformat():
                raise RepairProposalBuildError(
                    f"evidence is outside the incident target date: {reference.evidence_id}"
                )
            row = observation.get("observed_row")
            if not isinstance(row, Mapping):
                raise RepairProposalBuildError(
                    f"evidence lacks a structured observed row: {reference.evidence_id}"
                )
            if reference.source_type == "business_data":
                count = next(
                    (row.get(key) for key in ("android_event_count", "android_events", "event_count", "events") if key in row),
                    None,
                )
                if isinstance(count, bool) or not isinstance(count, (int, float)) or count != 0:
                    raise RepairProposalBuildError(
                        "business evidence does not prove an empty target partition"
                    )
                business_seen = True
            elif reference.source_type == "operational_metadata":
                value = row.get("partition_value")
                row_count = row.get("row_count")
                status = row.get("status")
                if not isinstance(value, str) or not isinstance(row_count, (int, float)) or isinstance(row_count, bool):
                    raise RepairProposalBuildError("partition metadata observation is incomplete")
                if row_count != 0 or not isinstance(status, str) or status.strip().lower() != "missing":
                    raise RepairProposalBuildError(
                        "partition metadata does not prove a missing partition"
                    )
                if partition_value is not None and partition_value != value:
                    raise RepairProposalBuildError("supporting observations disagree on partition scope")
                partition_value = value
                metadata_seen = True
        if not business_seen or not metadata_seen or partition_value is None:
            raise RepairProposalBuildError(
                "F01 requires structured business and partition metadata observations"
            )
        match = _PARTITION_VALUE.fullmatch(partition_value)
        if match is None:
            raise RepairProposalBuildError("partition_value must be YYYY-MM-DD/device")
        try:
            parsed_date = date.fromisoformat(match.group("date"))
        except ValueError as exc:
            raise RepairProposalBuildError("partition_value contains an invalid date") from exc
        if parsed_date != target_date:
            raise RepairProposalBuildError("partition_value date does not match incident scope")
        alert_device = state.alert.get("device_type", state.alert.get("segment"))
        if isinstance(alert_device, str) and alert_device.strip() and alert_device.strip() != match.group("device"):
            raise RepairProposalBuildError("partition_value device does not match incident scope")
        return partition_value

    def _normalized_now(self) -> datetime:
        value = self._now()
        if value.tzinfo is None or value.utcoffset() is None:
            raise RepairProposalBuildError("clock must return a timezone-aware datetime")
        return value.astimezone(UTC)

    @staticmethod
    def _alert_date(value: object) -> date:
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        if isinstance(value, str):
            normalized = value.strip().replace("Z", "+00:00")
            try:
                return datetime.fromisoformat(normalized).date()
            except ValueError:
                try:
                    return date.fromisoformat(normalized)
                except ValueError:
                    pass
        raise RepairProposalBuildError("incident alert observed_at must contain a valid date")


__all__ = [
    "MissingPartitionContext",
    "MissingPartitionRootCause",
    "RepairProposalBuildError",
    "RepairProposalBuilder",
]
