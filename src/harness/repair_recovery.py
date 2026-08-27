"""Crash-safe resumption of one approval-gated sandbox repair."""

from __future__ import annotations

import hmac
from collections.abc import Mapping

from harness.graph import HarnessGraph
from harness.repair import (
    ApprovalDecision,
    ApprovalOutcome,
    RepairProposal,
    SandboxRun,
    SandboxRunStatus,
    proposal_is_intact,
)
from harness.sandbox_repair import SandboxRepairError, SandboxRepairExecutor
from harness.state import IncidentState, IncidentStatus


class RepairRecoveryError(ValueError):
    """Raised when a pending repair cannot be resumed safely."""


def resume_approved_repair(
    state: IncidentState,
    proposal: RepairProposal,
    approval: ApprovalDecision,
    pending_run: SandboxRun,
    executor: SandboxRepairExecutor,
    graph: HarnessGraph,
) -> SandboxRun:
    """Recover or execute one pending run, then record it through the graph."""

    _validate_resume_bindings(state, proposal, approval, pending_run)
    try:
        recovered = executor.recover_run(proposal, pending_run)
    except SandboxRepairError as exc:
        raise RepairRecoveryError(str(exc)) from exc
    if recovered is None:
        try:
            terminal = executor.execute(proposal, pending_run)
        except SandboxRepairError as exc:
            raise RepairRecoveryError(str(exc)) from exc
    else:
        terminal = recovered
    graph.record_repair_result(
        state,
        result=terminal.model_dump(mode="json"),
    )
    return terminal


def _validate_resume_bindings(
    state: IncidentState,
    proposal: RepairProposal,
    approval: ApprovalDecision,
    pending_run: SandboxRun,
) -> None:
    if state.status is not IncidentStatus.SANDBOX_REPAIR:
        raise RepairRecoveryError("repair recovery requires SANDBOX_REPAIR")
    if not proposal_is_intact(proposal):
        raise RepairRecoveryError("repair proposal content hash is invalid")
    if not isinstance(state.fix_proposal, Mapping):
        raise RepairRecoveryError("checkpoint has no typed repair proposal")
    try:
        checkpoint_proposal = RepairProposal.model_validate(state.fix_proposal)
    except (TypeError, ValueError) as exc:
        raise RepairRecoveryError("checkpointed repair proposal is invalid") from exc
    if checkpoint_proposal.model_dump(mode="json") != proposal.model_dump(mode="json"):
        raise RepairRecoveryError("recovery proposal does not match checkpoint")
    if approval.outcome is not ApprovalOutcome.APPROVED:
        raise RepairRecoveryError("repair recovery requires an approved decision")
    if (
        approval.incident_id != proposal.incident_id
        or approval.proposal_id != proposal.proposal_id
        or not hmac.compare_digest(approval.proposal_hash, proposal.proposal_hash)
    ):
        raise RepairRecoveryError("approval decision does not bind to recovery proposal")
    if pending_run.status is not SandboxRunStatus.PENDING:
        raise RepairRecoveryError("repair recovery requires a pending SandboxRun")
    if (
        pending_run.incident_id != proposal.incident_id
        or pending_run.proposal_id != proposal.proposal_id
        or not hmac.compare_digest(pending_run.proposal_hash, proposal.proposal_hash)
        or pending_run.action is not proposal.action
        or pending_run.approval_decision_id != approval.decision_id
    ):
        raise RepairRecoveryError("pending run does not bind to recovery proposal")
    if not isinstance(state.approval, Mapping):
        raise RepairRecoveryError("checkpoint has no typed approval decision")
    approval_payload = dict(state.approval)
    if approval_payload.get("status") != "approved":
        raise RepairRecoveryError("checkpoint approval is not approved")
    approval_payload.pop("status", None)
    approval_payload.pop("reason", None)
    try:
        checkpoint_approval = ApprovalDecision.model_validate(approval_payload)
    except (TypeError, ValueError) as exc:
        raise RepairRecoveryError("checkpointed approval decision is invalid") from exc
    if checkpoint_approval.model_dump(mode="json") != approval.model_dump(mode="json"):
        raise RepairRecoveryError("recovery approval does not match checkpoint")
    if not isinstance(state.repair_result, Mapping):
        raise RepairRecoveryError("checkpoint has no pending repair artifact")
    raw_pending = state.repair_result.get("sandbox_run")
    try:
        checkpoint_run = SandboxRun.model_validate(raw_pending)
    except (TypeError, ValueError) as exc:
        raise RepairRecoveryError("checkpointed pending run is invalid") from exc
    if checkpoint_run.status is not SandboxRunStatus.PENDING:
        raise RepairRecoveryError("checkpointed repair artifact is not pending")
    if checkpoint_run.model_dump(mode="json") != pending_run.model_dump(mode="json"):
        raise RepairRecoveryError("recovery run does not match checkpoint")


__all__ = ["RepairRecoveryError", "resume_approved_repair"]
