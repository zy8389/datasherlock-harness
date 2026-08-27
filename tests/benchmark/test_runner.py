import json
import time
from pathlib import Path
from typing import Any

import pytest

from benchmark import case_generator
from benchmark.case_generator import load_case_manifest
from benchmark.runner import (
    BenchmarkCaseResult,
    BenchmarkRunConfig,
    BenchmarkTimeoutError,
    CurrentHarnessExecutor,
    HarnessExecutionOutput,
    build_harness_executor,
    build_run_summary,
    build_runtime_input,
    execute_with_timeout,
    load_selected_cases,
    run_batch,
    run_real_harness_smoke,
    run_seed_orchestration,
)

ROOT = Path(__file__).parents[2]
CASES_DIRECTORY = ROOT / "benchmark" / "cases"


def _config(
    output_dir: Path,
    *case_ids: str,
    run_id: str = "runner-test",
    timeout: float = 10.0,
    **overrides: Any,
) -> BenchmarkRunConfig:
    values: dict[str, Any] = {
        "case_ids": list(case_ids),
        "harness_version": "current-main",
        "model_name": "mock-model",
        "model_provider": "mock",
        "per_case_timeout_seconds": timeout,
        "output_dir": output_dir,
        "benchmark_run_id": run_id,
    }
    values.update(overrides)
    return BenchmarkRunConfig.model_validate(values)


def _runtime_input(tmp_path: Path):
    manifest = load_case_manifest("F01-001", CASES_DIRECTORY)
    config = _config(tmp_path, "F01-001", run_id="runtime-input")
    return build_runtime_input(
        manifest,
        tmp_path / "runtime.duckdb",
        run_id="runtime-input-00001",
        config=config,
    )


class _FixedExecutor:
    def __init__(self, prediction: str | None = None) -> None:
        self.prediction = prediction

    def __call__(self, runtime_input) -> HarnessExecutionOutput:
        return HarnessExecutionOutput(
            harness_status="UNRESOLVED",
            predicted_root_cause=self.prediction,
            trace_payload={
                "schema_version": 1,
                "partial": False,
                "runtime_incident_id": runtime_input.alert.incident_id,
            },
            tool_call_count=2,
            sql_call_count=2,
        )


class _FailFirstExecutor:
    def __call__(self, runtime_input) -> HarnessExecutionOutput:
        if runtime_input.run_id.endswith("-00001"):
            raise RuntimeError("intentional executor failure")
        return HarnessExecutionOutput(
            harness_status="UNRESOLVED",
            trace_payload={"schema_version": 1, "partial": False},
        )


class _BlockingExecutor:
    def __init__(self, marker: Path, delay_seconds: float) -> None:
        self.marker = marker
        self.delay_seconds = delay_seconds

    def __call__(self, _runtime_input) -> HarnessExecutionOutput:
        time.sleep(self.delay_seconds)
        self.marker.write_text("late side effect", encoding="utf-8")
        return HarnessExecutionOutput(
            harness_status="UNRESOLVED",
            trace_payload={"schema_version": 1, "partial": False},
        )


class _CaptureFactory:
    def __init__(self) -> None:
        self.runtime_payload: dict[str, Any] | None = None

    def __call__(self, runtime_input):
        self.runtime_payload = runtime_input.model_dump(mode="json")
        from benchmark.runner import _smoke_model_client_factory

        return _smoke_model_client_factory(runtime_input)


def test_runner_reuses_canonical_case_apis_and_rejects_duplicate_ids() -> None:
    assert case_generator.load_case_manifest is load_case_manifest
    from benchmark import runner

    assert runner.load_case_manifests is case_generator.load_case_manifests
    assert runner.validate_case_manifest is case_generator.validate_case_manifest
    assert runner.materialize_case is case_generator.materialize_case

    duplicate_config = _config(Path("output"), "F01-001", "F01-001")
    with pytest.raises(ValueError, match="must not contain duplicates"):
        load_selected_cases(duplicate_config, CASES_DIRECTORY)


def test_runtime_input_is_ground_truth_free(tmp_path: Path) -> None:
    runtime_input = _runtime_input(tmp_path)
    payload = runtime_input.model_dump(mode="json")
    serialized = json.dumps(payload, sort_keys=True)

    assert set(payload) == {"run_id", "database_path", "alert", "runtime_config"}
    assert "F01-001" not in serialized
    assert "missing_partition" not in serialized
    assert "expected_root_cause" not in serialized
    assert "source_seed_case_id" not in serialized
    assert "ground_truth" not in serialized.lower()

    manifest = load_case_manifest("F01-001", CASES_DIRECTORY)
    leaky_alert = manifest.original_alert.model_copy(
        update={
            "case_id": "F01-001",
            "root_cause_type": "missing_partition",
            "expected_evidence": ["should never cross the runtime boundary"],
        }
    )
    leaky_manifest = manifest.model_copy(update={"original_alert": leaky_alert})
    sanitized = build_runtime_input(
        leaky_manifest,
        tmp_path / "sanitized.duckdb",
        run_id="runtime-input-00002",
    )
    sanitized_serialized = sanitized.alert.model_dump_json()
    assert "F01-001" not in sanitized_serialized
    assert "missing_partition" not in sanitized_serialized
    assert "expected_evidence" not in sanitized_serialized


def test_batch_scores_top1_separately_from_execution_status(tmp_path: Path) -> None:
    correct = run_batch(
        _config(tmp_path, "F01-001", run_id="correct"),
        CASES_DIRECTORY,
        executor=_FixedExecutor("missing_partition"),
    )
    incorrect = run_batch(
        _config(tmp_path, "F01-001", run_id="incorrect"),
        CASES_DIRECTORY,
        executor=_FixedExecutor("data_delay"),
    )

    assert correct.results[0].status == "completed"
    assert correct.results[0].top1_correct is True
    assert incorrect.results[0].status == "completed"
    assert incorrect.results[0].top1_correct is False
    assert correct.completed == correct.scored == correct.correct == 1
    assert incorrect.completed == incorrect.scored == 1
    assert incorrect.correct == 0


def test_process_timeout_terminates_worker_and_prevents_late_side_effect(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "late-marker.txt"

    with pytest.raises(BenchmarkTimeoutError) as raised:
        execute_with_timeout(
            _BlockingExecutor(marker, delay_seconds=0.5),
            _runtime_input(tmp_path),
            timeout_seconds=0.05,
        )

    assert raised.value.child_terminated is True
    time.sleep(0.6)
    assert not marker.exists()


def test_executor_error_is_case_local_and_batch_continues(tmp_path: Path) -> None:
    summary = run_batch(
        _config(tmp_path, "F01-001", "F02-001", run_id="failure-isolation"),
        CASES_DIRECTORY,
        executor=_FailFirstExecutor(),
    )

    assert [result.status for result in summary.results] == ["error", "completed"]
    assert summary.error_count == 1
    assert summary.completed == 1
    assert summary.results[0].error_message is not None
    for case_id in ("F01-001", "F02-001"):
        assert (tmp_path / "failure-isolation" / case_id / "result.json").is_file()
        assert (tmp_path / "failure-isolation" / case_id / "trace.json").is_file()


def test_persistence_and_opaque_runtime_path(tmp_path: Path) -> None:
    summary = run_batch(
        _config(tmp_path, "F01-001", run_id="persistence"),
        CASES_DIRECTORY,
        executor=_FixedExecutor(),
    )
    run_dir = tmp_path / "persistence"

    assert (run_dir / "F01-001" / "result.json").is_file()
    assert (run_dir / "F01-001" / "trace.json").is_file()
    assert (run_dir / "summary.json").is_file()
    assert (run_dir / "results.jsonl").is_file()
    assert (run_dir / ".runtime" / "case-00001" / "datasherlock.duckdb").is_file()
    trace_text = (run_dir / "F01-001" / "trace.json").read_text(encoding="utf-8")
    assert "F01-001" not in trace_text
    assert json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))["attempted"] == 1
    assert len((run_dir / "results.jsonl").read_text(encoding="utf-8").splitlines()) == 1
    assert summary.results[0].result_path is not None
    assert summary.results[0].trace_path is not None


def test_summary_percentiles_and_unknown_cost_are_explicit() -> None:
    config = _config(Path("output"), "F01-001", run_id="summary")
    results = [
        BenchmarkCaseResult(
            case_id=f"F01-00{index}",
            status="completed",
            top1_correct=index == 1,
            duration_ms=float(duration),
            tool_call_count=index,
            sql_call_count=index - 1,
        )
        for index, duration in enumerate((10, 20, 40, 80), start=1)
    ]
    summary = build_run_summary(config, results)

    assert summary.p50_latency_ms == 20
    assert summary.p95_latency_ms == 80
    assert summary.total_cost is None
    assert summary.average_known_cost is None
    assert summary.cost_known_cases == 0
    assert summary.total_tool_calls == 10
    assert summary.total_sql_calls == 6
    assert summary.top1_accuracy_scored == pytest.approx(0.25)


def test_run_id_collision_requires_explicit_overwrite(tmp_path: Path) -> None:
    config = _config(tmp_path, "F01-001", run_id="collision")
    run_batch(config, CASES_DIRECTORY, executor=_FixedExecutor())
    stale = tmp_path / "collision" / "stale.txt"
    stale.write_text("stale", encoding="utf-8")

    with pytest.raises(FileExistsError, match="benchmark_run_id already exists"):
        run_batch(config, CASES_DIRECTORY, executor=_FixedExecutor())

    overwritten = run_batch(
        config.model_copy(update={"overwrite": True}),
        CASES_DIRECTORY,
        executor=_FixedExecutor(),
    )
    assert overwritten.attempted == 1
    assert not stale.exists()


def test_seed_orchestration_runs_all_12_in_stable_order(tmp_path: Path) -> None:
    summary = run_seed_orchestration(
        _config(tmp_path, "F01-001", run_id="seed-orchestration"),
        cases_directory=CASES_DIRECTORY,
        executor=_FixedExecutor(),
    )

    assert [result.case_id for result in summary.results] == [
        f"F{number:02d}-001" for number in range(1, 13)
    ]
    assert summary.attempted == summary.completed == 12
    assert summary.error_count == summary.timed_out_count == 0
    assert len(
        (tmp_path / "seed-orchestration" / "results.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ) == 12


def test_production_adapter_selection_and_fail_closed_config(tmp_path: Path) -> None:
    config = _config(tmp_path, "F01-001", model_name="deterministic-smoke")
    executor = build_harness_executor(config)
    assert isinstance(executor, CurrentHarnessExecutor)
    assert executor.config.model_name == "deterministic-smoke"

    with pytest.raises(ValueError, match="unsupported harness_version"):
        build_harness_executor(config.model_copy(update={"harness_version": "future"}))
    with pytest.raises(ValueError, match="unsupported mock model_name"):
        build_harness_executor(config.model_copy(update={"model_name": "unknown-model"}))


def test_real_harness_smoke_uses_current_graph_and_includes_f11(tmp_path: Path) -> None:
    summary = run_real_harness_smoke(
        _config(tmp_path, "F01-001", run_id="real-smoke", timeout=60.0),
        cases_directory=CASES_DIRECTORY,
    )

    assert summary.attempted == summary.completed == 5
    assert summary.error_count == summary.timed_out_count == 0
    assert summary.correct == summary.scored == 5
    assert {result.case_id for result in summary.results} == {
        "F01-001",
        "F02-001",
        "F06-001",
        "F11-001",
        "F12-001",
    }
    f11 = next(result for result in summary.results if result.case_id == "F11-001")
    assert f11.expected_root_cause == "metric_definition_change"
    assert f11.predicted_root_cause == "metric_definition_change"
    assert f11.top1_correct is True
    for result in summary.results:
        assert result.trace_path is not None
        trace_text = result.trace_path.read_text(encoding="utf-8")
        assert result.case_id not in trace_text
        assert "source_seed_case_id" not in trace_text
        assert "expected_root_cause" not in trace_text
        assert "ground_truth" not in trace_text.lower()


def test_real_adapter_planner_boundary_has_no_ground_truth_fields(tmp_path: Path) -> None:
    manifest = load_case_manifest("F01-001", CASES_DIRECTORY)
    config = _config(
        tmp_path,
        "F01-001",
        run_id="boundary",
        model_name="deterministic-smoke",
    )
    from benchmark.runner import materialize_case_environment

    database_path = materialize_case_environment(
        config,
        manifest,
        cases_directory=CASES_DIRECTORY,
        case_index=1,
    )
    runtime_input = build_runtime_input(
        manifest,
        database_path,
        run_id="boundary-00001",
        config=config,
    )
    factory = _CaptureFactory()
    output = CurrentHarnessExecutor(
        config,
        model_client_factory=factory,
    )(runtime_input)

    assert output.predicted_root_cause == "missing_partition"
    assert factory.runtime_payload is not None
    serialized = json.dumps(factory.runtime_payload, sort_keys=True)
    assert "F01-001" not in serialized
    assert "source_seed_case_id" not in serialized
    assert "expected_root_cause" not in serialized
    assert "ground_truth" not in serialized.lower()
    assert "case_id" not in serialized


def test_f11_manifest_is_loadable_through_canonical_api() -> None:
    manifest = load_case_manifest("F11-001", CASES_DIRECTORY)
    assert manifest.root_cause_type == "metric_definition_change"
    assert manifest.injection.strategy == "filter_dau_to_core_task_events"
