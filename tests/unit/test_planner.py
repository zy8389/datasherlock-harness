import pytest
from pydantic import ValidationError

from agents.planner import (
    PLANNER_ALERT_EXAMPLES,
    Alert,
    InvestigationPlan,
    Planner,
    PlannerFallbackReason,
    PlannerInput,
    build_fallback_plan,
    build_planner_prompt,
    load_metric_context,
)
from llm.mock_client import MockModelClient
from tools.registry import build_default_tool_registry
from tools.sql_runner import validate_readonly_sql


def _request_for(alert_payload: dict[str, object]):
    alert = Alert.model_validate(alert_payload)
    return alert, load_metric_context(alert.metric)


def test_three_alert_examples_produce_stable_bounded_fallback_plans() -> None:
    for alert_payload in PLANNER_ALERT_EXAMPLES:
        alert, metric_context = _request_for(alert_payload)
        first = Planner(lambda _: "not-json", max_retries=0).plan(alert, metric_context)
        second = Planner(lambda _: "not-json", max_retries=0).plan(alert, metric_context)

        assert first == second
        assert first.incident_id == alert.incident_id
        assert 3 <= len(first.hypotheses) <= 5
        assert 1 <= len(first.steps) <= 10
        hypothesis_ids = {hypothesis.hypothesis_id for hypothesis in first.hypotheses}
        for step in first.steps:
            assert step.hypothesis_id in hypothesis_ids
            assert step.purpose
            assert step.tool
            assert step.arguments
            assert step.expected_evidence
            assert step.stop_condition


def test_planner_retries_invalid_json_and_accepts_valid_schema() -> None:
    alert, metric_context = _request_for(dict(PLANNER_ALERT_EXAMPLES[0]))
    fallback = build_fallback_plan(PlannerInput(alert=alert, metric_context=metric_context))
    responses = iter(["{not-json", fallback.model_dump_json()])
    prompts: list[str] = []

    def generator(prompt: str) -> str:
        prompts.append(prompt)
        return next(responses)

    result = Planner(generator, max_retries=1).plan(alert, metric_context)

    assert result == fallback
    assert len(prompts) == 2
    assert "Retry this response" in prompts[1]


def test_planner_uses_fallback_after_provider_or_json_failures() -> None:
    alert, metric_context = _request_for(dict(PLANNER_ALERT_EXAMPLES[1]))
    calls = 0

    def broken_generator(_: str) -> str:
        nonlocal calls
        calls += 1
        raise RuntimeError("provider unavailable")

    result = Planner(broken_generator, max_retries=2).plan(alert, metric_context)

    assert calls == 3
    assert result.incident_id == alert.incident_id
    assert len(result.hypotheses) == 5


def test_prompt_contains_structured_input_schema_and_json_only_constraint() -> None:
    alert, metric_context = _request_for(dict(PLANNER_ALERT_EXAMPLES[2]))
    prompt = build_planner_prompt(alert, metric_context)

    assert "Output only one valid JSON object" in prompt
    assert '"incident_id"' in prompt
    assert '"hypotheses"' in prompt
    assert '"expected_evidence"' in prompt
    assert alert.incident_id in prompt
    assert metric_context.metric_id in prompt


def test_plan_schema_rejects_missing_fields_unknown_hypothesis_and_repairs() -> None:
    alert, _ = _request_for(dict(PLANNER_ALERT_EXAMPLES[0]))
    payload = {
        "incident_id": alert.incident_id,
        "hypotheses": [
            {
                "hypothesis_id": f"H0{index}",
                "root_cause_type": f"cause_{index}",
                "description": "candidate",
                "initial_confidence": 0.2,
            }
            for index in range(1, 4)
        ],
        "steps": [
            {
                "step_id": "S01",
                "purpose": "inspect",
                "hypothesis_id": "H01",
                "tool": "apply_patch_in_sandbox",
                "arguments": {},
                "expected_evidence": ["evidence"],
                "stop_condition": "stop",
            }
        ],
    }

    with pytest.raises(ValidationError, match="repair tool"):
        InvestigationPlan.model_validate(payload)

    del payload["steps"][0]["expected_evidence"]
    payload["steps"][0]["tool"] = "check_freshness"
    with pytest.raises(ValidationError):
        InvestigationPlan.model_validate(payload)


def test_prompt_input_mapping_accepts_metric_id_alias_from_metrics_config() -> None:
    alert_payload = dict(PLANNER_ALERT_EXAMPLES[0])
    prompt = build_planner_prompt(
        {
            "alert": alert_payload,
            "metric_context": {"id": "daily_active_users", "source_tables": ["events"]},
        }
    )

    assert "INC-DAU-001" in prompt
    assert "daily_active_users" in prompt


def test_planner_prompt_only_advertises_registry_tools_and_omits_formal_schema() -> None:
    alert, metric_context = _request_for(dict(PLANNER_ALERT_EXAMPLES[0]))
    prompt = build_planner_prompt(alert, metric_context)

    assert "Available tools:" in prompt
    assert "Tool: sql_query" in prompt
    assert "Legacy JSON Schema" not in prompt
    for unavailable in (
        "check_freshness",
        "get_partition_status",
        "check_null_rate",
        "detect_schema_drift",
    ):
        assert f"Tool: {unavailable}" not in prompt


def test_fallback_plan_uses_registered_readonly_sql_tool_only() -> None:
    registry = build_default_tool_registry()
    for alert_payload in PLANNER_ALERT_EXAMPLES:
        alert, metric_context = _request_for(dict(alert_payload))
        plan = build_fallback_plan(
            PlannerInput(alert=alert, metric_context=metric_context),
            tool_registry=registry,
        )

        assert all(registry.contains(step.tool) for step in plan.steps)
        assert all(step.tool == "sql_query" for step in plan.steps)
        for step in plan.steps:
            assert validate_readonly_sql(step.arguments["sql"]) in {
                "SELECT",
                "DESCRIBE",
                "EXPLAIN",
            }


def test_semantic_unknown_tool_is_repaired_then_accepted() -> None:
    alert, metric_context = _request_for(dict(PLANNER_ALERT_EXAMPLES[0]))
    fallback = build_fallback_plan(PlannerInput(alert=alert, metric_context=metric_context))
    invalid = fallback.model_dump(mode="json")
    invalid["steps"][0]["tool"] = "magic_tool"
    calls = 0

    def response_factory(response_model: type[InvestigationPlan], _: str, __: str):
        nonlocal calls
        calls += 1
        return response_model.model_validate(invalid if calls == 1 else fallback)

    result = Planner(MockModelClient(response_factory), max_retries=1).run(
        alert, metric_context
    )

    assert result.fallback_used is False
    assert result.fallback_reason is None
    assert result.planner_repair_count == 1
    assert result.model_result is not None


def test_semantic_tool_argument_error_exhausts_repair_and_falls_back() -> None:
    alert, metric_context = _request_for(dict(PLANNER_ALERT_EXAMPLES[0]))
    fallback = build_fallback_plan(PlannerInput(alert=alert, metric_context=metric_context))
    invalid = fallback.model_dump(mode="json")
    invalid["steps"][0]["arguments"] = {"query": "SELECT 1"}

    def response_factory(response_model: type[InvestigationPlan], _: str, __: str):
        return response_model.model_validate(invalid)

    result = Planner(MockModelClient(response_factory), max_retries=1).run(
        alert, metric_context
    )

    assert result.fallback_used is True
    assert result.fallback_reason == PlannerFallbackReason.PLANNER_VALIDATION_FAILED
    assert result.model_result is None
    assert result.planner_repair_count == 2
