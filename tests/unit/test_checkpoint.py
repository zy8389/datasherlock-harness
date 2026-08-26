import json
import os

import pytest

from agents.planner import InvestigationStep
from harness.checkpoint import (
    CHECKPOINT_SCHEMA_VERSION,
    CheckpointIntegrityError,
    CheckpointManager,
    CheckpointNotFoundError,
    CheckpointVersionError,
    FileCheckpointStore,
    ResumeAction,
    ResumeIntegrityError,
    ResumeMetadata,
    build_resume_plan,
    deterministic_tool_call_id,
)
from harness.guardrails import GuardrailEvent
from harness.state import IncidentState, IncidentStatus


def _step(
    step_id: str,
    sql: str,
) -> dict[str, object]:
    return {
        "step_id": step_id,
        "purpose": f"Run {step_id}.",
        "hypothesis_id": "H01",
        "tool": "sql_query",
        "arguments": {"sql": sql},
        "expected_evidence": ["the query result"],
        "stop_condition": "retain the result",
    }


def _state(
    incident_id: str = "INC-CP-001",
    *,
    status: IncidentStatus = IncidentStatus.EXECUTING,
) -> IncidentState:
    return IncidentState(
        alert={"incident_id": incident_id, "metric": "daily_active_users"},
        plan=[_step("S01", "SELECT 1"), _step("S02", "SELECT 2")],
        hypotheses=[
            {
                "hypothesis_id": "H01",
                "root_cause_type": "data_delay",
                "description": "The data may have arrived late.",
                "status": "TESTING",
                "confidence": 0.55,
                "evidence_ids": [],
                "supporting_evidence_ids": [],
                "contradicting_evidence_ids": [],
                "created_at": "2026-08-26T00:00:00Z",
                "updated_at": "2026-08-26T00:00:00Z",
            }
        ],
        evidence=[
            {
                "evidence_id": "Q01",
                "evidence_type": "tool_result",
                "tool_name": "sql_query",
                "success": True,
                "root_cause_validated": False,
            },
            {
                "evidence_id": "E01",
                "source_type": "business_data",
                "description": "The event volume observation.",
                "query_id": "Q01",
                "observation": {"rows": [[1]]},
            },
        ],
        tool_trace=[
            {
                "tool_name": "sql_query",
                "success": True,
                "query_id": "Q01",
                "result": {"rows": [[1]]},
            }
        ],
        planner_metadata={"fallback_used": False},
        root_cause={"hypothesis_id": "H01", "confidence": 0.9},
        rejected_hypotheses=[{"hypothesis_id": "H02", "reason": "contradicted"}],
        retry_count=1,
        token_cost=12.5,
        current_conclusion="The first bounded query completed.",
        status=status,
        final_status=status if status.is_terminal else None,
    )


def _completed_resume(state: IncidentState) -> ResumeMetadata:
    step = InvestigationStep.model_validate(state.plan[0])
    from harness.guardrails import fingerprint_step

    fingerprint = fingerprint_step(step)
    return ResumeMetadata(
        completed_step_ids=[step.step_id],
        completed_tool_fingerprints=[fingerprint],
        completed_step_fingerprints={step.step_id: fingerprint},
        completed_tool_call_ids={
            step.step_id: deterministic_tool_call_id(
                state.alert["incident_id"], step.step_id, fingerprint
            )
        },
        last_completed_step_id=step.step_id,
        next_step_index=1,
        resume_action=ResumeAction.CONTINUE_VALIDATION,
    )


def test_checkpoint_state_and_guardrail_round_trip(tmp_path) -> None:
    state = _state()
    state.guardrail_usage.agent_rounds = 1
    state.guardrail_usage.tool_calls = 1
    state.guardrail_usage.sql_calls = 1
    state.guardrail_usage.executed_fingerprints = ["f" * 64]
    state.guardrail_usage.fingerprint_counts = {"f" * 64: 1}
    state.guardrail_events = [
        GuardrailEvent(
            event_id="gr-001",
            event_type="preflight",
            incident_id="INC-CP-001",
            step_id="S01",
            tool_name="sql_query",
            allowed=True,
            fingerprint="f" * 64,
            agent_rounds=1,
            tool_calls=1,
            sql_calls=1,
            blocked_calls=0,
        )
    ]
    store = FileCheckpointStore(tmp_path)
    manager = CheckpointManager(store)
    checkpoint = manager.save(
        state,
        reason="after_s01",
        resume=_completed_resume(state),
    )

    loaded = store.load(checkpoint.checkpoint_id)
    restored = manager.restore_latest("INC-CP-001")

    assert checkpoint.schema_version == CHECKPOINT_SCHEMA_VERSION
    assert checkpoint.model_dump(mode="json") == loaded.model_dump(mode="json")
    assert restored.state == state
    assert restored.resume == checkpoint.resume
    assert restored.state.guardrail_usage.sql_calls == 1
    assert restored.state.guardrail_events[0].event_id == "gr-001"
    assert restored.state.retry_count == 1
    assert checkpoint.model_dump_json()


def test_latest_checkpoint_is_deterministic_and_incidents_are_isolated(tmp_path) -> None:
    store = FileCheckpointStore(tmp_path)
    manager = CheckpointManager(store)
    first = _state("INC-A", status=IncidentStatus.TRIAGE)
    second = _state("INC-A", status=IncidentStatus.EXECUTING)
    other = _state("INC-B", status=IncidentStatus.PLANNING)

    cp1 = manager.save(first, reason="triage")
    cp2 = manager.save(second, reason="executing")
    manager.save(other, reason="planning")

    assert cp2.sequence == cp1.sequence + 1
    assert store.load_latest("INC-A").checkpoint_id == cp2.checkpoint_id
    assert store.load_latest("INC-B").incident_id == "INC-B"
    assert len(store.list("INC-A")) == 2
    assert store.list("INC-B")[0].incident_id == "INC-B"


def test_unsupported_version_is_rejected(tmp_path) -> None:
    store = FileCheckpointStore(tmp_path)
    checkpoint = CheckpointManager(store).save(_state(), reason="versioned")
    path = next(tmp_path.rglob("*.json"))
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["schema_version"] = 999
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(CheckpointVersionError, match="unsupported checkpoint schema"):
        store.load(checkpoint.checkpoint_id)


def test_checksum_mismatch_is_rejected(tmp_path) -> None:
    store = FileCheckpointStore(tmp_path)
    checkpoint = CheckpointManager(store).save(_state(), reason="checksum")
    path = next(tmp_path.rglob("*.json"))
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["reason"] = "tampered"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(CheckpointIntegrityError, match="checksum mismatch"):
        store.load(checkpoint.checkpoint_id)


def test_malformed_json_is_rejected_and_missing_incident_is_clear(tmp_path) -> None:
    store = FileCheckpointStore(tmp_path)
    checkpoint = CheckpointManager(store).save(_state(), reason="truncate")
    path = next(tmp_path.rglob("*.json"))
    path.write_text('{"schema_version": 1,', encoding="utf-8")

    with pytest.raises(CheckpointIntegrityError):
        store.load(checkpoint.checkpoint_id)
    with pytest.raises(CheckpointNotFoundError, match="no checkpoint exists"):
        store.load_latest("MISSING")


def test_atomic_save_replaces_only_after_complete_temp_write(tmp_path, monkeypatch) -> None:
    store = FileCheckpointStore(tmp_path)
    checkpoint = CheckpointManager(store).save(_state(), reason="atomic")
    original_replace = os.replace
    calls: list[tuple[str, str]] = []

    def tracked_replace(source: str, destination: str) -> None:
        calls.append((str(source), str(destination)))
        original_replace(source, destination)

    monkeypatch.setattr("harness.checkpoint.os.replace", tracked_replace)
    store.save(checkpoint.model_copy(update={"sequence": 2}).with_integrity())

    assert len(calls) == 1
    assert calls[0][0].endswith(".tmp")
    assert calls[0][1].endswith(".json")
    assert not list(tmp_path.rglob("*.tmp"))


def test_resume_integrity_rejects_changed_plan_arguments(tmp_path) -> None:
    state = _state()
    manager = CheckpointManager(FileCheckpointStore(tmp_path))
    checkpoint = manager.save(state, reason="integrity", resume=_completed_resume(state))
    changed = checkpoint.state.model_copy(deep=True)
    changed.plan[0]["arguments"] = {"sql": "SELECT 99"}
    tampered = checkpoint.model_copy(update={"state": changed}).with_integrity()

    with pytest.raises(ResumeIntegrityError, match="fingerprint"):
        manager.restore(tampered)


def test_resume_plan_skips_completed_steps_and_handles_terminal_state() -> None:
    state = _state()
    resume = _completed_resume(state)
    plan = build_resume_plan(state, resume)
    assert plan.action is ResumeAction.EXECUTE_NEXT_TOOL
    assert plan.next_step_id == "S02"
    assert plan.next_step_index == 1

    terminal_state = _state(status=IncidentStatus.BUDGET_EXCEEDED)
    terminal_resume = ResumeMetadata(resume_action=ResumeAction.TERMINAL)
    terminal_plan = build_resume_plan(terminal_state, terminal_resume)
    assert terminal_plan.action is ResumeAction.TERMINAL
    assert terminal_plan.terminal is True
