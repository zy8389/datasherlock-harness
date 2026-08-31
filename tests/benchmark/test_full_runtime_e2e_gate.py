"""Full runtime gates for the F01 and F11 benchmark investigations.

The only fake boundary in these tests is the deterministic model response.  All
planning, tool authorization, execution, checkpoint recovery, evidence binding,
and root-cause validation stay on their production paths.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any, TypeVar

import numpy as np
import pandas as pd
import pytest
from pydantic import BaseModel

from agents.planner import MetricContext, Planner, load_metric_context
from benchmark.fault_injector import inject_case
from config.faults import EvidenceSourceType, GroundTruthCase, load_ground_truth_cases
from data.generator import generate_dataset, write_outputs
from harness.checkpoint import CheckpointManager, FileCheckpointStore, ResumeAction
from harness.graph import HarnessGraph
from harness.guardrails import GuardrailRuntime
from harness.hypothesis import EvidenceReference, HypothesisManager
from harness.state import IncidentState, IncidentStatus
from llm.models import ModelCallResult
from tools.executor import ToolExecutor
from tools.registry import build_default_tool_registry

START_DATE = pd.Timestamp("2026-01-01")
TARGET_DATE = "2026-01-30"
T = TypeVar("T", bound=BaseModel)


class _DeterministicModelClient:
    """Return one JSON-shaped provider response without calling a live model."""

    provider = "deterministic-test-provider"

    def __init__(self, payload: Mapping[str, Any]) -> None:
        self.payload = dict(payload)
        self.calls: list[dict[str, str]] = []

    async def generate_structured(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        response_model: type[T],
    ) -> ModelCallResult[T]:
        self.calls.append(
            {"system_prompt": system_prompt, "user_prompt": user_prompt}
        )
        parsed = response_model.model_validate(self.payload)
        return ModelCallResult(
            provider=self.provider,
            model="deterministic-model",
            parsed=parsed,
            raw_text=json.dumps(self.payload, sort_keys=True),
            request_id=f"deterministic-{len(self.calls)}",
            latency_ms=0.0,
        )


class _SimulatedInterruption(Exception):
    """Represent a process stop after the first durable tool checkpoint."""


@pytest.fixture(scope="module")
def baseline() -> dict[str, pd.DataFrame]:
    return generate_dataset(500, 30, 10_000, 42, START_DATE)


def _case(case_id: str) -> GroundTruthCase:
    return next(
        case
        for case in load_ground_truth_cases(Path("benchmark/ground_truth"))
        if case.case_id == case_id
    )


def _materialize_fault(
    baseline: dict[str, pd.DataFrame],
    case: GroundTruthCase,
    output_dir: Path,
) -> Path:
    result = inject_case(
        baseline,
        case,
        rng=np.random.default_rng(99),
        start_date=START_DATE,
        days=30,
    )
    write_outputs(output_dir, result.tables)
    return output_dir / "datasherlock.duckdb"


def _alert(incident_id: str) -> dict[str, object]:
    """Provide detector output only; no fault answer or Ground Truth payload."""

    return {
        "incident_id": incident_id,
        "metric": "daily_active_users",
        "observed_at": f"{TARGET_DATE}T00:00:00Z",
        "expected_value": 10_000.0,
        "observed_value": 7_500.0,
        "change_rate": -0.25,
        "severity": "high",
    }


def _hypothesis(
    hypothesis_id: str,
    root_cause_type: str,
    description: str,
    confidence: float,
) -> dict[str, object]:
    return {
        "hypothesis_id": hypothesis_id,
        "root_cause_type": root_cause_type,
        "description": description,
        "initial_confidence": confidence,
    }


def _step(
    step_id: str,
    purpose: str,
    arguments: dict[str, object],
    *,
    hypothesis_id: str = "H01",
) -> dict[str, object]:
    return {
        "step_id": step_id,
        "purpose": purpose,
        "hypothesis_id": hypothesis_id,
        "tool": "sql_query",
        "arguments": arguments,
        "expected_evidence": ["the bounded SQL observation"],
        "stop_condition": "retain the observation and test the candidate",
    }


def _f01_plan_payload() -> dict[str, object]:
    return {
        "incident_id": "INC-DAU-RUNTIME-001",
        "hypotheses": [
            _hypothesis(
                "H01",
                "missing_partition",
                "A target event partition may be absent.",
                0.60,
            ),
            _hypothesis(
                "H02",
                "data_delay",
                "The target data may have arrived late.",
                0.20,
            ),
            _hypothesis(
                "H03",
                "null_value_anomaly",
                "A null-value issue may distort the metric.",
                0.10,
            ),
        ],
        "steps": [
            _step(
                "S01",
                "Measure target-date Android business events.",
                {
                    "sql": (
                        "SELECT COUNT(*) AS android_event_count FROM events "
                        "WHERE CAST(event_time AS DATE) = DATE '2026-01-30' "
                        "AND device_type = 'android'"
                    )
                },
            ),
            _step(
                "S02",
                "Inspect target Android partition metadata.",
                {
                    "sql": (
                        "SELECT partition_value, row_count, status "
                        "FROM partition_metadata "
                        "WHERE table_name = 'events' "
                        "AND partition_value = '2026-01-30/android'"
                    )
                },
            ),
            _step(
                "S03",
                "Measure target-date events for the delay decoy.",
                {
                    "sql": (
                        "SELECT COUNT(*) AS event_count FROM events "
                        "WHERE CAST(event_time AS DATE) = DATE '2026-01-30'"
                    )
                },
                hypothesis_id="H02",
            ),
            _step(
                "S04",
                "Inspect pipeline metadata for the delay decoy.",
                {
                    "sql": (
                        "SELECT status, error_type FROM pipeline_runs "
                        "WHERE target_table = 'events' "
                        "AND target_partition = '2026-01-30'"
                    )
                },
                hypothesis_id="H02",
            ),
            _step(
                "S05",
                "Measure null user identifiers for the null-value decoy.",
                {
                    "sql": (
                        "SELECT COUNT(*) AS row_count FROM events "
                        "WHERE user_id IS NULL AND CAST(event_time AS DATE) = "
                        "DATE '2026-01-30'"
                    )
                },
                hypothesis_id="H03",
            ),
        ],
    }


def _f11_plan_payload() -> dict[str, object]:
    return {
        "incident_id": "INC-DAU-RUNTIME-002",
        "hypotheses": [
            _hypothesis(
                "H01",
                "metric_definition_change",
                "The metric definition may have changed at the target date.",
                0.60,
            ),
            _hypothesis(
                "H02",
                "missing_partition",
                "A source partition may be missing.",
                0.20,
            ),
            _hypothesis(
                "H03",
                "data_delay",
                "The source data may have arrived late.",
                0.10,
            ),
        ],
        "steps": [
            _step(
                "S01",
                "Compare raw target-day activity with the materialized metric.",
                {
                    "sql": (
                        "SELECT COUNT(*) AS raw_event_count, "
                        "COUNT(DISTINCT user_id) AS raw_user_count, "
                        "(SELECT daily_active_users FROM daily_metrics "
                        "WHERE metric_date = DATE '2026-01-30') "
                        "AS daily_active_users "
                        "FROM events "
                        "WHERE CAST(event_time AS DATE) = DATE '2026-01-30'"
                    )
                },
            ),
            _step(
                "S02",
                "Compare metric definition versions around the target date.",
                {
                    "sql": (
                        "SELECT version, definition_hash, query "
                        "FROM metric_versions "
                        "WHERE metric_id = 'daily_active_users' "
                        "ORDER BY version"
                    )
                },
            ),
            _step(
                "S03",
                "Measure target-date events for the partition decoy.",
                {
                    "sql": (
                        "SELECT COUNT(*) AS event_count FROM events "
                        "WHERE CAST(event_time AS DATE) = DATE '2026-01-30'"
                    )
                },
                hypothesis_id="H02",
            ),
            _step(
                "S04",
                "Inspect partition metadata for the partition decoy.",
                {
                    "sql": (
                        "SELECT partition_value, row_count, status "
                        "FROM partition_metadata WHERE table_name = 'events'"
                    )
                },
                hypothesis_id="H02",
            ),
            _step(
                "S05",
                "Measure target-date events for the delay decoy.",
                {
                    "sql": (
                        "SELECT COUNT(*) AS event_count FROM events "
                        "WHERE CAST(event_time AS DATE) = DATE '2026-01-30'"
                    )
                },
                hypothesis_id="H03",
            ),
            _step(
                "S06",
                "Inspect pipeline metadata for the delay decoy.",
                {
                    "sql": (
                        "SELECT status, error_type FROM pipeline_runs "
                        "WHERE target_table = 'events' "
                        "AND target_partition = '2026-01-30'"
                    )
                },
                hypothesis_id="H03",
            ),
        ],
    }


def _runtime(
    database_path: Path,
    checkpoint_manager: CheckpointManager,
    payload: Mapping[str, Any],
) -> tuple[HarnessGraph, IncidentState, _DeterministicModelClient]:
    registry = build_default_tool_registry()
    client = _DeterministicModelClient(payload)
    planner = Planner(client, tool_registry=registry, max_retries=0)
    executor = ToolExecutor(database_path, registry=registry)
    graph = HarnessGraph(
        planner=planner,
        tool_executor=executor,
        guardrail_runtime=GuardrailRuntime(registry=registry),
        hypothesis_manager=HypothesisManager(),
        checkpoint_manager=checkpoint_manager,
    )
    state = IncidentState(alert=_alert(str(payload["incident_id"])))
    return graph, state, client


def _metric_context() -> MetricContext:
    return load_metric_context("daily_active_users")


def _assert_plan_contract(
    graph: HarnessGraph,
    state: IncidentState,
) -> None:
    registry = graph.tool_executor.registry
    assert state.plan
    for raw_step in state.plan:
        tool = str(raw_step["tool"])
        assert registry.contains(tool)
        registry.validate_arguments(tool, raw_step["arguments"])
    assert all(
        event.reason not in {"invalid_tool_contract", "unknown_tool"}
        for event in state.guardrail_events
    )


def _sql_row(trace: Mapping[str, Any], row_index: int = 0) -> dict[str, Any]:
    result = trace["result"]
    assert isinstance(result, Mapping)
    columns = result["columns"]
    rows = result["rows"]
    assert isinstance(columns, list)
    assert isinstance(rows, list)
    row = rows[row_index]
    assert isinstance(row, list)
    return dict(zip(columns, row, strict=True))


def _interpret_sql(
    trace: Mapping[str, Any],
    source_type: EvidenceSourceType,
    description: str,
) -> EvidenceReference:
    query_id = trace["query_id"]
    assert isinstance(query_id, str)
    return EvidenceReference(
        evidence_id=f"interpreted-{query_id}",
        source_type=source_type.value,
        description=description,
        query_id=query_id,
        observation={"sql_result": trace["result"]},
    )


def _bind_supporting_evidence(
    graph: HarnessGraph,
    state: IncidentState,
    evidence: EvidenceReference,
) -> None:
    graph.register_evidence(state, evidence)
    graph.attach_evidence(state, "H01", evidence.evidence_id, supports=True)


def _assert_no_ground_truth_leakage(state: IncidentState) -> None:
    runtime_payload = {
        "alert": state.alert,
        "plan": state.plan,
        "tool_trace": state.tool_trace,
        "evidence": state.evidence,
        "planner_metadata": state.planner_metadata,
    }
    serialized = json.dumps(runtime_payload, sort_keys=True).lower()
    assert "ground_truth" not in serialized
    assert "expected_root_cause" not in serialized
    assert "groundtruthcase" not in serialized
    assert all(
        "expected_evidence" in step and "ground_truth" not in json.dumps(step).lower()
        for step in state.plan
    )


def test_f01_full_runtime_gate_restarts_without_replanning_or_repeating_s01(
    tmp_path: Path,
    baseline: dict[str, pd.DataFrame],
) -> None:
    injection_case = _case("F01-001")
    database_path = _materialize_fault(
        baseline,
        injection_case,
        tmp_path / "F01-001",
    )
    checkpoint_manager = CheckpointManager(
        FileCheckpointStore(tmp_path / "F01-001-checkpoints")
    )

    first_graph, state, first_client = _runtime(
        database_path,
        checkpoint_manager,
        _f01_plan_payload(),
    )
    first_graph.plan_incident(state, metric_context=_metric_context())
    assert first_client.calls
    assert len(first_client.calls) == 1
    _assert_plan_contract(first_graph, state)
    first_graph.execute_next_step(state)

    assert state.status is IncidentStatus.VALIDATING
    assert [trace["tool_name"] for trace in state.tool_trace] == ["sql_query"]
    assert state.guardrail_usage.tool_calls == 1
    assert state.guardrail_usage.sql_calls == 1
    assert state.guardrail_usage.agent_rounds == 1
    assert all(event.allowed for event in state.guardrail_events)
    assert all(
        event.reason not in {"unsafe_sql", "duplicate_tool_call"}
        for event in state.guardrail_events
    )

    with pytest.raises(_SimulatedInterruption):
        raise _SimulatedInterruption("interrupted after S01 checkpoint")

    second_graph, _, second_client = _runtime(
        database_path,
        checkpoint_manager,
        _f01_plan_payload(),
    )
    restored, resume = second_graph.resume_latest("INC-DAU-RUNTIME-001")
    assert second_client.calls == []
    assert resume.action is ResumeAction.CONTINUE_VALIDATION
    assert restored.status is IncidentStatus.VALIDATING
    assert [trace["tool_name"] for trace in restored.tool_trace] == ["sql_query"]
    assert restored.guardrail_usage.tool_calls == 1
    assert second_graph.hypothesis_manager.get_hypothesis("H01").status.value == "PROPOSED"

    first_row = _sql_row(restored.tool_trace[0])
    assert int(first_row["android_event_count"]) == 0
    business_evidence = _interpret_sql(
        restored.tool_trace[0],
        EvidenceSourceType.BUSINESS_DATA,
        "The target-date Android business event count is zero.",
    )
    second_graph.enter_hypothesis_testing(restored)
    _bind_supporting_evidence(second_graph, restored, business_evidence)
    second_graph.request_more_evidence(restored)
    assert restored.retry_count == 1
    assert second_graph.resume_plan(restored).next_step_id == "S02"

    second_graph.execute_next_step(restored)
    assert [trace["tool_name"] for trace in restored.tool_trace] == [
        "sql_query",
        "sql_query",
    ]
    assert restored.guardrail_usage.tool_calls == 2
    assert restored.guardrail_usage.sql_calls == 2
    metadata_row = _sql_row(restored.tool_trace[1])
    assert int(metadata_row["row_count"]) == 0
    assert metadata_row["status"] == "missing"
    metadata_evidence = _interpret_sql(
        restored.tool_trace[1],
        EvidenceSourceType.OPERATIONAL_METADATA,
        "Partition metadata reports zero rows and a missing target Android partition.",
    )
    second_graph.enter_hypothesis_testing(restored)
    _bind_supporting_evidence(second_graph, restored, metadata_evidence)
    validation = second_graph.validate_hypothesis(
        restored,
        "H01",
        second_graph.hypothesis_manager.evidence(),
    )

    assert validation.to_status is IncidentStatus.ROOT_CAUSE_FOUND
    assert restored.status is IncidentStatus.ROOT_CAUSE_FOUND
    assert restored.root_cause is not None
    assert restored.root_cause["root_cause_type"] == "missing_partition"
    assert restored.root_cause["supporting_evidence_ids"] == [
        business_evidence.evidence_id,
        metadata_evidence.evidence_id,
    ]
    assert restored.root_cause["independent_source_types"] == [
        "business_data",
        "operational_metadata",
    ]
    assert all(event.allowed for event in restored.guardrail_events)
    assert restored.guardrail_usage.blocked_calls == 0
    latest = checkpoint_manager.restore_latest("INC-DAU-RUNTIME-001")
    assert latest.state.status is IncidentStatus.ROOT_CAUSE_FOUND
    assert latest.resume.completed_step_ids == ["S01", "S02"]

    _assert_no_ground_truth_leakage(restored)
    expected_after_runtime = _case("F01-001")
    assert restored.root_cause["root_cause_type"] == expected_after_runtime.root_cause_type


def test_f11_full_runtime_gate_interprets_business_and_metric_version_sql(
    tmp_path: Path,
    baseline: dict[str, pd.DataFrame],
) -> None:
    injection_case = _case("F11-001")
    database_path = _materialize_fault(
        baseline,
        injection_case,
        tmp_path / "F11-001",
    )
    checkpoint_manager = CheckpointManager(
        FileCheckpointStore(tmp_path / "F11-001-checkpoints")
    )
    graph, state, client = _runtime(
        database_path,
        checkpoint_manager,
        _f11_plan_payload(),
    )

    graph.plan_incident(state, metric_context=_metric_context())
    assert len(client.calls) == 1
    _assert_plan_contract(graph, state)
    graph.execute_next_step(state)
    assert state.status is IncidentStatus.VALIDATING
    first_row = _sql_row(state.tool_trace[0])
    assert int(first_row["raw_event_count"]) > 0
    assert int(first_row["raw_user_count"]) > int(first_row["daily_active_users"])
    business_evidence = _interpret_sql(
        state.tool_trace[0],
        EvidenceSourceType.BUSINESS_DATA,
        "Raw target-day event activity remains present while the materialized metric is lower.",
    )
    graph.enter_hypothesis_testing(state)
    _bind_supporting_evidence(graph, state, business_evidence)
    graph.request_more_evidence(state)
    graph.execute_next_step(state)

    version_result = state.tool_trace[1]["result"]
    assert isinstance(version_result, Mapping)
    assert version_result["row_count"] == 2
    version_rows = version_result["rows"]
    assert isinstance(version_rows, list)
    assert len(version_rows) == 2
    assert version_rows[0][0] < version_rows[1][0]
    assert version_rows[0][1] != version_rows[1][1]
    assert version_rows[0][2] != version_rows[1][2]
    version_evidence = _interpret_sql(
        state.tool_trace[1],
        EvidenceSourceType.METRIC_VERSION,
        "Metric metadata records a new definition hash and query for daily_active_users.",
    )
    graph.enter_hypothesis_testing(state)
    _bind_supporting_evidence(graph, state, version_evidence)
    validation = graph.validate_hypothesis(
        state,
        "H01",
        graph.hypothesis_manager.evidence(),
    )

    assert validation.to_status is IncidentStatus.ROOT_CAUSE_FOUND
    assert state.status is IncidentStatus.ROOT_CAUSE_FOUND
    assert state.root_cause is not None
    assert state.root_cause["root_cause_type"] == "metric_definition_change"
    assert state.root_cause["supporting_evidence_ids"] == [
        business_evidence.evidence_id,
        version_evidence.evidence_id,
    ]
    assert state.root_cause["independent_source_types"] == [
        "business_data",
        "metric_version",
    ]
    assert state.guardrail_usage.tool_calls == 2
    assert state.guardrail_usage.sql_calls == 2
    assert state.guardrail_usage.agent_rounds == 2
    assert state.guardrail_usage.blocked_calls == 0
    assert all(event.allowed for event in state.guardrail_events)
    assert all(
        event.reason not in {"unsafe_sql", "duplicate_tool_call", "invalid_tool_contract"}
        for event in state.guardrail_events
    )

    _assert_no_ground_truth_leakage(state)
    expected_after_runtime = _case("F11-001")
    assert state.root_cause["root_cause_type"] == expected_after_runtime.root_cause_type


def test_full_runtime_gate_guardrail_blocks_unsafe_sql_before_executor(
    tmp_path: Path,
) -> None:
    registry = build_default_tool_registry()
    executor = ToolExecutor(tmp_path / "unused.duckdb", registry=registry)
    graph = HarnessGraph(
        tool_executor=executor,
        guardrail_runtime=GuardrailRuntime(registry=registry),
    )
    unsafe_step = {
        "step_id": "S-UNSAFE",
        "purpose": "Attempt an unsafe mutation.",
        "hypothesis_id": "H01",
        "tool": "sql_query",
        "arguments": {"sql": "DROP TABLE events"},
        "expected_evidence": ["none"],
        "stop_condition": "stop when the guardrail blocks the call",
    }
    state = IncidentState(
        alert=_alert("INC-DAU-GUARDRAIL-001"),
        plan=[unsafe_step],
        status=IncidentStatus.EXECUTING,
    )

    graph.execute_next_step(state, unsafe_step)

    assert state.status is IncidentStatus.TOOL_FAILED
    assert state.root_cause is None
    assert state.tool_trace == []
    assert state.evidence == []
    assert state.guardrail_usage.tool_calls == 0
    assert state.guardrail_usage.sql_calls == 0
    assert state.guardrail_usage.blocked_calls == 1
    assert len(state.guardrail_events) == 1
    assert state.guardrail_events[0].event_type == "preflight"
    assert state.guardrail_events[0].allowed is False
    assert state.guardrail_events[0].reason == "unsafe_sql"
