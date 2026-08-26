import json
import math

import pytest
from pydantic import ValidationError

from agents.planner import InvestigationStep
from harness.guardrails import (
    GuardrailPolicy,
    GuardrailRuntime,
    GuardrailUsage,
    fingerprint_step,
)
from harness.state import IncidentState
from tools.registry import ToolDefinition, ToolRegistry


def _step(
    tool: str = "sql_query",
    arguments: dict[str, object] | None = None,
    *,
    step_id: str = "S01",
) -> InvestigationStep:
    return InvestigationStep(
        step_id=step_id,
        purpose="Run one bounded investigation action.",
        hypothesis_id="H01",
        tool=tool,
        arguments=arguments if arguments is not None else {"sql": "SELECT 1"},
        expected_evidence=["the structured result"],
        stop_condition="retain the result",
    )


def test_guardrail_policy_defaults_are_bounded() -> None:
    policy = GuardrailPolicy()

    assert policy.max_agent_rounds == 20
    assert policy.max_sql_calls == 15
    assert policy.max_tool_calls == 20
    assert policy.tool_timeout_seconds == 30.0
    assert policy.max_result_rows == 1000
    assert policy.max_duplicate_calls == 1
    assert policy.max_repair_retries == 2


@pytest.mark.parametrize(
    "field",
    [
        "max_agent_rounds",
        "max_sql_calls",
        "max_tool_calls",
        "max_result_rows",
        "max_duplicate_calls",
    ],
)
@pytest.mark.parametrize("value", [0, -1])
def test_guardrail_policy_rejects_non_positive_limits(field: str, value: int) -> None:
    with pytest.raises(ValidationError):
        GuardrailPolicy(**{field: value})


@pytest.mark.parametrize("value", [0, -1, math.nan, math.inf, -math.inf])
def test_guardrail_policy_rejects_invalid_timeout(value: float) -> None:
    with pytest.raises(ValidationError):
        GuardrailPolicy(tool_timeout_seconds=value)


def test_guardrail_policy_allows_zero_repair_retries_but_rejects_negative() -> None:
    assert GuardrailPolicy(max_repair_retries=0).max_repair_retries == 0
    with pytest.raises(ValidationError):
        GuardrailPolicy(max_repair_retries=-1)


def test_agent_round_budget_blocks_without_consuming_another_call() -> None:
    runtime = GuardrailRuntime(GuardrailPolicy(max_agent_rounds=1))
    usage = GuardrailUsage()

    first = runtime.preflight(usage, _step())
    assert first.allowed is True
    runtime.record_allowed(usage, first)

    second = runtime.preflight(usage, _step(arguments={"sql": "SELECT 2"}))
    assert second.allowed is False
    assert second.reason == "agent_round_budget_exceeded"
    runtime.record_blocked(usage)
    assert usage.agent_rounds == 1
    assert usage.tool_calls == 1
    assert usage.sql_calls == 1
    assert usage.blocked_calls == 1


def test_sql_budget_only_counts_planner_sql_calls() -> None:
    runtime = GuardrailRuntime(GuardrailPolicy(max_sql_calls=1))
    usage = GuardrailUsage()

    first = runtime.preflight(usage, _step(arguments={"sql": "SELECT 1"}))
    assert first.allowed is True
    runtime.record_allowed(usage, first)

    second = runtime.preflight(usage, _step(arguments={"sql": "SELECT 2"}))
    assert second.allowed is False
    assert second.reason == "sql_call_budget_exceeded"
    assert usage.sql_calls == 1


def test_tool_budget_counts_data_quality_calls_as_tool_calls() -> None:
    runtime = GuardrailRuntime(GuardrailPolicy(max_tool_calls=1))
    usage = GuardrailUsage()
    first_step = _step(
        "check_null_rate",
        {"table": "events", "column": "user_id"},
    )
    second_step = _step(
        "check_duplicate_rate",
        {"table": "events", "keys": ["event_id"]},
        step_id="S02",
    )

    first = runtime.preflight(usage, first_step)
    assert first.allowed is True
    runtime.record_allowed(usage, first)
    second = runtime.preflight(usage, second_step)

    assert second.allowed is False
    assert second.reason == "tool_call_budget_exceeded"
    assert usage.sql_calls == 0


def test_duplicate_policy_is_deterministic_and_argument_sensitive() -> None:
    runtime = GuardrailRuntime(GuardrailPolicy(max_duplicate_calls=1))
    usage = GuardrailUsage()
    first_step = _step("check_null_rate", {"table": "events", "column": "user_id"})

    first = runtime.preflight(usage, first_step)
    assert first.allowed is True
    runtime.record_allowed(usage, first)

    duplicate = runtime.preflight(usage, first_step)
    different_arguments = runtime.preflight(
        usage,
        _step(
            "check_null_rate",
            {"table": "events", "column": "session_id"},
            step_id="S02",
        ),
    )
    assert duplicate.allowed is False
    assert duplicate.reason == "duplicate_tool_call"
    assert different_arguments.allowed is True
    assert len(usage.executed_fingerprints) == 1

    reordered = _step("sql_query", {"sql": "SELECT 1"}, step_id="S03")
    reordered_payload = reordered.model_dump(mode="json")
    reordered_payload["arguments"] = {"sql": "SELECT 1"}
    assert fingerprint_step(reordered) == fingerprint_step(reordered_payload)


def test_guardrail_rejects_unsafe_unknown_malformed_and_non_read_only_calls() -> None:
    runtime = GuardrailRuntime()
    usage = GuardrailUsage()

    unsafe = runtime.preflight(usage, _step(arguments={"sql": "DELETE FROM events"}))
    unknown = runtime.preflight(usage, _step("missing_tool", {"value": 1}))
    malformed = runtime.preflight(usage, {})
    assert unsafe.reason == "unsafe_sql"
    assert unknown.reason == "unknown_tool"
    assert malformed.reason == "invalid_tool_contract"

    registry = ToolRegistry(
        (
            ToolDefinition(
                name="write_like_tool",
                description="test",
                argument_schema={
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
                read_only=False,
            ),
        )
    )
    non_read_only = GuardrailRuntime(registry=registry).preflight(
        usage,
        _step("write_like_tool", {}),
    )
    assert non_read_only.reason == "non_read_only_tool"


def test_guardrail_postflight_reports_timeout_and_truncation() -> None:
    runtime = GuardrailRuntime()
    observations = runtime.postflight(
        {
            "error": {"type": "timeout", "message": "deadline"},
            "result": {"truncated": True},
        }
    )

    assert observations == (
        ("tool_timeout", "tool execution exceeded the configured timeout"),
        ("result_truncated", "tool result was truncated at the configured row limit"),
    )


def test_guardrail_usage_and_events_round_trip_through_incident_json() -> None:
    runtime = GuardrailRuntime()
    state = IncidentState()
    step = _step()
    decision = runtime.preflight(state.guardrail_usage, step)
    runtime.record_allowed(state.guardrail_usage, decision)
    state.guardrail_events.append(
        runtime.event(
            state.guardrail_usage,
            decision,
            event_type="preflight",
            incident_id="INC-001",
            trace_id="TRACE-001",
            step_id=step.step_id,
            sequence=1,
        )
    )

    payload = json.loads(state.to_json())
    restored = IncidentState.from_json(state.to_json())
    assert payload["guardrail_usage"]["sql_calls"] == 1
    assert restored == state
    assert restored.guardrail_events[0].event_id == state.guardrail_events[0].event_id
