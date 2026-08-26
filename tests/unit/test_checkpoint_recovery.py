from pathlib import Path

import pytest

from agents.planner import (
    Hypothesis,
    InvestigationPlan,
    InvestigationStep,
    MetricContext,
    PlannerRunResult,
)
from harness.checkpoint import (
    CheckpointManager,
    FileCheckpointStore,
    ResumeAction,
    ResumeMetadata,
)
from harness.graph import HarnessGraph, HarnessTransitionError
from harness.guardrails import GuardrailPolicy, GuardrailRuntime
from harness.hypothesis import EvidenceReference, HypothesisManager, HypothesisStatus
from harness.state import IncidentState, IncidentStatus
from tools.data_quality import DataQualityCheckResult
from tools.executor import ToolExecutor
from tools.sql_runner import SqlExecutionResponse


class SimulatedProcessExit(BaseException):
    """Represent an interruption after a durable checkpoint was written."""


def _alert(incident_id: str = "INC-RECOVERY-001") -> dict[str, object]:
    return {
        "incident_id": incident_id,
        "metric": "daily_active_users",
        "observed_at": "2026-08-25T00:00:00Z",
        "expected_value": 100.0,
        "observed_value": 75.0,
        "change_rate": -0.25,
        "severity": "high",
    }


def _step(
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
        expected_evidence=["the structured result"],
        stop_condition="retain the observation",
    )


def _plan(
    steps: list[InvestigationStep],
    incident_id: str = "INC-RECOVERY-001",
) -> InvestigationPlan:
    return InvestigationPlan(
        incident_id=incident_id,
        hypotheses=[
            Hypothesis(
                hypothesis_id="H01",
                root_cause_type="missing_partition",
                description="The target partition may be missing.",
                initial_confidence=0.55,
            ),
            Hypothesis(
                hypothesis_id="H02",
                root_cause_type="data_delay",
                description="The target data may have arrived late.",
                initial_confidence=0.25,
            ),
            Hypothesis(
                hypothesis_id="H03",
                root_cause_type="null_value_anomaly",
                description="The target rows may contain nulls.",
                initial_confidence=0.20,
            ),
        ],
        steps=steps,
    )


class _CountingPlanner:
    def __init__(self, plan: InvestigationPlan) -> None:
        self.plan = plan
        self.calls = 0

    def run(self, _alert: object, _metric_context: object) -> PlannerRunResult:
        self.calls += 1
        return PlannerRunResult(plan=self.plan)


def _manager(tmp_path: Path) -> CheckpointManager:
    return CheckpointManager(FileCheckpointStore(tmp_path / "checkpoints"))


def _advance_to_next_execution(graph: HarnessGraph, state: IncidentState) -> None:
    graph.enter_hypothesis_testing(state)
    graph.request_more_evidence(state)


def _sql_response(query_id: str) -> SqlExecutionResponse:
    return SqlExecutionResponse(
        query_id=query_id,
        status="success",
        statement_type="SELECT",
        columns=["answer"],
        rows=[[1]],
        row_count=1,
    )


def test_restart_resumes_sql_at_s02_without_replanning_or_repeating_s01(tmp_path) -> None:
    calls: list[str] = []
    plan = _plan(
        [
            _step("S01", "sql_query", {"sql": "SELECT 1"}),
            _step("S02", "sql_query", {"sql": "SELECT 2"}),
        ]
    )

    def execute_sql(
        _database_path: str | Path,
        sql: str,
        **_: object,
    ) -> SqlExecutionResponse:
        calls.append(sql)
        return _sql_response(f"Q{len(calls):02d}")

    first_planner = _CountingPlanner(plan)
    manager = _manager(tmp_path)
    first_graph = HarnessGraph(
        planner=first_planner,
        tool_executor=ToolExecutor("first.duckdb", sql_execution=execute_sql),
        checkpoint_manager=manager,
    )
    state = IncidentState(alert=_alert())
    first_graph.plan_incident(
        state,
        metric_context=MetricContext(
            metric_id="daily_active_users",
            source_tables=["events"],
        ),
    )
    first_graph.execute_next_step(state)

    assert first_planner.calls == 1
    assert calls == ["SELECT 1"]
    assert state.status is IncidentStatus.VALIDATING

    try:
        raise SimulatedProcessExit("process stopped after S01 checkpoint")
    except SimulatedProcessExit:
        pass

    second_planner = _CountingPlanner(plan)
    second_executor = ToolExecutor("second.duckdb", sql_execution=execute_sql)
    second_graph = HarnessGraph(
        planner=second_planner,
        tool_executor=second_executor,
        guardrail_runtime=GuardrailRuntime(registry=second_executor.registry),
        hypothesis_manager=HypothesisManager(),
        checkpoint_manager=manager,
    )
    restored, resume = second_graph.resume_latest("INC-RECOVERY-001")

    assert second_planner.calls == 0
    assert resume.action is ResumeAction.CONTINUE_VALIDATION
    assert restored.tool_trace[0]["query_id"] == "Q01"
    assert second_graph.hypothesis_manager.get_hypothesis("H01").hypothesis_id == "H01"

    _advance_to_next_execution(second_graph, restored)
    assert restored.retry_count == 1
    assert second_graph.resume_plan(restored).next_step_id == "S02"
    second_graph.execute_next_step(restored)

    assert calls == ["SELECT 1", "SELECT 2"]
    assert [item["query_id"] for item in restored.tool_trace] == ["Q01", "Q02"]
    latest = manager.restore_latest("INC-RECOVERY-001")
    assert latest.state.retry_count == 1
    assert latest.resume.completed_step_ids == ["S01", "S02"]


def test_restart_resumes_data_quality_at_s02_without_repeating_s01(tmp_path) -> None:
    calls: list[str] = []
    plan = _plan(
        [
            _step(
                "S01",
                "check_null_rate",
                {"table": "events", "column": "user_id"},
            ),
            _step("S02", "detect_schema_drift", {"table": "events"}),
        ]
    )

    def fake_null_rate(
        _database_path: str | Path,
        table: str,
        column: str,
        **_: object,
    ) -> DataQualityCheckResult:
        calls.append("check_null_rate")
        return DataQualityCheckResult(
            check_name="check_null_rate",
            status="success",
            passed=True,
            table=table,
            column=column,
            observed_value=0.0,
            threshold=0.01,
            query_id="DQ-NULL-001",
        )

    def fake_schema_drift(
        _database_path: str | Path,
        table: str,
        **_: object,
    ) -> DataQualityCheckResult:
        calls.append("detect_schema_drift")
        return DataQualityCheckResult(
            check_name="detect_schema_drift",
            status="success",
            passed=True,
            table=table,
            query_id="DQ-SCHEMA-001",
        )

    adapters = {
        "check_null_rate": fake_null_rate,
        "detect_schema_drift": fake_schema_drift,
    }
    manager = _manager(tmp_path)
    first_executor = ToolExecutor("first.duckdb", data_quality_execution=adapters)
    first_graph = HarnessGraph(
        tool_executor=first_executor,
        checkpoint_manager=manager,
    )
    state = IncidentState(alert=_alert("INC-DQ-RECOVERY"), plan=[step.model_dump(mode="json") for step in plan.steps], status=IncidentStatus.EXECUTING)
    first_graph.execute_next_step(state)
    assert calls == ["check_null_rate"]

    second_executor = ToolExecutor("second.duckdb", data_quality_execution=adapters)
    second_graph = HarnessGraph(
        tool_executor=second_executor,
        guardrail_runtime=GuardrailRuntime(registry=second_executor.registry),
        checkpoint_manager=manager,
    )
    restored, resume = second_graph.resume_latest("INC-DQ-RECOVERY")
    assert resume.action is ResumeAction.CONTINUE_VALIDATION
    _advance_to_next_execution(second_graph, restored)
    second_graph.execute_next_step(restored)

    assert calls == ["check_null_rate", "detect_schema_drift"]
    assert [item["tool_name"] for item in restored.tool_trace] == [
        "check_null_rate",
        "detect_schema_drift",
    ]


def test_restart_preserves_sql_budget_and_blocks_at_remaining_boundary(tmp_path) -> None:
    calls: list[str] = []

    def execute_sql(
        _database_path: str | Path,
        sql: str,
        **_: object,
    ) -> SqlExecutionResponse:
        calls.append(sql)
        return _sql_response(f"Q{len(calls):02d}")

    plan = _plan(
        [
            _step("S01", "sql_query", {"sql": "SELECT 1"}),
            _step("S02", "sql_query", {"sql": "SELECT 2"}),
        ],
        incident_id="INC-BUDGET-RECOVERY",
    )
    policy = GuardrailPolicy(max_sql_calls=1)
    manager = _manager(tmp_path)
    first_graph = HarnessGraph(
        tool_executor=ToolExecutor("first.duckdb", sql_execution=execute_sql),
        guardrail_policy=policy,
        checkpoint_manager=manager,
    )
    state = IncidentState(
        alert=_alert("INC-BUDGET-RECOVERY"),
        plan=[step.model_dump(mode="json") for step in plan.steps],
        status=IncidentStatus.EXECUTING,
    )
    first_graph.execute_next_step(state)
    assert state.guardrail_usage.sql_calls == 1

    second_executor = ToolExecutor("second.duckdb", sql_execution=execute_sql)
    second_graph = HarnessGraph(
        tool_executor=second_executor,
        guardrail_runtime=GuardrailRuntime(policy=policy, registry=second_executor.registry),
        checkpoint_manager=manager,
    )
    restored, _ = second_graph.resume_latest("INC-BUDGET-RECOVERY")
    assert restored.guardrail_usage.sql_calls == 1
    _advance_to_next_execution(second_graph, restored)
    second_graph.execute_next_step(restored)

    assert calls == ["SELECT 1"]
    assert restored.status is IncidentStatus.BUDGET_EXCEEDED
    assert restored.guardrail_usage.blocked_calls == 1
    assert manager.restore_latest("INC-BUDGET-RECOVERY").state.guardrail_usage.sql_calls == 1


def test_restart_preserves_duplicate_fingerprint_history(tmp_path) -> None:
    calls: list[str] = []

    def execute_sql(
        _database_path: str | Path,
        sql: str,
        **_: object,
    ) -> SqlExecutionResponse:
        calls.append(sql)
        return _sql_response(f"Q{len(calls):02d}")

    plan = _plan(
        [
            _step("S01", "sql_query", {"sql": "SELECT 1"}),
            _step("S02", "sql_query", {"sql": "SELECT 1"}),
        ],
        incident_id="INC-DUP-RECOVERY",
    )
    manager = _manager(tmp_path)
    first_graph = HarnessGraph(
        tool_executor=ToolExecutor("first.duckdb", sql_execution=execute_sql),
        checkpoint_manager=manager,
    )
    state = IncidentState(
        alert=_alert("INC-DUP-RECOVERY"),
        plan=[step.model_dump(mode="json") for step in plan.steps],
        status=IncidentStatus.EXECUTING,
    )
    first_graph.execute_next_step(state)
    _advance_to_next_execution(first_graph, state)

    second_executor = ToolExecutor("second.duckdb", sql_execution=execute_sql)
    second_graph = HarnessGraph(
        tool_executor=second_executor,
        guardrail_runtime=GuardrailRuntime(registry=second_executor.registry),
        checkpoint_manager=manager,
    )
    restored, _ = second_graph.resume_latest("INC-DUP-RECOVERY")
    assert restored.guardrail_usage.fingerprint_counts
    second_graph.execute_next_step(restored)

    assert calls == ["SELECT 1"]
    assert restored.status is IncidentStatus.TOOL_FAILED
    assert restored.guardrail_events[-1].reason == "duplicate_tool_call"


def test_restart_preserves_explicit_retry_replay_for_completed_step(tmp_path) -> None:
    calls: list[str] = []

    def execute_sql(
        _database_path: str | Path,
        sql: str,
        **_: object,
    ) -> SqlExecutionResponse:
        calls.append(sql)
        return _sql_response(f"Q{len(calls):02d}")

    plan = _plan(
        [_step("S01", "sql_query", {"sql": "SELECT 1"})],
        incident_id="INC-RETRY-RECOVERY",
    )
    manager = _manager(tmp_path)
    first_graph = HarnessGraph(
        tool_executor=ToolExecutor("first.duckdb", sql_execution=execute_sql),
        checkpoint_manager=manager,
    )
    state = IncidentState(
        alert=_alert("INC-RETRY-RECOVERY"),
        plan=[step.model_dump(mode="json") for step in plan.steps],
        status=IncidentStatus.EXECUTING,
    )
    first_graph.execute_next_step(state)
    _advance_to_next_execution(first_graph, state)

    second_executor = ToolExecutor("second.duckdb", sql_execution=execute_sql)
    second_graph = HarnessGraph(
        tool_executor=second_executor,
        guardrail_runtime=GuardrailRuntime(registry=second_executor.registry),
        checkpoint_manager=manager,
    )
    restored, resume = second_graph.resume_latest("INC-RETRY-RECOVERY")

    assert restored.retry_count == 1
    assert resume.action is ResumeAction.EXECUTE_NEXT_TOOL
    assert resume.next_step_id == "S01"
    assert second_graph.resume_plan(restored).next_step_id == "S01"

    second_graph.execute_next_step(restored)

    assert calls == ["SELECT 1", "SELECT 1"]
    assert restored.guardrail_usage.fingerprint_counts
    assert restored.status is IncidentStatus.VALIDATING


def test_persisted_planning_checkpoint_enters_execution_without_replanning(tmp_path) -> None:
    plan = _plan(
        [_step("S01", "sql_query", {"sql": "SELECT 1"})],
        incident_id="INC-PLANNING-RECOVERY",
    )
    state = IncidentState(
        alert=_alert("INC-PLANNING-RECOVERY"),
        plan=[step.model_dump(mode="json") for step in plan.steps],
        planner_metadata={"fallback_used": False},
        status=IncidentStatus.PLANNING,
    )
    manager = _manager(tmp_path)
    manager.save(state, reason="planner output persisted")

    planner = _CountingPlanner(plan)
    graph = HarnessGraph(planner=planner, checkpoint_manager=manager)
    restored, resume = graph.resume_latest("INC-PLANNING-RECOVERY")

    assert resume.action is ResumeAction.ENTER_EXECUTING
    assert planner.calls == 0
    assert restored.plan == state.plan
    graph.transition(restored, IncidentStatus.EXECUTING)
    assert restored.status is IncidentStatus.EXECUTING
    assert planner.calls == 0


def test_fix_proposal_is_checkpointed_before_approval_transition(tmp_path) -> None:
    proposal = {
        "repair_type": "backfill_partition",
        "target": "events/2026-08-25",
    }
    store = FileCheckpointStore(tmp_path / "checkpoints")
    manager = CheckpointManager(store)
    graph = HarnessGraph(checkpoint_manager=manager)
    state = IncidentState(
        alert=_alert("INC-FIX-RECOVERY"),
        root_cause={"hypothesis_id": "H01", "confidence": 0.9},
        status=IncidentStatus.ROOT_CAUSE_FOUND,
    )

    graph.propose_fix(state, proposal)

    fix_checkpoint = next(
        checkpoint
        for checkpoint in store.list("INC-FIX-RECOVERY")
        if checkpoint.state.status is IncidentStatus.FIX_PROPOSED
    )
    assert fix_checkpoint.state.fix_proposal == proposal

    restored_graph = HarnessGraph(checkpoint_manager=manager)
    restored = manager.restore(fix_checkpoint)
    resume = restored_graph.restore_runtime(restored.state, restored.resume)
    assert restored.state.status is IncidentStatus.FIX_PROPOSED
    assert restored.state.fix_proposal == proposal
    assert resume.action is ResumeAction.CONTINUE_POST_ROOT_CAUSE_FLOW

    restored_graph.transition(restored.state, IncidentStatus.AWAITING_APPROVAL)
    assert restored.state.status is IncidentStatus.AWAITING_APPROVAL


def test_hypothesis_manager_rehydrates_and_authoritatively_validates(tmp_path) -> None:
    initial_manager = HypothesisManager()
    hypothesis = initial_manager.create_hypothesis(
        Hypothesis(
            hypothesis_id="H01",
            root_cause_type="missing_partition",
            description="The target partition may be missing.",
            initial_confidence=0.55,
        )
    )
    initial_manager.start_testing("H01")
    evidence_one = EvidenceReference(
        evidence_id="E01",
        source_type="business_data",
        description="The event partition is empty.",
    )
    evidence_two = EvidenceReference(
        evidence_id="E02",
        source_type="operational_metadata",
        description="Partition metadata reports no rows.",
    )
    initial_manager.register_evidence(evidence_one)
    initial_manager.attach_evidence("H01", "E01", supports=True)
    state = IncidentState(
        alert=_alert("INC-HYP-RECOVERY"),
        hypotheses=[hypothesis.model_dump(mode="json")],
        evidence=[evidence_one.model_dump(mode="json")],
        status=IncidentStatus.HYPOTHESIS_TESTING,
    )
    manager = _manager(tmp_path)
    manager.save(
        state,
        reason="hypothesis testing",
        resume=ResumeMetadata(resume_action=ResumeAction.CONTINUE_HYPOTHESIS_TESTING),
    )

    graph = HarnessGraph(
        hypothesis_manager=HypothesisManager(),
        checkpoint_manager=manager,
    )
    restored, resume = graph.resume_latest("INC-HYP-RECOVERY")
    restored_hypothesis = graph.hypothesis_manager.get_hypothesis("H01")

    assert resume.action is ResumeAction.CONTINUE_HYPOTHESIS_TESTING
    assert restored_hypothesis.status is HypothesisStatus.TESTING
    assert restored_hypothesis.supporting_evidence_ids == ["E01"]
    assert graph.hypothesis_manager.evidence() == (evidence_one,)

    graph.attach_evidence(restored, "H01", "E02", supports=True)
    transition = graph.validate_hypothesis(
        restored,
        "H01",
        [evidence_one, evidence_two],
    )

    assert transition.to_status is IncidentStatus.ROOT_CAUSE_FOUND
    assert restored.status is IncidentStatus.ROOT_CAUSE_FOUND
    assert restored.root_cause is not None
    assert restored.root_cause["hypothesis_id"] == "H01"


def test_terminal_checkpoint_restores_as_terminal_and_cannot_execute_tools(tmp_path) -> None:
    calls: list[str] = []

    def execute_sql(
        _database_path: str | Path,
        sql: str,
        **_: object,
    ) -> SqlExecutionResponse:
        calls.append(sql)
        return _sql_response("Q-UNEXPECTED")

    manager = _manager(tmp_path)
    graph = HarnessGraph(
        tool_executor=ToolExecutor("first.duckdb", sql_execution=execute_sql),
        checkpoint_manager=manager,
    )
    state = IncidentState(
        alert=_alert("INC-TERMINAL-RECOVERY"),
        plan=[
            _step("S01", "sql_query", {"sql": "SELECT 1"}).model_dump(mode="json")
        ],
        status=IncidentStatus.EXECUTING,
    )
    graph.mark_budget_exceeded(state, reason="test terminal checkpoint")

    restored_graph = HarnessGraph(
        tool_executor=ToolExecutor("second.duckdb", sql_execution=execute_sql),
        checkpoint_manager=manager,
    )
    restored, resume = restored_graph.resume_latest("INC-TERMINAL-RECOVERY")

    assert resume.action is ResumeAction.TERMINAL
    assert resume.terminal is True
    assert restored.status is IncidentStatus.BUDGET_EXCEEDED
    assert restored_graph.next_pending_step(restored) is None
    with pytest.raises(HarnessTransitionError):
        restored_graph.execute_next_step(restored)
    assert calls == []
