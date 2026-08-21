"""Deterministic validation of a supported root-cause hypothesis.

The validator checks only runtime hypothesis state and registered evidence. It
does not load benchmark ground truth, execute tools, or call an LLM.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field

from harness.hypothesis import (
    SUPPORTED_CONFIDENCE_THRESHOLD,
    EvidenceReference,
    HypothesisState,
    HypothesisStatus,
)

MIN_SUPPORTING_EVIDENCE: Final[int] = 2
MIN_INDEPENDENT_SOURCE_TYPES: Final[int] = 2

RecommendedNextState = Literal["ROOT_CAUSE_FOUND", "HYPOTHESIS_TESTING"]


class RootCauseValidationError(ValueError):
    """Raised when a hypothesis or evidence collection is malformed."""


class RootCauseValidationResult(BaseModel):
    """Stable, serializable result of one root-cause validation attempt."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    hypothesis_id: str = Field(min_length=1)
    root_cause_type: str = Field(min_length=1)
    validated: bool
    confidence: float = Field(ge=0, le=1)
    supporting_evidence_ids: list[str] = Field(default_factory=list)
    independent_source_types: list[str] = Field(default_factory=list)
    missing_evidence: list[str] = Field(default_factory=list)
    contradictions: list[str] = Field(default_factory=list)
    recommended_next_state: RecommendedNextState


class RootCauseValidator:
    """Validate whether a candidate has enough independent runtime evidence."""

    def __init__(
        self,
        *,
        confidence_threshold: float = SUPPORTED_CONFIDENCE_THRESHOLD,
        min_supporting_evidence: int = MIN_SUPPORTING_EVIDENCE,
        min_independent_source_types: int = MIN_INDEPENDENT_SOURCE_TYPES,
    ) -> None:
        if not 0 <= confidence_threshold <= 1:
            raise ValueError("confidence_threshold must be between 0 and 1")
        if min_supporting_evidence < 1:
            raise ValueError("min_supporting_evidence must be greater than zero")
        if min_independent_source_types < 1:
            raise ValueError(
                "min_independent_source_types must be greater than zero"
            )
        self.confidence_threshold = confidence_threshold
        self.min_supporting_evidence = min_supporting_evidence
        self.min_independent_source_types = min_independent_source_types

    def validate(
        self,
        hypothesis: HypothesisState,
        evidence: Sequence[EvidenceReference]
        | Mapping[str, EvidenceReference],
        *,
        resolved_contradiction_ids: Collection[str] = (),
    ) -> RootCauseValidationResult:
        """Return a validation result without mutating the hypothesis state.

        A supporting ID counts only when the hypothesis explicitly binds it in
        ``supporting_evidence_ids``. Descriptions are deliberately ignored.
        """

        self._validate_hypothesis(hypothesis)
        evidence_by_id = self._index_evidence(evidence)
        supporting_ids = self._validate_evidence_bindings(hypothesis)
        contradiction_ids = hypothesis.contradicting_evidence_ids
        resolved_ids = self._validate_resolved_contradictions(
            resolved_contradiction_ids,
            contradiction_ids,
        )

        referenced_ids = _ordered_unique(
            [
                *hypothesis.evidence_ids,
                *supporting_ids,
                *contradiction_ids,
            ]
        )
        missing_evidence = [
            evidence_id
            for evidence_id in referenced_ids
            if evidence_id not in evidence_by_id
        ]

        independent_source_types = _ordered_unique(
            [
                evidence_by_id[evidence_id].source_type
                for evidence_id in supporting_ids
                if evidence_id in evidence_by_id
            ]
        )
        unresolved_contradictions = [
            evidence_id
            for evidence_id in contradiction_ids
            if evidence_id not in resolved_ids
        ]

        validated = (
            not missing_evidence
            and len(supporting_ids) >= self.min_supporting_evidence
            and len(independent_source_types) >= self.min_independent_source_types
            and hypothesis.confidence >= self.confidence_threshold
            and not unresolved_contradictions
        )
        next_state: RecommendedNextState = (
            "ROOT_CAUSE_FOUND" if validated else "HYPOTHESIS_TESTING"
        )
        return RootCauseValidationResult(
            hypothesis_id=hypothesis.hypothesis_id,
            root_cause_type=hypothesis.root_cause_type,
            validated=validated,
            confidence=hypothesis.confidence,
            supporting_evidence_ids=list(supporting_ids),
            independent_source_types=list(independent_source_types),
            missing_evidence=missing_evidence,
            contradictions=unresolved_contradictions,
            recommended_next_state=next_state,
        )

    @staticmethod
    def _validate_hypothesis(hypothesis: HypothesisState) -> None:
        if not isinstance(hypothesis, HypothesisState):
            raise RootCauseValidationError(
                "hypothesis must be a HypothesisState instance"
            )
        if hypothesis.status is HypothesisStatus.REJECTED:
            raise RootCauseValidationError(
                "rejected hypotheses cannot be root-cause validated"
            )

    @staticmethod
    def _index_evidence(
        evidence: Sequence[EvidenceReference]
        | Mapping[str, EvidenceReference],
    ) -> dict[str, EvidenceReference]:
        if isinstance(evidence, Mapping):
            items = list(evidence.items())
        elif isinstance(evidence, Sequence) and not isinstance(
            evidence, (str, bytes, bytearray)
        ):
            items = [(None, item) for item in evidence]
        else:
            raise RootCauseValidationError(
                "evidence must be a sequence or mapping of EvidenceReference"
            )

        indexed: dict[str, EvidenceReference] = {}
        for mapping_key, reference in items:
            if not isinstance(reference, EvidenceReference):
                raise RootCauseValidationError(
                    "evidence entries must be EvidenceReference instances"
                )
            evidence_id = reference.evidence_id
            if mapping_key is not None and mapping_key != evidence_id:
                raise RootCauseValidationError(
                    "evidence mapping keys must match EvidenceReference.evidence_id: "
                    f"{mapping_key!r} != {evidence_id!r}"
                )
            if evidence_id in indexed:
                raise RootCauseValidationError(
                    f"duplicate evidence id: {evidence_id}"
                )
            indexed[evidence_id] = reference
        return indexed

    @staticmethod
    def _validate_evidence_bindings(hypothesis: HypothesisState) -> list[str]:
        evidence_ids = hypothesis.evidence_ids
        supporting_ids = hypothesis.supporting_evidence_ids
        contradiction_ids = hypothesis.contradicting_evidence_ids
        for field_name, ids in (
            ("evidence_ids", evidence_ids),
            ("supporting_evidence_ids", supporting_ids),
            ("contradicting_evidence_ids", contradiction_ids),
        ):
            if len(ids) != len(set(ids)):
                raise RootCauseValidationError(f"duplicate evidence id in {field_name}")

        overlap = set(supporting_ids).intersection(contradiction_ids)
        if overlap:
            raise RootCauseValidationError(
                "evidence cannot be both supporting and contradicting: "
                + ", ".join(sorted(overlap))
            )

        declared_ids = set(evidence_ids)
        unbound_supporting = set(supporting_ids).difference(declared_ids)
        unbound_contradictions = set(contradiction_ids).difference(declared_ids)
        if unbound_supporting or unbound_contradictions:
            unbound = _ordered_unique(
                [
                    *supporting_ids,
                    *contradiction_ids,
                ]
            )
            unbound = [evidence_id for evidence_id in unbound if evidence_id not in declared_ids]
            raise RootCauseValidationError(
                "polarity evidence IDs must be present in evidence_ids: "
                + ", ".join(unbound)
            )
        return list(supporting_ids)

    @staticmethod
    def _validate_resolved_contradictions(
        resolved_ids: Collection[str], contradiction_ids: Sequence[str]
    ) -> set[str]:
        if isinstance(resolved_ids, (str, bytes, bytearray)):
            raise RootCauseValidationError(
                "resolved_contradiction_ids must be a collection of IDs"
            )
        try:
            resolved_list = list(resolved_ids)
        except TypeError as exc:
            raise RootCauseValidationError(
                "resolved_contradiction_ids must be a collection of IDs"
            ) from exc
        if any(
            not isinstance(evidence_id, str) or not evidence_id.strip()
            for evidence_id in resolved_list
        ):
            raise RootCauseValidationError(
                "resolved_contradiction_ids must contain non-blank string IDs"
            )
        if len(resolved_list) != len(set(resolved_list)):
            raise RootCauseValidationError(
                "duplicate resolved contradiction evidence id"
            )
        invalid_ids = set(resolved_list).difference(contradiction_ids)
        if invalid_ids:
            raise RootCauseValidationError(
                "resolved contradiction IDs must be declared contradictions: "
                + ", ".join(sorted(invalid_ids))
            )
        return set(resolved_list)


def _ordered_unique(values: Sequence[str]) -> list[str]:
    return list(dict.fromkeys(values))


__all__ = [
    "MIN_INDEPENDENT_SOURCE_TYPES",
    "MIN_SUPPORTING_EVIDENCE",
    "RootCauseValidationError",
    "RootCauseValidationResult",
    "RootCauseValidator",
]
