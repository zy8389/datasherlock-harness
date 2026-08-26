"""Runtime guardrails for bounded investigation tool calls.

The guardrail runtime owns call authorization and usage accounting.  It does
not execute tools or interpret their results as root-cause evidence; those
responsibilities remain with ``ToolExecutor`` and the hypothesis validator.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from agents.planner import InvestigationStep
from tools.registry import ToolRegistry, ToolRegistryError, build_default_tool_registry
from tools.sql_runner import SqlRunnerError, validate_readonly_sql


class GuardrailPolicy(BaseModel):
    """Positive runtime limits for one investigation execution."""

    model_config = ConfigDict(extra="forbid", strict=True)

    max_agent_rounds: int = Field(default=20, gt=0)
    max_sql_calls: int = Field(default=15, gt=0)
    max_tool_calls: int = Field(default=20, gt=0)
    tool_timeout_seconds: float = Field(default=30.0, gt=0)
    max_result_rows: int = Field(default=1000, gt=0)
    max_duplicate_calls: int = Field(default=1, gt=0)
    max_repair_retries: int = Field(default=2, ge=0)

    @field_validator("tool_timeout_seconds")
    @classmethod
    def require_finite_timeout(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("tool_timeout_seconds must be finite")
        return value


class GuardrailUsage(BaseModel):
    """Authoritative, checkpoint-safe counters for the guardrail runtime."""

    model_config = ConfigDict(extra="forbid", strict=True)

    agent_rounds: int = Field(default=0, ge=0)
    tool_calls: int = Field(default=0, ge=0)
    sql_calls: int = Field(default=0, ge=0)
    blocked_calls: int = Field(default=0, ge=0)
    executed_fingerprints: list[str] = Field(default_factory=list)
    fingerprint_counts: dict[str, int] = Field(default_factory=dict)

    @field_validator("executed_fingerprints")
    @classmethod
    def require_string_fingerprints(cls, value: list[str]) -> list[str]:
        if any(not fingerprint for fingerprint in value):
            raise ValueError("executed_fingerprints must contain non-empty strings")
        return value

    @field_validator("fingerprint_counts")
    @classmethod
    def require_non_negative_fingerprint_counts(
        cls, value: dict[str, int]
    ) -> dict[str, int]:
        if any(not fingerprint or count < 0 for fingerprint, count in value.items()):
            raise ValueError("fingerprint_counts must contain non-negative counts")
        return value


class GuardrailViolation(BaseModel):
    """Structured reason why a planned call was not authorized."""

    model_config = ConfigDict(extra="forbid", strict=True)

    reason: str = Field(min_length=1)
    message: str = Field(min_length=1)
    tool_name: str | None = None
    fingerprint: str | None = None


class GuardrailDecision(BaseModel):
    """Result of a guardrail preflight check."""

    model_config = ConfigDict(extra="forbid", strict=True)

    allowed: bool
    tool_name: str = Field(min_length=1)
    fingerprint: str | None = None
    reason: str | None = None
    message: str | None = None
    timeout_seconds: float | None = None
    max_rows: int | None = None
    violation: GuardrailViolation | None = None


GuardrailEventType = Literal["preflight", "postflight"]


class GuardrailEvent(BaseModel):
    """Traceable event persisted with the incident checkpoint."""

    model_config = ConfigDict(extra="forbid", strict=True)

    event_id: str = Field(min_length=1)
    event_type: GuardrailEventType
    incident_id: str | None = None
    trace_id: str | None = None
    step_id: str | None = None
    tool_name: str = Field(min_length=1)
    allowed: bool
    reason: str | None = None
    message: str | None = None
    fingerprint: str | None = None
    agent_rounds: int = Field(ge=0)
    tool_calls: int = Field(ge=0)
    sql_calls: int = Field(ge=0)
    blocked_calls: int = Field(ge=0)


class GuardrailRuntime:
    """Authorize bounded calls against a registry and a persisted usage model."""

    def __init__(
        self,
        policy: GuardrailPolicy | None = None,
        *,
        registry: ToolRegistry | None = None,
    ) -> None:
        self.policy = policy or GuardrailPolicy()
        self.registry = registry or build_default_tool_registry()

    def preflight(
        self,
        usage: GuardrailUsage,
        step: InvestigationStep | Mapping[str, Any],
        *,
        allow_duplicate: bool = False,
    ) -> GuardrailDecision:
        """Fail closed before execution and return runtime parameters."""

        normalized, tool_name, error = _normalize_step(step)
        if normalized is None:
            return self._blocked(
                tool_name,
                reason="invalid_tool_contract",
                message=error or "step is not a valid InvestigationStep",
            )

        try:
            definition = self.registry.get(normalized.tool)
            self.registry.validate_arguments(normalized.tool, normalized.arguments)
        except ToolRegistryError as exc:
            reason = "unknown_tool" if not self.registry.contains(normalized.tool) else "invalid_tool_contract"
            return self._blocked(normalized.tool, reason=reason, message=str(exc))

        if not definition.read_only:
            return self._blocked(
                normalized.tool,
                reason="non_read_only_tool",
                message=f"tool is not read-only: {normalized.tool}",
            )

        if normalized.tool == "sql_query":
            sql = normalized.arguments.get("sql")
            try:
                validate_readonly_sql(sql)
            except (SqlRunnerError, TypeError, ValueError) as exc:
                return self._blocked(
                    normalized.tool,
                    reason="unsafe_sql",
                    message=str(exc),
                )

        fingerprint = fingerprint_step(normalized)
        if usage.agent_rounds >= self.policy.max_agent_rounds:
            return self._blocked(
                normalized.tool,
                reason="agent_round_budget_exceeded",
                message="agent round budget exceeded",
                fingerprint=fingerprint,
            )
        if usage.tool_calls >= self.policy.max_tool_calls:
            return self._blocked(
                normalized.tool,
                reason="tool_call_budget_exceeded",
                message="tool call budget exceeded",
                fingerprint=fingerprint,
            )
        if normalized.tool == "sql_query" and usage.sql_calls >= self.policy.max_sql_calls:
            return self._blocked(
                normalized.tool,
                reason="sql_call_budget_exceeded",
                message="SQL call budget exceeded",
                fingerprint=fingerprint,
            )

        previous_calls = usage.fingerprint_counts.get(fingerprint, 0)
        if previous_calls >= self.policy.max_duplicate_calls and not allow_duplicate:
            return self._blocked(
                normalized.tool,
                reason="duplicate_tool_call",
                message="the exact tool call has already reached its duplicate limit",
                fingerprint=fingerprint,
            )

        return GuardrailDecision(
            allowed=True,
            tool_name=normalized.tool,
            fingerprint=fingerprint,
            timeout_seconds=self.policy.tool_timeout_seconds,
            max_rows=self.policy.max_result_rows,
        )

    def record_allowed(self, usage: GuardrailUsage, decision: GuardrailDecision) -> None:
        """Commit one authorized call to the checkpoint-owned usage counters."""

        if not decision.allowed or not decision.fingerprint:
            raise ValueError("only an allowed decision with a fingerprint can be committed")
        usage.agent_rounds += 1
        usage.tool_calls += 1
        if decision.tool_name == "sql_query":
            usage.sql_calls += 1
        previous_calls = usage.fingerprint_counts.get(decision.fingerprint, 0)
        usage.fingerprint_counts[decision.fingerprint] = previous_calls + 1
        if decision.fingerprint not in usage.executed_fingerprints:
            usage.executed_fingerprints.append(decision.fingerprint)

    def record_blocked(self, usage: GuardrailUsage) -> None:
        """Record a blocked attempt without consuming execution budgets."""

        usage.blocked_calls += 1

    def postflight(
        self,
        result: Mapping[str, Any],
    ) -> tuple[tuple[str, str], ...]:
        """Return auditable result-level guardrail observations."""

        observations: list[tuple[str, str]] = []
        error = result.get("error")
        if isinstance(error, Mapping) and error.get("type") == "timeout":
            observations.append(("tool_timeout", "tool execution exceeded the configured timeout"))
        result_payload = result.get("result")
        if isinstance(result_payload, Mapping) and result_payload.get("truncated") is True:
            observations.append(("result_truncated", "tool result was truncated at the configured row limit"))
        return tuple(observations)

    def event(
        self,
        usage: GuardrailUsage,
        decision: GuardrailDecision,
        *,
        event_type: GuardrailEventType,
        incident_id: str | None = None,
        trace_id: str | None = None,
        step_id: str | None = None,
        sequence: int,
        reason: str | None = None,
        message: str | None = None,
    ) -> GuardrailEvent:
        """Build a deterministic event envelope from the decision and usage."""

        resolved_reason = reason or decision.reason
        event_material = {
            "event_type": event_type,
            "incident_id": incident_id,
            "trace_id": trace_id,
            "step_id": step_id,
            "sequence": sequence,
            "tool_name": decision.tool_name,
            "allowed": decision.allowed,
            "reason": resolved_reason,
            "fingerprint": decision.fingerprint,
        }
        event_id = "gr-" + hashlib.sha256(
            json.dumps(event_material, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()[:24]
        return GuardrailEvent(
            event_id=event_id,
            event_type=event_type,
            incident_id=incident_id,
            trace_id=trace_id,
            step_id=step_id,
            tool_name=decision.tool_name,
            allowed=decision.allowed,
            reason=resolved_reason,
            message=message or decision.message,
            fingerprint=decision.fingerprint,
            agent_rounds=usage.agent_rounds,
            tool_calls=usage.tool_calls,
            sql_calls=usage.sql_calls,
            blocked_calls=usage.blocked_calls,
        )

    def _blocked(
        self,
        tool_name: str,
        *,
        reason: str,
        message: str,
        fingerprint: str | None = None,
    ) -> GuardrailDecision:
        violation = GuardrailViolation(
            reason=reason,
            message=message,
            tool_name=tool_name,
            fingerprint=fingerprint,
        )
        return GuardrailDecision(
            allowed=False,
            tool_name=tool_name or "invalid",
            fingerprint=fingerprint,
            reason=reason,
            message=message,
            violation=violation,
        )


def fingerprint_step(step: InvestigationStep | Mapping[str, Any]) -> str:
    """Return a deterministic identity for a validated tool call."""

    normalized = (
        step
        if isinstance(step, InvestigationStep)
        else InvestigationStep.model_validate(step)
    )
    material = {"tool": normalized.tool, "arguments": normalized.arguments}
    return hashlib.sha256(
        json.dumps(material, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _normalize_step(
    step: InvestigationStep | Mapping[str, Any],
) -> tuple[InvestigationStep | None, str, str | None]:
    tool_name = "invalid"
    if isinstance(step, Mapping):
        value = step.get("tool")
        if isinstance(value, str) and value.strip():
            tool_name = value
    try:
        normalized = (
            step
            if isinstance(step, InvestigationStep)
            else InvestigationStep.model_validate(step)
        )
    except (TypeError, ValueError) as exc:
        return None, tool_name, str(exc)
    return normalized, normalized.tool, None


__all__ = [
    "GuardrailDecision",
    "GuardrailEvent",
    "GuardrailEventType",
    "GuardrailPolicy",
    "GuardrailRuntime",
    "GuardrailUsage",
    "GuardrailViolation",
    "fingerprint_step",
]
