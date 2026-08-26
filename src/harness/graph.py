"""Explicit, guarded orchestration for the DataSherlock Harness.

``HarnessGraph`` owns the incident state topology and the small amount of
runtime coordination needed to move through it. Planner, tool execution,
hypothesis lifecycle, and root-cause validation remain injected collaborators;
the graph does not duplicate their domain rules.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any, Final, cast

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from agents.planner import (
    InvestigationPlan,
    InvestigationStep,
    MetricContext,
    Planner,
    PlannerRunResult,
)
from harness.guardrails import (
    GuardrailDecision,
    GuardrailEventType,
    GuardrailPolicy,
    GuardrailRuntime,
)
from harness.hypothesis import (
    EvidenceReference,
    HypothesisManager,
    HypothesisState,
    HypothesisStateError,
    HypothesisStatus,
)
from harness.state import IncidentState, IncidentStatus
from tools.executor import ToolExecutionResult, ToolExecutor
from validators.root_cause_validator import (
    RootCauseValidationError,
    RootCauseValidationResult,
    RootCauseValidator,
)


class HarnessTransitionError(ValueError):
    """Raised when an incident cannot make a requested state transition."""

    def __init__(
        self,
        message: str,
        *,
        from_status: IncidentStatus | None = None,
        to_status: IncidentStatus | str | None = None,
        reason: str | None = None,
    ) -> None:
        super().__init__(message)
        self.from_status = from_status
        self.to_status = to_status
        self.reason = reason or message


class HarnessPlanningError(ValueError):
    """Raised when the injected Planner cannot produce a usable plan."""


class HarnessTransitionResult(BaseModel):
    """Stable result for one graph operation, suitable for traces/checkpoints."""

    model_config = ConfigDict(extra="forbid")

    from_status: IncidentStatus
    to_status: IncidentStatus
    changed: bool
    retry_count: int = Field(ge=0)
    terminal: bool
    reason: str | None = None


# This is the only source of truth for the incident state topology. Guards
# below may restrict an edge further, but no other module may invent one.
ALLOWED_TRANSITIONS: Final[
    dict[IncidentStatus, frozenset[IncidentStatus]]
] = {
    IncidentStatus.RECEIVED: frozenset({IncidentStatus.TRIAGE}),
    IncidentStatus.TRIAGE: frozenset({IncidentStatus.PLANNING}),
    IncidentStatus.PLANNING: frozenset(
        {
            IncidentStatus.EXECUTING,
            IncidentStatus.UNRESOLVED,
            IncidentStatus.BUDGET_EXCEEDED,
        }
    ),
    IncidentStatus.EXECUTING: frozenset(
        {
            IncidentStatus.VALIDATING,
            IncidentStatus.TOOL_FAILED,
            IncidentStatus.BUDGET_EXCEEDED,
        }
    ),
    IncidentStatus.VALIDATING: frozenset(
        {
            IncidentStatus.HYPOTHESIS_TESTING,
            IncidentStatus.VALIDATION_FAILED,
            IncidentStatus.BUDGET_EXCEEDED,
        }
    ),
    IncidentStatus.HYPOTHESIS_TESTING: frozenset(
        {
            IncidentStatus.EXECUTING,
            IncidentStatus.ROOT_CAUSE_FOUND,
            IncidentStatus.UNRESOLVED,
            IncidentStatus.BUDGET_EXCEEDED,
        }
    ),
    IncidentStatus.ROOT_CAUSE_FOUND: frozenset({IncidentStatus.FIX_PROPOSED}),
    IncidentStatus.FIX_PROPOSED: frozenset({IncidentStatus.AWAITING_APPROVAL}),
    IncidentStatus.AWAITING_APPROVAL: frozenset(
        {IncidentStatus.SANDBOX_REPAIR, IncidentStatus.REJECTED}
    ),
    IncidentStatus.SANDBOX_REPAIR: frozenset(
        {IncidentStatus.POST_VALIDATION, IncidentStatus.TOOL_FAILED}
    ),
    IncidentStatus.POST_VALIDATION: frozenset(
        {IncidentStatus.RESOLVED, IncidentStatus.VALIDATION_FAILED}
    ),
    IncidentStatus.RESOLVED: frozenset(),
    IncidentStatus.REJECTED: frozenset(),
    IncidentStatus.UNRESOLVED: frozenset(),
    IncidentStatus.BUDGET_EXCEEDED: frozenset(),
    IncidentStatus.TOOL_FAILED: frozenset(),
    IncidentStatus.VALIDATION_FAILED: frozenset(),
}

_BeforeCommit = Callable[[], None]


class HarnessGraph:
    """Apply guarded transitions and coordinate injected runtime ports."""

    def __init__(
        self,
        *,
        planner: Planner | Any | None = None,
        tool_executor: ToolExecutor | Any | None = None,
        hypothesis_manager: HypothesisManager | None = None,
        root_cause_validator: RootCauseValidator | None = None,
        guardrail_runtime: GuardrailRuntime | None = None,
        guardrail_policy: GuardrailPolicy | None = None,
    ) -> None:
        if guardrail_runtime is not None and guardrail_policy is not None:
            raise ValueError("provide guardrail_runtime or guardrail_policy, not both")
        self.planner = planner
        self.tool_executor = tool_executor
        self.hypothesis_manager = hypothesis_manager or HypothesisManager()
        self.root_cause_validator = root_cause_validator or RootCauseValidator()
        self.guardrail_runtime = guardrail_runtime or GuardrailRuntime(
            policy=guardrail_policy,
            registry=getattr(tool_executor, "registry", None),
        )

    # ------------------------------------------------------------------
    # State nodes and runtime orchestration
    # ------------------------------------------------------------------
    def triage(self, state: IncidentState, *, reason: str | None = None) -> HarnessTransitionResult:
        """Validate the minimum alert contract and enter TRIAGE."""

        return self.transition(state, IncidentStatus.TRIAGE, reason=reason)

    def prepare_plan(
        self,
        state: IncidentState,
        *,
        planner: Planner | Any | None = None,
        metric_context: MetricContext | Mapping[str, Any] | None = None,
    ) -> PlannerRunResult:
        """Run Planner in PLANNING and persist plan, hypotheses, and fallback metadata."""

        self._ensure_incident_state(state)
        self._ensure_active(state)
        if state.status is IncidentStatus.RECEIVED:
            self.transition(state, IncidentStatus.TRIAGE)
        if state.status is IncidentStatus.TRIAGE:
            self.transition(state, IncidentStatus.PLANNING)
        if state.status is not IncidentStatus.PLANNING:
            raise self._error(
                state,
                IncidentStatus.PLANNING,
                "Planner requires TRIAGE or PLANNING as the current state",
            )

        provider = planner if planner is not None else self.planner
        if provider is None:
            raise HarnessPlanningError("an injected Planner or planning port is required")

        try:
            if hasattr(provider, "run"):
                raw_result = provider.run(state.alert, metric_context)
            elif callable(provider):
                raw_result = provider(state.alert, metric_context)
            else:
                raise TypeError("planner must expose run() or be callable")
            run_result = _coerce_planner_result(raw_result)
        except HarnessPlanningError:
            raise
        except Exception as exc:
            raise HarnessPlanningError(f"Planner failed: {exc}") from exc

        manager_states: list[dict[str, JsonValue]] = []
        for hypothesis in run_result.plan.hypotheses:
            managed = self.hypothesis_manager.create_hypothesis(hypothesis)
            manager_states.append(
                cast(dict[str, JsonValue], managed.model_dump(mode="json"))
            )

        state.plan = [
            cast(dict[str, JsonValue], step.model_dump(mode="json"))
            for step in run_result.plan.steps
        ]
        state.hypotheses = manager_states
        state.planner_metadata = cast(
            dict[str, JsonValue],
            run_result.model_dump(
                mode="json",
                exclude={"plan", "model_result"},
            ),
        )
        return run_result

    def plan_incident(
        self,
        state: IncidentState,
        *,
        planner: Planner | Any | None = None,
        metric_context: MetricContext | Mapping[str, Any] | None = None,
        reason: str | None = None,
    ) -> HarnessTransitionResult:
        """Run Planner, persist its output, then open the EXECUTING node."""

        self.prepare_plan(state, planner=planner, metric_context=metric_context)
        return self.transition(state, IncidentStatus.EXECUTING, reason=reason)

    plan = plan_incident

    def execute_next_step(
        self,
        state: IncidentState,
        step: InvestigationStep | Mapping[str, Any] | None = None,
        *,
        tool_executor: ToolExecutor | Any | None = None,
        trace_id: str | None = None,
    ) -> HarnessTransitionResult:
        """Execute one planned step and enter VALIDATING or TOOL_FAILED."""

        self._ensure_incident_state(state)
        self._ensure_active(state)
        if state.status is not IncidentStatus.EXECUTING:
            raise self._error(
                state,
                IncidentStatus.EXECUTING,
                "tool execution requires EXECUTING as the current state",
            )
        selected = step if step is not None else _first_plan_step(state.plan)
        if selected is None:
            raise HarnessPlanningError("EXECUTING requires at least one planned step")

        executor = tool_executor if tool_executor is not None else self.tool_executor
        if executor is None:
            raise HarnessPlanningError("an injected ToolExecutor or execution port is required")

        decision = self.guardrail_runtime.preflight(state.guardrail_usage, selected)
        if decision.reason == "duplicate_tool_call" and _is_explicit_retry_replay(
            state, selected
        ):
            decision = self.guardrail_runtime.preflight(
                state.guardrail_usage,
                selected,
                allow_duplicate=True,
            )
        if not decision.allowed:
            self.guardrail_runtime.record_blocked(state.guardrail_usage)
            self._record_guardrail_event(
                state,
                decision,
                event_type="preflight",
                trace_id=trace_id,
                step_id=_step_id_from_step(selected),
            )
            if decision.reason in {
                "agent_round_budget_exceeded",
                "tool_call_budget_exceeded",
                "sql_call_budget_exceeded",
            }:
                return self.mark_budget_exceeded(state, reason=decision.reason)
            return self.transition(state, IncidentStatus.TOOL_FAILED, reason=decision.reason)

        self.guardrail_runtime.record_allowed(state.guardrail_usage, decision)
        self._record_guardrail_event(
            state,
            decision,
            event_type="preflight",
            trace_id=trace_id,
            step_id=_step_id_from_step(selected),
        )
        try:
            if hasattr(executor, "execute_step"):
                raw_result = executor.execute_step(
                    selected,
                    incident_id=_incident_id(state),
                    trace_id=trace_id,
                    timeout_seconds=decision.timeout_seconds,
                    max_rows=decision.max_rows,
                )
            elif callable(executor):
                raw_result = executor(selected)
            else:
                raise TypeError("tool_executor must expose execute_step() or be callable")
            result = _coerce_tool_result(raw_result)
        except Exception as exc:  # noqa: BLE001 - normalize port failures
            result = ToolExecutionResult(
                tool_name=_tool_name_from_step(selected),
                success=False,
                error={"type": "execution", "message": str(exc)},
            )

        result_payload = cast(dict[str, JsonValue], result.model_dump(mode="json"))
        for reason, message in self.guardrail_runtime.postflight(result_payload):
            self._record_guardrail_event(
                state,
                decision,
                event_type="postflight",
                trace_id=trace_id,
                step_id=_step_id_from_step(selected),
                reason=reason,
                message=message,
            )

        state.tool_trace.append(result_payload)
        state.evidence.append(_tool_observation(result_payload, len(state.tool_trace)))
        for reference in result.evidence:
            self.register_evidence(state, reference)
        target = IncidentStatus.VALIDATING if result.success else IncidentStatus.TOOL_FAILED
        return self.transition(state, target)

    execute_step = execute_next_step

    def enter_hypothesis_testing(
        self,
        state: IncidentState,
        *,
        reason: str | None = None,
    ) -> HarnessTransitionResult:
        """Enter hypothesis testing after a tool result is available."""

        return self.transition(state, IncidentStatus.HYPOTHESIS_TESTING, reason=reason)

    def register_evidence(
        self,
        state: IncidentState,
        evidence: EvidenceReference,
    ) -> EvidenceReference:
        """Register evidence with HypothesisManager and persist its snapshot."""

        registered = self.hypothesis_manager.register_evidence(evidence)
        if not any(item.get("evidence_id") == evidence.evidence_id for item in state.evidence):
            state.evidence.append(cast(dict[str, JsonValue], registered.model_dump(mode="json")))
        return registered

    def attach_evidence(
        self,
        state: IncidentState,
        hypothesis_id: str,
        evidence_id: str,
        supports: bool,
    ) -> HypothesisState:
        """Delegate hypothesis updates to HypothesisManager and sync the snapshot."""

        managed = self.hypothesis_manager.attach_evidence(hypothesis_id, evidence_id, supports)
        self._sync_hypotheses(state)
        return managed

    def validate_hypothesis(
        self,
        state: IncidentState,
        hypothesis_id: str,
        evidence: Sequence[EvidenceReference],
        *,
        resolved_contradiction_ids: Sequence[str] = (),
        reason: str | None = None,
    ) -> HarnessTransitionResult:
        """Validate one managed hypothesis and honor the validator result."""

        self._ensure_incident_state(state)
        if state.status is not IncidentStatus.HYPOTHESIS_TESTING:
            raise self._error(
                state,
                IncidentStatus.HYPOTHESIS_TESTING,
                "hypothesis validation requires HYPOTHESIS_TESTING",
            )
        for reference in evidence:
            self.register_evidence(state, reference)
        hypothesis = self.hypothesis_manager.get_hypothesis(hypothesis_id)
        result = self.root_cause_validator.validate(
            hypothesis,
            evidence,
            resolved_contradiction_ids=resolved_contradiction_ids,
        )
        transition_result = self.apply_root_cause_validation(
            state,
            result,
            resolved_contradiction_ids=resolved_contradiction_ids,
            reason=reason,
        )
        if not result.validated:
            self._sync_hypotheses(state)
        return transition_result

    def request_more_evidence(
        self,
        state: IncidentState,
        *,
        reason: str | None = None,
    ) -> HarnessTransitionResult:
        """Explicitly consume one retry and return to EXECUTING."""

        return self._transition(
            state,
            IncidentStatus.EXECUTING,
            reason=reason,
            before_commit=lambda: setattr(state, "retry_count", state.retry_count + 1),
        )

    def apply_root_cause_validation(
        self,
        state: IncidentState,
        result: RootCauseValidationResult,
        *,
        resolved_contradiction_ids: Sequence[str] = (),
        reason: str | None = None,
    ) -> HarnessTransitionResult:
        """Apply an authoritative result; FAIL remains in HYPOTHESIS_TESTING.

        The result is revalidated against the live ``HypothesisManager`` before
        any incident mutation. This prevents a caller from constructing a
        ``validated=True`` envelope for an unrelated or non-supported
        hypothesis and using it to enter ``ROOT_CAUSE_FOUND``.
        """

        self._ensure_incident_state(state)
        self._ensure_active(state)
        if state.status is not IncidentStatus.HYPOTHESIS_TESTING:
            raise self._error(
                state,
                IncidentStatus.ROOT_CAUSE_FOUND,
                "root-cause validation requires HYPOTHESIS_TESTING",
            )
        if not isinstance(result, RootCauseValidationResult):
            raise self._error(
                state,
                IncidentStatus.ROOT_CAUSE_FOUND,
                "result must be a RootCauseValidationResult instance",
            )

        expected_state = (
            IncidentStatus.ROOT_CAUSE_FOUND.value
            if result.validated
            else IncidentStatus.HYPOTHESIS_TESTING.value
        )
        if result.recommended_next_state != expected_state:
            raise self._error(
                state,
                IncidentStatus.ROOT_CAUSE_FOUND,
                "malformed root-cause validation result: validated="
                f"{result.validated!r} recommends {result.recommended_next_state!r}",
            )

        try:
            managed_hypothesis = self.hypothesis_manager.get_hypothesis(
                result.hypothesis_id
            )
        except HypothesisStateError as exc:
            raise self._error(
                state,
                IncidentStatus.ROOT_CAUSE_FOUND,
                f"validation result references an unmanaged hypothesis: {result.hypothesis_id}",
            ) from exc

        if result.validated and managed_hypothesis.status is not HypothesisStatus.SUPPORTED:
            raise self._error(
                state,
                IncidentStatus.ROOT_CAUSE_FOUND,
                "validated=True requires the current managed hypothesis to be SUPPORTED",
            )
        try:
            authoritative_result = self.root_cause_validator.validate(
                managed_hypothesis,
                self.hypothesis_manager.evidence(),
                resolved_contradiction_ids=resolved_contradiction_ids,
            )
        except RootCauseValidationError as exc:
            raise self._error(
                state,
                IncidentStatus.ROOT_CAUSE_FOUND,
                f"current managed hypothesis failed validation: {exc}",
            ) from exc
        if authoritative_result.model_dump(mode="json") != result.model_dump(mode="json"):
            raise self._error(
                state,
                IncidentStatus.ROOT_CAUSE_FOUND,
                "validation result does not match the current HypothesisManager state",
            )

        if not result.validated:
            if managed_hypothesis.status is HypothesisStatus.SUPPORTED:
                self.hypothesis_manager.return_to_testing(result.hypothesis_id)
                self._sync_hypotheses(state)
            return HarnessTransitionResult(
                from_status=state.status,
                to_status=state.status,
                changed=False,
                retry_count=state.retry_count,
                terminal=False,
                reason=reason,
            )

        root_cause: dict[str, JsonValue] = {
            "hypothesis_id": result.hypothesis_id,
            "root_cause_type": result.root_cause_type,
            "confidence": result.confidence,
            "supporting_evidence_ids": list(result.supporting_evidence_ids),
            "independent_source_types": list(result.independent_source_types),
        }
        return self._transition(
            state,
            IncidentStatus.ROOT_CAUSE_FOUND,
            reason=reason,
            validator_authorized=True,
            before_commit=lambda: setattr(state, "root_cause", root_cause),
        )

    def propose_fix(
        self,
        state: IncidentState,
        proposal: Mapping[str, JsonValue],
        *,
        reason: str | None = None,
    ) -> HarnessTransitionResult:
        """Record a repair proposal and wait for explicit approval."""

        if not isinstance(proposal, Mapping) or not proposal:
            raise self._error(state, IncidentStatus.FIX_PROPOSED, "fix proposal must be a non-empty mapping")
        self.transition(state, IncidentStatus.FIX_PROPOSED, reason=reason)
        state.fix_proposal = dict(proposal)
        return self.transition(state, IncidentStatus.AWAITING_APPROVAL, reason=reason)

    def record_approval(
        self,
        state: IncidentState,
        approved: bool,
        *,
        reason: str | None = None,
        metadata: Mapping[str, JsonValue] | None = None,
        **details: JsonValue,
    ) -> HarnessTransitionResult:
        """Record an approval decision and enter repair or REJECTED."""

        if not isinstance(approved, bool):
            raise self._error(state, IncidentStatus.SANDBOX_REPAIR, "approved must be a boolean")
        payload: dict[str, JsonValue] = {"status": "approved" if approved else "rejected"}
        if reason is not None:
            payload["reason"] = reason
        if metadata is not None:
            payload.update(dict(metadata))
        payload.update(details)
        payload["status"] = "approved" if approved else "rejected"
        target = IncidentStatus.SANDBOX_REPAIR if approved else IncidentStatus.REJECTED
        return self._transition(
            state,
            target,
            reason=reason,
            approval_approved=approved,
            before_commit=lambda: setattr(state, "approval", payload),
        )

    def record_repair_result(
        self,
        state: IncidentState,
        succeeded: bool | Mapping[str, JsonValue] | None = None,
        *,
        success: bool | None = None,
        fatal: bool | None = None,
        result: Mapping[str, JsonValue] | None = None,
        reason: str | None = None,
        **details: JsonValue,
    ) -> HarnessTransitionResult:
        """Record a repair outcome without implementing a repair engine."""

        if isinstance(succeeded, Mapping):
            if result is not None:
                raise self._error(state, IncidentStatus.POST_VALIDATION, "repair result was provided more than once")
            result = succeeded
            succeeded = None

        outcome_values = [value for value in (succeeded, success) if value is not None]
        if fatal is not None:
            if not isinstance(fatal, bool):
                raise self._error(state, IncidentStatus.POST_VALIDATION, "fatal must be a boolean")
            outcome_values.append(not fatal)
        if not outcome_values and result is not None:
            result_status = result.get("status")
            result_success = result.get("success")
            result_succeeded = result.get("succeeded")
            if isinstance(result_success, bool):
                outcome_values.append(result_success)
            elif isinstance(result_succeeded, bool):
                outcome_values.append(result_succeeded)
            elif result_status in ("succeeded", "success", "ok", "passed"):
                outcome_values.append(True)
            elif result_status in ("failed", "failure", "fatal"):
                outcome_values.append(False)
        if not outcome_values:
            raise self._error(
                state,
                IncidentStatus.POST_VALIDATION,
                "record_repair_result requires succeeded, success, or fatal",
            )
        if any(not isinstance(value, bool) for value in outcome_values):
            raise self._error(state, IncidentStatus.POST_VALIDATION, "repair outcome must be boolean")
        if len(set(outcome_values)) != 1:
            raise self._error(state, IncidentStatus.POST_VALIDATION, "repair outcome arguments disagree")
        repair_succeeded = outcome_values[0]
        payload: dict[str, JsonValue] = dict(result or {})
        payload.update(details)
        payload["status"] = "succeeded" if repair_succeeded else "failed"
        target = IncidentStatus.POST_VALIDATION if repair_succeeded else IncidentStatus.TOOL_FAILED
        return self._transition(
            state,
            target,
            reason=reason,
            repair_succeeded=repair_succeeded,
            before_commit=lambda: setattr(state, "repair_result", payload),
        )

    def record_post_validation_result(
        self,
        state: IncidentState,
        validated: bool,
        *,
        reason: str | None = None,
    ) -> HarnessTransitionResult:
        """Resolve a repaired incident or emit terminal validation failure."""

        if not isinstance(validated, bool):
            raise self._error(state, IncidentStatus.RESOLVED, "validated must be a boolean")
        target = IncidentStatus.RESOLVED if validated else IncidentStatus.VALIDATION_FAILED
        return self._transition(state, target, reason=reason, fatal=not validated)

    def mark_budget_exceeded(
        self,
        state: IncidentState,
        *,
        reason: str | None = None,
    ) -> HarnessTransitionResult:
        """Accept an explicit external budget event where the graph allows it."""

        return self._transition(state, IncidentStatus.BUDGET_EXCEEDED, reason=reason)

    # ------------------------------------------------------------------
    # Single transition entry point and guards
    # ------------------------------------------------------------------
    def transition(
        self,
        state: IncidentState,
        target: IncidentStatus,
        *,
        reason: str | None = None,
    ) -> HarnessTransitionResult:
        """Apply one public transition after topology and node guards."""

        return self._transition(state, target, reason=reason)

    def _transition(
        self,
        state: IncidentState,
        target: IncidentStatus,
        *,
        reason: str | None,
        validator_authorized: bool = False,
        approval_approved: bool | None = None,
        repair_succeeded: bool | None = None,
        fatal: bool = False,
        before_commit: _BeforeCommit | None = None,
    ) -> HarnessTransitionResult:
        self._ensure_incident_state(state)
        current = state.status
        try:
            target = self._coerce_status(target)
        except HarnessTransitionError as exc:
            exc.from_status = current
            raise
        self._ensure_active(state, target=target)

        allowed = ALLOWED_TRANSITIONS[current]
        if target not in allowed:
            raise self._error(state, target, f"illegal incident transition: {current.value} -> {target.value}")
        if target is IncidentStatus.ROOT_CAUSE_FOUND and not validator_authorized:
            raise self._error(state, target, "ROOT_CAUSE_FOUND can only be entered through apply_root_cause_validation")
        if target is IncidentStatus.VALIDATION_FAILED and not fatal:
            raise self._error(state, target, "VALIDATION_FAILED requires an explicit fatal validation condition")

        self._check_guards(
            state,
            current,
            target,
            approval_approved=approval_approved,
            repair_succeeded=repair_succeeded,
        )
        if before_commit is not None:
            before_commit()
        state.status = target
        if target.is_terminal:
            state.final_status = target
        return HarnessTransitionResult(
            from_status=current,
            to_status=target,
            changed=current is not target,
            retry_count=state.retry_count,
            terminal=target.is_terminal,
            reason=reason,
        )

    @staticmethod
    def _ensure_incident_state(state: IncidentState) -> None:
        if not isinstance(state, IncidentState):
            raise HarnessTransitionError("state must be an IncidentState instance")

    @staticmethod
    def _ensure_active(
        state: IncidentState,
        *,
        target: IncidentStatus | str | None = None,
    ) -> None:
        if state.status.is_terminal:
            raise HarnessTransitionError(
                f"terminal incident state cannot transition: {state.status.value}",
                from_status=state.status,
                to_status=target,
                reason="terminal incident state cannot transition",
            )
        if state.final_status is not None:
            raise HarnessTransitionError(
                "non-terminal incident state must not have final_status",
                from_status=state.status,
                to_status=target,
                reason="non-terminal incident state must not have final_status",
            )

    @staticmethod
    def _coerce_status(target: IncidentStatus) -> IncidentStatus:
        if isinstance(target, IncidentStatus):
            return target
        try:
            return IncidentStatus(target)
        except (TypeError, ValueError) as exc:
            raise HarnessTransitionError(
                f"unknown incident status: {target!r}",
                to_status=target,
                reason="unknown incident status",
            ) from exc

    @classmethod
    def _check_guards(
        cls,
        state: IncidentState,
        current: IncidentStatus,
        target: IncidentStatus,
        *,
        approval_approved: bool | None,
        repair_succeeded: bool | None,
    ) -> None:
        if current is IncidentStatus.RECEIVED and target is IncidentStatus.TRIAGE:
            try:
                _validate_minimum_alert(state.alert)
            except HarnessTransitionError as exc:
                exc.from_status = current
                exc.to_status = target
                raise
        elif current is IncidentStatus.TRIAGE and target is IncidentStatus.PLANNING:
            try:
                _validate_mvp_scope(state.alert)
            except HarnessTransitionError as exc:
                exc.from_status = current
                exc.to_status = target
                raise
        elif current is IncidentStatus.PLANNING and target is IncidentStatus.EXECUTING:
            if not state.plan:
                raise cls._error(state, target, "PLANNING -> EXECUTING requires a non-empty plan")
        elif current is IncidentStatus.EXECUTING and target is IncidentStatus.VALIDATING:
            if not state.tool_trace and not state.evidence:
                raise cls._error(state, target, "EXECUTING -> VALIDATING requires tool_trace or evidence")
        elif current is IncidentStatus.ROOT_CAUSE_FOUND and target is IncidentStatus.FIX_PROPOSED:
            if state.root_cause is None:
                raise cls._error(state, target, "ROOT_CAUSE_FOUND -> FIX_PROPOSED requires root_cause")
        elif current is IncidentStatus.FIX_PROPOSED and target is IncidentStatus.AWAITING_APPROVAL:
            if not state.fix_proposal:
                raise cls._error(state, target, "FIX_PROPOSED -> AWAITING_APPROVAL requires fix_proposal")
        elif current is IncidentStatus.AWAITING_APPROVAL and target is IncidentStatus.SANDBOX_REPAIR:
            approved = (
                approval_approved
                if approval_approved is not None
                else bool(state.approval) and state.approval.get("status") == "approved"
            )
            if not approved:
                raise cls._error(state, target, "SANDBOX_REPAIR requires an approved approval payload")
        elif current is IncidentStatus.AWAITING_APPROVAL and target is IncidentStatus.REJECTED:
            rejected = (
                approval_approved is False
                or bool(state.approval) and state.approval.get("status") == "rejected"
            )
            if not rejected:
                raise cls._error(state, target, "REJECTED requires an explicit rejected approval")
        elif current is IncidentStatus.SANDBOX_REPAIR and target is IncidentStatus.POST_VALIDATION:
            if repair_succeeded is not True and not _repair_payload_succeeded(state):
                raise cls._error(state, target, "SANDBOX_REPAIR -> POST_VALIDATION requires a successful repair result")
        elif (
            current is IncidentStatus.POST_VALIDATION
            and target is IncidentStatus.RESOLVED
            and not state.repair_result
        ):
            raise cls._error(state, target, "POST_VALIDATION -> RESOLVED requires repair_result")

    @staticmethod
    def _error(
        state: IncidentState,
        target: IncidentStatus | str | None,
        reason: str,
    ) -> HarnessTransitionError:
        return HarnessTransitionError(
            reason,
            from_status=state.status if isinstance(state, IncidentState) else None,
            to_status=target,
            reason=reason,
        )

    def _sync_hypotheses(self, state: IncidentState) -> None:
        state.hypotheses = [
            cast(dict[str, JsonValue], item.model_dump(mode="json"))
            for item in self.hypothesis_manager.hypotheses()
        ]

    def _record_guardrail_event(
        self,
        state: IncidentState,
        decision: GuardrailDecision,
        *,
        event_type: GuardrailEventType,
        trace_id: str | None,
        step_id: str | None,
        reason: str | None = None,
        message: str | None = None,
    ) -> None:
        event = self.guardrail_runtime.event(
            state.guardrail_usage,
            decision,
            event_type=event_type,
            incident_id=_incident_id(state),
            trace_id=trace_id,
            step_id=step_id,
            sequence=len(state.guardrail_events) + 1,
            reason=reason,
            message=message,
        )
        state.guardrail_events.append(event)


def _validate_minimum_alert(alert: Mapping[str, JsonValue]) -> None:
    required = ("incident_id", "metric", "observed_at")
    missing = [field for field in required if not _has_text(alert.get(field))]
    if missing:
        raise HarnessTransitionError(
            "RECEIVED -> TRIAGE requires alert fields: " + ", ".join(missing),
            reason="minimum alert contract is incomplete",
        )


def _validate_mvp_scope(alert: Mapping[str, JsonValue]) -> None:
    scope_fields = ("scope", "category", "alert_type", "anomaly_type", "domain", "kind")
    values = [str(alert[field]).strip().lower() for field in scope_fields if _has_text(alert.get(field))]
    if not values:
        return
    supported_tokens = ("metric", "pipeline", "data_quality", "data quality", "anomaly")
    if not any(any(token in value for token in supported_tokens) for value in values):
        raise HarnessTransitionError(
            "MVP triage scope: TRIAGE -> PLANNING only supports metric, data pipeline, or data quality anomalies",
            reason="incident is outside the MVP triage scope",
        )


def _has_text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _coerce_planner_result(raw_result: object) -> PlannerRunResult:
    if isinstance(raw_result, PlannerRunResult):
        return raw_result
    if isinstance(raw_result, InvestigationPlan):
        return PlannerRunResult(plan=raw_result)
    if hasattr(raw_result, "plan"):
        plan = raw_result.plan
        if isinstance(plan, InvestigationPlan):
            payload: dict[str, Any] = {"plan": plan}
            for name in (
                "fallback_used",
                "fallback_reason",
                "planner_repair_count",
                "transport_retry_count",
                "model_latency_ms",
                "provider",
                "model",
            ):
                if hasattr(raw_result, name):
                    payload[name] = getattr(raw_result, name)
            return PlannerRunResult.model_validate(payload)
    raise HarnessPlanningError("planning port must return InvestigationPlan or PlannerRunResult")


def _coerce_tool_result(raw_result: object) -> ToolExecutionResult:
    if isinstance(raw_result, ToolExecutionResult):
        return raw_result
    return ToolExecutionResult.model_validate(raw_result)


def _first_plan_step(plan: Sequence[Mapping[str, JsonValue]]) -> Mapping[str, JsonValue] | None:
    return plan[0] if plan else None


def _incident_id(state: IncidentState) -> str | None:
    value = state.alert.get("incident_id")
    return value if isinstance(value, str) else None


def _tool_name_from_step(step: object) -> str:
    if isinstance(step, InvestigationStep):
        return step.tool
    if isinstance(step, Mapping):
        value = step.get("tool")
        if isinstance(value, str):
            return value
    return "unknown"


def _step_id_from_step(step: object) -> str | None:
    if isinstance(step, InvestigationStep):
        return step.step_id
    if isinstance(step, Mapping):
        value = step.get("step_id")
        return value if isinstance(value, str) else None
    return None


def _is_explicit_retry_replay(
    state: IncidentState,
    step: InvestigationStep | Mapping[str, JsonValue],
) -> bool:
    """Allow an explicit retry to rerun its same planned step once authorized.

    A new planned step with the same tool and arguments remains a duplicate.
    This preserves the graph's existing retry semantics without weakening the
    runtime fingerprint contract for direct or cross-step calls.
    """

    if state.retry_count <= 0:
        return False
    step_id = _step_id_from_step(step)
    if step_id is None:
        return False
    for event in reversed(state.guardrail_events):
        if event.event_type == "preflight":
            return event.allowed and event.step_id == step_id
    return False


def _tool_observation(
    result: Mapping[str, JsonValue],
    ordinal: int,
) -> dict[str, JsonValue]:
    """Record a result as an observation without calling it root-cause proof."""

    query_id = result.get("query_id")
    evidence_id = (
        query_id
        if isinstance(query_id, str) and query_id
        else f"tool-observation-{ordinal}"
    )
    return {
        "evidence_id": evidence_id,
        "evidence_type": "tool_result",
        "tool_name": result.get("tool_name", "unknown"),
        "query_id": query_id,
        "success": result.get("success", False),
        "result": result.get("result"),
        "error": result.get("error"),
        "root_cause_validated": False,
    }


def _repair_payload_succeeded(state: IncidentState) -> bool:
    if state.repair_result is None:
        return False
    status = state.repair_result.get("status")
    return (
        status in ("succeeded", "success", "ok", "passed")
        or state.repair_result.get("success") is True
        or state.repair_result.get("succeeded") is True
    )


__all__ = [
    "ALLOWED_TRANSITIONS",
    "HarnessGraph",
    "HarnessPlanningError",
    "HarnessTransitionError",
    "HarnessTransitionResult",
]
