import time
from pathlib import Path

import pytest

from benchmark.runner import (
    BenchmarkRunConfig,
    HarnessExecutionOutput,
    build_runtime_input,
    load_selected_cases,
    materialize_case_environment,
    run_batch,
    run_case,
)


def build_config(tmp_path: Path, case_ids: list[str]) -> BenchmarkRunConfig:
    return BenchmarkRunConfig(
        case_ids=case_ids,
        harness_version="main",
        model_name="mock",
        output_dir=tmp_path / "benchmark-results",
    )


def test_load_selected_cases_returns_requested_manifests(tmp_path: Path) -> None:
    config = build_config(tmp_path, ["F01-001", "F11-001"])

    cases = load_selected_cases(config, Path("benchmark/cases"))

    assert [case.case_id for case in cases] == ["F01-001", "F11-001"]


def test_load_selected_cases_rejects_duplicate_case_ids(tmp_path: Path) -> None:
    config = build_config(tmp_path, ["F01-001", "F01-001"])

    with pytest.raises(ValueError, match="must not contain duplicates"):
        load_selected_cases(config, Path("benchmark/cases"))


def test_materialize_case_environment_writes_duckdb_database(tmp_path: Path) -> None:
    config = build_config(tmp_path, ["F01-001"])
    case = load_selected_cases(config, Path("benchmark/cases"))[0]

    database_path = materialize_case_environment(config, case)

    assert database_path.name == "datasherlock.duckdb"
    assert database_path.is_file()

def test_runtime_input_hides_ground_truth_and_case_identity(tmp_path: Path) -> None:
    config = build_config(tmp_path, ["F01-001"])
    case = load_selected_cases(config, Path("benchmark/cases"))[0]

    runtime_input = build_runtime_input(
        case,
        tmp_path / "F01-001" / "datasherlock.duckdb",
        run_id="run-001",
    )

    serialized_alert = runtime_input.alert.model_dump_json().lower()

    assert runtime_input.alert.incident_id == "run-001"
    assert runtime_input.alert.metric == "daily_active_users"
    assert "f01-001" not in serialized_alert
    assert "root_cause" not in serialized_alert
    assert "expected_evidence" not in serialized_alert

def test_run_case_scores_a_correct_root_cause(tmp_path: Path) -> None:
    config = build_config(tmp_path, ["F01-001"])
    case = load_selected_cases(config, Path("benchmark/cases"))[0]

    def successful_executor(_input: object) -> HarnessExecutionOutput:
        return HarnessExecutionOutput(
            harness_status="ROOT_CAUSE_FOUND",
            predicted_root_cause="missing_partition",
            trace_payload={"result": "root cause found"},
            tool_call_count=2,
            sql_call_count=2,
            cost=0.0,
            unsafe_operation_count=0,
        )

    result = run_case(config, case, successful_executor, run_id="run-001")

    assert result.status == "passed"
    assert result.top1_correct is True
    assert result.expected_root_cause == "missing_partition"
    assert result.trace_path is not None
    assert result.trace_path.is_file()


def test_run_case_marks_an_incorrect_root_cause_as_failed(tmp_path: Path) -> None:
    config = build_config(tmp_path, ["F01-001"])
    case = load_selected_cases(config, Path("benchmark/cases"))[0]

    def incorrect_executor(_input: object) -> HarnessExecutionOutput:
        return HarnessExecutionOutput(
            harness_status="ROOT_CAUSE_FOUND",
            predicted_root_cause="data_delay",
        )

    result = run_case(config, case, incorrect_executor, run_id="run-002")

    assert result.status == "failed"
    assert result.top1_correct is False
    assert result.predicted_root_cause == "data_delay"


def test_run_case_records_a_timeout(tmp_path: Path) -> None:
    config = build_config(tmp_path, ["F01-001"])
    case = load_selected_cases(config, Path("benchmark/cases"))[0]

    def timeout_executor(_input: object) -> HarnessExecutionOutput:
        raise TimeoutError("case execution exceeded its time limit")

    result = run_case(config, case, timeout_executor, run_id="run-003")

    assert result.status == "timed_out"
    assert result.top1_correct is None
    assert result.error_message == "case execution exceeded its time limit"


def test_run_case_records_an_executor_error(tmp_path: Path) -> None:
    config = build_config(tmp_path, ["F01-001"])
    case = load_selected_cases(config, Path("benchmark/cases"))[0]

    def broken_executor(_input: object) -> HarnessExecutionOutput:
        raise RuntimeError("mock harness failed")

    result = run_case(config, case, broken_executor, run_id="run-004")

    assert result.status == "error"
    assert result.top1_correct is None
    assert "RuntimeError: mock harness failed" in str(result.error_message)
def test_run_batch_aggregates_multiple_case_results(tmp_path: Path) -> None:
    config = build_config(tmp_path, ["F01-001", "F02-001"])

    def successful_executor(runtime_input: object) -> HarnessExecutionOutput:
        metric = runtime_input.alert.metric

        if metric == "daily_active_users":
            return HarnessExecutionOutput(
                harness_status="ROOT_CAUSE_FOUND",
                predicted_root_cause="missing_partition",
                tool_call_count=2,
                sql_call_count=1,
                cost=1.0,
                unsafe_operation_count=0,
            )

        return HarnessExecutionOutput(
            harness_status="ROOT_CAUSE_FOUND",
            predicted_root_cause="duplicate_batch",
            tool_call_count=3,
            sql_call_count=2,
            cost=2.0,
            unsafe_operation_count=1,
        )

    summary = run_batch(
        config,
        Path("benchmark/cases"),
        successful_executor,
    )

    assert summary.total_cases == 2
    assert summary.passed_cases == 2
    assert summary.top1_accuracy == 1.0
    assert summary.total_tool_call_count == 5
    assert summary.total_sql_call_count == 3
    assert summary.total_cost == 3.0
    assert summary.total_unsafe_operation_count == 1
    summary_path = config.output_dir / "summary.json"
    assert summary_path.is_file()
    assert '"total_cases": 2' in summary_path.read_text(encoding="utf-8")


def test_run_batch_continues_after_one_case_error(tmp_path: Path) -> None:
    config = build_config(tmp_path, ["F01-001", "F02-001"])

    def partially_broken_executor(runtime_input: object) -> HarnessExecutionOutput:
        if runtime_input.alert.metric == "daily_active_users":
            raise RuntimeError("first case failed")

        return HarnessExecutionOutput(
            harness_status="ROOT_CAUSE_FOUND",
            predicted_root_cause="duplicate_batch",
        )

    summary = run_batch(
        config,
        Path("benchmark/cases"),
        partially_broken_executor,
    )

    assert [result.status for result in summary.results] == ["error", "passed"]
    assert summary.passed_cases == 1
    assert summary.top1_accuracy == 0.5
    assert "RuntimeError: first case failed" in str(
        summary.results[0].error_message
    )

def test_run_case_enforces_configured_timeout(tmp_path: Path) -> None:
    config = build_config(
        tmp_path,
        ["F01-001"],
    ).model_copy(
        update={"per_case_timeout_seconds": 1}
    )
    case = load_selected_cases(config, Path("benchmark/cases"))[0]

    def slow_executor(_input: object) -> HarnessExecutionOutput:
        time.sleep(2)
        return HarnessExecutionOutput(
            harness_status="ROOT_CAUSE_FOUND",
            predicted_root_cause="missing_partition",
        )

    result = run_case(config, case, slow_executor, run_id="run-timeout")

    assert result.status == "timed_out"
    assert result.top1_correct is None
    assert "case execution exceeded 1 seconds" in str(result.error_message)