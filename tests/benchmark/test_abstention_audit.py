from __future__ import annotations

import pytest

from benchmark.abstention_audit import (
    AbstentionCause,
    AbstentionSignals,
    classify_abstention,
)
from harness.hypothesis import HypothesisStatus


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
