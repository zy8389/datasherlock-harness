import json

import pytest

from harness.graph import (
    ALLOWED_TRANSITIONS,
    HarnessGraph,
    HarnessTransitionError,
)
from harness.hypothesis import EvidenceReference, HypothesisStatus
from harness.state import IncidentState, IncidentStatus
from agents.planner import (
    Hypothesis,
    InvestigationPlan,
    InvestigationStep,
    MetricContext,
    Planner,
    PlannerFallbackReason,
    PlannerRunResult,
)
from tools.executor import ToolExecutionResult, ToolExecutor
from tools.sql_runner import SqlExecutionResponse


def _alert() -> dict[str, object]:
    return {
        "incident_id": "INC-001",
        "metric": "daily_active_users",
        "observed_at": "2026-08-25T00:00:00Z",
        "expected_value": 100.0,
        "observed_value": 75.0,
        "change_rate": -0.25,
        "severity": "high",
    }


def _plan() -> InvestigationPlan:
    hypotheses = [
        Hypothesis(
            hypothesis_id=f"H0{index}",
            root_cause_type=f"cause_{index}",
            description=f"Candidate cause {index}.",
            initial_confidence=0.3,
        )
        for index in range(1, 4)
    ]
    return InvestigationPlan(
        incident_id="INC-001",
        hypotheses=hypotheses,
        steps=[
            InvestigationStep(
                step_id="S01",
                purpose="Inspect event volume.",
                hypothesis_id="H01",
                tool="sql_query",
                arguments={"sql": "SELECT 1"},
                expected_evidence=["row count"],
                stop_condition="stop after one bounded query",
            )
        ],
    )


class _FallbackPlanner:
    def run(self, alert: object, metric_context: object) -> PlannerRunResult:
        return PlannerRunResult(
            plan=_plan(),
            fallback_used=True,
            fallback_reason=PlannerFallbackReason.RETRY_EXHAUSTED,
        )


class _SuccessfulExecutor:
    def execute_step(self, step: object, **_: object) -> ToolExecutionResult:
        return ToolExecutionResult(
            tool_name="sql_query",
            success=True,
            query_id="Q01",
            result={"status": "success", "rows": [[1]]},
        )


def _real_plan_payload() -> dict[str, object]:
    return {
        "incident_id": "INC-001",
        "hypotheses": [
            {
                "hypothesis_id": "H01",
                "root_cause_type": "missing_partition",
                "description": "The target partition may be missing.",
                "initial_confidence": 0.55,
            },
            {
                "hypothesis_id": "H02",
                "root_cause_type": "data_delay",
                "description": "The target data may have arrived late.",
                "initial_confidence": 0.25,
            },
            {
                "hypothesis_id": "H03",
                "root_cause_type": "null_value_anomaly",
                "description": "The target rows may contain null user IDs.",
                "initial_confidence": 0.20,
            },
        ],
        "steps": [
            {
                "step_id": "S01",
                "purpose": "Inspect the bounded event result.",
                "hypothesis_id": "H01",
                "tool": "sql_query",
                "arguments": {"sql": "SELECT 1"},
                "expected_evidence": ["event result"],
                "stop_condition": "stop after the bounded query",
            }
        ],
    }


def _real_runtime(
    *,
    source_types: tuple[str, str] = ("business_data", "operational_metadata"),
) -> tuple[HarnessGraph, IncidentState, list[EvidenceReference]]:
    query_number = 0

    def execute_sql(
        _database_path: str,
        _sql: str,
        **_kwargs: object,
    ) -> SqlExecutionResponse:
        nonlocal query_number
        query_number += 1
        return SqlExecutionResponse(
            query_id=f"Q{query_number:02d}",
            status="success",
            statement_type="SELECT",
            columns=["answer"],
            rows=[[1]],
            row_count=1,
        )

    planner = Planner(
        lambda _: json.dumps(_real_plan_payload()),
        max_retries=0,
    )
    graph = HarnessGraph(
        planner=planner,
        tool_executor=ToolExecutor("unused.duckdb", sql_execution=execute_sql),
    )
    state = IncidentState(alert=_alert())
    graph.plan_incident(
        state,
        metric_context=MetricContext(
            metric_id="daily_active_users",
            source_tables=["events"],
        ),
    )
    graph.execute_next_step(state)
    graph.enter_hypothesis_testing(state)
    evidence = [
        EvidenceReference(
            evidence_id="E01",
            source_type=source_types[0],
            description="The target event partition is empty.",
        ),
        EvidenceReference(
            evidence_id="E02",
            source_type=source_types[1],
            description="Partition metadata is missing.",
        ),
    ]
    for reference in evidence:
        graph.register_evidence(state, reference)
    graph.attach_evidence(state, "H01", "E01", supports=True)
    graph.attach_evidence(state, "H01", "E02", supports=True)
    return graph, state, evidence


def test_planner_output_and_fallback_metadata_are_persisted() -> None:
    graph = HarnessGraph(planner=_FallbackPlanner())
    state = IncidentState(alert=_alert())

    graph.plan_incident(state)

    assert state.status is IncidentStatus.EXECUTING
    assert state.plan[0]["tool"] == "sql_query"
    assert len(state.hypotheses) == 3
    assert state.planner_metadata == {
        "fallback_used": True,
        "fallback_reason": "RETRY_EXHAUSTED",
        "planner_repair_count": 0,
        "transport_retry_count": 0,
        "model_latency_ms": None,
        "provider": None,
        "model": None,
    }


def test_execution_result_becomes_unvalidated_observation() -> None:
    graph = HarnessGraph(tool_executor=_SuccessfulExecutor())
    state = IncidentState(status=IncidentStatus.EXECUTING, plan=[_plan().steps[0].model_dump(mode="json")])

    graph.execute_next_step(state)

    assert state.status is IncidentStatus.VALIDATING
    assert state.tool_trace[0]["query_id"] == "Q01"
    assert state.evidence[0]["evidence_id"] == "Q01"
    assert state.evidence[0]["evidence_type"] == "tool_result"
    assert state.evidence[0]["root_cause_validated"] is False


def _to_hypothesis_testing(
    graph: HarnessGraph,
    *,
    source_types: tuple[str, str] = ("business_data", "operational_metadata"),
) -> IncidentState:
    state = IncidentState(alert=_alert(), plan=[{"step_id": "P01"}])
    graph.hypothesis_manager.create_hypothesis(
        Hypothesis(
            hypothesis_id="H01",
            root_cause_type="missing_partition",
            description="The target partition may be missing.",
            initial_confidence=0.55,
        )
    )
    evidence = [
        EvidenceReference(
            evidence_id="E01",
            source_type=source_types[0],
            description="The target event partition is empty.",
        ),
        EvidenceReference(
            evidence_id="E02",
            source_type=source_types[1],
            description="Partition metadata is missing.",
        ),
    ]
    for reference in evidence:
        graph.register_evidence(state, reference)
    graph.attach_evidence(state, "H01", "E01", supports=True)
    graph.attach_evidence(state, "H01", "E02", supports=True)
    graph.transition(state, IncidentStatus.TRIAGE)
    graph.transition(state, IncidentStatus.PLANNING)
    graph.transition(state, IncidentStatus.EXECUTING)
    state.tool_trace.append({"tool": "sql_runner", "status": "ok"})
    graph.transition(state, IncidentStatus.VALIDATING)
    graph.transition(state, IncidentStatus.HYPOTHESIS_TESTING)
    return state


def test_full_success_path_uses_real_runtime_components() -> None:
    graph, state, evidence = _real_runtime()

    result = graph.validate_hypothesis(state, "H01", evidence)
    assert result.to_status is IncidentStatus.ROOT_CAUSE_FOUND
    graph.propose_fix(state, {"action": "rerun_partition"})
    graph.record_approval(state, approved=True, reviewer="data-engineer")
    graph.record_repair_result(state, succeeded=True, action="rerun_partition")
    graph.record_post_validation_result(state, validated=True)

    assert state.status is IncidentStatus.RESOLVED
    assert state.final_status is IncidentStatus.RESOLVED
    assert state.retry_count == 0
    assert state.root_cause["confidence"] == pytest.approx(0.85)
    assert {
        key: value
        for key, value in state.root_cause.items()
        if key != "confidence"
    } == {
        "hypothesis_id": "H01",
        "root_cause_type": "missing_partition",
        "supporting_evidence_ids": ["E01", "E02"],
        "independent_source_types": ["business_data", "operational_metadata"],
    }


def test_validator_fail_then_retry_then_pass_uses_real_runtime_components() -> None:
    graph, state, evidence = _real_runtime(
        source_types=("business_data", "business_data"),
    )

    failed = graph.validate_hypothesis(state, "H01", evidence)
    assert failed.to_status is IncidentStatus.HYPOTHESIS_TESTING
    assert state.status is IncidentStatus.HYPOTHESIS_TESTING
    assert graph.hypothesis_manager.get_hypothesis("H01").status is HypothesisStatus.TESTING
    graph.request_more_evidence(state)
    assert state.status is IncidentStatus.EXECUTING
    assert state.retry_count == 1

    additional_evidence = EvidenceReference(
        evidence_id="E03",
        source_type="operational_metadata",
        description="A second metadata source confirms the missing partition.",
    )
    graph.register_evidence(state, additional_evidence)
    graph.attach_evidence(state, "H01", "E03", supports=True)
    graph.execute_next_step(state)
    graph.enter_hypothesis_testing(state)
    passing = graph.validate_hypothesis(
        state,
        "H01",
        [*evidence, additional_evidence],
    )
    assert passing.to_status is IncidentStatus.ROOT_CAUSE_FOUND
    assert state.status is IncidentStatus.ROOT_CAUSE_FOUND
    assert state.retry_count == 1


def test_root_cause_cannot_be_reached_by_generic_transition() -> None:
    graph = HarnessGraph()
    state = _to_hypothesis_testing(graph)

    with pytest.raises(HarnessTransitionError, match="only be entered"):
        graph.transition(state, IncidentStatus.ROOT_CAUSE_FOUND)
    assert state.status is IncidentStatus.HYPOTHESIS_TESTING
    assert state.root_cause is None


def test_illegal_transition_is_rejected() -> None:
    graph = HarnessGraph()
    state = IncidentState(alert=_alert())

    with pytest.raises(HarnessTransitionError, match="illegal"):
        graph.transition(state, IncidentStatus.EXECUTING)
    assert state.status is IncidentStatus.RECEIVED
    assert state.final_status is None


def test_terminal_state_cannot_transition() -> None:
    graph = HarnessGraph()
    state = IncidentState(
        status=IncidentStatus.RESOLVED,
        final_status=IncidentStatus.RESOLVED,
    )

    with pytest.raises(HarnessTransitionError, match="terminal"):
        graph.transition(state, IncidentStatus.TRIAGE)
    assert state.status is IncidentStatus.RESOLVED
    assert state.final_status is IncidentStatus.RESOLVED


def test_empty_alert_is_rejected_without_mutating_state() -> None:
    graph = HarnessGraph()
    state = IncidentState()

    with pytest.raises(HarnessTransitionError, match="alert fields"):
        graph.transition(state, IncidentStatus.TRIAGE)
    assert state.status is IncidentStatus.RECEIVED
    assert state.final_status is None


def test_empty_plan_is_rejected() -> None:
    graph = HarnessGraph()
    state = IncidentState(alert=_alert(), status=IncidentStatus.PLANNING)

    with pytest.raises(HarnessTransitionError, match="non-empty plan"):
        graph.transition(state, IncidentStatus.EXECUTING)
    assert state.status is IncidentStatus.PLANNING


def test_triage_rejects_out_of_scope_incident_without_mutating_state() -> None:
    graph = HarnessGraph()
    state = IncidentState(
        alert={**_alert(), "category": "security"},
        status=IncidentStatus.TRIAGE,
    )

    with pytest.raises(HarnessTransitionError, match="MVP triage scope"):
        graph.transition(state, IncidentStatus.PLANNING)
    assert state.status is IncidentStatus.TRIAGE


def test_execution_without_result_is_rejected() -> None:
    graph = HarnessGraph()
    state = IncidentState(status=IncidentStatus.EXECUTING)

    with pytest.raises(HarnessTransitionError, match="tool_trace or evidence"):
        graph.transition(state, IncidentStatus.VALIDATING)
    assert state.status is IncidentStatus.EXECUTING


def test_approval_guard_and_rejection_contract() -> None:
    graph = HarnessGraph()
    state = IncidentState(status=IncidentStatus.AWAITING_APPROVAL)

    with pytest.raises(HarnessTransitionError, match="approved approval"):
        graph.transition(state, IncidentStatus.SANDBOX_REPAIR)
    assert state.status is IncidentStatus.AWAITING_APPROVAL
    assert state.approval is None

    graph.record_approval(state, approved=False, reason="not approved")
    assert state.status is IncidentStatus.REJECTED
    assert state.final_status is IncidentStatus.REJECTED
    assert state.approval == {"status": "rejected", "reason": "not approved"}


def test_repair_failure_is_terminal_and_success_requires_result() -> None:
    graph = HarnessGraph()
    state = IncidentState(status=IncidentStatus.SANDBOX_REPAIR)

    with pytest.raises(HarnessTransitionError, match="successful repair"):
        graph.transition(state, IncidentStatus.POST_VALIDATION)
    assert state.status is IncidentStatus.SANDBOX_REPAIR

    graph.record_repair_result(state, success=False, error="repair failed")
    assert state.status is IncidentStatus.TOOL_FAILED
    assert state.final_status is IncidentStatus.TOOL_FAILED


def test_post_validation_failure_is_terminal() -> None:
    graph = HarnessGraph()
    state = IncidentState(
        status=IncidentStatus.POST_VALIDATION,
        repair_result={"status": "succeeded"},
    )

    graph.record_post_validation_result(state, validated=False)
    assert state.status is IncidentStatus.VALIDATION_FAILED
    assert state.final_status is IncidentStatus.VALIDATION_FAILED


def test_terminal_map_has_no_outgoing_edges() -> None:
    for terminal in (
        IncidentStatus.RESOLVED,
        IncidentStatus.REJECTED,
        IncidentStatus.UNRESOLVED,
        IncidentStatus.BUDGET_EXCEEDED,
        IncidentStatus.TOOL_FAILED,
        IncidentStatus.VALIDATION_FAILED,
    ):
        assert ALLOWED_TRANSITIONS[terminal] == frozenset()


def test_transition_result_serializes_to_json() -> None:
    graph = HarnessGraph()
    state = IncidentState(alert=_alert())

    result = graph.transition(state, IncidentStatus.TRIAGE, reason="received")

    assert result.model_dump(mode="json") == {
        "from_status": "RECEIVED",
        "to_status": "TRIAGE",
        "changed": True,
        "retry_count": 0,
        "terminal": False,
        "reason": "received",
    }


def test_fix_proposal_is_required_before_approval() -> None:
    graph = HarnessGraph()
    state = IncidentState(
        status=IncidentStatus.FIX_PROPOSED,
        root_cause={"hypothesis_id": "H01"},
    )

    with pytest.raises(HarnessTransitionError, match="fix_proposal"):
        graph.transition(state, IncidentStatus.AWAITING_APPROVAL)
    assert state.status is IncidentStatus.FIX_PROPOSED


def test_transition_error_exposes_structured_statuses() -> None:
    graph = HarnessGraph()
    state = IncidentState(alert=_alert())

    with pytest.raises(HarnessTransitionError) as captured:
        graph.transition(state, IncidentStatus.ROOT_CAUSE_FOUND)

    assert captured.value.from_status is IncidentStatus.RECEIVED
    assert captured.value.to_status is IncidentStatus.ROOT_CAUSE_FOUND
    assert captured.value.reason


def test_incident_state_serialization_survives_graph_operations() -> None:
    graph = HarnessGraph()
    state = _to_hypothesis_testing(graph)
    result = graph.root_cause_validator.validate(
        graph.hypothesis_manager.get_hypothesis("H01"),
        graph.hypothesis_manager.evidence(),
    )
    graph.apply_root_cause_validation(state, result)

    restored = IncidentState.from_json(state.to_json())
    assert restored == state


def test_real_validator_pass_integrates_without_llm() -> None:
    graph = HarnessGraph()
    state = _to_hypothesis_testing(graph)
    result = graph.root_cause_validator.validate(
        graph.hypothesis_manager.get_hypothesis("H01"),
        graph.hypothesis_manager.evidence(),
    )
    graph.apply_root_cause_validation(state, result)

    assert result.validated is True
    assert state.status is IncidentStatus.ROOT_CAUSE_FOUND
    assert state.root_cause is not None
    assert state.root_cause["hypothesis_id"] == "H01"


def test_real_validator_fail_integrates_and_can_request_evidence() -> None:
    graph = HarnessGraph()
    state = _to_hypothesis_testing(
        graph,
        source_types=("business_data", "business_data"),
    )
    result = graph.root_cause_validator.validate(
        graph.hypothesis_manager.get_hypothesis("H01"),
        graph.hypothesis_manager.evidence(),
    )
    graph.apply_root_cause_validation(state, result)
    assert result.validated is False
    assert state.status is IncidentStatus.HYPOTHESIS_TESTING

    graph.request_more_evidence(state)
    assert state.status is IncidentStatus.EXECUTING
    assert state.retry_count == 1


@pytest.mark.parametrize(
    ("validated", "next_state"),
    [
        (True, "HYPOTHESIS_TESTING"),
        (False, "ROOT_CAUSE_FOUND"),
    ],
)
def test_malformed_validator_result_is_rejected(
    validated: bool, next_state: str
) -> None:
    graph = HarnessGraph()
    state = _to_hypothesis_testing(graph)
    authoritative = graph.root_cause_validator.validate(
        graph.hypothesis_manager.get_hypothesis("H01"),
        graph.hypothesis_manager.evidence(),
    )
    malformed = authoritative.model_copy(
        update={
            "validated": validated,
            "recommended_next_state": next_state,
        }
    )

    with pytest.raises(HarnessTransitionError, match="malformed"):
        graph.apply_root_cause_validation(state, malformed)
    assert state.status is IncidentStatus.HYPOTHESIS_TESTING
    assert state.root_cause is None


def test_forged_validated_result_must_match_live_managed_hypothesis() -> None:
    graph = HarnessGraph()
    state = _to_hypothesis_testing(graph)
    authoritative = graph.root_cause_validator.validate(
        graph.hypothesis_manager.get_hypothesis("H01"),
        graph.hypothesis_manager.evidence(),
    )
    forged = authoritative.model_copy(update={"confidence": 1.0})

    with pytest.raises(HarnessTransitionError, match="does not match"):
        graph.apply_root_cause_validation(state, forged)
    assert state.status is IncidentStatus.HYPOTHESIS_TESTING
    assert state.root_cause is None
