import inspect
import json
from dataclasses import dataclass

import duckdb
import pytest
from pydantic import ValidationError

from agents.planner import (
    Alert,
    Hypothesis,
    InvestigationPlan,
    InvestigationStep,
    PlannerInput,
    load_metric_context,
    validate_plan_semantics,
)
from benchmark.evidence_interpreter import (
    EvidenceInterpretation,
    EvidencePolarity,
    IncidentEvidenceContext,
    RuntimeEvidenceInterpreter,
)
from harness.hypothesis import (
    SUPPORTED_CONFIDENCE_THRESHOLD,
    SUPPORTED_EVIDENCE_COUNT,
    EvidenceReference,
    HypothesisManager,
    HypothesisState,
    HypothesisStatus,
)
from tools.executor import ToolExecutionResult, ToolExecutor
from tools.registry import build_default_tool_registry
from validators.root_cause_validator import (
    MIN_INDEPENDENT_SOURCE_TYPES,
    MIN_SUPPORTING_EVIDENCE,
    RootCauseValidator,
)
from validators.sql_result import SqlResultEvidence, SqlResultValidation


@dataclass(frozen=True)
class _RuntimeReplay:
    manager: HypothesisManager
    hypothesis: HypothesisState
    interpretations: tuple[EvidenceInterpretation, ...]


def _hypotheses(root_cause_type: str, *, confidence: float) -> list[Hypothesis]:
    decoys = ["data_delay", "timezone_error"]
    if root_cause_type in decoys:
        decoys = ["missing_partition", "field_drift"]
    return [
        Hypothesis(
            hypothesis_id="H01",
            root_cause_type=root_cause_type,
            description=f"Test {root_cause_type} with fixed observations.",
            initial_confidence=confidence,
        ),
        Hypothesis(
            hypothesis_id="H02",
            root_cause_type=decoys[0],
            description="Deterministic decoy one.",
            initial_confidence=0.2,
        ),
        Hypothesis(
            hypothesis_id="H03",
            root_cause_type=decoys[1],
            description="Deterministic decoy two.",
            initial_confidence=0.1,
        ),
    ]


def _step(step_id: str, sql: str) -> InvestigationStep:
    return InvestigationStep(
        step_id=step_id,
        purpose="Inspect one fixed, bounded SQL observation.",
        hypothesis_id="H01",
        tool="sql_query",
        arguments={"sql": sql},
        expected_evidence=["A structured relation in the returned values."],
        stop_condition="Retain only evidence admitted by the runtime interpreter.",
    )


def _plan(
    root_cause_type: str,
    steps: list[InvestigationStep],
    *,
    confidence: float = 0.5,
) -> InvestigationPlan:
    return InvestigationPlan(
        incident_id="INC-SCOPED-SQL",
        hypotheses=_hypotheses(root_cause_type, confidence=confidence),
        steps=steps,
    )


def _context(metric_id: str) -> IncidentEvidenceContext:
    return IncidentEvidenceContext(
        incident_id="INC-SCOPED-SQL",
        metric_id=metric_id,
        observed_at="2026-01-30T00:00:00+00:00",
        target_date="2026-01-30",
    )


def _result(
    *,
    query_id: str,
    columns: list[str],
    row: list[object],
) -> ToolExecutionResult:
    result = {
        "query_id": query_id,
        "status": "success",
        "statement_type": "SELECT",
        "columns": columns,
        "column_types": ["BIGINT"] * len(columns),
        "rows": [row],
        "row_count": 1,
        "truncated": False,
        "duration_ms": 1.0,
        "error": None,
    }
    validation = SqlResultValidation(
        passed=True,
        evidence=SqlResultEvidence(
            query_id=query_id,
            statement_type="SELECT",
            columns=columns,
            column_types=["BIGINT"] * len(columns),
            row_count=1,
            truncated=False,
            usable=True,
        ),
    )
    return ToolExecutionResult(
        tool_name="sql_query",
        success=True,
        query_id=query_id,
        result=result,
        sql_validation=validation,
    )


def _run_fixed_plan(
    plan: InvestigationPlan,
    results: list[ToolExecutionResult],
    *,
    metric_id: str,
) -> _RuntimeReplay:
    manager = HypothesisManager()
    for hypothesis in plan.hypotheses:
        manager.create_hypothesis(hypothesis)
    interpreter = RuntimeEvidenceInterpreter(context=_context(metric_id))
    interpretations: list[EvidenceInterpretation] = []

    for step, result in zip(plan.steps, results, strict=True):
        state = manager.get_hypothesis(step.hypothesis_id)
        interpretation = interpreter.interpret(
            hypothesis=state,
            step=step,
            tool_result=result,
        )
        interpretations.append(interpretation)
        for decision in interpretation.decisions:
            if decision.polarity is EvidencePolarity.NEUTRAL:
                continue
            manager.register_evidence(decision.evidence)
            manager.attach_evidence(
                step.hypothesis_id,
                decision.evidence.evidence_id,
                supports=decision.polarity is EvidencePolarity.SUPPORTS,
            )

    return _RuntimeReplay(
        manager=manager,
        hypothesis=manager.get_hypothesis("H01"),
        interpretations=tuple(interpretations),
    )


def _f02_observation(
    *,
    step_id: str = "S01",
    query_id: str = "Q-F02-1",
) -> tuple[InvestigationStep, ToolExecutionResult]:
    sql = (
        "SELECT COUNT(*) AS row_count, "
        "COUNT(DISTINCT event_id) AS distinct_event_id_count, "
        "COUNT(*) - COUNT(DISTINCT event_id) AS duplicate_count "
        "FROM events WHERE event_name = 'run_ai_task' "
        "AND CAST(event_time AS DATE) = DATE '2026-01-30'"
    )
    return (
        _step(step_id, sql),
        _result(
            query_id=query_id,
            columns=["row_count", "distinct_event_id_count", "duplicate_count"],
            row=[126, 90, 36],
        ),
    )


def _f07_observation() -> tuple[InvestigationStep, ToolExecutionResult]:
    sql = (
        "SELECT COUNT(DISTINCT e.user_id) AS event_users, "
        "COUNT(DISTINCT s.user_id) AS subscribed_users "
        "FROM events AS e LEFT JOIN subscriptions AS s ON e.user_id = s.user_id "
        "WHERE CAST(e.event_time AS DATE) = DATE '2026-01-30'"
    )
    return (
        _step("S01", sql),
        _result(
            query_id="Q-F07-1",
            columns=["event_users", "subscribed_users"],
            row=[111, 26],
        ),
    )


def test_f02_scoped_sql_support_flows_into_hypothesis_manager_without_a_model(
) -> None:
    step, result = _f02_observation()
    replay = _run_fixed_plan(
        _plan("duplicate_batch", [step]),
        [result],
        metric_id="ai_task_count",
    )

    decision = replay.interpretations[0].decisions[0]
    assert decision.polarity is EvidencePolarity.SUPPORTS
    assert decision.evidence.observation["rule"] == "f02_duplicate_identity_counts"
    assert decision.evidence.source_type == "business_data"
    assert replay.manager.evidence() == (decision.evidence,)
    assert replay.hypothesis.supporting_evidence_ids == [decision.evidence.evidence_id]
    assert replay.hypothesis.confidence == pytest.approx(0.65)
    assert replay.hypothesis.status is HypothesisStatus.TESTING


def test_f07_survivor_probe_registers_no_runtime_evidence() -> None:
    step, result = _f07_observation()
    replay = _run_fixed_plan(
        _plan("join_filter", [step]),
        [result],
        metric_id="daily_active_users",
    )

    interpretation = replay.interpretations[0]
    assert interpretation.polarity is EvidencePolarity.NEUTRAL
    assert interpretation.evidence is None
    assert replay.manager.evidence() == ()
    assert replay.hypothesis.evidence_ids == []
    assert replay.hypothesis.supporting_evidence_ids == []
    assert replay.hypothesis.confidence == pytest.approx(0.5)
    assert replay.hypothesis.status is HypothesisStatus.PROPOSED


def test_production_interpreter_api_and_context_remain_ground_truth_free() -> None:
    forbidden = {"case_id", "fault_id", "expected_root_cause", "ground_truth"}
    constructor_fields = set(inspect.signature(RuntimeEvidenceInterpreter).parameters)
    interpret_fields = set(
        inspect.signature(RuntimeEvidenceInterpreter.interpret).parameters
    )

    assert constructor_fields == {"context"}
    assert interpret_fields == {"self", "hypothesis", "step", "tool_result"}
    assert forbidden.isdisjoint(IncidentEvidenceContext.model_fields)
    with pytest.raises(ValidationError):
        IncidentEvidenceContext.model_validate(
            {
                **_context("ai_task_count").model_dump(mode="json"),
                "expected_root_cause": "duplicate_batch",
            }
        )


def test_scoped_sql_evidence_serializes_and_has_a_deterministic_id() -> None:
    step, result = _f02_observation()
    first = _run_fixed_plan(
        _plan("duplicate_batch", [step]),
        [result],
        metric_id="ai_task_count",
    ).interpretations[0]
    second = _run_fixed_plan(
        _plan("duplicate_batch", [step]),
        [result],
        metric_id="ai_task_count",
    ).interpretations[0]

    first_evidence = first.decisions[0].evidence
    second_evidence = second.decisions[0].evidence
    serialized = json.dumps(first_evidence.model_dump(mode="json"), sort_keys=True)

    assert EvidenceReference.model_validate_json(serialized) == first_evidence
    assert EvidenceInterpretation.model_validate_json(first.model_dump_json()) == first
    assert first_evidence.evidence_id == second_evidence.evidence_id

    changed_step, changed_result = _f02_observation(query_id="Q-F02-2")
    changed = _run_fixed_plan(
        _plan("duplicate_batch", [changed_step]),
        [changed_result],
        metric_id="ai_task_count",
    ).interpretations[0]
    assert changed.decisions[0].evidence.evidence_id != first_evidence.evidence_id


def test_validator_contract_still_rejects_two_supports_from_one_source_type() -> None:
    first_step, first_result = _f02_observation()
    second_step, second_result = _f02_observation(
        step_id="S02",
        query_id="Q-F02-2",
    )
    replay = _run_fixed_plan(
        _plan(
            "duplicate_batch",
            [first_step, second_step],
            confidence=0.5,
        ),
        [first_result, second_result],
        metric_id="ai_task_count",
    )
    validation = RootCauseValidator().validate(
        replay.hypothesis,
        replay.manager.evidence(),
    )

    assert SUPPORTED_EVIDENCE_COUNT == 2
    assert SUPPORTED_CONFIDENCE_THRESHOLD == pytest.approx(0.75)
    assert MIN_SUPPORTING_EVIDENCE == 2
    assert MIN_INDEPENDENT_SOURCE_TYPES == 2
    assert replay.hypothesis.status is HypothesisStatus.SUPPORTED
    assert len(replay.hypothesis.supporting_evidence_ids) == 2
    assert validation.independent_source_types == ["business_data"]
    assert validation.validated is False
    assert validation.recommended_next_state == "HYPOTHESIS_TESTING"


def test_f11_real_sql_reaches_supported_and_validated_without_a_model(tmp_path) -> None:
    database_path = tmp_path / "f11-evidence.duckdb"
    with duckdb.connect(str(database_path)) as connection:
        connection.execute(
            "CREATE TABLE events (event_time TIMESTAMP, user_id INTEGER)"
        )
        connection.execute(
            "INSERT INTO events VALUES "
            "('2026-01-30 01:00:00', 1), ('2026-01-30 02:00:00', 2), "
            "('2026-01-30 03:00:00', 3), ('2026-01-30 04:00:00', 4)"
        )
        connection.execute(
            "CREATE TABLE daily_metrics (metric_date DATE, daily_active_users BIGINT)"
        )
        connection.execute("INSERT INTO daily_metrics VALUES (DATE '2026-01-30', 2)")
        connection.execute(
            "CREATE TABLE metric_versions (metric_id VARCHAR, version INTEGER, "
            "definition_hash VARCHAR, query VARCHAR, effective_at TIMESTAMP)"
        )
        connection.execute(
            "INSERT INTO metric_versions VALUES "
            "('daily_active_users', 1, 'hash-1', 'SELECT old', '2026-01-01'), "
            "('daily_active_users', 2, 'hash-2', 'SELECT new', '2026-01-30')"
        )

    hypotheses = [
        Hypothesis(
            hypothesis_id="H01",
            root_cause_type="metric_definition_change",
            description="The metric definition changed at the target date.",
            initial_confidence=0.45,
        ),
        Hypothesis(
            hypothesis_id="H02",
            root_cause_type="duplicate_batch",
            description="Deterministic decoy one.",
            initial_confidence=0.2,
        ),
        Hypothesis(
            hypothesis_id="H03",
            root_cause_type="null_value_anomaly",
            description="Deterministic decoy two.",
            initial_confidence=0.1,
        ),
    ]
    steps = [
        _step(
            "S01",
            (
                "SELECT COUNT(*) AS raw_event_count, "
                "COUNT(DISTINCT user_id) AS raw_user_count, "
                "(SELECT daily_active_users FROM daily_metrics "
                "WHERE metric_date = DATE '2026-01-30') AS daily_active_users "
                "FROM events WHERE CAST(event_time AS DATE) = DATE '2026-01-30'"
            ),
        ),
        _step(
            "S02",
            (
                "SELECT metric_id, version, definition_hash, query, effective_at "
                "FROM metric_versions WHERE metric_id = 'daily_active_users' "
                "ORDER BY version"
            ),
        ),
        InvestigationStep(
            step_id="S03",
            purpose="Inspect the first decoy.",
            hypothesis_id="H02",
            tool="sql_query",
            arguments={"sql": "SELECT COUNT(*) FROM events"},
            expected_evidence=["one structured SQL result"],
            stop_condition="stop after the bounded observation",
        ),
        InvestigationStep(
            step_id="S04",
            purpose="Inspect the second decoy.",
            hypothesis_id="H03",
            tool="sql_query",
            arguments={"sql": "SELECT COUNT(*) FROM events WHERE user_id IS NULL"},
            expected_evidence=["one structured SQL result"],
            stop_condition="stop after the bounded observation",
        ),
    ]
    plan = InvestigationPlan(
        incident_id="INC-SCOPED-SQL",
        hypotheses=hypotheses,
        steps=steps,
    )
    request = PlannerInput(
        alert=Alert(
            incident_id="INC-SCOPED-SQL",
            metric="daily_active_users",
            observed_at="2026-01-30T00:00:00+00:00",
            expected_value=4,
            observed_value=2,
            change_rate=-0.5,
            severity="high",
        ),
        metric_context=load_metric_context("daily_active_users"),
    )
    registry = build_default_tool_registry()
    validate_plan_semantics(plan, request, registry)

    manager = HypothesisManager()
    for hypothesis in plan.hypotheses:
        manager.create_hypothesis(hypothesis)
    interpreter = RuntimeEvidenceInterpreter(context=_context("daily_active_users"))
    executor = ToolExecutor(database_path, registry=registry)
    support_sources: list[str] = []

    for step in plan.steps[:2]:
        result = executor.execute_step(
            step,
            incident_id=plan.incident_id,
            metric_id="daily_active_users",
        )
        assert result.success is True
        assert result.sql_validation is not None
        assert result.sql_validation.passed is True
        interpretation = interpreter.interpret(
            hypothesis=manager.get_hypothesis("H01"),
            step=step,
            tool_result=result,
        )
        assert len(interpretation.decisions) == 1
        decision = interpretation.decisions[0]
        assert decision.polarity is EvidencePolarity.SUPPORTS
        manager.register_evidence(decision.evidence)
        manager.attach_evidence("H01", decision.evidence.evidence_id, supports=True)
        support_sources.append(decision.evidence.source_type)

    hypothesis = manager.get_hypothesis("H01")
    validation = RootCauseValidator().validate(hypothesis, manager.evidence())

    assert support_sources == ["business_data", "metric_version"]
    assert len(hypothesis.supporting_evidence_ids) == 2
    assert hypothesis.confidence == pytest.approx(0.75)
    assert hypothesis.status is HypothesisStatus.SUPPORTED
    assert validation.validated is True
    assert validation.independent_source_types == [
        "business_data",
        "metric_version",
    ]
