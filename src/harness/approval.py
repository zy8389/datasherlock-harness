"""Validation helpers for graph-owned approval transitions.

This module intentionally contains no state machine.  It validates immutable
artifacts and then delegates the actual transition to ``HarnessGraph``.
"""

from __future__ import annotations

import hmac
from datetime import UTC, datetime

from harness.repair import (
    ApprovalDecision,
    ApprovalOutcome,
    RepairProposal,
    proposal_is_intact,
)
from harness.state import IncidentState, IncidentStatus


class ApprovalValidationError(ValueError):
    """Raised when a decision cannot safely authorize a repair."""


def validate_proposal_for_approval(
    proposal: RepairProposal,
    *,
    now: datetime | None = None,
) -> None:
    """Reject tampered or expired proposals before any graph mutation."""

    if not proposal_is_intact(proposal):
        raise ApprovalValidationError("repair proposal content hash is invalid")
    current = _aware_utc(now or datetime.now(UTC), "now")
    if current < proposal.created_at or current > proposal.valid_until:
        raise ApprovalValidationError("repair proposal has expired or is not yet valid")


def validate_approval_decision(
    proposal: RepairProposal,
    decision: ApprovalDecision,
    *,
    now: datetime | None = None,
) -> None:
    """Verify every identity and time binding on an approval decision."""

    validate_proposal_for_approval(proposal, now=now)
    if (
        decision.incident_id != proposal.incident_id
        or decision.proposal_id != proposal.proposal_id
        or not hmac.compare_digest(decision.proposal_hash, proposal.proposal_hash)
    ):
        raise ApprovalValidationError("approval decision does not bind to proposal")
    if decision.decided_at < proposal.created_at or decision.decided_at > proposal.valid_until:
        raise ApprovalValidationError("approval decision was made outside proposal validity")
    if decision.outcome is ApprovalOutcome.REJECTED and not decision.comment:
        raise ApprovalValidationError("rejected approval requires a non-empty comment")


def record_approval(
    graph: object,
    state: IncidentState,
    proposal: RepairProposal,
    decision: ApprovalDecision,
    *,
    now: datetime | None = None,
) -> object:
    """Validate a decision, then invoke the existing graph approval API."""

    if state.status is not IncidentStatus.AWAITING_APPROVAL:
        raise ApprovalValidationError(
            "approval decisions require AWAITING_APPROVAL incident status"
        )
    state_proposal = _proposal_from_state(state)
    if not proposal_is_intact(state_proposal):
        raise ApprovalValidationError("checkpointed proposal content hash is invalid")
    if state_proposal.model_dump(mode="json") != proposal.model_dump(mode="json"):
        raise ApprovalValidationError("approval proposal does not match checkpointed proposal")
    validate_approval_decision(proposal, decision, now=now)
    method = getattr(graph, "record_approval", None)
    if not callable(method):
        raise ApprovalValidationError("graph does not expose record_approval")
    metadata = decision.model_dump(mode="json")
    if decision.outcome is ApprovalOutcome.APPROVED:
        return method(state, approved=True, metadata=metadata, now=now)
    return method(
        state,
        approved=False,
        reason=decision.comment,
        metadata=metadata,
        now=now,
    )


def _proposal_from_state(state: IncidentState) -> RepairProposal:
    if state.fix_proposal is None:
        raise ApprovalValidationError("incident has no checkpointed repair proposal")
    try:
        return RepairProposal.model_validate(state.fix_proposal)
    except (TypeError, ValueError) as exc:
        raise ApprovalValidationError("checkpointed repair proposal is invalid") from exc


def _aware_utc(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ApprovalValidationError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


__all__ = [
    "ApprovalValidationError",
    "record_approval",
    "validate_approval_decision",
    "validate_proposal_for_approval",
]
