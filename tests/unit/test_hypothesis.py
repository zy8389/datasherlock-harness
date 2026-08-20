import pytest

from agents.planner import Hypothesis
from harness.hypothesis import (
    EvidenceReference,
    HypothesisManager,
    HypothesisStateError,
    HypothesisStatus,
)


def _hypothesis(
    hypothesis_id: str = "H01", initial_confidence: float = 0.5
) -> Hypothesis:
    return Hypothesis(
        hypothesis_id=hypothesis_id,
        root_cause_type="missing_partition",
        description="The target partition may be missing.",
        initial_confidence=initial_confidence,
    )


def test_create_hypothesis_starts_proposed_with_planner_confidence() -> None:
    manager = HypothesisManager()

    state = manager.create_hypothesis(_hypothesis(initial_confidence=0.62))

    assert state.status is HypothesisStatus.PROPOSED
    assert state.confidence == pytest.approx(0.62)
    assert state.evidence_ids == []
    assert state.created_at == state.updated_at


def test_start_testing_moves_proposed_to_testing_and_is_idempotent() -> None:
    manager = HypothesisManager()
    state = manager.create_hypothesis(_hypothesis())

    assert manager.start_testing(state.hypothesis_id).status is HypothesisStatus.TESTING
    assert manager.start_testing(state.hypothesis_id).status is HypothesisStatus.TESTING


def test_supporting_evidence_binds_and_can_support_hypothesis() -> None:
    manager = HypothesisManager()
    manager.register_evidence(
        EvidenceReference(
            evidence_id="E01",
            source_type="business_data",
            description="Android events are absent.",
        )
    )
    manager.register_evidence(
        EvidenceReference(
            evidence_id="E02",
            source_type="operational_metadata",
            description="The partition is marked missing.",
        )
    )
    state = manager.create_hypothesis(_hypothesis(initial_confidence=0.5))

    manager.attach_evidence(state.hypothesis_id, "E01", supports=True)
    manager.attach_evidence(state.hypothesis_id, "E02", supports=True)

    assert state.supporting_evidence_ids == ["E01", "E02"]
    assert state.contradicting_evidence_ids == []
    assert state.evidence_ids == ["E01", "E02"]
    assert state.confidence == pytest.approx(0.8)
    assert state.status is HypothesisStatus.SUPPORTED


def test_contradicting_evidence_rejects_and_confidence_is_clamped() -> None:
    manager = HypothesisManager()
    state = manager.create_hypothesis(_hypothesis(initial_confidence=0.5))

    manager.attach_evidence(state.hypothesis_id, "E01", supports=False)
    manager.attach_evidence(state.hypothesis_id, "E02", supports=False)

    assert state.contradicting_evidence_ids == ["E01", "E02"]
    assert state.supporting_evidence_ids == []
    assert state.confidence == pytest.approx(0.1)
    assert state.status is HypothesisStatus.REJECTED

    with pytest.raises(HypothesisStateError, match="terminal state"):
        manager.start_testing(state.hypothesis_id)


def test_confidence_updates_stay_between_zero_and_one() -> None:
    manager = HypothesisManager()
    high = manager.create_hypothesis(_hypothesis("H01", initial_confidence=0.95))
    low = manager.create_hypothesis(_hypothesis("H02", initial_confidence=0.1))

    manager.attach_evidence(high.hypothesis_id, "E01", supports=True)
    manager.attach_evidence(low.hypothesis_id, "E02", supports=False)

    assert high.confidence == pytest.approx(1.0)
    assert low.confidence == pytest.approx(0.0)


def test_terminal_hypothesis_cannot_receive_opposite_evidence() -> None:
    manager = HypothesisManager()
    state = manager.create_hypothesis(_hypothesis(initial_confidence=0.5))
    manager.attach_evidence(state.hypothesis_id, "E01", supports=False)
    manager.attach_evidence(state.hypothesis_id, "E02", supports=False)

    with pytest.raises(HypothesisStateError, match="terminal state"):
        manager.attach_evidence(state.hypothesis_id, "E03", supports=True)

    assert state.status is HypothesisStatus.REJECTED


def test_duplicate_evidence_with_opposite_polarity_is_rejected() -> None:
    manager = HypothesisManager()
    state = manager.create_hypothesis(_hypothesis())

    manager.attach_evidence(state.hypothesis_id, "E01", supports=True)

    with pytest.raises(HypothesisStateError, match="opposite polarity"):
        manager.attach_evidence(state.hypothesis_id, "E01", supports=False)
