from __future__ import annotations

import json
from pathlib import Path
from typing import Any, TypeVar

import duckdb
import pytest
from pydantic import BaseModel

from agents.planner import Alert, MetricContext
from benchmark.ablation import (
    CANONICAL_CASE_IDS,
    CANONICAL_ROOT_CAUSES,
    VARIANT_ORDER,
    AblationCaseResult,
    AblationConfig,
    AblationExecutionOutput,
    AblationRunner,
    AblationRuntimeInput,
    compute_metrics,
    recompute_report,
    score_execution,
    serialize_runtime_input,
    validate_fairness,
)
from benchmark.case_generator import load_case_manifest
from llm.models import ModelCallResult, ModelUsage

T = TypeVar("T", bound=BaseModel)


class _SequenceClient:
    provider = "test-provider"

    def __init__(self, payloads: list[dict[str, Any]]) -> None:
        self.payloads = payloads
        self.calls = 0

    async def generate_structured(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        response_model: type[T],
    ) -> ModelCallResult[T]:
        del system_prompt, user_prompt
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
    return AblationConfig(
        case_ids=["F01-001"],
        model_name="mock-model",
        output_dir=tmp_path,
        **updates,
    )


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
    assert output.tool_call_count == 1
    assert output.sql_call_count == 1


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
                        "root_cause_type": "missing_partition",
                        "description": "A candidate explanation.",
                        "initial_confidence": 0.6,
                    },
                    {
                        "hypothesis_id": "H02",
                        "root_cause_type": "data_delay",
                        "description": "Another candidate explanation.",
                        "initial_confidence": 0.2,
                    },
                    {
                        "hypothesis_id": "H03",
                        "root_cause_type": "null_value_anomaly",
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
                    }
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
    assert output.tool_call_count == 1


def test_full_harness_adapter_reuses_production_builder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from benchmark import ablation

    calls: list[tuple[str, str]] = []

    def fake_builder(config: Any, *, model_client_factory: Any) -> Any:
        calls.append((config.model_provider, config.model_name))

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
    output = ablation.FullHarnessAdapter(_config(tmp_path))(_runtime(tmp_path))
    assert calls == [("mock", "mock-model")]
    assert output.variant == "full_harness"
    assert output.completion_status == "UNRESOLVED"


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
    assert metrics.total_sql_attempts == 2
    assert metrics.invalid_sql_rate == 0.5
    assert metrics.unsafe_operation_rate == 1 / 3
    assert metrics.duplicate_operation_rate == 1 / 3
    assert metrics.known_average_cost is None

    failed = score_execution(
        output.model_copy(update={"completion_status": "tool_failed"}),
        manifest,
    )
    assert failed.status == "error"


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
    assert fairness.same_model_fingerprint
    assert not fairness.gt_runtime_leakage


def test_runner_persists_all_pairs_and_resume_reuses_them(tmp_path: Path) -> None:
    config = _config(tmp_path, run_id="persistence-smoke")
    first = AblationRunner(config).run()
    assert len(first.results) == 4
    assert first.fairness.complete_pair_matrix
    assert first.fairness.same_db_hash
    assert (first.run_dir / "comparison.csv").is_file()
    assert (first.run_dir / "report.md").is_file()
    assert (
        json.loads((first.run_dir / "fairness.json").read_text())["attempted_pairs"]
        == 4
    )
    assert recompute_report(first.run_dir) == first.metrics

    resumed = AblationRunner(config.model_copy(update={"resume": True})).run()
    assert len(resumed.results) == 4
    assert resumed.fairness.complete_pair_matrix
    with pytest.raises(ValueError, match="fingerprint mismatch"):
        AblationRunner(
            config.model_copy(update={"resume": True, "max_tool_calls": 19})
        ).run()
