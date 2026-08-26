"""End-to-end orchestration for approved sandbox repairs."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any
from uuid import uuid4

from harness.approval import ApprovalFlow
from harness.post_validation import PostRepairValidationError, PostRepairValidator
from harness.repair import SandboxRun, SandboxRunStatus
from harness.sandbox_repair import SandboxRepairError, SandboxRepairExecutor
from harness.state import IncidentState, IncidentStatus


class RepairWorkflowError(ValueError):
    """Raised before an approved repair can safely start."""


class RepairWorkflowService:
    """Run one already-approved repair from sandbox creation through validation."""

    def __init__(
        self,
        sandbox_executor: SandboxRepairExecutor,
        post_repair_validator: PostRepairValidator,
        *,
        approval_flow: ApprovalFlow | None = None,
    ) -> None:
        self._sandbox_executor = sandbox_executor
        self._post_repair_validator = post_repair_validator
        self._approval_flow = approval_flow or ApprovalFlow()

    def execute_approved_repair(
        self,
        state: IncidentState,
        *,
        run_id: str | None = None,
        allowed_relative_error: float = 0.05,
        regression_metric_ids: tuple[str, ...] = (),
        max_regression_ratio: float = 0.05,
        validation_id: str | None = None,
    ) -> IncidentState:
        """Execute the approved proposal and set one final incident outcome.

        The target metric, observed date, and expected value come only from the
        saved alert. API and UI callers cannot substitute those values after a
        reviewer has approved the repair proposal.
        """

        metric_id, metric_date, expected_value = self._alert_validation_inputs(state)
        pending_run = self._approval_flow.create_sandbox_run(
            state,
            run_id=run_id or f"SR-{uuid4()}",
            sandbox_path="managed-by-sandbox-executor",
        )
        try:
            completed_run = self._sandbox_executor.execute(
                state.repair_proposal,
                pending_run,
            )
        except SandboxRepairError as exc:
            completed_run = self._failed_run(pending_run, str(exc))
        self._record_sandbox_trace(state, completed_run)
        self._approval_flow.record_sandbox_run(state, completed_run)
        if completed_run.status is not SandboxRunStatus.SUCCEEDED:
            return state

        try:
            result = self._post_repair_validator.validate(
                state.repair_proposal,
                completed_run,
                metric_id=metric_id,
                metric_date=metric_date,
                expected_value=expected_value,
                allowed_relative_error=allowed_relative_error,
                regression_metric_ids=regression_metric_ids,
                max_regression_ratio=max_regression_ratio,
                validation_id=validation_id,
            )
        except PostRepairValidationError as exc:
            self._record_validation_error(state, str(exc))
            state.status = IncidentStatus.VALIDATION_FAILED
            state.final_status = IncidentStatus.VALIDATION_FAILED
            return state

        self._record_validation_trace(state, result.model_dump(mode="json"))
        return self._approval_flow.record_post_validation(state, result)

    @staticmethod
    def _alert_validation_inputs(state: IncidentState) -> tuple[str, date, float]:
        metric_id = state.alert.get("metric")
        observed_at = state.alert.get("observed_at")
        expected_value = state.alert.get("expected_value")
        if not isinstance(metric_id, str) or not metric_id.strip():
            raise RepairWorkflowError("alert.metric must be a non-empty string")
        if not isinstance(observed_at, str):
            raise RepairWorkflowError("alert.observed_at must be an ISO date string")
        try:
            metric_date = date.fromisoformat(observed_at)
        except ValueError as exc:
            raise RepairWorkflowError("alert.observed_at must be an ISO date string") from exc
        if isinstance(expected_value, bool) or not isinstance(expected_value, (int, float)):
            raise RepairWorkflowError("alert.expected_value must be numeric")
        return metric_id, metric_date, float(expected_value)

    @staticmethod
    def _failed_run(pending_run: SandboxRun, error: str) -> SandboxRun:
        now = datetime.now(UTC)
        return SandboxRun.model_validate(
            {
                **pending_run.model_dump(),
                "status": SandboxRunStatus.FAILED,
                "started_at": now,
                "finished_at": now,
                "error": f"SandboxRepairError: {error}",
            }
        )

    @staticmethod
    def _record_sandbox_trace(state: IncidentState, run: SandboxRun) -> None:
        state.tool_trace.append(
            {
                "trace_id": f"sandbox:{run.run_id}",
                "tool": "sandbox_repair_executor",
                "status": run.status.value,
                "run": run.model_dump(mode="json"),
            }
        )

    @staticmethod
    def _record_validation_trace(state: IncidentState, result: dict[str, Any]) -> None:
        state.tool_trace.append(
            {
                "trace_id": f"post-validation:{result['validation_id']}",
                "tool": "post_repair_validator",
                "status": result["status"],
                "result": result,
            }
        )

    @staticmethod
    def _record_validation_error(state: IncidentState, error: str) -> None:
        state.tool_trace.append(
            {
                "trace_id": f"post-validation:error:{uuid4()}",
                "tool": "post_repair_validator",
                "status": "failed",
                "error": error,
            }
        )


__all__ = ["RepairWorkflowError", "RepairWorkflowService"]
