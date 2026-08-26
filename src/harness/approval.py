"""Stateful approval orchestration for sandbox-only repair proposals."""

from __future__ import annotations

import hmac
from datetime import UTC, datetime

from harness.repair import (
    ApprovalDecision,
    ApprovalOutcome,
    PostValidationResult,
    RepairProposal,
    SandboxRun,
    SandboxRunStatus,
)
from harness.state import IncidentState, IncidentStatus


class ApprovalFlowError(ValueError):
    """Raised when an approval or sandbox transition violates its contract."""


class ApprovalFlow:
    """Apply the approval-gated repair transitions to one incident state."""

    def propose(self, state: IncidentState, proposal: RepairProposal) -> IncidentState:
        """Bind a proposal to a confirmed root cause and enter ``FIX_PROPOSED``."""

        self._require_status(state, IncidentStatus.ROOT_CAUSE_FOUND)
        self._require_incident_match(state, proposal.incident_id)
        root_cause_type = (state.root_cause or {}).get("root_cause_type")
        if root_cause_type != proposal.root_cause_type:
            raise ApprovalFlowError("repair proposal root cause does not match incident")
        state.repair_proposal = proposal
        state.approval = None
        state.sandbox_run = None
        state.repair_result = None
        state.status = IncidentStatus.FIX_PROPOSED
        return state

    def request_approval(self, state: IncidentState) -> IncidentState:
        """Move a proposed repair into the reviewer queue while it is valid."""

        self._require_status(state, IncidentStatus.FIX_PROPOSED)
        proposal = self._require_proposal(state)
        if proposal.valid_until <= datetime.now(UTC):
            raise ApprovalFlowError("repair proposal has expired")
        state.status = IncidentStatus.AWAITING_APPROVAL
        return state

    def record_decision(
        self, state: IncidentState, decision: ApprovalDecision
    ) -> IncidentState:
        """Record a valid reviewer decision and gate sandbox execution."""

        self._require_status(state, IncidentStatus.AWAITING_APPROVAL)
        proposal = self._require_proposal(state)
        self._require_decision_match(proposal, decision)
        if decision.decided_at > proposal.valid_until:
            raise ApprovalFlowError("approval decision was made after proposal expiry")
        state.approval = decision
        if decision.outcome is ApprovalOutcome.REJECTED:
            state.status = IncidentStatus.REJECTED
            state.final_status = IncidentStatus.REJECTED
        else:
            state.status = IncidentStatus.SANDBOX_REPAIR
        return state

    def create_sandbox_run(
        self, state: IncidentState, *, run_id: str, sandbox_path: str
    ) -> SandboxRun:
        """Create the pending run record that an executor may subsequently use."""

        self._require_status(state, IncidentStatus.SANDBOX_REPAIR)
        proposal = self._require_proposal(state)
        decision = state.approval
        if decision is None:
            raise ApprovalFlowError("sandbox repair requires an approval decision")
        if state.sandbox_run is not None:
            raise ApprovalFlowError("incident already has a sandbox run")
        run = SandboxRun.for_approved_proposal(
            proposal,
            decision,
            run_id=run_id,
            sandbox_path=sandbox_path,
        )
        state.sandbox_run = run
        return run

    def record_sandbox_run(self, state: IncidentState, run: SandboxRun) -> IncidentState:
        """Persist a terminal sandbox outcome and advance only successful runs."""

        self._require_status(state, IncidentStatus.SANDBOX_REPAIR)
        proposal = self._require_proposal(state)
        current_run = state.sandbox_run
        if current_run is None:
            raise ApprovalFlowError("sandbox run was not created")
        if (
            run.run_id != current_run.run_id
            or run.incident_id != state.alert.get("incident_id")
            or run.proposal_id != proposal.proposal_id
            or not hmac.compare_digest(run.proposal_hash, proposal.proposal_hash)
            or run.action is not proposal.action
        ):
            raise ApprovalFlowError("sandbox run does not bind to the approved proposal")
        if run.status in {SandboxRunStatus.PENDING, SandboxRunStatus.RUNNING}:
            raise ApprovalFlowError("sandbox run must be terminal before recording")
        state.sandbox_run = run
        if run.status is SandboxRunStatus.SUCCEEDED:
            state.status = IncidentStatus.POST_VALIDATION
        else:
            state.status = IncidentStatus.TOOL_FAILED
            state.final_status = IncidentStatus.TOOL_FAILED
        return state

    def record_post_validation(
        self, state: IncidentState, result: PostValidationResult
    ) -> IncidentState:
        """Store post-repair validation and set the final incident status."""

        self._require_status(state, IncidentStatus.POST_VALIDATION)
        proposal = self._require_proposal(state)
        run = state.sandbox_run
        if run is None or run.status is not SandboxRunStatus.SUCCEEDED:
            raise ApprovalFlowError("post-validation requires a successful sandbox run")
        if (
            result.incident_id != state.alert.get("incident_id")
            or result.sandbox_run_id != run.run_id
            or not hmac.compare_digest(result.proposal_hash, proposal.proposal_hash)
        ):
            raise ApprovalFlowError("post-validation result does not bind to the sandbox run")
        state.repair_result = result
        if result.status.value == "passed":
            state.status = IncidentStatus.RESOLVED
            state.final_status = IncidentStatus.RESOLVED
        else:
            state.status = IncidentStatus.VALIDATION_FAILED
            state.final_status = IncidentStatus.VALIDATION_FAILED
        return state

    @staticmethod
    def _require_status(state: IncidentState, expected: IncidentStatus) -> None:
        if state.status is not expected:
            raise ApprovalFlowError(
                f"expected incident status {expected.value}, got {state.status.value}"
            )

    @staticmethod
    def _require_incident_match(state: IncidentState, incident_id: str) -> None:
        state_incident_id = state.alert.get("incident_id")
        if not isinstance(state_incident_id, str) or state_incident_id != incident_id:
            raise ApprovalFlowError("repair proposal incident does not match state alert")

    @staticmethod
    def _require_proposal(state: IncidentState) -> RepairProposal:
        if state.repair_proposal is None:
            raise ApprovalFlowError("incident has no repair proposal")
        return state.repair_proposal

    @staticmethod
    def _require_decision_match(
        proposal: RepairProposal, decision: ApprovalDecision
    ) -> None:
        if (
            decision.incident_id != proposal.incident_id
            or decision.proposal_id != proposal.proposal_id
            or not hmac.compare_digest(decision.proposal_hash, proposal.proposal_hash)
        ):
            raise ApprovalFlowError("approval decision does not bind to repair proposal")


__all__ = ["ApprovalFlow", "ApprovalFlowError"]
