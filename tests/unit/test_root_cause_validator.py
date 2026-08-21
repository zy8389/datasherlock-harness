import pytest

from harness.hypothesis import (
    EvidenceReference,
    HypothesisManager,
    HypothesisState,
    HypothesisStateError,
    HypothesisStatus,
)
from validators.root_cause_validator import (
    RootCauseValidationError,
    RootCauseValidator,
)


def _evidence(evidence_id: str, source_type: str) -> EvidenceReference:
    return EvidenceReference(
        evidence_id=evidence_id,
        source_type=source_type,
        description=f"Finding for {evidence_id}.",
    )


def _state(
    *,
    confidence: float = 0.85,
    evidence_ids: list[str] | None = None,
    supporting_ids: list[str] | None = None,
    contradicting_ids: list[str] | None = None,
) -> HypothesisState:
    return HypothesisState(
        hypothesis_id="H01",
        root_cause_type="missing_partition",
        description="The target partition may be missing.",
        status=HypothesisStatus.SUPPORTED,
        confidence=confidence,
        evidence_ids=evidence_ids or ["E01", "E02"],
        supporting_evidence_ids=supporting_ids or ["E01", "E02"],
        contradicting_evidence_ids=contradicting_ids or [],
    )


def _independent_supporting_evidence() -> list[EvidenceReference]:
    return [
        _evidence("E01", "business_data"),
        _evidence("E02", "operational_metadata"),
    ]


def test_high_confidence_does_not_bypass_minimum_supporting_evidence() -> None:
    hypothesis = _state(
        confidence=0.9,
        evidence_ids=["E01"],
        supporting_ids=["E01"],
    )

    result = RootCauseValidator().validate(
        hypothesis,
        [_evidence("E01", "business_data")],
    )

    assert result.validated is False
    assert result.confidence == pytest.approx(0.9)
    assert result.recommended_next_state == "HYPOTHESIS_TESTING"


def test_two_supporting_evidence_from_one_source_are_not_independent() -> None:
    hypothesis = _state()
    evidence = [
        _evidence("E01", "business_data"),
        _evidence("E02", "business_data"),
    ]

    result = RootCauseValidator().validate(hypothesis, evidence)

    assert result.validated is False
    assert result.independent_source_types == ["business_data"]


def test_missing_evidence_id_is_reported_and_blocks_validation() -> None:
    hypothesis = _state()

    result = RootCauseValidator().validate(
        hypothesis,
        [_evidence("E01", "business_data")],
    )

    assert result.validated is False
    assert result.missing_evidence == ["E02"]


def test_unresolved_contradiction_blocks_validation() -> None:
    hypothesis = _state(
        evidence_ids=["E01", "E02", "E03"],
        contradicting_ids=["E03"],
    )
    evidence = [
        *_independent_supporting_evidence(),
        _evidence("E03", "schema_metadata"),
    ]

    result = RootCauseValidator().validate(hypothesis, evidence)

    assert result.validated is False
    assert result.contradictions == ["E03"]
    assert result.recommended_next_state == "HYPOTHESIS_TESTING"


def test_explicitly_resolved_contradiction_allows_validation() -> None:
    hypothesis = _state(
        evidence_ids=["E01", "E02", "E03"],
        contradicting_ids=["E03"],
    )
    evidence = [
        *_independent_supporting_evidence(),
        _evidence("E03", "schema_metadata"),
    ]

    result = RootCauseValidator().validate(
        hypothesis,
        evidence,
        resolved_contradiction_ids={"E03"},
    )

    assert result.validated is True
    assert result.contradictions == []
    assert result.recommended_next_state == "ROOT_CAUSE_FOUND"


def test_resolved_contradiction_must_be_declared_as_a_contradiction() -> None:
    hypothesis = _state()

    with pytest.raises(RootCauseValidationError, match="declared contradictions"):
        RootCauseValidator().validate(
            hypothesis,
            _independent_supporting_evidence(),
            resolved_contradiction_ids={"E999"},
        )


def test_low_confidence_blocks_validation() -> None:
    result = RootCauseValidator().validate(
        _state(confidence=0.7),
        _independent_supporting_evidence(),
    )

    assert result.validated is False
    assert result.recommended_next_state == "HYPOTHESIS_TESTING"


def test_validation_passes_with_independent_supporting_evidence() -> None:
    result = RootCauseValidator().validate(
        _state(confidence=0.85),
        _independent_supporting_evidence(),
    )

    assert result.validated is True
    assert result.hypothesis_id == "H01"
    assert result.root_cause_type == "missing_partition"
    assert result.supporting_evidence_ids == ["E01", "E02"]
    assert result.independent_source_types == [
        "business_data",
        "operational_metadata",
    ]
    assert result.missing_evidence == []
    assert result.contradictions == []
    assert result.model_dump(mode="json")["recommended_next_state"] == (
        "ROOT_CAUSE_FOUND"
    )


def test_evidence_must_be_explicitly_bound_as_supporting() -> None:
    hypothesis = _state(
        evidence_ids=["E01", "E02"],
        supporting_ids=["E01"],
    )

    result = RootCauseValidator().validate(
        hypothesis,
        _independent_supporting_evidence(),
    )

    assert result.validated is False
    assert result.supporting_evidence_ids == ["E01"]


def test_duplicate_evidence_reference_id_is_rejected() -> None:
    with pytest.raises(RootCauseValidationError, match="duplicate evidence id"):
        RootCauseValidator().validate(
            _state(),
            [
                _evidence("E01", "business_data"),
                _evidence("E01", "operational_metadata"),
            ],
        )


def test_supported_candidate_can_return_to_testing_after_validation_failure() -> None:
    manager = HypothesisManager()
    state = manager.create_hypothesis(
        _planner_hypothesis(initial_confidence=0.5)
    )
    for evidence in (
        _evidence("E01", "business_data"),
        _evidence("E02", "business_data"),
    ):
        manager.register_evidence(evidence)
    manager.attach_evidence(state.hypothesis_id, "E01", supports=True)
    manager.attach_evidence(state.hypothesis_id, "E02", supports=True)

    validator = RootCauseValidator()
    failed = validator.validate(
        state,
        [
            _evidence("E01", "business_data"),
            _evidence("E02", "business_data"),
        ],
    )

    assert state.status is HypothesisStatus.SUPPORTED
    assert failed.validated is False
    assert manager.return_to_testing(state.hypothesis_id).status is HypothesisStatus.TESTING

    manager.register_evidence(_evidence("E03", "operational_metadata"))
    manager.attach_evidence(state.hypothesis_id, "E03", supports=True)
    passed = validator.validate(
        state,
        [
            _evidence("E01", "business_data"),
            _evidence("E02", "business_data"),
            _evidence("E03", "operational_metadata"),
        ],
    )

    assert state.status is HypothesisStatus.SUPPORTED
    assert passed.validated is True


def test_rejected_hypothesis_remains_terminal() -> None:
    manager = HypothesisManager()
    state = manager.create_hypothesis(_planner_hypothesis())
    manager.attach_evidence(state.hypothesis_id, "E01", supports=False)
    manager.attach_evidence(state.hypothesis_id, "E02", supports=False)

    with pytest.raises(HypothesisStateError, match="cannot return"):
        manager.return_to_testing(state.hypothesis_id)


def _planner_hypothesis(initial_confidence: float = 0.5):
    from agents.planner import Hypothesis

    return Hypothesis(
        hypothesis_id="H01",
        root_cause_type="missing_partition",
        description="The target partition may be missing.",
        initial_confidence=initial_confidence,
    )
