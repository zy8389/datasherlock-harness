from __future__ import annotations

import json
from pathlib import Path
from typing import Any, TypeVar

import duckdb
import pytest
from pydantic import BaseModel

from agents.planner import Alert, MetricContext, build_planner_prompt
from benchmark.ablation import (
    CANONICAL_CASE_IDS,
    CANONICAL_ROOT_CAUSES,
    VARIANT_ORDER,
    AblationCaseResult,
    AblationConfig,
    AblationExecutionOutput,
    AblationRunner,
    AblationRuntimeInput,
    StructuredReactAction,
    compute_metrics,
    recompute_report,
    score_execution,
    serialize_runtime_input,
    structured_react_action_to_action,
    validate_fairness,
)
from benchmark.case_generator import load_case_manifest
from llm.models import ModelCallResult, ModelUsage
from tools.registry import (
    ToolArgumentsError,
    UnknownToolError,
    build_default_tool_registry,
)

T = TypeVar("T", bound=BaseModel)


class _SequenceClient:
    provider = "test-provider"

    def __init__(self, payloads: list[dict[str, Any]]) -> None:
        self.payloads = payloads
        self.calls = 0
        self.system_prompts: list[str] = []
        self.user_prompts: list[str] = []

    async def generate_structured(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        response_model: type[T],
    ) -> ModelCallResult[T]:
        self.system_prompts.append(system_prompt)
        self.user_prompts.append(user_prompt)
        payload = self.payloads[self.calls]
        self.calls += 1
        parsed = response_model.model_validate(payload)
        return ModelCallResult(
            provider=self.provider,
            model="same-model",
            parsed=parsed,
            usage=ModelUsage(input_tokens=10, output_tokens=5, total_tokens=15),
            latency_ms=1.0,
        )


def _config(tmp_path: Path, **updates: Any) -> AblationConfig:
    values: dict[str, Any] = {
        "case_ids": ["F01-001"],
        "model_name": "mock-model",
        "output_dir": tmp_path,
    }
    values.update(updates)
    return AblationConfig(**values)


def _runtime(tmp_path: Path) -> AblationRuntimeInput:
    return AblationRuntimeInput(
        run_id="opaque-run-case-00001",
        database_path=tmp_path / "datasherlock.duckdb",
        alert=Alert(
            incident_id="opaque-run-case-00001",
            metric="daily_active_users",
            observed_at="2026-01-30",
            expected_value=100.0,
            observed_value=80.0,
            change_rate=-0.2,
            severity="high",
        ),
        metric_context=MetricContext(metric_id="daily_active_users"),
        allowed_root_causes=list(CANONICAL_ROOT_CAUSES),
    )


def _logical_fingerprint_payload(fill: str = "a") -> dict[str, Any]:
    digest = f"sha256:{fill * 64}"
    return {
        "schema_version": 1,
        "algorithm": "sha256",
        "database_logical_hash": digest,
        "tables": [
            {
                "table_name": "users",
                "columns": [
                    {
                        "ordinal_position": 1,
                        "column_name": "value",
                        "column_type": "INTEGER",
                    }
                ],
                "row_count": 1,
                "table_hash": digest,
            }
        ],
    }


def test_contract_has_exact_canonical_case_set_and_four_variants(
    tmp_path: Path,
) -> None:
    config = AblationConfig(
        case_ids=list(CANONICAL_CASE_IDS),
        model_name="mock-model",
        output_dir=tmp_path,
    )
    assert config.is_full_selection
    assert VARIANT_ORDER == (
        "single_prompt",
        "react",
        "state_graph_no_validator",
        "full_harness",
    )


def test_runtime_serialization_rejects_ground_truth_fields(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    serialized = serialize_runtime_input(runtime)
    assert "expected_root_cause" not in serialized
    assert "case_id" not in serialized

    leaked = runtime.model_copy(
        update={
            "alert": Alert(
                **runtime.alert.model_dump(),
                expected_root_cause="missing_partition",
            )
        }
    )
    with pytest.raises(ValueError, match="Ground Truth"):
        serialize_runtime_input(leaked)


def test_single_prompt_makes_one_call_and_no_tool_calls(tmp_path: Path) -> None:
    from benchmark.ablation import SinglePromptAdapter

    client = _SequenceClient(
        [{"ranked_root_causes": ["missing_partition", "data_delay"]}]
    )
    output = SinglePromptAdapter(
        _config(tmp_path), model_client_factory=lambda _runtime: client
    )(_runtime(tmp_path))
    assert client.calls == 1
    assert output.completion_status == "completed"
    assert output.primary_prediction == "missing_partition"
    assert output.tool_call_count == 0
    assert output.sql_call_count == 0


def test_react_loop_is_bounded_and_uses_current_guardrails(tmp_path: Path) -> None:
    from benchmark.ablation import ReActAdapter

    client = _SequenceClient(
        [
            {
                "type": "tool",
                "tool": "sql_query",
                "arguments": {"sql": "SELECT 1"},
                "ranked_root_causes": [],
            }
        ]
    )
    duckdb.connect(str(_runtime(tmp_path).database_path)).close()
    output = ReActAdapter(
        _config(tmp_path, max_agent_rounds=1),
        model_client_factory=lambda _runtime: client,
    )(_runtime(tmp_path))
    assert client.calls == 1
    assert output.completion_status == "budget_exceeded"
    assert output.primary_prediction is None
    assert output.tool_call_count == 1
    assert output.sql_call_count == 1

    prompt_payload = json.loads(client.user_prompts[0])
    expected_tools = [
        definition.model_dump(mode="json")
        for definition in build_default_tool_registry().definitions()
    ]
    assert prompt_payload["available_tools"] == expected_tools
    planner_prompt = build_planner_prompt(
        _runtime(tmp_path).alert,
        _runtime(tmp_path).metric_context,
        tool_registry=build_default_tool_registry(),
    )
    for definition in build_default_tool_registry().definitions():
        assert f"Tool: {definition.name}" in planner_prompt
        assert definition.description in planner_prompt
        assert json.dumps(
            definition.argument_schema, ensure_ascii=False, indent=2
        ) in planner_prompt
        assert f"Read-only: {str(definition.read_only).lower()}" in planner_prompt


def test_react_primary_prediction_is_first_final_label(tmp_path: Path) -> None:
    from benchmark.ablation import ReActAdapter

    client = _SequenceClient(
        [
            {
                "type": "final",
                "tool": None,
                "arguments_json": "{}",
                "ranked_root_causes": ["missing_partition", "data_delay"],
            }
        ]
    )
    output = ReActAdapter(
        _config(tmp_path), model_client_factory=lambda _runtime: client
    )(_runtime(tmp_path))
    assert output.primary_prediction == "missing_partition"
    assert output.ranked_root_causes[0] == output.primary_prediction


def test_structured_react_schema_is_flat_strict_and_closed() -> None:
    schema = StructuredReactAction.model_json_schema()

    assert schema["type"] == "object"
    assert "anyOf" not in schema
    assert "oneOf" not in schema
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {
        "type",
        "tool",
        "arguments_json",
        "ranked_root_causes",
    }
    assert schema["properties"]["arguments_json"]["type"] == "string"
    assert "arguments" not in schema["properties"]

    def assert_no_free_form_object(node: object) -> None:
        if isinstance(node, dict):
            if node.get("type") == "object":
                assert node.get("additionalProperties") is False
            for child in node.values():
                assert_no_free_form_object(child)
        elif isinstance(node, list):
            for child in node:
                assert_no_free_form_object(child)

    assert_no_free_form_object(schema)


def test_structured_react_accepts_local_canonical_arguments_payload() -> None:
    structured = StructuredReactAction.model_validate(
        {
            "type": "tool",
            "tool": "sql_query",
            "arguments": {"sql": "SELECT 1"},
            "ranked_root_causes": [],
        }
    )

    assert structured.arguments_json == '{"sql":"SELECT 1"}'
    assert "arguments" not in structured.model_dump()


def test_structured_react_converter_validates_sql_and_nested_scope() -> None:
    registry = build_default_tool_registry()
    sql_action = structured_react_action_to_action(
        StructuredReactAction(
            type="tool",
            tool="sql_query",
            arguments_json='{"sql":"SELECT 1"}',
            ranked_root_causes=[],
        ),
        registry,
    )
    assert sql_action.arguments == {"sql": "SELECT 1"}

    nested_scope = {
        "table": "events",
        "column": "user_id",
        "threshold": 0.01,
        "scope": {
            "equals": {"device": "android"},
            "time_column": "event_time",
            "start": "2026-01-30T00:00:00Z",
            "end": "2026-01-31T00:00:00Z",
        },
    }
    data_quality_action = structured_react_action_to_action(
        StructuredReactAction(
            type="tool",
            tool="check_null_rate",
            arguments_json=json.dumps(nested_scope, sort_keys=True),
            ranked_root_causes=[],
        ),
        registry,
    )
    assert data_quality_action.arguments == nested_scope


@pytest.mark.parametrize(
    ("arguments_json", "message"),
    [
        ("{not-json", "valid JSON"),
        ("[1, 2]", "JSON object"),
    ],
)
def test_structured_react_rejects_invalid_argument_payloads(
    arguments_json: str, message: str
) -> None:
    with pytest.raises((ValueError, TypeError), match=message):
        StructuredReactAction(
            type="tool",
            tool="sql_query",
            arguments_json=arguments_json,
            ranked_root_causes=[],
        )


def test_structured_react_converter_uses_registry_for_tool_and_argument_checks() -> None:
    registry = build_default_tool_registry()
    with pytest.raises(ToolArgumentsError, match="unknown field"):
        structured_react_action_to_action(
            StructuredReactAction(
                type="tool",
                tool="sql_query",
                arguments_json='{"sql":"SELECT 1","extra":true}',
                ranked_root_causes=[],
            ),
            registry,
        )
    with pytest.raises(UnknownToolError, match="unknown tool"):
        structured_react_action_to_action(
            StructuredReactAction(
                type="tool",
                tool="not_registered",
                arguments_json="{}",
                ranked_root_causes=[],
            ),
            registry,
        )


def test_structured_react_final_action_contract() -> None:
    final = structured_react_action_to_action(
        StructuredReactAction(
            type="final",
            tool=None,
            arguments_json="{}",
            ranked_root_causes=["missing_partition"],
        ),
        build_default_tool_registry(),
    )
    assert final.type == "final"
    assert final.tool is None
    assert final.arguments == {}
    assert final.ranked_root_causes == ["missing_partition"]

    with pytest.raises((ValueError, TypeError), match="arguments_json"):
        StructuredReactAction(
            type="final",
            tool=None,
            arguments_json='{"sql":"SELECT 1"}',
            ranked_root_causes=[],
        )
    with pytest.raises((ValueError, TypeError), match="ranked_root_causes"):
        StructuredReactAction(
            type="tool",
            tool="sql_query",
            arguments_json='{"sql":"SELECT 1"}',
            ranked_root_causes=["missing_partition"],
        )


def test_state_graph_without_validator_never_sets_authoritative_root_cause(
    tmp_path: Path,
) -> None:
    from benchmark.ablation import StateGraphNoValidatorAdapter

    duckdb.connect(str(_runtime(tmp_path).database_path)).close()
    client = _SequenceClient(
        [
            {
                "incident_id": "opaque-run-case-00001",
                "hypotheses": [
                    {
                        "hypothesis_id": "H01",
                        "root_cause_type": "duplicate_batch",
                        "description": "A candidate explanation.",
                        "initial_confidence": 0.6,
                    },
                    {
                        "hypothesis_id": "H02",
                        "root_cause_type": "null_value_anomaly",
                        "description": "Another candidate explanation.",
                        "initial_confidence": 0.2,
                    },
                    {
                        "hypothesis_id": "H03",
                        "root_cause_type": "unit_error",
                        "description": "A third candidate explanation.",
                        "initial_confidence": 0.1,
                    },
                ],
                "steps": [
                    {
                        "step_id": "S01",
                        "purpose": "Collect a bounded observation.",
                        "hypothesis_id": "H01",
                        "tool": "sql_query",
                        "arguments": {"sql": "SELECT 1 AS observed"},
                        "expected_evidence": ["the bounded observation"],
                        "stop_condition": "retain the result",
                    },
                    {
                        "step_id": "S02",
                        "purpose": "Collect a second bounded observation.",
                        "hypothesis_id": "H02",
                        "tool": "sql_query",
                        "arguments": {"sql": "SELECT 2 AS observed"},
                        "expected_evidence": ["the bounded observation"],
                        "stop_condition": "retain the result",
                    },
                    {
                        "step_id": "S03",
                        "purpose": "Collect a third bounded observation.",
                        "hypothesis_id": "H03",
                        "tool": "sql_query",
                        "arguments": {"sql": "SELECT 3 AS observed"},
                        "expected_evidence": ["the bounded observation"],
                        "stop_condition": "retain the result",
                    },
                ],
            }
        ]
    )
    output = StateGraphNoValidatorAdapter(
        _config(tmp_path), model_client_factory=lambda _runtime: client
    )(_runtime(tmp_path))
    assert client.calls == 1
    assert output.completion_status == "unresolved"
    assert output.trace_payload["state"]["root_cause"] is None
    assert output.primary_prediction == "duplicate_batch"
    assert output.tool_call_count == 3


def test_full_harness_adapter_reuses_production_builder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from benchmark import ablation

    calls: list[tuple[str, str, str | None, float, int, float]] = []

    def fake_builder(config: Any, *, model_client_factory: Any) -> Any:
        calls.append(
            (
                config.model_provider,
                config.model_name,
                config.model_base_url,
                config.model_timeout_seconds,
                config.model_retries,
                config.model_retry_base_delay_seconds,
            )
        )

        def execute(_runtime: Any) -> Any:
            return ablation.HarnessExecutionOutput(
                harness_status="UNRESOLVED",
                predicted_root_cause=None,
                trace_payload={
                    "schema_version": 1,
                    "state": {
                        "hypotheses": [],
                        "root_cause": None,
                        "tool_trace": [],
                        "guardrail_events": [],
                    },
                },
            )

        return execute

    monkeypatch.setattr(ablation, "build_harness_executor", fake_builder)
    config = _config(
        tmp_path,
        model_base_url="https://model.example/v1",
        model_timeout_seconds=91.5,
        model_retries=4,
        model_retry_base_delay_seconds=1.25,
    )
    output = ablation.FullHarnessAdapter(config)(_runtime(tmp_path))
    assert calls == [("mock", "mock-model", "https://model.example/v1", 91.5, 4, 1.25)]
    assert output.variant == "full_harness"
    assert output.completion_status == "UNRESOLVED"


def test_all_ablation_adapters_bind_the_same_model_transport(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from benchmark import ablation

    config = _config(
        tmp_path,
        model_provider="openai",
        model_name="gpt-test",
        model_base_url=" https://model.example/v1 ",
        model_timeout_seconds=91.5,
        model_retries=4,
        model_retry_base_delay_seconds=1.25,
    )
    captured: list[Any] = []
    sentinel = object()
    monkeypatch.setattr(
        ablation,
        "create_model_client",
        lambda settings: captured.append(settings) or sentinel,
    )

    adapters = (
        ablation.SinglePromptAdapter,
        ablation.ReActAdapter,
        ablation.StateGraphNoValidatorAdapter,
        ablation.FullHarnessAdapter,
    )
    runtime = _runtime(tmp_path)
    for adapter_type in adapters:
        assert adapter_type(config)._model_client(runtime) is sentinel

    assert len(captured) == len(adapters)
    assert [
        (
            settings.model_provider,
            settings.openai_model,
            settings.openai_base_url,
            settings.llm_timeout_seconds,
            settings.llm_max_retries,
            settings.llm_retry_base_delay_seconds,
        )
        for settings in captured
    ] == [
        ("openai", "gpt-test", "https://model.example/v1", 91.5, 4, 1.25)
    ] * len(adapters)


def test_pilot_requires_a_real_provider(tmp_path: Path) -> None:
    from benchmark.ablation import run_blocker

    config = _config(tmp_path, run_kind="pilot")
    assert run_blocker(config) == "mock provider is reserved for deterministic smoke"

    full_config = config.model_copy(
        update={"case_ids": list(CANONICAL_CASE_IDS), "model_provider": "openai"}
    )
    assert run_blocker(full_config) == "pilot requires a canonical case subset"


def test_full_harness_unvalidated_hypothesis_cannot_receive_top1_credit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from benchmark import ablation

    manifest = load_case_manifest("F01-001")

    def fake_builder(config: Any, *, model_client_factory: Any) -> Any:
        del config, model_client_factory

        def execute(_runtime: Any) -> Any:
            return ablation.HarnessExecutionOutput(
                harness_status="UNRESOLVED",
                predicted_root_cause=None,
                trace_payload={
                    "state": {
                        "hypotheses": [
                            {
                                "root_cause_type": manifest.root_cause_type,
                                "confidence": 0.9,
                            },
                            {"root_cause_type": "data_delay", "confidence": 0.1},
                        ],
                        "root_cause": None,
                        "tool_trace": [],
                        "guardrail_events": [],
                    }
                },
            )

        return execute

    monkeypatch.setattr(ablation, "build_harness_executor", fake_builder)
    output = ablation.FullHarnessAdapter(_config(tmp_path))(_runtime(tmp_path))
    result = score_execution(output, manifest)

    assert output.primary_prediction is None
    assert output.ranked_root_causes[0] == manifest.root_cause_type
    assert result.top1_correct is False
    assert result.top3_correct is True
    assert result.abstention is True


def test_full_harness_primary_prediction_is_validator_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from benchmark import ablation

    manifest = load_case_manifest("F01-001")

    def fake_builder(config: Any, *, model_client_factory: Any) -> Any:
        del config, model_client_factory

        def execute(_runtime: Any) -> Any:
            return ablation.HarnessExecutionOutput(
                harness_status="ROOT_CAUSE_FOUND",
                predicted_root_cause=manifest.root_cause_type,
                trace_payload={
                    "state": {
                        "hypotheses": [
                            {"root_cause_type": "data_delay", "confidence": 0.9},
                            {
                                "root_cause_type": manifest.root_cause_type,
                                "confidence": 0.1,
                            },
                        ],
                        "root_cause": {
                            "root_cause_type": manifest.root_cause_type
                        },
                        "tool_trace": [],
                        "guardrail_events": [],
                    }
                },
            )

        return execute

    monkeypatch.setattr(ablation, "build_harness_executor", fake_builder)
    output = ablation.FullHarnessAdapter(_config(tmp_path))(_runtime(tmp_path))
    result = score_execution(output, manifest)

    assert output.primary_prediction == manifest.root_cause_type
    assert output.ranked_root_causes[0] == manifest.root_cause_type
    assert result.top1_correct is True
    assert result.abstention is False


def test_scoring_keeps_unresolved_in_denominator_and_deduplicates_top3() -> None:
    manifest = load_case_manifest("F02-001")
    output = AblationExecutionOutput(
        variant="single_prompt",
        completion_status="completed",
        ranked_root_causes=[
            "not_a_canonical_label",
            "duplicate_batch",
            "duplicate_batch",
        ],
    )
    scored = score_execution(output, manifest)
    unresolved = score_execution(
        AblationExecutionOutput(
            variant="single_prompt",
            completion_status="unresolved",
        ),
        manifest,
    )
    metrics = compute_metrics([scored, unresolved], "single_prompt")
    assert scored.top1_correct is False
    assert scored.top3_correct is True
    assert scored.invalid_prediction is True
    assert unresolved.top1_correct is False
    assert metrics.attempted == 2
    assert metrics.top1_accuracy == 0
    assert metrics.top3_accuracy == 0.5


def test_empty_sql_is_valid_and_guardrail_reasons_drive_rates() -> None:
    manifest = load_case_manifest("F01-001")
    trace = {
        "model_fingerprint": "fp",
        "guardrail_events": [
            {
                "event_type": "preflight",
                "tool_name": "sql_query",
                "allowed": True,
                "reason": None,
            },
            {
                "event_type": "preflight",
                "tool_name": "sql_query",
                "allowed": False,
                "reason": "unsafe_sql",
            },
            {
                "event_type": "preflight",
                "tool_name": "sql_query",
                "allowed": False,
                "reason": "duplicate_tool_call",
            },
        ],
        "tool_trace": [
            {
                "tool_name": "sql_query",
                "success": True,
                "result": {"status": "success", "row_count": 0, "rows": []},
            }
        ],
    }
    output = AblationExecutionOutput(
        variant="react",
        completion_status="completed",
        ranked_root_causes=[manifest.root_cause_type],
        tool_call_count=1,
        sql_call_count=1,
        trace_payload=trace,
    )
    result = score_execution(output, manifest)
    metrics = compute_metrics([result], "react")
    assert result.invalid_sql_attempts == 1
    assert result.unsafe_operation_attempts == 1
    assert result.duplicate_operation_attempts == 1
    assert metrics.total_tool_attempts == 3
    assert result.total_sql_attempts == 3
    assert metrics.total_sql_attempts == 3
    assert metrics.invalid_sql_rate == 1 / 3
    assert metrics.unsafe_operation_rate == 1 / 3
    assert metrics.duplicate_operation_rate == 1 / 3
    assert metrics.known_average_cost is None

    failed = score_execution(
        output.model_copy(update={"completion_status": "tool_failed"}),
        manifest,
    )
    assert failed.status == "error"


def test_allowed_successful_sql_has_zero_invalid_rate() -> None:
    manifest = load_case_manifest("F01-001")
    output = AblationExecutionOutput(
        variant="react",
        completion_status="completed",
        trace_payload={
            "guardrail_events": [
                {
                    "event_type": "preflight",
                    "tool_name": "sql_query",
                    "allowed": True,
                    "reason": None,
                }
            ],
            "tool_trace": [
                {
                    "tool_name": "sql_query",
                    "success": True,
                    "result": {"status": "success", "row_count": 1, "rows": [[1]]},
                }
            ],
        },
    )
    result = score_execution(output, manifest)
    metrics = compute_metrics([result], "react")

    assert result.total_sql_attempts == 1
    assert result.invalid_sql_attempts == 0
    assert metrics.invalid_sql_rate == 0


def test_allowed_sql_execution_failure_is_one_invalid_attempt() -> None:
    manifest = load_case_manifest("F01-001")
    output = AblationExecutionOutput(
        variant="react",
        completion_status="tool_failed",
        trace_payload={
            "guardrail_events": [
                {
                    "event_type": "preflight",
                    "tool_name": "sql_query",
                    "allowed": True,
                    "reason": None,
                }
            ],
            "tool_trace": [
                {
                    "tool_name": "sql_query",
                    "success": False,
                    "error": {"type": "execution", "message": "missing table"},
                }
            ],
        },
    )
    result = score_execution(output, manifest)
    metrics = compute_metrics([result], "react")

    assert result.total_sql_attempts == 1
    assert result.invalid_sql_attempts == 1
    assert metrics.invalid_sql_rate == 1.0


def test_valid_empty_sql_result_is_not_invalid() -> None:
    manifest = load_case_manifest("F01-001")
    output = AblationExecutionOutput(
        variant="react",
        completion_status="completed",
        trace_payload={
            "guardrail_events": [
                {
                    "event_type": "preflight",
                    "tool_name": "sql_query",
                    "allowed": True,
                    "reason": None,
                }
            ],
            "tool_trace": [
                {
                    "tool_name": "sql_query",
                    "success": True,
                    "result": {"status": "success", "row_count": 0, "rows": []},
                    "sql_validation": {"passed": False, "reason": "empty_result"},
                }
            ],
        },
    )
    result = score_execution(output, manifest)

    assert result.total_sql_attempts == 1
    assert result.invalid_sql_attempts == 0


def test_budget_blocked_sql_counts_in_denominator_only() -> None:
    manifest = load_case_manifest("F01-001")
    output = AblationExecutionOutput(
        variant="react",
        completion_status="budget_exceeded",
        trace_payload={
            "guardrail_events": [
                {
                    "event_type": "preflight",
                    "tool_name": "sql_query",
                    "allowed": False,
                    "reason": "sql_call_budget_exceeded",
                }
            ],
            "tool_trace": [],
        },
    )
    result = score_execution(output, manifest)
    metrics = compute_metrics([result], "react")

    assert result.total_sql_attempts == 1
    assert result.invalid_sql_attempts == 0
    assert metrics.invalid_sql_rate == 0


def test_fairness_requires_complete_pairs_and_equal_hashes() -> None:
    case_inputs = [
        {
            "case_id": "F01-001",
            "database_sha256": {variant: "same-db" for variant in VARIANT_ORDER},
        }
    ]
    results = [
        AblationCaseResult(
            case_id="F01-001",
            fault_id="F01",
            variant=variant,
            status="completed",
            completion_status="completed",
            expected_root_cause="missing_partition",
            top1_correct=False,
            top3_correct=False,
            trace_payload={"model_fingerprint": "same-model"},
        )
        for variant in VARIANT_ORDER
    ]
    fairness = validate_fairness(
        case_inputs,
        results,
        model_fingerprint="same-model",
    )
    assert fairness.complete_pair_matrix
    assert fairness.same_db_hash
    assert fairness.same_logical_fixture_fingerprint is None
    assert fairness.fixture_identity_matches
    assert fairness.same_model_fingerprint
    assert not fairness.gt_runtime_leakage


def test_fairness_uses_logical_fixture_gate_and_reports_mismatch() -> None:
    logical_fingerprints = {
        variant: _logical_fingerprint_payload() for variant in VARIANT_ORDER
    }
    logical_fingerprints["react"] = _logical_fingerprint_payload("b")
    case_inputs = [
        {
            "case_id": "F01-001",
            "database_sha256": {
                variant: f"physical-{variant}" for variant in VARIANT_ORDER
            },
            "logical_fixture_fingerprints": logical_fingerprints,
        }
    ]

    fairness = validate_fairness(
        case_inputs,
        [],
        model_fingerprint="same-model",
    )

    assert not fairness.same_db_hash
    assert fairness.same_logical_fixture_fingerprint is False
    assert not fairness.fixture_identity_matches
    assert fairness.logical_fixture_mismatches[0].case_id == "F01-001"
    assert fairness.logical_fixture_mismatches[0].variant == "react"
    assert (
        fairness.logical_fixture_mismatches[0]
        .comparison.changed_table_hashes[0]
        .table_name
        == "users"
    )


def test_logical_match_overrides_physical_hash_difference() -> None:
    case_inputs = [
        {
            "case_id": "F01-001",
            "database_sha256": {
                variant: f"physical-{variant}" for variant in VARIANT_ORDER
            },
            "logical_fixture_fingerprints": {
                variant: _logical_fingerprint_payload() for variant in VARIANT_ORDER
            },
        }
    ]

    fairness = validate_fairness(
        case_inputs,
        [],
        model_fingerprint="same-model",
    )

    assert not fairness.same_db_hash
    assert fairness.same_logical_fixture_fingerprint is True
    assert fairness.fixture_identity_matches
    assert not fairness.logical_fixture_mismatches


def test_runner_persists_all_pairs_and_resume_reuses_them(tmp_path: Path) -> None:
    config = _config(tmp_path, run_id="persistence-smoke")
    first = AblationRunner(config).run()
    assert len(first.results) == 4
    assert first.fairness.complete_pair_matrix
    assert first.fairness.same_db_hash
    assert first.fairness.same_logical_fixture_fingerprint is True
    assert first.fairness.fixture_identity_matches
    assert (first.run_dir / "comparison.csv").is_file()
    assert (first.run_dir / "report.md").is_file()
    assert (
        json.loads((first.run_dir / "fairness.json").read_text())["attempted_pairs"]
        == 4
    )
    assert recompute_report(first.run_dir) == first.metrics

    input_path = first.run_dir / "case_inputs.jsonl"
    payload = json.loads(input_path.read_text(encoding="utf-8"))
    assert set(payload["logical_fixture_fingerprints"]) == set(VARIANT_ORDER)
    assert all(
        fingerprint["schema_version"] == 1
        for fingerprint in payload["logical_fixture_fingerprints"].values()
    )

    resumed = AblationRunner(config.model_copy(update={"resume": True})).run()
    assert len(resumed.results) == 4
    assert resumed.fairness.complete_pair_matrix

    tampered = json.loads(input_path.read_text(encoding="utf-8"))
    tampered["logical_fixture_fingerprints"]["single_prompt"][
        "database_logical_hash"
    ] = f"sha256:{'f' * 64}"
    input_path.write_text(json.dumps(tampered) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="logical fixture fingerprint differs"):
        AblationRunner(config.model_copy(update={"resume": True})).run()

    payload.pop("logical_fixture_fingerprints")
    input_path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    legacy_resumed = AblationRunner(config.model_copy(update={"resume": True})).run()
    assert legacy_resumed.fairness.same_logical_fixture_fingerprint is None
    assert legacy_resumed.fairness.fixture_identity_matches

    with pytest.raises(ValueError, match="fingerprint mismatch"):
        AblationRunner(
            config.model_copy(update={"resume": True, "max_tool_calls": 19})
        ).run()
