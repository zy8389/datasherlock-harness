from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from agents.planner import (
    Hypothesis,
    InvestigationPlan,
    InvestigationStep,
    MetricContext,
    PlannerRunResult,
)
from benchmark.fault_injector import inject_case
from config.faults import GroundTruthCase, load_ground_truth_cases
from data.generator import generate_dataset, write_outputs
from harness.graph import HarnessGraph
from harness.hypothesis import EvidenceReference, HypothesisStatus
from harness.state import IncidentState, IncidentStatus
from tools.executor import ToolExecutor

START_DATE = pd.Timestamp("2026-01-01")


class _StaticPlanner:
    def __init__(self, plan: InvestigationPlan) -> None:
        self.plan = plan

    def run(self, _alert: object, _metric_context: object) -> PlannerRunResult:
        return PlannerRunResult(plan=self.plan)


@pytest.fixture(scope="module")
def baseline() -> dict[str, pd.DataFrame]:
    return generate_dataset(500, 30, 10_000, 42, START_DATE)


@pytest.fixture(scope="module")
def cases() -> dict[str, GroundTruthCase]:
    return {
        case.case_id: case
        for case in load_ground_truth_cases(Path("benchmark/ground_truth"))
        if case.case_id in {"F01-001", "F03-001", "F10-001"}
    }


def _plan(
    case: GroundTruthCase,
    steps: list[InvestigationStep],
) -> InvestigationPlan:
    alternative_types = [
        root_cause_type
        for root_cause_type in (
            "data_delay",
            "null_value_anomaly",
            "schema_change",
            "metric_definition_change",
        )
        if root_cause_type != case.root_cause_type
    ]
    return InvestigationPlan(
        incident_id=f"INC-{case.case_id}",
        hypotheses=[
            Hypothesis(
                hypothesis_id="H01",
                root_cause_type=case.root_cause_type,
                description=f"The {case.root_cause_type} hypothesis needs evidence.",
                initial_confidence=0.55,
            ),
            Hypothesis(
                hypothesis_id="H02",
                root_cause_type=alternative_types[0],
                description="The data may have arrived late.",
                initial_confidence=0.20,
            ),
            Hypothesis(
                hypothesis_id="H03",
                root_cause_type=alternative_types[1],
                description="The data may contain anomalous nulls.",
                initial_confidence=0.20,
            ),
        ],
        steps=steps,
    )


def _step(
    *,
    step_id: str,
    tool: str,
    arguments: dict[str, object],
) -> InvestigationStep:
    return InvestigationStep(
        step_id=step_id,
        purpose=f"Run the bounded {tool} check.",
        hypothesis_id="H01",
        tool=tool,
        arguments=arguments,
        expected_evidence=["the structured check result"],
        stop_condition="retain the observation for hypothesis testing",
    )


def _runtime(
    case: GroundTruthCase,
    database_path: Path,
    plan: InvestigationPlan,
) -> tuple[HarnessGraph, IncidentState]:
    graph = HarnessGraph(
        planner=_StaticPlanner(plan),
        tool_executor=ToolExecutor(database_path),
    )
    state = IncidentState(
        alert={
            "incident_id": plan.incident_id,
            "metric": case.affected_metric,
            "observed_at": f"{case.injection.metric_date}T00:00:00Z",
            "expected_value": 100.0,
            "observed_value": 75.0,
            "change_rate": -0.25,
            "severity": "high",
        }
    )
    graph.plan_incident(
        state,
        metric_context=MetricContext(
            metric_id=case.affected_metric,
            source_tables=case.affected_assets,
        ),
    )
    return graph, state


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


def test_f03_null_anomaly_runs_through_graph_without_one_evidence_confirmation(
    tmp_path: Path,
    baseline: dict[str, pd.DataFrame],
    cases: dict[str, GroundTruthCase],
) -> None:
    case = cases["F03-001"]
    database_path = _materialize_fault(baseline, case, tmp_path / "F03-001")
    plan = _plan(
        case,
        [
            _step(
                step_id="S01",
                tool="check_null_rate",
                arguments={
                    "table": "events",
                    "column": "user_id",
                    "threshold": 0.01,
                    "scope": {
                        "equals": {"device_type": ["ios", "android"]},
                        "time_column": "event_time",
                        "start": "2026-01-30T00:00:00+00:00",
                        "end": "2026-01-31T00:00:00+00:00",
                    },
                },
            )
        ],
    )
    graph, state = _runtime(case, database_path, plan)

    graph.execute_next_step(state)

    assert state.status is IncidentStatus.VALIDATING
    assert state.tool_trace[0]["tool_name"] == "check_null_rate"
    assert state.tool_trace[0]["result"]["passed"] is False
    evidence = graph.hypothesis_manager.evidence()
    assert len(evidence) == 1
    assert evidence[0].evidence_id.startswith("dq-")
    assert evidence[0].source_type == "business_data"
    assert evidence[0].query_id == state.tool_trace[0]["query_id"]

    graph.enter_hypothesis_testing(state)
    graph.attach_evidence(state, "H01", evidence[0].evidence_id, supports=True)
    validation = graph.validate_hypothesis(state, "H01", evidence)

    assert validation.to_status is IncidentStatus.HYPOTHESIS_TESTING
    assert state.status is IncidentStatus.HYPOTHESIS_TESTING
    assert graph.hypothesis_manager.get_hypothesis("H01").status is HypothesisStatus.TESTING
    assert state.root_cause is None
    assert state.model_dump(mode="json")["evidence"]


def test_f10_schema_drift_produces_schema_evidence_and_requires_independent_support(
    tmp_path: Path,
    baseline: dict[str, pd.DataFrame],
    cases: dict[str, GroundTruthCase],
) -> None:
    case = cases["F10-001"]
    database_path = _materialize_fault(baseline, case, tmp_path / "F10-001")
    plan = _plan(
        case,
        [
            _step(
                step_id="S01",
                tool="detect_schema_drift",
                arguments={"table": "events"},
            )
        ],
    )
    graph, state = _runtime(case, database_path, plan)

    graph.execute_next_step(state)
    evidence = graph.hypothesis_manager.evidence()

    assert state.status is IncidentStatus.VALIDATING
    assert len(evidence) == 1
    assert evidence[0].source_type == "schema_metadata"
    assert evidence[0].observation["details"]["type_changes"] == [
        {
            "column": "app_build_number",
            "previous_type": "BIGINT",
            "current_type": "VARCHAR",
        }
    ]

    graph.enter_hypothesis_testing(state)
    graph.attach_evidence(state, "H01", evidence[0].evidence_id, supports=True)
    validation = graph.validate_hypothesis(state, "H01", evidence)

    assert validation.to_status is IncidentStatus.HYPOTHESIS_TESTING
    assert state.status is IncidentStatus.HYPOTHESIS_TESTING


def test_f01_business_data_quality_and_operational_sql_can_validate_together(
    tmp_path: Path,
    baseline: dict[str, pd.DataFrame],
    cases: dict[str, GroundTruthCase],
) -> None:
    case = cases["F01-001"]
    database_path = _materialize_fault(baseline, case, tmp_path / "F01-001")
    plan = _plan(
        case,
        [
            _step(
                step_id="S01",
                tool="check_freshness",
                arguments={
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
            ),
            _step(
                step_id="S02",
                tool="sql_query",
                arguments={
                    "sql": (
                        "SELECT partition_value, row_count, status "
                        "FROM partition_metadata "
                        "WHERE table_name = 'events' "
                        "AND partition_value = '2026-01-30/android'"
                    )
                },
            ),
        ],
    )
    graph, state = _runtime(case, database_path, plan)

    graph.execute_next_step(state)
    business_evidence = graph.hypothesis_manager.evidence()[0]
    assert business_evidence.source_type == "business_data"
    graph.enter_hypothesis_testing(state)
    graph.attach_evidence(state, "H01", business_evidence.evidence_id, supports=True)

    graph.request_more_evidence(state)
    graph.execute_next_step(state, plan.steps[1])
    sql_trace = state.tool_trace[-1]
    operational_evidence = EvidenceReference(
        evidence_id=f"sql-{sql_trace['query_id']}",
        source_type="operational_metadata",
        description="The target Android partition has zero rows and is missing.",
        query_id=sql_trace["query_id"],
        observation={"result": sql_trace["result"]},
    )
    graph.register_evidence(state, operational_evidence)
    graph.attach_evidence(state, "H01", operational_evidence.evidence_id, supports=True)
    graph.enter_hypothesis_testing(state)

    validation = graph.validate_hypothesis(
        state,
        "H01",
        graph.hypothesis_manager.evidence(),
    )

    assert validation.to_status is IncidentStatus.ROOT_CAUSE_FOUND
    assert state.status is IncidentStatus.ROOT_CAUSE_FOUND
    assert state.root_cause is not None
    assert state.root_cause["hypothesis_id"] == "H01"
    assert state.root_cause["independent_source_types"] == [
        "business_data",
        "operational_metadata",
    ]
    assert state.model_dump_json()
