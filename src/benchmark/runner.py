"""Batch execution contracts for benchmark cases."""

from __future__ import annotations

import json
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from pathlib import Path
from time import perf_counter
from typing import Literal

import numpy as np
import pandas as pd
import yaml
from pydantic import BaseModel, ConfigDict, Field

from agents.planner import Alert
from benchmark.cases import CASE_ID_RE, CaseManifest, concrete_case_from_manifest
from benchmark.fault_injector import inject_case
from data.generator import generate_dataset, write_outputs

CaseRunStatus = Literal["passed", "failed", "timed_out", "error"]


class BenchmarkRunConfig(BaseModel):
    """Configuration shared by one batch benchmark run."""

    model_config = ConfigDict(extra="forbid")

    case_ids: list[str] = Field(min_length=1)
    harness_version: str = Field(min_length=1)
    model_name: str = Field(min_length=1)
    per_case_timeout_seconds: int = Field(default=30, gt=0)
    output_dir: Path

class HarnessRuntimeInput(BaseModel):
    """The only benchmark data allowed into one Harness execution."""

    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(min_length=1)
    database_path: Path
    alert: Alert

class HarnessExecutionOutput(BaseModel):
    """The observable result returned by one Harness execution."""

    model_config = ConfigDict(extra="forbid")

    harness_status: str = Field(min_length=1)
    predicted_root_cause: str | None = None
    trace_payload: dict[str, object] = Field(default_factory=dict)
    tool_call_count: int = Field(default=0, ge=0)
    sql_call_count: int = Field(default=0, ge=0)
    cost: float | None = Field(default=None, ge=0)
    unsafe_operation_count: int = Field(default=0, ge=0)


HarnessExecutor = Callable[[HarnessRuntimeInput], HarnessExecutionOutput]
class BenchmarkCaseResult(BaseModel):
    """One case result, written only after the Harness run finishes."""

    model_config = ConfigDict(extra="forbid")

    case_id: str = Field(min_length=1)
    harness_status: str | None = None
    status: CaseRunStatus
    predicted_root_cause: str | None = None
    expected_root_cause: str | None = None
    top1_correct: bool | None = None
    trace_path: Path | None = None
    tool_call_count: int = Field(default=0, ge=0)
    sql_call_count: int = Field(default=0, ge=0)
    duration_ms: float = Field(default=0.0, ge=0)
    cost: float | None = Field(default=None, ge=0)
    unsafe_operation_count: int = Field(default=0, ge=0)
    error_message: str | None = None


class BenchmarkRunSummary(BaseModel):
    """Aggregated output for one benchmark batch."""

    model_config = ConfigDict(extra="forbid")

    config: BenchmarkRunConfig
    results: list[BenchmarkCaseResult]
    total_cases: int = Field(ge=0)
    passed_cases: int = Field(ge=0)
    top1_accuracy: float = Field(ge=0, le=1)
    total_tool_call_count: int = Field(ge=0)
    total_sql_call_count: int = Field(ge=0)
    total_duration_ms: float = Field(ge=0)
    total_cost: float = Field(ge=0)
    total_unsafe_operation_count: int = Field(ge=0)

def load_case_manifest(cases_directory: Path, case_id: str) -> CaseManifest:
    """Load one validated generated case manifest by its stable case id."""

    if CASE_ID_RE.fullmatch(case_id) is None:
        raise ValueError(f"unsupported benchmark case_id: {case_id}")

    manifest_path = cases_directory / f"{case_id}.yaml"
    if not manifest_path.is_file():
        raise FileNotFoundError(f"benchmark case manifest not found: {manifest_path}")

    payload = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise TypeError(f"benchmark case manifest must be a mapping: {manifest_path}")

    manifest = CaseManifest.model_validate(payload)
    if manifest.case_id != case_id:
        raise ValueError(
            f"manifest case_id does not match requested case_id: {case_id}"
        )
    return manifest


def load_selected_cases(
    config: BenchmarkRunConfig,
    cases_directory: Path,
) -> list[CaseManifest]:
    """Load the requested manifests and reject duplicate case selections."""

    if len(set(config.case_ids)) != len(config.case_ids):
        raise ValueError("benchmark case_ids must not contain duplicates")

    return [
        load_case_manifest(cases_directory, case_id)
        for case_id in config.case_ids
    ]

def materialize_case_environment(
    config: BenchmarkRunConfig,
    manifest: CaseManifest,
) -> Path:
    """Create an isolated DuckDB dataset for one benchmark case."""

    baseline = generate_dataset(
        manifest.baseline_user_count,
        manifest.baseline_days,
        manifest.baseline_event_count,
        manifest.baseline_seed,
        pd.Timestamp(manifest.baseline_start_date),
    )

    injected = inject_case(
        baseline,
        concrete_case_from_manifest(manifest),
        rng=np.random.default_rng(manifest.seed),
        start_date=pd.Timestamp(manifest.baseline_start_date),
        days=manifest.baseline_days,
    )

    case_output_directory = config.output_dir / manifest.case_id
    write_outputs(case_output_directory, injected.tables)
    return case_output_directory / "datasherlock.duckdb"

def build_runtime_input(
    manifest: CaseManifest,
    database_path: Path,
    *,
    run_id: str,
) -> HarnessRuntimeInput:
    """Create the Ground-Truth-free input passed to the Harness."""

    if not run_id.strip():
        raise ValueError("benchmark run_id must not be blank")

    safe_alert = manifest.original_alert.model_copy(
        update={"incident_id": run_id}
    )
    return HarnessRuntimeInput(
        run_id=run_id,
        database_path=database_path,
        alert=safe_alert,
    )

def write_trace(
    config: BenchmarkRunConfig,
    manifest: CaseManifest,
    trace_payload: dict[str, object],
) -> Path:
    """Persist one completed Harness trace under the benchmark output directory."""

    trace_path = config.output_dir / manifest.case_id / "trace.json"
    trace_path.parent.mkdir(parents=True, exist_ok=True)
    trace_path.write_text(
        json.dumps(trace_payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    return trace_path

def execute_with_timeout(
    executor: HarnessExecutor,
    runtime_input: HarnessRuntimeInput,
    timeout_seconds: int,
) -> HarnessExecutionOutput:
    """Run one executor call with a bounded wall-clock timeout."""

    pool = ThreadPoolExecutor(max_workers=1)
    future = pool.submit(executor, runtime_input)

    try:
        return future.result(timeout=timeout_seconds)
    except FutureTimeoutError as exc:
        if future.done():
            raise

        future.cancel()
        raise TimeoutError(
            f"case execution exceeded {timeout_seconds} seconds"
        ) from exc
    finally:
        pool.shutdown(wait=False, cancel_futures=True)

def run_case(
    config: BenchmarkRunConfig,
    manifest: CaseManifest,
    executor: HarnessExecutor,
    *,
    run_id: str,
) -> BenchmarkCaseResult:
    """Materialize, execute, score, and persist one isolated benchmark case."""

    started = perf_counter()

    try:
        database_path = materialize_case_environment(config, manifest)
        runtime_input = build_runtime_input(
            manifest,
            database_path,
            run_id=run_id,
        )
        execution = execute_with_timeout(
            executor,
            runtime_input,
            config.per_case_timeout_seconds,
        )
    except TimeoutError as exc:
        return BenchmarkCaseResult(
            case_id=manifest.case_id,
            status="timed_out",
            expected_root_cause=manifest.root_cause_type,
            duration_ms=(perf_counter() - started) * 1000,
            error_message=str(exc),
        )
    except Exception as exc:  # noqa: BLE001
        return BenchmarkCaseResult(
            case_id=manifest.case_id,
            status="error",
            expected_root_cause=manifest.root_cause_type,
            duration_ms=(perf_counter() - started) * 1000,
            error_message=f"{type(exc).__name__}: {exc}",
        )

    top1_correct = execution.predicted_root_cause == manifest.root_cause_type
    trace_path = write_trace(config, manifest, execution.trace_payload)

    return BenchmarkCaseResult(
        case_id=manifest.case_id,
        status="passed" if top1_correct else "failed",
        harness_status=execution.harness_status,
        predicted_root_cause=execution.predicted_root_cause,
        expected_root_cause=manifest.root_cause_type,
        top1_correct=top1_correct,
        trace_path=trace_path,
        tool_call_count=execution.tool_call_count,
        sql_call_count=execution.sql_call_count,
        duration_ms=(perf_counter() - started) * 1000,
        cost=execution.cost,
        unsafe_operation_count=execution.unsafe_operation_count,
    )
def build_run_summary(
    config: BenchmarkRunConfig,
    results: list[BenchmarkCaseResult],
) -> BenchmarkRunSummary:
    """Aggregate case-level benchmark results into batch-level metrics."""

    total_cases = len(results)
    passed_cases = sum(result.top1_correct is True for result in results)

    return BenchmarkRunSummary(
        config=config,
        results=results,
        total_cases=total_cases,
        passed_cases=passed_cases,
        top1_accuracy=(passed_cases / total_cases) if total_cases else 0.0,
        total_tool_call_count=sum(
            result.tool_call_count for result in results
        ),
        total_sql_call_count=sum(
            result.sql_call_count for result in results
        ),
        total_duration_ms=sum(
            result.duration_ms for result in results
        ),
        total_cost=sum(
            result.cost or 0.0 for result in results
        ),
        total_unsafe_operation_count=sum(
            result.unsafe_operation_count for result in results
        ),
    )

def write_run_summary(
    config: BenchmarkRunConfig,
    summary: BenchmarkRunSummary,
) -> Path:
    """Persist one complete benchmark batch summary as JSON."""

    config.output_dir.mkdir(parents=True, exist_ok=True)
    summary_path = config.output_dir / "summary.json"
    summary_path.write_text(
        summary.model_dump_json(indent=2),
        encoding="utf-8",
    )
    return summary_path
def run_batch(
    config: BenchmarkRunConfig,
    cases_directory: Path,
    executor: HarnessExecutor,
) -> BenchmarkRunSummary:
    """Run selected cases sequentially and keep later cases alive after failures."""

    results: list[BenchmarkCaseResult] = []

    for index, manifest in enumerate(
        load_selected_cases(config, cases_directory),
        start=1,
    ):
        result = run_case(
            config,
            manifest,
            executor,
            run_id=f"benchmark-run-{index:03d}",
        )
        results.append(result)

    summary = build_run_summary(config, results)
    write_run_summary(config, summary)
    return summary