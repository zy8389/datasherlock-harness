from __future__ import annotations

import pytest

from benchmark.abstention_audit import (
    AbstentionCause,
    AbstentionSignals,
    _eligible_for_validator,
    _secondary_causes,
    classify_abstention,
)
from harness.hypothesis import HypothesisState, HypothesisStatus


def _signals(**updates: object) -> AbstentionSignals:
    payload: dict[str, object] = {
        "golden_hypothesis_present": True,
        "supporting_evidence_count": 0,
        "contradicting_evidence_count": 0,
        "golden_neutral_evidence_count": 0,
        "final_confidence": 0.40,
        "final_hypothesis_status": HypothesisStatus.TESTING,
        "validator_invoked": False,
        "validator_validated": None,
        "plan_exhausted": True,
    }
    payload.update(updates)
    return AbstentionSignals.model_validate(payload)


@pytest.mark.parametrize(
    ("signals", "expected"),
    [
        (
            _signals(
                golden_hypothesis_present=False,
                final_confidence=None,
                final_hypothesis_status=None,
            ),
            AbstentionCause.HYPOTHESIS_MISSING,
        ),
        (
            _signals(),
            AbstentionCause.EVIDENCE_MISSING,
        ),
        (
            _signals(golden_neutral_evidence_count=2),
            AbstentionCause.EVIDENCE_NEUTRALIZED,
        ),
        (
            _signals(
                supporting_evidence_count=2,
                final_confidence=0.70,
                validator_invoked=True,
                validator_validated=False,
            ),
            AbstentionCause.CONFIDENCE_SHORTFALL,
        ),
        (
            _signals(
                supporting_evidence_count=2,
                final_confidence=0.80,
                final_hypothesis_status=HypothesisStatus.SUPPORTED,
                validator_invoked=True,
                validator_validated=False,
            ),
            AbstentionCause.VALIDATOR_REJECTED,
        ),
        (
            _signals(
                supporting_evidence_count=1,
                contradicting_evidence_count=1,
                golden_neutral_evidence_count=3,
                final_confidence=0.35,
                validator_invoked=True,
                validator_validated=False,
            ),
            AbstentionCause.CONTRADICTION_BLOCKED,
        ),
    ],
)
def test_classify_abstention_uses_earliest_causal_bottleneck(
    signals: AbstentionSignals,
    expected: AbstentionCause,
) -> None:
    assert classify_abstention(signals) is expected


@pytest.mark.parametrize(
    ("signals", "expected_primary", "secondary_rejected"),
    [
        (
            _signals(
                final_hypothesis_status=HypothesisStatus.PROPOSED,
                validator_invoked=True,
                validator_validated=False,
            ),
            AbstentionCause.EVIDENCE_MISSING,
            False,
        ),
        (
            _signals(
                supporting_evidence_count=1,
                final_hypothesis_status=HypothesisStatus.TESTING,
                validator_invoked=True,
                validator_validated=False,
            ),
            AbstentionCause.EVIDENCE_MISSING,
            False,
        ),
        (
            _signals(
                supporting_evidence_count=2,
                final_confidence=0.80,
                final_hypothesis_status=HypothesisStatus.SUPPORTED,
                validator_invoked=True,
                validator_validated=False,
            ),
            AbstentionCause.VALIDATOR_REJECTED,
            True,
        ),
        (
            _signals(
                supporting_evidence_count=2,
                final_confidence=0.80,
                final_hypothesis_status=HypothesisStatus.SUPPORTED,
                validator_invoked=True,
                validator_validated=True,
            ),
            AbstentionCause.PLAN_EXHAUSTED,
            False,
        ),
    ],
)
def test_validator_rejection_requires_a_supported_hypothesis(
    signals: AbstentionSignals,
    expected_primary: AbstentionCause,
    secondary_rejected: bool,
) -> None:
    primary = classify_abstention(signals)
    secondary = _secondary_causes(signals, AbstentionCause.OTHER)

    assert primary is expected_primary
    assert (
        AbstentionCause.VALIDATOR_REJECTED in secondary
    ) is secondary_rejected


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (HypothesisStatus.PROPOSED, False),
        (HypothesisStatus.TESTING, False),
        (HypothesisStatus.SUPPORTED, True),
    ],
)
def test_gate_eligibility_requires_supported_status(
    status: HypothesisStatus,
    expected: bool,
) -> None:
    hypothesis = HypothesisState(
        hypothesis_id="H01",
        root_cause_type="duplicate_batch",
        description="Synthetic gate eligibility candidate.",
        status=status,
        confidence=0.80,
        evidence_ids=["E01", "E02"],
        supporting_evidence_ids=["E01", "E02"],
    )

    assert _eligible_for_validator(hypothesis) is expected
