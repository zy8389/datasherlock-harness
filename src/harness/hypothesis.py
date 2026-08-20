"""Candidate hypothesis lifecycle management for incident investigations.

The manager deliberately stops at candidate state.  It records evidence
references and confidence changes, but it does not decide or emit a final root
cause.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Final

from pydantic import BaseModel, ConfigDict, Field

from agents.planner import Hypothesis

CONFIDENCE_SUPPORT_DELTA: Final[float] = 0.15
CONFIDENCE_CONTRADICTION_DELTA: Final[float] = 0.20
SUPPORTED_EVIDENCE_COUNT: Final[int] = 2
REJECTED_EVIDENCE_COUNT: Final[int] = 2
SUPPORTED_CONFIDENCE_THRESHOLD: Final[float] = 0.75
REJECTED_CONFIDENCE_THRESHOLD: Final[float] = 0.20


def _utc_now() -> datetime:
    return datetime.now(UTC)


class HypothesisStatus(StrEnum):
    """Lifecycle states for one candidate hypothesis."""

    PROPOSED = "PROPOSED"
    TESTING = "TESTING"
    SUPPORTED = "SUPPORTED"
    REJECTED = "REJECTED"


_ALLOWED_STATUS_TRANSITIONS: Final[dict[HypothesisStatus, frozenset[HypothesisStatus]]] = {
    HypothesisStatus.PROPOSED: frozenset(
        {HypothesisStatus.PROPOSED, HypothesisStatus.TESTING}
    ),
    HypothesisStatus.TESTING: frozenset(
        {
            HypothesisStatus.TESTING,
            HypothesisStatus.SUPPORTED,
            HypothesisStatus.REJECTED,
        }
    ),
    HypothesisStatus.SUPPORTED: frozenset({HypothesisStatus.SUPPORTED}),
    HypothesisStatus.REJECTED: frozenset({HypothesisStatus.REJECTED}),
}


class HypothesisStateError(ValueError):
    """Raised when a hypothesis lifecycle operation is invalid."""


class EvidenceReference(BaseModel):
    """Lightweight evidence metadata used until a full EvidenceResult exists."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    evidence_id: str = Field(min_length=1)
    source_type: str = Field(min_length=1)
    description: str = Field(min_length=1)


class HypothesisState(BaseModel):
    """Serializable state for one Planner hypothesis."""

    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
        str_strip_whitespace=True,
    )

    hypothesis_id: str = Field(min_length=1)
    root_cause_type: str = Field(min_length=1)
    description: str = Field(min_length=1)
    status: HypothesisStatus = HypothesisStatus.PROPOSED
    confidence: float = Field(ge=0, le=1)
    evidence_ids: list[str] = Field(default_factory=list)
    supporting_evidence_ids: list[str] = Field(default_factory=list)
    contradicting_evidence_ids: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=_utc_now)
    updated_at: datetime = Field(default_factory=_utc_now)


class HypothesisManager:
    """Manage candidate hypothesis state without producing a final diagnosis."""

    def __init__(self) -> None:
        self._hypotheses: dict[str, HypothesisState] = {}
        self._evidence: dict[str, EvidenceReference] = {}

    def create_hypothesis(self, hypothesis: Hypothesis) -> HypothesisState:
        """Create a proposed state from one Planner hypothesis."""

        if hypothesis.hypothesis_id in self._hypotheses:
            raise HypothesisStateError(
                f"hypothesis already exists: {hypothesis.hypothesis_id}"
            )
        now = _utc_now()
        state = HypothesisState(
            hypothesis_id=hypothesis.hypothesis_id,
            root_cause_type=hypothesis.root_cause_type,
            description=hypothesis.description,
            confidence=hypothesis.initial_confidence,
            created_at=now,
            updated_at=now,
        )
        self._hypotheses[state.hypothesis_id] = state
        return state

    def get_hypothesis(self, hypothesis_id: str) -> HypothesisState:
        """Return a managed hypothesis or raise a clear lookup error."""

        try:
            return self._hypotheses[hypothesis_id]
        except KeyError as exc:
            raise HypothesisStateError(f"unknown hypothesis: {hypothesis_id}") from exc

    def hypotheses(self) -> tuple[HypothesisState, ...]:
        """Return managed states in creation order."""

        return tuple(self._hypotheses.values())

    def register_evidence(self, evidence: EvidenceReference) -> EvidenceReference:
        """Register lightweight evidence metadata for later binding."""

        existing = self._evidence.get(evidence.evidence_id)
        if existing is not None and existing != evidence:
            raise HypothesisStateError(
                f"evidence id already registered with different metadata: {evidence.evidence_id}"
            )
        self._evidence[evidence.evidence_id] = evidence
        return evidence

    def start_testing(self, hypothesis_id: str) -> HypothesisState:
        """Move a proposed hypothesis into testing."""

        state = self.get_hypothesis(hypothesis_id)
        if state.status is HypothesisStatus.PROPOSED:
            self._set_status(state, HypothesisStatus.TESTING)
            return state
        if state.status is HypothesisStatus.TESTING:
            return state
        raise HypothesisStateError(
            f"cannot start testing from terminal state {state.status.value}"
        )

    def attach_evidence(
        self,
        hypothesis_id: str,
        evidence_id: str,
        supports: bool,
    ) -> HypothesisState:
        """Bind evidence, update confidence, and reevaluate lifecycle status."""

        if not evidence_id.strip():
            raise HypothesisStateError("evidence_id must not be blank")
        state = self.get_hypothesis(hypothesis_id)
        if state.status in {HypothesisStatus.SUPPORTED, HypothesisStatus.REJECTED}:
            raise HypothesisStateError(
                f"cannot attach evidence to terminal state {state.status.value}"
            )
        if evidence_id in state.evidence_ids:
            already_supports = evidence_id in state.supporting_evidence_ids
            if already_supports != supports:
                raise HypothesisStateError(
                    f"evidence already attached with opposite polarity: {evidence_id}"
                )
            return state

        state.evidence_ids.append(evidence_id)
        if supports:
            state.supporting_evidence_ids.append(evidence_id)
            state.confidence = _clamp_confidence(
                state.confidence + CONFIDENCE_SUPPORT_DELTA
            )
        else:
            state.contradicting_evidence_ids.append(evidence_id)
            state.confidence = _clamp_confidence(
                state.confidence - CONFIDENCE_CONTRADICTION_DELTA
            )
        state.updated_at = _utc_now()
        self.update_status(hypothesis_id)
        return state

    def update_status(self, hypothesis_id: str) -> HypothesisState:
        """Apply evidence/count thresholds to a non-terminal hypothesis."""

        state = self.get_hypothesis(hypothesis_id)
        if state.status in {HypothesisStatus.SUPPORTED, HypothesisStatus.REJECTED}:
            return state
        if state.status is HypothesisStatus.PROPOSED:
            self._set_status(state, HypothesisStatus.TESTING)

        # Contradictory evidence wins an otherwise conflicting evaluation; a
        # terminal state cannot be reopened by later evidence.
        if (
            len(state.contradicting_evidence_ids) >= REJECTED_EVIDENCE_COUNT
            or state.confidence <= REJECTED_CONFIDENCE_THRESHOLD
        ):
            self._set_status(state, HypothesisStatus.REJECTED)
        elif (
            len(state.supporting_evidence_ids) >= SUPPORTED_EVIDENCE_COUNT
            and state.confidence >= SUPPORTED_CONFIDENCE_THRESHOLD
        ):
            self._set_status(state, HypothesisStatus.SUPPORTED)
        else:
            self._set_status(state, HypothesisStatus.TESTING)
        return state

    @staticmethod
    def _set_status(state: HypothesisState, status: HypothesisStatus) -> None:
        if state.status is status:
            return
        if status not in _ALLOWED_STATUS_TRANSITIONS[state.status]:
            if (
                state.status is HypothesisStatus.REJECTED
                and status is HypothesisStatus.SUPPORTED
            ):
                raise HypothesisStateError("rejected hypotheses cannot become supported")
            if (
                state.status is HypothesisStatus.SUPPORTED
                and status is HypothesisStatus.PROPOSED
            ):
                raise HypothesisStateError("supported hypotheses cannot become proposed")
            raise HypothesisStateError(
                f"invalid hypothesis transition: {state.status.value} -> {status.value}"
            )
        state.status = status
        state.updated_at = _utc_now()


def _clamp_confidence(value: float) -> float:
    return max(0.0, min(1.0, value))


__all__ = [
    "CONFIDENCE_CONTRADICTION_DELTA",
    "CONFIDENCE_SUPPORT_DELTA",
    "EvidenceReference",
    "HypothesisManager",
    "HypothesisState",
    "HypothesisStateError",
    "HypothesisStatus",
]
