import json

import pytest
from pydantic import ValidationError

from agents.planner import (
    PLANNER_ALERT_EXAMPLES,
    Alert,
    Hypothesis,
    InvestigationPlan,
    InvestigationStep,
    Planner,
    PlannerFallbackReason,
    PlannerInput,
    PlannerValidationError,
    StructuredInvestigationPlan,
    _diagnostic_capability_context,
    _sql_for_root_cause,
    build_fallback_plan,
    build_planner_prompt,
    infer_step_evidence_source,
    load_metric_context,
    structured_plan_to_investigation_plan,
    validate_plan_diagnostic_tool_bindings,
    validate_plan_semantics,
    validate_plan_tools,
)
from config.faults import (
    EvidenceSourceType,
    evidence_assets_by_source,
    load_fault_catalog,
)
from config.metrics import load_metrics_config
from llm.mock_client import MockModelClient
from tools.registry import build_default_tool_registry
from tools.sql_runner import validate_readonly_sql


def _request_for(alert_payload: dict[str, object]):
    alert = Alert.model_validate(alert_payload)
    return alert, load_metric_context(alert.metric)


def _alert_payload_for_metric(metric_id: str) -> dict[str, object]:
    return next(
        (
            payload
            for payload in PLANNER_ALERT_EXAMPLES
            if payload["metric"] == metric_id
        ),
        {
            "incident_id": f"INC-{metric_id.upper()}",
            "metric": metric_id,
            "observed_at": "2026-01-30",
            "expected_value": 100,
            "observed_value": 75,
            "change_rate": -0.25,
            "severity": "medium",
        },
    )


def _structured_plan(
    arguments_json: str,
    *,
    tool: str = "sql_query",
) -> StructuredInvestigationPlan:
    alert, metric_context = _request_for(dict(PLANNER_ALERT_EXAMPLES[0]))
    canonical = build_fallback_plan(
        PlannerInput(alert=alert, metric_context=metric_context)
    )
    payload = canonical.model_dump(mode="json")
    step = dict(payload["steps"][0])
    step["tool"] = tool
    step.pop("arguments")
    step["arguments_json"] = arguments_json
    payload["steps"] = [step, *payload["steps"][1:]]
    return StructuredInvestigationPlan.model_validate(payload)


def _walk_schema(value: object):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_schema(child)


    elif isinstance(value, list):
        for child in value:
            yield from _walk_schema(child)


def _binding_plan(root_cause_type: str, tool: str, metric_id: str) -> InvestigationPlan:
    alert_payload = _alert_payload_for_metric(metric_id)
    alert = Alert.model_validate(alert_payload)
    catalog = load_fault_catalog()
    root_causes = [root_cause_type]
    root_causes.extend(
        fault.root_cause_type
        for fault in catalog.faults
        if fault.root_cause_type != root_cause_type
    )
    hypotheses = [
        Hypothesis(
            hypothesis_id=f"H{index:02d}",
            root_cause_type=root,
            description="candidate",
            initial_confidence=0.2,
        )
        for index, root in enumerate(root_causes[:3], start=1)
    ]
    return InvestigationPlan(
        incident_id=alert.incident_id,
        hypotheses=hypotheses,
        steps=[
            InvestigationStep(
                step_id="S01",
                purpose="inspect candidate",
                hypothesis_id="H01",
                tool=tool,
                arguments={"sql": "SELECT 1"},
                expected_evidence=["structured evidence"],
                stop_condition="continue if not supported",
            )
        ],
    )


def _coverage_step(
    step_id: str,
    hypothesis_id: str,
    *,
    sql: str | None = None,
    tool: str = "sql_query",
    arguments: dict[str, object] | None = None,
) -> InvestigationStep:
    return InvestigationStep(
        step_id=step_id,
        purpose="collect one bounded source observation",
        hypothesis_id=hypothesis_id,
        tool=tool,
        arguments=arguments or {"sql": sql or "SELECT COUNT(*) FROM events"},
        expected_evidence=["one structured observation"],
        stop_condition="continue only if the observation supports the candidate",
    )


def _coverage_plan(
    root_cause_type: str,
    metric_id: str,
    target_steps: list[InvestigationStep],
) -> tuple[InvestigationPlan, PlannerInput]:
    alert = Alert.model_validate(_alert_payload_for_metric(metric_id))
    plan = InvestigationPlan(
        incident_id=alert.incident_id,
        hypotheses=[
            Hypothesis(
                hypothesis_id="H01",
                root_cause_type=root_cause_type,
                description="target candidate",
                initial_confidence=0.45,
            ),
            Hypothesis(
                hypothesis_id="H02",
                root_cause_type="duplicate_batch",
                description="first decoy",
                initial_confidence=0.2,
            ),
            Hypothesis(
                hypothesis_id="H03",
                root_cause_type="null_value_anomaly",
                description="second decoy",
                initial_confidence=0.1,
            ),
        ],
        steps=[
            *target_steps,
            _coverage_step("S09", "H02"),
            _coverage_step("S10", "H03"),
        ],
    )
    request = PlannerInput(
        alert=alert,
        metric_context=load_metric_context(metric_id),
    )
    return plan, request


def test_structured_planner_schema_is_strict_and_uses_string_arguments() -> None:
    schema = StructuredInvestigationPlan.model_json_schema()

    assert schema["type"] == "object"
    assert "anyOf" not in schema
    step_schema = schema["$defs"]["StructuredInvestigationStep"]
    assert step_schema["properties"]["arguments_json"]["type"] == "string"
    assert "arguments" not in step_schema["properties"]
    assert set(step_schema["required"]) == set(step_schema["properties"])
    assert set(schema["required"]) == set(schema["properties"])

    for candidate in _walk_schema(schema):
        if candidate.get("type") == "object":
            assert candidate.get("additionalProperties") is False
        assert candidate.get("additionalProperties") is not True
        assert not isinstance(candidate.get("additionalProperties"), dict)


def test_structured_plan_converter_decodes_sql_arguments() -> None:
    structured = _structured_plan('{"sql":"SELECT 1"}')

    plan = structured_plan_to_investigation_plan(
        structured, build_default_tool_registry()
    )

    assert plan.steps[0].arguments == {"sql": "SELECT 1"}


def test_structured_plan_converter_decodes_nested_data_quality_scope() -> None:
    arguments = {
        "table": "events",
        "column": "user_id",
        "threshold": 0.01,
        "scope": {
            "equals": {"device_type": ["ios", "android"]},
            "time_column": "event_time",
            "start": "2026-01-30T00:00:00+00:00",
            "end": "2026-01-31T00:00:00+00:00",
        },
    }
    structured = _structured_plan(
        json.dumps(arguments, sort_keys=True, separators=(",", ":")),
        tool="check_null_rate",
    )

    plan = structured_plan_to_investigation_plan(
        structured, build_default_tool_registry()
    )

    assert plan.steps[0].arguments == arguments


@pytest.mark.parametrize("arguments_json", ["[]", '"foo"', "1", "null", "true"])
def test_structured_plan_converter_rejects_non_object_arguments(
    arguments_json: str,
) -> None:
    with pytest.raises(PlannerValidationError, match="JSON object"):
        structured_plan_to_investigation_plan(
            _structured_plan(arguments_json), build_default_tool_registry()
        )


def test_structured_plan_converter_rejects_malformed_json() -> None:
    with pytest.raises(PlannerValidationError, match="not valid JSON"):
        structured_plan_to_investigation_plan(
            _structured_plan("{not-json"), build_default_tool_registry()
        )


def test_structured_plan_converter_uses_registry_for_unknown_arguments() -> None:
    with pytest.raises(PlannerValidationError, match="unknown field"):
        structured_plan_to_investigation_plan(
            _structured_plan('{"query":"SELECT 1"}'), build_default_tool_registry()
        )


def test_structured_plan_keeps_unsafe_sql_rejected_by_existing_semantics() -> None:
    plan = structured_plan_to_investigation_plan(
        _structured_plan('{"sql":"DELETE FROM events"}'),
        build_default_tool_registry(),
    )

    with pytest.raises(PlannerValidationError, match="unsafe SQL"):
        validate_plan_tools(plan, build_default_tool_registry())


def test_structured_plan_accepts_canonical_arguments_only_at_local_boundary() -> None:
    alert, metric_context = _request_for(dict(PLANNER_ALERT_EXAMPLES[0]))
    canonical = build_fallback_plan(
        PlannerInput(alert=alert, metric_context=metric_context)
    )

    structured = StructuredInvestigationPlan.model_validate(canonical)

    assert structured.steps[0].arguments_json == json.dumps(
        canonical.steps[0].arguments,
        sort_keys=True,
        separators=(",", ":"),
    )
    assert "arguments" not in structured.model_dump(mode="json")["steps"][0]


def test_malformed_arguments_json_enters_planner_repair_and_succeeds() -> None:
    alert, metric_context = _request_for(dict(PLANNER_ALERT_EXAMPLES[0]))
    fallback = build_fallback_plan(
        PlannerInput(alert=alert, metric_context=metric_context)
    )
    valid_arguments = fallback.steps[0].arguments
    valid = _structured_plan(
        json.dumps(valid_arguments, sort_keys=True, separators=(",", ":"))
    )
    invalid = _structured_plan("{not-json")
    calls = 0

    def response_factory(response_model: type[StructuredInvestigationPlan], _: str, __: str):
        nonlocal calls
        calls += 1
        return response_model.model_validate(invalid if calls == 1 else valid)

    result = Planner(MockModelClient(response_factory), max_retries=1).run(
        alert, metric_context
    )

    assert result.fallback_used is False
    assert result.planner_repair_count == 1
    assert result.plan.steps[0].arguments == valid_arguments
    assert result.model_result is not None
    assert isinstance(result.model_result.parsed, InvestigationPlan)


def test_strict_model_client_repairs_fault_tool_binding_in_one_retry() -> None:
    alert, metric_context = _request_for(dict(PLANNER_ALERT_EXAMPLES[0]))
    invalid = _structured_plan(
        json.dumps(
            {
                "table": "events",
                "column": "user_id",
                "threshold": 0.01,
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
        tool="check_null_rate",
    )
    valid = _structured_plan(
        json.dumps(
            {
                "table": "events",
                "timestamp_column": "event_time",
                "reference_time": "2026-01-31T00:00:00+00:00",
                "max_age": 86400,
                "scope": {
                    "equals": {"device_type": "android"},
                    "time_column": "event_time",
                    "start": "2026-01-30T00:00:00+00:00",
                    "end": "2026-01-31T00:00:00+00:00",
                },
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
        tool="check_freshness",
    )
    prompts: list[str] = []

    def response_factory(
        response_model: type[StructuredInvestigationPlan],
        _: str,
        user_prompt: str,
    ) -> StructuredInvestigationPlan:
        prompts.append(user_prompt)
        return response_model.model_validate(invalid if len(prompts) == 1 else valid)

    result = Planner(MockModelClient(response_factory), max_retries=1).run(
        alert, metric_context
    )

    assert result.fallback_used is False
    assert result.planner_repair_count == 1
    assert result.model_result is not None
    assert result.plan.steps[0].tool == "check_freshness"
    assert (
        "tool 'check_null_rate' is not mapped to root_cause_type 'missing_partition'; "
        "allowed tool(s): check_freshness, sql_query"
    ) in prompts[1]
    assert "Traceback" not in prompts[1]
    assert "source_seed_case_id" not in prompts[1]


def test_repeated_malformed_arguments_json_returns_audited_fallback() -> None:
    alert, metric_context = _request_for(dict(PLANNER_ALERT_EXAMPLES[0]))
    invalid = _structured_plan("{not-json")

    def response_factory(response_model: type[StructuredInvestigationPlan], _: str, __: str):
        return response_model.model_validate(invalid)

    result = Planner(MockModelClient(response_factory), max_retries=1).run(
        alert, metric_context
    )

    assert result.fallback_used is True
    assert result.fallback_reason == PlannerFallbackReason.PLANNER_VALIDATION_FAILED
    assert result.planner_repair_count == 2
    assert result.model_result is None


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
    assert "JSON-encoded object string" in prompt
    assert 'arguments_json = "{\\"sql\\":\\"SELECT ...\\"}"' in prompt
    assert '"incident_id"' in prompt
    assert '"hypotheses"' in prompt
    assert '"expected_evidence"' in prompt
    assert alert.incident_id in prompt
    assert metric_context.metric_id in prompt


def test_metric_diagnostics_stay_out_of_metric_context_but_capabilities_are_prompt_visible() -> None:
    metric = load_metrics_config().metrics[0]
    context = load_metric_context(metric.id)
    request = PlannerInput(alert=PLANNER_ALERT_EXAMPLES[0], metric_context=context)
    prompt = build_planner_prompt(PLANNER_ALERT_EXAMPLES[0], context)

    context_payload = context.model_dump(mode="json")
    request_payload = request.model_dump(mode="json")
    assert "common_anomalies" not in context_payload
    assert "verification_fields" not in context_payload
    assert "diagnostic_tools" not in context_payload
    assert "validation" not in context_payload
    assert "common_anomalies" not in request_payload["metric_context"]
    assert "verification_fields" not in request_payload["metric_context"]
    assert "diagnostic_tools" not in request_payload["metric_context"]
    assert "validation" not in request_payload["metric_context"]
    assert "expected_column_types" not in prompt
    assert "numeric_ranges" not in prompt
    assert "max_result_rows" not in prompt
    for diagnostic in metric.common_anomalies:
        assert diagnostic not in prompt
    assert "diagnostic_tools" in prompt
    assert "Tool: sql_query" in prompt


def test_diagnostic_capability_context_matches_the_complete_catalog() -> None:
    catalog = load_fault_catalog()

    assert _diagnostic_capability_context() == [
        {
            "root_cause_type": fault.root_cause_type,
            "diagnostic_tools": fault.diagnostic_tools,
        }
        for fault in catalog.faults
    ]
    assert len(_diagnostic_capability_context()) == 12
    assert len(
        {
            entry["root_cause_type"] for entry in _diagnostic_capability_context()
        }
    ) == 12


@pytest.mark.parametrize(
    "metric_id",
    ["average_session_duration", "conversion_rate"],
)
def test_prompt_exposes_capabilities_for_every_canonical_hypothesis(
    metric_id: str,
) -> None:
    prompt = build_planner_prompt(
        _alert_payload_for_metric(metric_id),
        load_metric_context(metric_id),
    )
    catalog = load_fault_catalog()
    capability_section = prompt.split("Canonical diagnostic capability map", 1)[
        1
    ].split("Canonical evidence source-to-asset map", 1)[0]
    relevant_section = prompt.split("Relevant canonical fault vocabulary", 1)[1].split(
        "Canonical root_cause_type vocabulary", 1
    )[0]

    capability_start = capability_section.index("[")
    capability_end = capability_section.rindex("]") + 1
    assert json.loads(capability_section[capability_start:capability_end]) == (
        _diagnostic_capability_context()
    )

    applicable_ids = {
        fault.id for fault in catalog.faults if metric_id in fault.affected_metrics
    }
    assert len(applicable_ids) == 1
    relevant_start = relevant_section.index("[")
    relevant_end = relevant_section.rindex("]") + 1
    relevant_payload = json.loads(relevant_section[relevant_start:relevant_end])
    assert {entry["fault_id"] for entry in relevant_payload} == applicable_ids
    for entry in relevant_payload:
        fault = catalog.by_id(entry["fault_id"])
        assert entry["verification_fields"] == fault.verification_fields
        assert entry["expected_evidence"] == fault.expected_evidence
        assert entry["evidence_source_types"] == [
            source.value for source in fault.evidence_source_types
        ]

    for forbidden in (
        "evidence_paths",
        "injection_strategy",
        "expected_direction",
        "effect_size_type",
        "minimum_effect_size",
        "source_seed_case_id",
        "Ground Truth",
    ):
        assert forbidden not in prompt
    for fault in catalog.faults:
        assert f"{fault.id}-001" not in prompt


def test_single_fault_metric_accepts_catalog_consistent_filler_hypotheses() -> None:
    alert_payload = _alert_payload_for_metric("average_session_duration")
    alert, metric_context = _request_for(alert_payload)
    request = PlannerInput(alert=alert, metric_context=metric_context)
    root_causes = ["unit_error", "missing_partition", "data_delay"]
    structured = StructuredInvestigationPlan.model_validate(
        {
            "incident_id": alert.incident_id,
            "hypotheses": [
                {
                    "hypothesis_id": f"H{index:02d}",
                    "root_cause_type": root_cause,
                    "description": "candidate",
                    "initial_confidence": 0.2,
                }
                for index, root_cause in enumerate(root_causes, start=1)
            ],
            "steps": [
                {
                    "step_id": "S01",
                    "purpose": "inspect candidate",
                    "hypothesis_id": "H01",
                    "tool": "sql_query",
                    "arguments_json": '{"sql":"SELECT COUNT(*) FROM events"}',
                    "expected_evidence": ["structured evidence"],
                    "stop_condition": "continue if not supported",
                },
                {
                    "step_id": "S02",
                    "purpose": "inspect business data",
                    "hypothesis_id": "H02",
                    "tool": "sql_query",
                    "arguments_json": '{"sql":"SELECT COUNT(*) FROM events"}',
                    "expected_evidence": ["structured evidence"],
                    "stop_condition": "continue if not supported",
                },
                {
                    "step_id": "S03",
                    "purpose": "inspect partition metadata",
                    "hypothesis_id": "H02",
                    "tool": "sql_query",
                    "arguments_json": '{"sql":"SELECT * FROM partition_metadata"}',
                    "expected_evidence": ["structured evidence"],
                    "stop_condition": "continue if not supported",
                },
                {
                    "step_id": "S04",
                    "purpose": "inspect business data",
                    "hypothesis_id": "H03",
                    "tool": "sql_query",
                    "arguments_json": '{"sql":"SELECT COUNT(*) FROM events"}',
                    "expected_evidence": ["structured evidence"],
                    "stop_condition": "continue if not supported",
                },
                {
                    "step_id": "S05",
                    "purpose": "inspect pipeline metadata",
                    "hypothesis_id": "H03",
                    "tool": "sql_query",
                    "arguments_json": '{"sql":"SELECT * FROM pipeline_runs"}',
                    "expected_evidence": ["structured evidence"],
                    "stop_condition": "continue if not supported",
                },
            ],
        }
    )

    plan = structured_plan_to_investigation_plan(
        structured, build_default_tool_registry()
    )
    validate_plan_semantics(plan, request, build_default_tool_registry())


def test_prompt_shows_all_applicable_fault_capabilities_without_ground_truth() -> None:
    examples_by_metric = {
        str(payload["metric"]): payload for payload in PLANNER_ALERT_EXAMPLES
    }

    for metric_id, alert_payload in examples_by_metric.items():
        prompt = build_planner_prompt(
            alert_payload,
            load_metric_context(metric_id),
        )
        catalog = load_fault_catalog()
        applicable = [
            fault for fault in catalog.faults if metric_id in fault.affected_metrics
        ]

        for fault in applicable:
            assert f'"fault_id": "{fault.id}"' in prompt
            assert f'"root_cause_type": "{fault.root_cause_type}"' in prompt
            assert '"affected_assets"' in prompt
            assert '"diagnostic_tools"' in prompt
            assert '"evidence_source_types"' in prompt
            assert '"verification_fields"' in prompt
            assert '"expected_evidence"' in prompt
            for tool in fault.diagnostic_tools:
                assert f'"{tool}"' in prompt
            for field in fault.verification_fields:
                assert field in prompt
            for evidence in fault.expected_evidence:
                assert evidence in prompt

        for forbidden in (
            "expected_root_cause",
            "evidence_paths",
            "injection_strategy",
            "source_seed_case_id",
            "benchmark case ID",
            "Ground Truth",
        ):
            assert forbidden not in prompt
        assert not any(
            f"{fault.id}-001" in prompt for fault in catalog.faults
        )


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


def test_planner_prompt_advertises_registry_tools_and_omits_formal_schema() -> None:
    alert, metric_context = _request_for(dict(PLANNER_ALERT_EXAMPLES[0]))
    prompt = build_planner_prompt(alert, metric_context)

    assert "Available tools:" in prompt
    assert "Tool: sql_query" in prompt
    assert "Legacy JSON Schema" not in prompt
    assert "target Android partition row_count is zero" in prompt
    assert "closed-set candidate labels" in prompt
    for available in (
        "check_freshness",
        "check_null_rate",
        "detect_schema_drift",
        "detect_distribution_drift",
        "check_duplicate_rate",
    ):
        assert f"Tool: {available}" in prompt


def test_fallback_plan_uses_registered_readonly_sql_tool_only() -> None:
    registry = build_default_tool_registry()
    for metric in load_metrics_config().metrics:
        alert, metric_context = _request_for(_alert_payload_for_metric(metric.id))
        plan = build_fallback_plan(
            PlannerInput(alert=alert, metric_context=metric_context),
            tool_registry=registry,
        )

        assert all(registry.contains(step.tool) for step in plan.steps)
        assert all(step.tool == "sql_query" for step in plan.steps)
        sql_statements = [str(step.arguments["sql"]) for step in plan.steps]
        assert len(sql_statements) == len(set(sql_statements))
        for step in plan.steps:
            assert validate_readonly_sql(step.arguments["sql"]) in {
                "SELECT",
                "DESCRIBE",
                "EXPLAIN",
            }


def test_fallback_plans_pass_full_planner_semantics() -> None:
    registry = build_default_tool_registry()

    for metric in load_metrics_config().metrics:
        alert, metric_context = _request_for(_alert_payload_for_metric(metric.id))
        request = PlannerInput(alert=alert, metric_context=metric_context)
        plan = build_fallback_plan(request, tool_registry=registry)

        validate_plan_semantics(plan, request, registry)


def test_every_hypothesis_requires_an_investigation_step() -> None:
    plan, request = _coverage_plan(
        "schema_change",
        "daily_active_users",
        [],
    )

    with pytest.raises(PlannerValidationError, match="H01.*has no investigation step"):
        validate_plan_semantics(plan, request, build_default_tool_registry())


@pytest.mark.parametrize(
    ("root_cause_type", "metric_id", "incomplete_steps", "complete_steps", "missing"),
    [
        (
            "schema_change",
            "daily_active_users",
            [
                _coverage_step(
                    "S01",
                    "H01",
                    tool="detect_schema_drift",
                    arguments={"table": "events"},
                )
            ],
            [
                _coverage_step("S01", "H01", sql="SELECT COUNT(*) FROM events"),
                _coverage_step(
                    "S02",
                    "H01",
                    tool="detect_schema_drift",
                    arguments={"table": "events"},
                ),
            ],
            "business_data",
        ),
        (
            "timezone_error",
            "daily_active_users",
            [_coverage_step("S01", "H01", sql="SELECT COUNT(*) FROM events")],
            [
                _coverage_step("S01", "H01", sql="SELECT COUNT(*) FROM events"),
                _coverage_step("S02", "H01", sql="SELECT * FROM metric_versions"),
            ],
            "metric_version",
        ),
        (
            "ab_split_anomaly",
            "conversion_rate",
            [
                _coverage_step(
                    "S01", "H01", sql="SELECT COUNT(*) FROM experiment_assignments"
                )
            ],
            [
                _coverage_step(
                    "S01", "H01", sql="SELECT COUNT(*) FROM experiment_assignments"
                ),
                _coverage_step("S02", "H01", sql="SELECT * FROM experiment_configs"),
            ],
            "experiment_config",
        ),
    ],
)
def test_catalog_declared_multisource_coverage_is_enforced(
    root_cause_type: str,
    metric_id: str,
    incomplete_steps: list[InvestigationStep],
    complete_steps: list[InvestigationStep],
    missing: str,
) -> None:
    registry = build_default_tool_registry()
    incomplete, request = _coverage_plan(
        root_cause_type,
        metric_id,
        incomplete_steps,
    )
    with pytest.raises(PlannerValidationError, match=missing):
        validate_plan_semantics(incomplete, request, registry)

    complete, request = _coverage_plan(
        root_cause_type,
        metric_id,
        complete_steps,
    )
    validate_plan_semantics(complete, request, registry)


def test_duplicate_business_steps_do_not_satisfy_independent_coverage() -> None:
    plan, request = _coverage_plan(
        "schema_change",
        "daily_active_users",
        [
            _coverage_step("S01", "H01", sql="SELECT COUNT(*) FROM events"),
            _coverage_step("S02", "H01", sql="SELECT COUNT(*) FROM users"),
        ],
    )

    with pytest.raises(PlannerValidationError, match="schema_metadata"):
        validate_plan_semantics(plan, request, build_default_tool_registry())


def test_mixed_source_sql_cannot_count_as_two_independent_paths() -> None:
    plan, request = _coverage_plan(
        "schema_change",
        "daily_active_users",
        [
            _coverage_step(
                "S01",
                "H01",
                sql=(
                    "SELECT COUNT(*) FROM events AS e CROSS JOIN schema_snapshots AS s"
                ),
            )
        ],
    )

    with pytest.raises(PlannerValidationError, match="business_data, schema_metadata"):
        validate_plan_semantics(plan, request, build_default_tool_registry())


def test_unknown_asset_fails_closed_for_source_coverage() -> None:
    plan, request = _coverage_plan(
        "schema_change",
        "daily_active_users",
        [
            _coverage_step("S01", "H01", sql="SELECT COUNT(*) FROM mystery_events"),
            _coverage_step("S02", "H01", sql="SELECT * FROM schema_snapshots"),
        ],
    )

    with pytest.raises(PlannerValidationError, match="business_data"):
        validate_plan_semantics(plan, request, build_default_tool_registry())


def test_cte_names_are_not_treated_as_physical_assets() -> None:
    step = _coverage_step(
        "S01",
        "H01",
        sql=(
            "WITH recent_events AS (SELECT * FROM events) "
            "SELECT COUNT(*) FROM recent_events"
        ),
    )

    assert (
        infer_step_evidence_source(step, build_default_tool_registry())
        is EvidenceSourceType.BUSINESS_DATA
    )


def test_prompt_exposes_canonical_source_asset_map() -> None:
    prompt = build_planner_prompt(
        PLANNER_ALERT_EXAMPLES[0],
        load_metric_context("daily_active_users"),
    )

    for source, assets in evidence_assets_by_source().items():
        assert f'"{source}"' in prompt
        for asset in assets:
            assert f'"{asset}"' in prompt
    assert "These evidence objectives describe what would test each candidate" in prompt
    assert "They do not mean the candidate is true" in prompt


@pytest.mark.parametrize(
    ("root_cause_type", "tool", "metric_id"),
    [
        ("missing_partition", "check_freshness", "daily_active_users"),
        ("null_value_anomaly", "check_null_rate", "daily_active_users"),
        ("field_drift", "detect_distribution_drift", "ai_task_count"),
        ("schema_change", "detect_schema_drift", "daily_active_users"),
    ],
)
def test_diagnostic_tool_binding_accepts_catalog_mapping(
    root_cause_type: str,
    tool: str,
    metric_id: str,
) -> None:
    validate_plan_diagnostic_tool_bindings(
        _binding_plan(root_cause_type, tool, metric_id)
    )


@pytest.mark.parametrize(
    "fault_id",
    ["F02", "F04", "F05", "F06", "F07", "F08", "F11", "F12"],
)
def test_sql_only_fault_rejects_unrelated_data_quality_tool(fault_id: str) -> None:
    fault = load_fault_catalog().by_id(fault_id)

    with pytest.raises(
        PlannerValidationError,
        match=(
            rf"tool 'check_null_rate' is not mapped to root_cause_type "
            rf"'{fault.root_cause_type}'"
        ),
    ):
        validate_plan_diagnostic_tool_bindings(
            _binding_plan(
                fault.root_cause_type,
                "check_null_rate",
                fault.affected_metrics[0],
            )
        )


def test_planner_semantics_validate_a_data_quality_tool_against_registry() -> None:
    alert, metric_context = _request_for(dict(PLANNER_ALERT_EXAMPLES[0]))
    fallback = build_fallback_plan(
        PlannerInput(alert=alert, metric_context=metric_context)
    )
    step = fallback.steps[0].model_copy(
        update={
            "tool": "check_null_rate",
            "arguments": {
                "table": "events",
                "column": "user_id",
                "threshold": 0.01,
            },
        }
    )
    plan = fallback.model_copy(update={"steps": [step]})

    validate_plan_tools(plan, build_default_tool_registry())

    invalid_step = step.model_copy(
        update={"arguments": {**step.arguments, "threshold": 2.0}}
    )
    with pytest.raises(PlannerValidationError, match="less than or equal to 1"):
        validate_plan_tools(
            fallback.model_copy(update={"steps": [invalid_step]}),
            build_default_tool_registry(),
        )


def test_planner_semantics_rejects_tool_outside_fault_diagnostic_mapping() -> None:
    alert, metric_context = _request_for(dict(PLANNER_ALERT_EXAMPLES[0]))
    request = PlannerInput(alert=alert, metric_context=metric_context)
    fallback = build_fallback_plan(request)
    step = fallback.steps[0].model_copy(
        update={
            "tool": "check_null_rate",
            "arguments": {
                "table": "events",
                "column": "user_id",
                "threshold": 0.01,
            },
        }
    )

    with pytest.raises(
        PlannerValidationError,
        match="not mapped to root_cause_type 'missing_partition'",
    ):
        validate_plan_semantics(
            fallback.model_copy(update={"steps": [step]}),
            request,
            build_default_tool_registry(),
        )


def test_planner_semantics_accepts_tool_mapped_to_fault_root_cause() -> None:
    alert, metric_context = _request_for(dict(PLANNER_ALERT_EXAMPLES[0]))
    request = PlannerInput(alert=alert, metric_context=metric_context)
    fallback = build_fallback_plan(request)
    step = fallback.steps[0].model_copy(
        update={
            "tool": "check_freshness",
            "arguments": {
                "table": "events",
                "timestamp_column": "event_time",
                "reference_time": "2026-01-31T00:00:00+00:00",
                "max_age": 86400,
                "scope": {
                    "equals": {"device_type": "android"},
                    "time_column": "event_time",
                    "start": "2026-01-30T00:00:00+00:00",
                    "end": "2026-01-31T00:00:00+00:00",
                },
            },
        }
    )

    validate_plan_semantics(
        fallback.model_copy(update={"steps": [step, *fallback.steps[1:]]}),
        request,
        build_default_tool_registry(),
    )


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


def test_noncanonical_root_cause_is_repaired_then_accepted() -> None:
    alert, metric_context = _request_for(dict(PLANNER_ALERT_EXAMPLES[0]))
    fallback = build_fallback_plan(PlannerInput(alert=alert, metric_context=metric_context))
    invalid = fallback.model_dump(mode="json")
    invalid["hypotheses"][0]["root_cause_type"] = "magic_database_issue"
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
    assert result.plan.hypotheses[0].root_cause_type == "missing_partition"


def test_repeated_noncanonical_root_cause_returns_audited_fallback() -> None:
    alert, metric_context = _request_for(dict(PLANNER_ALERT_EXAMPLES[0]))
    fallback = build_fallback_plan(PlannerInput(alert=alert, metric_context=metric_context))
    invalid = fallback.model_dump(mode="json")
    invalid["hypotheses"][0]["root_cause_type"] = "magic_database_issue"

    def response_factory(response_model: type[InvestigationPlan], _: str, __: str):
        return response_model.model_validate(invalid)

    result = Planner(MockModelClient(response_factory), max_retries=1).run(
        alert, metric_context
    )

    assert result.fallback_used is True
    assert result.fallback_reason == PlannerFallbackReason.PLANNER_VALIDATION_FAILED
    assert result.planner_repair_count == 2
    assert result.model_result is None


def test_root_cause_taxonomy_is_loaded_from_catalog() -> None:
    catalog = load_fault_catalog()
    alert = Alert.model_validate(PLANNER_ALERT_EXAMPLES[0])
    plan = build_fallback_plan(
        PlannerInput(alert=alert, metric_context=load_metric_context(alert.metric))
    )

    assert len(catalog.faults) == 12
    assert {hypothesis.root_cause_type for hypothesis in plan.hypotheses}.issubset(
        {fault.root_cause_type for fault in catalog.faults}
    )


def test_timezone_fallback_uses_region_join_and_readonly_sql() -> None:
    sql = _sql_for_root_cause(
        "timezone_error",
        metric="daily_active_users",
        entity_column="user_id",
        observed_at="2026-01-30",
    )

    assert "INNER JOIN users AS u ON e.user_id = u.user_id" in sql
    assert "u.region" in sql
    assert "event_hour" in sql
    assert validate_readonly_sql(sql) == "SELECT"
