"""Current-main benchmark orchestration and production Harness adapter.

The Runner owns batch lifecycle concerns only.  Case meaning and materialized
data come from ``benchmark.case_generator``; execution is delegated to the
same Planner, HarnessGraph, GuardrailRuntime, ToolExecutor, HypothesisManager,
and RootCauseValidator used by the application runtime.
"""

from __future__ import annotations

import json
import multiprocessing as mp
import pickle
import queue
import re
import shutil
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from pathlib import Path
from time import perf_counter
from typing import Any, Literal, Protocol, cast

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator

from agents.planner import (
    Alert,
    InvestigationPlan,
    InvestigationStep,
    Planner,
    load_metric_context,
)
from benchmark.case_generator import (
    load_case_manifest,
    load_case_manifests,
    materialize_case,
    validate_case_manifest,
)
from benchmark.cases import CASE_ID_RE, CaseManifest
from config.model_settings import ModelSettings
from harness.checkpoint import CheckpointManager, FileCheckpointStore
from harness.graph import HarnessGraph
from harness.guardrails import GuardrailRuntime
from harness.hypothesis import EvidenceReference, HypothesisManager
from harness.state import IncidentState, IncidentStatus
from llm.base import ModelClient
from llm.factory import create_model_client
from llm.mock_client import MockModelClient
from llm.models import ModelUsage
from tools.executor import ToolExecutor
from tools.registry import build_default_tool_registry
from validators.root_cause_validator import RootCauseValidator

CaseRunStatus = Literal["completed", "error", "timed_out"]
SUPPORTED_HARNESS_VERSIONS = frozenset({"main", "current", "current-main"})
SUPPORTED_MOCK_MODELS = frozenset(
    {"mock", "mock-model", "deterministic", "deterministic-smoke"}
)
_SAFE_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_GROUND_TRUTH_ALERT_FIELDS = frozenset(
    {
        "case_id",
        "expected_evidence",
        "expected_root_cause",
        "fault_id",
        "ground_truth_case",
        "root_cause_type",
        "source_seed_case_id",
    }
)


class BenchmarkRunConfig(BaseModel):
    """Configuration for one isolated benchmark run."""

    model_config = ConfigDict(extra="forbid", protected_namespaces=())

    case_ids: list[str] = Field(min_length=1)
    harness_version: str = Field(min_length=1)
    model_name: str = Field(min_length=1)
    per_case_timeout_seconds: float = Field(default=30.0, gt=0)
    output_dir: Path
    benchmark_run_id: str = "benchmark-run"
    model_provider: Literal["mock", "openai"] = "mock"
    mock_plan: dict[str, Any] | None = None
    checkpoint_enabled: bool = False
    overwrite: bool = False
    max_planner_retries: int = Field(default=2, ge=0)
    input_cost_per_token: float | None = Field(default=None, ge=0)
    output_cost_per_token: float | None = Field(default=None, ge=0)

    @field_validator("case_ids")
    @classmethod
    def validate_case_ids(cls, values: list[str]) -> list[str]:
        if any(CASE_ID_RE.fullmatch(value) is None for value in values):
            raise ValueError("case_ids must use canonical F01-001 through F12-005 IDs")
        return values

    @field_validator("harness_version", "model_name", "benchmark_run_id")
    @classmethod
    def reject_blank_values(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("configuration values must not be blank")
        return value.strip()

    @field_validator("benchmark_run_id")
    @classmethod
    def validate_run_id(cls, value: str) -> str:
        if _SAFE_RUN_ID.fullmatch(value) is None:
            raise ValueError(
                "benchmark_run_id must contain only letters, numbers, '.', '_' or '-'"
            )
        return value


class HarnessRuntimeConfig(BaseModel):
    """Non-Ground-Truth runtime/model configuration passed to the Harness."""

    model_config = ConfigDict(extra="forbid", protected_namespaces=())

    harness_version: str = Field(min_length=1)
    model_name: str = Field(min_length=1)
    model_provider: Literal["mock", "openai"]
    checkpoint_enabled: bool = False


class HarnessRuntimeInput(BaseModel):
    """The Ground-Truth-free input passed into one Harness execution."""

    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(min_length=1)
    database_path: Path
    alert: Alert
    runtime_config: HarnessRuntimeConfig | None = None


class HarnessExecutionOutput(BaseModel):
    """Observable output derived from one real ``IncidentState``."""

    model_config = ConfigDict(extra="forbid", protected_namespaces=())

    harness_status: str = Field(min_length=1)
    predicted_root_cause: str | None = None
    trace_payload: dict[str, Any] = Field(default_factory=dict)
    tool_call_count: int = Field(default=0, ge=0)
    sql_call_count: int = Field(default=0, ge=0)
    cost: float | None = Field(default=None, ge=0)
    unsafe_operation_count: int = Field(default=0, ge=0)
    model_usage: ModelUsage | None = None


HarnessExecutor = Callable[[HarnessRuntimeInput], HarnessExecutionOutput]


class BenchmarkCaseResult(BaseModel):
    """One case result; execution status and prediction score are separate."""

    model_config = ConfigDict(extra="forbid")

    case_id: str = Field(min_length=1)
    status: CaseRunStatus
    harness_status: str | None = None
    predicted_root_cause: str | None = None
    expected_root_cause: str | None = None
    top1_correct: bool | None = None
    result_path: Path | None = None
    trace_path: Path | None = None
    tool_call_count: int = Field(default=0, ge=0)
    sql_call_count: int = Field(default=0, ge=0)
    duration_ms: float = Field(default=0.0, ge=0)
    cost: float | None = Field(default=None, ge=0)
    unsafe_operation_count: int = Field(default=0, ge=0)
    error_message: str | None = None
    partial_trace: bool = False


class BenchmarkRunSummary(BaseModel):
    """Run-level result and metric envelope."""

    model_config = ConfigDict(extra="forbid")

    config: BenchmarkRunConfig
    results: list[BenchmarkCaseResult]
    attempted: int = Field(ge=0)
    completed: int = Field(ge=0)
    error_count: int = Field(ge=0)
    timed_out_count: int = Field(ge=0)
    scored: int = Field(ge=0)
    correct: int = Field(ge=0)
    top1_accuracy_scored: float | None = Field(default=None, ge=0, le=1)
    top1_accuracy_attempted: float | None = Field(default=None, ge=0, le=1)
    total_tool_calls: int = Field(ge=0)
    average_tool_calls: float = Field(ge=0)
    total_sql_calls: int = Field(ge=0)
    average_sql_calls: float = Field(ge=0)
    total_duration_ms: float = Field(ge=0)
    average_latency_ms: float = Field(ge=0)
    p50_latency_ms: float = Field(ge=0)
    p95_latency_ms: float = Field(ge=0)
    total_cost: float | None = Field(default=None, ge=0)
    average_known_cost: float | None = Field(default=None, ge=0)
    cost_known_cases: int = Field(ge=0)
    blocked_calls: int = Field(ge=0)
    unsafe_attempts: int = Field(ge=0)
    budget_exceeded: int = Field(ge=0)
    timeouts: int = Field(ge=0)
    errors: int = Field(ge=0)

    @property
    def total_cases(self) -> int:
        """Compatibility alias for the original Runner envelope."""

        return self.attempted

    @property
    def total_tool_call_count(self) -> int:
        """Compatibility alias for the original Runner envelope."""

        return self.total_tool_calls

    @property
    def total_sql_call_count(self) -> int:
        """Compatibility alias for the original Runner envelope."""

        return self.total_sql_calls

    @property
    def total_unsafe_operation_count(self) -> int:
        """Compatibility alias for the original Runner envelope."""

        return self.unsafe_attempts

    @property
    def passed_cases(self) -> int:
        """Compatibility alias; score correctness remains separate in JSON."""

        return self.correct

    @property
    def top1_accuracy(self) -> float:
        """Compatibility alias using the explicit attempted denominator."""

        return self.top1_accuracy_attempted or 0.0


class BenchmarkTimeoutError(TimeoutError):
    """Raised when a worker process is terminated at the case deadline."""

    def __init__(self, message: str, *, worker_pid: int | None = None) -> None:
        super().__init__(message)
        self.worker_pid = worker_pid
        self.child_terminated = True


class ModelClientFactory(Protocol):
    """Factory used by deterministic smoke tests without leaking case data."""

    def __call__(self, runtime_input: HarnessRuntimeInput) -> ModelClient: ...


def _validate_runtime_config(config: BenchmarkRunConfig) -> None:
    version = config.harness_version.strip().lower()
    if version not in SUPPORTED_HARNESS_VERSIONS:
        raise ValueError(f"unsupported harness_version: {config.harness_version!r}")
    if config.model_provider == "mock" and config.model_name not in SUPPORTED_MOCK_MODELS:
        raise ValueError(f"unsupported mock model_name: {config.model_name!r}")


def _runtime_config(config: BenchmarkRunConfig) -> HarnessRuntimeConfig:
    return HarnessRuntimeConfig(
        harness_version=config.harness_version,
        model_name=config.model_name,
        model_provider=config.model_provider,
        checkpoint_enabled=config.checkpoint_enabled,
    )


def build_runtime_input(
    manifest: CaseManifest,
    database_path: Path,
    *,
    run_id: str,
    config: BenchmarkRunConfig | None = None,
) -> HarnessRuntimeInput:
    """Build runtime input without passing manifest or Ground Truth fields."""

    if not run_id.strip():
        raise ValueError("benchmark run_id must not be blank")
    alert_payload = manifest.original_alert.model_dump(mode="python")
    for field_name in _GROUND_TRUTH_ALERT_FIELDS:
        alert_payload.pop(field_name, None)
    alert_payload["incident_id"] = run_id
    safe_alert = Alert.model_validate(alert_payload)
    return HarnessRuntimeInput(
        run_id=run_id,
        database_path=database_path,
        alert=safe_alert,
        runtime_config=_runtime_config(config) if config is not None else None,
    )


def _run_directory(config: BenchmarkRunConfig) -> Path:
    return (config.output_dir / config.benchmark_run_id).resolve()


def _case_directory(config: BenchmarkRunConfig, manifest: CaseManifest) -> Path:
    return _run_directory(config) / manifest.case_id


def _runtime_database_directory(config: BenchmarkRunConfig, case_index: int) -> Path:
    """Keep infrastructure paths opaque so case identity cannot reach Planner."""

    return _run_directory(config) / ".runtime" / f"case-{case_index:05d}"


def materialize_case_environment(
    config: BenchmarkRunConfig,
    manifest: CaseManifest,
    *,
    cases_directory: Path = Path("benchmark/cases"),
    case_index: int | None = None,
) -> Path:
    """Materialize one case through the canonical case generator API."""

    result = materialize_case(manifest, directory=cases_directory)
    output_directory = (
        _runtime_database_directory(config, case_index)
        if case_index is not None
        else _case_directory(config, manifest)
    )
    output_directory.mkdir(parents=True, exist_ok=True)
    from data.generator import write_outputs

    write_outputs(output_directory, result.tables)
    return output_directory / "datasherlock.duckdb"


def _default_mock_plan(alert: Alert) -> InvestigationPlan:
    """Provide a bounded offline plan for the configured mock production mode."""

    date_literal = alert.observed_at[:10]
    return InvestigationPlan.model_validate(
        {
            "incident_id": alert.incident_id,
            "hypotheses": [
                {
                    "hypothesis_id": "H01",
                    "root_cause_type": "missing_partition",
                    "description": "A target partition may be missing.",
                    "initial_confidence": 0.60,
                },
                {
                    "hypothesis_id": "H02",
                    "root_cause_type": "data_delay",
                    "description": "The target data may have arrived late.",
                    "initial_confidence": 0.20,
                },
                {
                    "hypothesis_id": "H03",
                    "root_cause_type": "null_value_anomaly",
                    "description": "Null values may distort the metric.",
                    "initial_confidence": 0.10,
                },
            ],
            "steps": [
                {
                    "step_id": "S01",
                    "purpose": "Inspect target-day business activity.",
                    "hypothesis_id": "H01",
                    "tool": "sql_query",
                    "arguments": {
                        "sql": (
                            "SELECT COUNT(*) AS event_count FROM events "
                            f"WHERE CAST(event_time AS DATE) = DATE '{date_literal}'"
                        )
                    },
                    "expected_evidence": ["target-day activity observation"],
                    "stop_condition": "retain the observation for hypothesis testing",
                },
                {
                    "step_id": "S02",
                    "purpose": "Inspect operational partition metadata.",
                    "hypothesis_id": "H01",
                    "tool": "sql_query",
                    "arguments": {
                        "sql": (
                            "SELECT row_count, status FROM partition_metadata "
                            f"WHERE partition_value LIKE '{date_literal}/%'"
                        )
                    },
                    "expected_evidence": ["partition metadata observation"],
                    "stop_condition": "retain the observation for root-cause validation",
                },
            ],
        }
    )


class CurrentHarnessExecutor:
    """Production adapter that builds and runs the current Harness components."""

    def __init__(
        self,
        config: BenchmarkRunConfig,
        *,
        model_client_factory: ModelClientFactory | None = None,
    ) -> None:
        _validate_runtime_config(config)
        self.config = config.model_copy(deep=True)
        self.model_client_factory = model_client_factory

    def __call__(self, runtime_input: HarnessRuntimeInput) -> HarnessExecutionOutput:
        return self.execute(runtime_input)

    def execute(self, runtime_input: HarnessRuntimeInput) -> HarnessExecutionOutput:
        """Run the graph until a legal terminal or benchmark stop state."""

        config = self.config
        runtime_config = runtime_input.runtime_config or _runtime_config(config)
        if runtime_config.harness_version.lower() not in SUPPORTED_HARNESS_VERSIONS:
            raise ValueError(f"unsupported harness_version: {runtime_config.harness_version!r}")
        registry = build_default_tool_registry()
        model_client = self._model_client(runtime_input)
        planner = Planner(
            model_client,
            tool_registry=registry,
            max_retries=config.max_planner_retries,
        )
        tool_executor = ToolExecutor(runtime_input.database_path, registry=registry)
        checkpoint_manager = None
        if runtime_config.checkpoint_enabled:
            checkpoint_manager = CheckpointManager(
                FileCheckpointStore(Path(runtime_input.database_path).parent / "checkpoints")
            )
        graph = HarnessGraph(
            planner=planner,
            tool_executor=tool_executor,
            hypothesis_manager=HypothesisManager(),
            root_cause_validator=RootCauseValidator(),
            guardrail_runtime=GuardrailRuntime(registry=registry),
            checkpoint_manager=checkpoint_manager,
        )
        state = IncidentState(
            alert=cast(dict[str, JsonValue], runtime_input.alert.model_dump(mode="json"))
        )
        planner_result = graph.prepare_plan(
            state,
            metric_context=load_metric_context(runtime_input.alert.metric),
        )
        graph.transition(state, IncidentStatus.EXECUTING)
        self._run_graph_loop(graph, state)
        usage = planner_result.model_result.usage if planner_result.model_result else None
        return HarnessExecutionOutput(
            harness_status=state.status.value,
            predicted_root_cause=(
                str(state.root_cause["root_cause_type"])
                if state.root_cause is not None
                else None
            ),
            trace_payload={
                "schema_version": 1,
                "partial": False,
                "state": state.to_dict(),
                "planner": planner_result.model_dump(
                    mode="json", exclude={"model_result"}
                ),
                "model_usage": usage.model_dump(mode="json") if usage else None,
            },
            tool_call_count=state.guardrail_usage.tool_calls,
            sql_call_count=state.guardrail_usage.sql_calls,
            cost=_cost_from_usage(usage, config),
            unsafe_operation_count=state.guardrail_usage.blocked_calls,
            model_usage=usage,
        )

    def _model_client(self, runtime_input: HarnessRuntimeInput) -> ModelClient:
        runtime_config = runtime_input.runtime_config or _runtime_config(self.config)
        if runtime_config.model_provider != self.config.model_provider:
            raise ValueError(
                "runtime model_provider does not match the configured Harness executor"
            )
        if runtime_config.model_name != self.config.model_name:
            raise ValueError(
                "runtime model_name does not match the configured Harness executor"
            )
        if self.model_client_factory is not None:
            return self.model_client_factory(runtime_input)
        if runtime_config.model_provider == "mock":
            response = (
                InvestigationPlan.model_validate(self.config.mock_plan)
                if self.config.mock_plan is not None
                else _default_mock_plan(runtime_input.alert)
            )
            return MockModelClient(response, model=runtime_config.model_name)
        settings = ModelSettings(
            model_provider=runtime_config.model_provider,
            openai_model=runtime_config.model_name,
        )
        return create_model_client(settings)

    @staticmethod
    def _run_graph_loop(graph: HarnessGraph, state: IncidentState) -> None:
        """Advance through graph APIs and interpret observations generically."""

        steps = [InvestigationStep.model_validate(step) for step in state.plan]
        for index, step in enumerate(steps, start=1):
            if state.status is not IncidentStatus.EXECUTING:
                break
            graph.execute_next_step(
                state,
                step,
                trace_id=f"{state.alert.get('incident_id', 'incident')}-step-{index}",
            )
            if state.status is not IncidentStatus.VALIDATING:
                break
            graph.enter_hypothesis_testing(state)
            trace = state.tool_trace[-1]
            evidence = _interpret_tool_observation(trace, step, index)
            graph.register_evidence(state, evidence)
            graph.attach_evidence(
                state,
                step.hypothesis_id,
                evidence.evidence_id,
                supports=True,
            )
            validation = graph.validate_hypothesis(
                state,
                step.hypothesis_id,
                graph.hypothesis_manager.evidence(),
            )
            if validation.to_status is IncidentStatus.ROOT_CAUSE_FOUND:
                break
            if index < len(steps) and state.status is IncidentStatus.HYPOTHESIS_TESTING:
                graph.request_more_evidence(state)
                continue
            if state.status is IncidentStatus.HYPOTHESIS_TESTING:
                graph.transition(state, IncidentStatus.UNRESOLVED, reason="plan exhausted")
            break


def build_harness_executor(
    config: BenchmarkRunConfig,
    *,
    model_client_factory: ModelClientFactory | None = None,
) -> CurrentHarnessExecutor:
    """Validate selection and build the real current-main Harness adapter."""

    return CurrentHarnessExecutor(config, model_client_factory=model_client_factory)


def load_selected_cases(
    config: BenchmarkRunConfig,
    cases_directory: Path = Path("benchmark/cases"),
) -> list[CaseManifest]:
    """Load the selected manifests through the canonical case loader."""

    if len(set(config.case_ids)) != len(config.case_ids):
        raise ValueError("benchmark case_ids must not contain duplicates")
    return [
        load_case_manifest(case_id, directory=cases_directory)
        for case_id in config.case_ids
    ]


def _cost_from_usage(
    usage: ModelUsage | None,
    config: BenchmarkRunConfig,
) -> float | None:
    if usage is None:
        return None
    if (
        usage.input_tokens is None
        or usage.output_tokens is None
        or config.input_cost_per_token is None
        or config.output_cost_per_token is None
    ):
        return None
    return (
        usage.input_tokens * config.input_cost_per_token
        + usage.output_tokens * config.output_cost_per_token
    )


def _source_type_for_step(step: InvestigationStep) -> str:
    sql = str(step.arguments.get("sql", "")).lower()
    material = f"{step.purpose} {step.expected_evidence[0]} {sql}".lower()
    if "schema_snapshot" in material or "schema drift" in material:
        return "schema_metadata"
    if "metric_version" in material or "definition_hash" in material:
        return "metric_version"
    if "experiment_config" in material or "allocation" in material:
        return "experiment_config"
    if "partition_metadata" in material or "pipeline_runs" in material:
        return "operational_metadata"
    return "business_data"


def _interpret_tool_observation(
    trace: Mapping[str, Any],
    step: InvestigationStep,
    sequence: int,
) -> EvidenceReference:
    """Turn a completed tool result into explicit runtime evidence metadata."""

    query_id = trace.get("query_id")
    evidence_id = f"runtime-observation-{query_id or sequence}"
    observation = cast(
        dict[str, JsonValue],
        {"tool_result": cast(JsonValue, dict(trace))},
    )
    return EvidenceReference(
        evidence_id=evidence_id,
        source_type=_source_type_for_step(step),
        description=step.expected_evidence[0],
        query_id=str(query_id) if query_id else None,
        observation=observation,
    )


def _worker_put(result_queue: Any, payload: dict[str, Any]) -> None:
    try:
        result_queue.put(payload)
    except (BrokenPipeError, EOFError, OSError):
        pass


def _executor_worker(
    executor: HarnessExecutor,
    runtime_payload: dict[str, Any],
    result_queue: Any,
) -> None:
    try:
        runtime_input = HarnessRuntimeInput.model_validate(runtime_payload)
        output = executor(runtime_input)
        if not isinstance(output, HarnessExecutionOutput):
            output = HarnessExecutionOutput.model_validate(output)
        _worker_put(result_queue, {"ok": True, "output": output.model_dump(mode="json")})
    except BaseException as exc:  # noqa: BLE001 - worker boundary reports every failure
        _worker_put(
            result_queue,
            {
                "ok": False,
                "error_type": type(exc).__name__,
                "error_message": str(exc),
            },
        )


def _production_case_worker(
    executor: CurrentHarnessExecutor,
    config_payload: dict[str, Any],
    manifest_payload: dict[str, Any],
    cases_directory: str,
    case_index: int,
    run_id: str,
    result_queue: Any,
) -> None:
    try:
        config = BenchmarkRunConfig.model_validate(config_payload)
        manifest = CaseManifest.model_validate(manifest_payload)
        database_path = materialize_case_environment(
            config,
            manifest,
            cases_directory=Path(cases_directory),
            case_index=case_index,
        )
        runtime_input = build_runtime_input(
            manifest,
            database_path,
            run_id=run_id,
            config=config,
        )
        output = executor(runtime_input)
        _worker_put(result_queue, {"ok": True, "output": output.model_dump(mode="json")})
    except BaseException as exc:  # noqa: BLE001 - worker boundary reports every failure
        _worker_put(
            result_queue,
            {
                "ok": False,
                "error_type": type(exc).__name__,
                "error_message": str(exc),
            },
        )


def _run_process_with_timeout(
    target: Callable[..., None],
    args: tuple[Any, ...],
    timeout_seconds: float,
) -> HarnessExecutionOutput:
    context = mp.get_context("spawn")
    result_queue = context.Queue()
    process = context.Process(target=target, args=(*args, result_queue))
    started = False
    try:
        process.start()
        started = True
        deadline = perf_counter() + timeout_seconds
        message: dict[str, Any] | None = None
        while message is None:
            remaining = deadline - perf_counter()
            if remaining <= 0:
                _terminate_worker(process)
                raise BenchmarkTimeoutError(
                    f"case execution exceeded {timeout_seconds:g} seconds; worker process terminated",
                    worker_pid=process.pid,
                )
            try:
                candidate = result_queue.get(timeout=min(0.05, remaining))
            except queue.Empty:
                if not process.is_alive():
                    raise RuntimeError(
                        "benchmark worker exited without a result "
                        f"(exit code {process.exitcode})"
                    )
                continue
            if not isinstance(candidate, dict):
                raise TypeError("benchmark worker returned an invalid result envelope")
            message = candidate

        # Consume the queue before joining. A large trace can otherwise leave
        # the child feeder blocked while the parent waits for child exit.
        process.join(1.0)
        if process.is_alive():
            _terminate_worker(process)
    finally:
        if started and process.is_alive():
            _terminate_worker(process)
        result_queue.close()
        result_queue.join_thread()
    assert message is not None
    if not message.get("ok"):
        raise RuntimeError(
            f"{message.get('error_type', 'WorkerError')}: "
            f"{message.get('error_message', 'worker failed')}"
        )
    return HarnessExecutionOutput.model_validate(message["output"])


def _terminate_worker(process: Any) -> None:
    """Stop a worker and wait for it without allowing a queue leak."""

    if process.is_alive():
        process.terminate()
        process.join(1.0)
    if process.is_alive():
        process.kill()
        process.join(1.0)


def _execute_thread_with_timeout(
    executor: HarnessExecutor,
    runtime_input: HarnessRuntimeInput,
    timeout_seconds: float,
) -> HarnessExecutionOutput:
    """Compatibility path for non-picklable unit-test callbacks only."""

    pool = ThreadPoolExecutor(max_workers=1)
    future = pool.submit(executor, runtime_input)
    try:
        result = future.result(timeout=timeout_seconds)
        return result if isinstance(result, HarnessExecutionOutput) else HarnessExecutionOutput.model_validate(result)
    except FutureTimeoutError as exc:
        future.cancel()
        raise TimeoutError(f"case execution exceeded {timeout_seconds:g} seconds") from exc
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


def execute_with_timeout(
    executor: HarnessExecutor,
    runtime_input: HarnessRuntimeInput,
    timeout_seconds: float,
) -> HarnessExecutionOutput:
    """Use killable process isolation whenever the injected executor is picklable."""

    try:
        pickle.dumps(executor)
    except (pickle.PicklingError, TypeError, AttributeError):
        return _execute_thread_with_timeout(executor, runtime_input, timeout_seconds)
    return _run_process_with_timeout(
        _executor_worker,
        (executor, runtime_input.model_dump(mode="json")),
        timeout_seconds,
    )


def write_trace(
    config: BenchmarkRunConfig,
    manifest: CaseManifest,
    trace_payload: Mapping[str, Any],
) -> Path:
    """Persist a JSON-safe trace under the isolated case directory."""

    path = _case_directory(config, manifest) / "trace.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(dict(trace_payload), ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    return path


def write_case_result(
    config: BenchmarkRunConfig,
    manifest: CaseManifest,
    result: BenchmarkCaseResult,
) -> Path:
    """Persist every case result, including errors and hard timeouts."""

    path = _case_directory(config, manifest) / "result.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(result.model_dump_json(indent=2), encoding="utf-8")
    return path


def _error_result(
    manifest: CaseManifest | None,
    case_id: str,
    *,
    status: CaseRunStatus,
    duration_ms: float,
    message: str,
) -> BenchmarkCaseResult:
    return BenchmarkCaseResult(
        case_id=case_id,
        status=status,
        expected_root_cause=manifest.root_cause_type if manifest else None,
        duration_ms=duration_ms,
        error_message=message,
        partial_trace=status == "timed_out",
    )


def _persist_case_artifacts(
    config: BenchmarkRunConfig,
    manifest: CaseManifest,
    result: BenchmarkCaseResult,
    trace_payload: Mapping[str, Any],
) -> BenchmarkCaseResult:
    try:
        trace_path = write_trace(config, manifest, trace_payload)
        result = result.model_copy(update={"trace_path": trace_path})
    except Exception as exc:  # noqa: BLE001 - trace failure is case-local
        result = result.model_copy(
            update={
                "status": "error",
                "top1_correct": None,
                "error_message": f"trace write failed: {type(exc).__name__}: {exc}",
                "partial_trace": True,
            }
        )
    try:
        result_path = write_case_result(config, manifest, result)
        return result.model_copy(update={"result_path": result_path})
    except Exception as exc:  # noqa: BLE001 - caller still receives case result
        return result.model_copy(
            update={
                "status": "error",
                "top1_correct": None,
                "error_message": f"result write failed: {type(exc).__name__}: {exc}",
            }
        )


def _persist_case_id_error(
    config: BenchmarkRunConfig,
    case_id: str,
    result: BenchmarkCaseResult,
) -> BenchmarkCaseResult:
    """Persist a load/build error when no valid manifest is available."""

    case_directory = _run_directory(config) / case_id
    trace_path = case_directory / "trace.json"
    try:
        case_directory.mkdir(parents=True, exist_ok=True)
        trace_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "partial": True,
                    "status": result.status,
                    "error": result.error_message,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        result = result.model_copy(update={"trace_path": trace_path})
    except Exception as exc:  # noqa: BLE001 - artifact failure is case-local
        result = result.model_copy(
            update={
                "status": "error",
                "top1_correct": None,
                "error_message": f"{result.error_message}; trace write failed: {type(exc).__name__}: {exc}",
                "partial_trace": True,
            }
        )
    result_path = case_directory / "result.json"
    try:
        case_directory.mkdir(parents=True, exist_ok=True)
        result_path.write_text(result.model_dump_json(indent=2), encoding="utf-8")
        return result.model_copy(update={"result_path": result_path})
    except Exception as exc:  # noqa: BLE001 - artifact failure is case-local
        return result.model_copy(
            update={
                "status": "error",
                "top1_correct": None,
                "error_message": f"{result.error_message}; result write failed: {type(exc).__name__}: {exc}",
            }
        )


def run_case(
    config: BenchmarkRunConfig,
    manifest: CaseManifest,
    executor: HarnessExecutor,
    *,
    run_id: str,
    case_index: int = 1,
    cases_directory: Path = Path("benchmark/cases"),
) -> BenchmarkCaseResult:
    """Run, score, and persist one case without allowing it to abort a batch."""

    started = perf_counter()
    trace_payload: dict[str, Any] = {"schema_version": 1, "partial": True}
    try:
        if isinstance(executor, CurrentHarnessExecutor):
            config_payload = config.model_dump(mode="json")
            manifest_payload = manifest.model_dump(mode="json")
            execution = _run_process_with_timeout(
                _production_case_worker,
                (
                    executor,
                    config_payload,
                    manifest_payload,
                    str(cases_directory.resolve()),
                    case_index,
                    run_id,
                ),
                config.per_case_timeout_seconds,
            )
        else:
            database_path = materialize_case_environment(
                config,
                manifest,
                cases_directory=cases_directory,
                case_index=case_index,
            )
            runtime_input = build_runtime_input(
                manifest,
                database_path,
                run_id=run_id,
                config=config,
            )
            execution = execute_with_timeout(
                executor,
                runtime_input,
                config.per_case_timeout_seconds,
            )
        top1_correct = (
            execution.predicted_root_cause == manifest.root_cause_type
            if execution.predicted_root_cause is not None
            else None
        )
        result = BenchmarkCaseResult(
            case_id=manifest.case_id,
            status="completed",
            harness_status=execution.harness_status,
            predicted_root_cause=execution.predicted_root_cause,
            expected_root_cause=manifest.root_cause_type,
            top1_correct=top1_correct,
            tool_call_count=execution.tool_call_count,
            sql_call_count=execution.sql_call_count,
            duration_ms=(perf_counter() - started) * 1000,
            cost=execution.cost,
            unsafe_operation_count=execution.unsafe_operation_count,
        )
        trace_payload = cast(dict[str, Any], execution.trace_payload)
    except BenchmarkTimeoutError as exc:
        result = _error_result(
            manifest,
            manifest.case_id,
            status="timed_out",
            duration_ms=(perf_counter() - started) * 1000,
            message=str(exc),
        )
        trace_payload = {
            "schema_version": 1,
            "partial": True,
            "status": "timed_out",
            "error": str(exc),
            "worker_pid": exc.worker_pid,
            "child_terminated": exc.child_terminated,
        }
    except TimeoutError as exc:
        result = _error_result(
            manifest,
            manifest.case_id,
            status="timed_out",
            duration_ms=(perf_counter() - started) * 1000,
            message=str(exc),
        )
        trace_payload = {"schema_version": 1, "partial": True, "status": "timed_out", "error": str(exc)}
    except Exception as exc:  # noqa: BLE001 - one case must not abort the batch
        result = _error_result(
            manifest,
            manifest.case_id,
            status="error",
            duration_ms=(perf_counter() - started) * 1000,
            message=f"{type(exc).__name__}: {exc}",
        )
        trace_payload = {
            "schema_version": 1,
            "partial": True,
            "status": "error",
            "error": result.error_message,
        }
    return _persist_case_artifacts(config, manifest, result, trace_payload)


def _percentile(values: Sequence[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(1, int((percentile / 100) * len(ordered) + 0.999999))
    return ordered[min(rank, len(ordered)) - 1]


def build_run_summary(
    config: BenchmarkRunConfig,
    results: list[BenchmarkCaseResult],
) -> BenchmarkRunSummary:
    """Aggregate status, score, latency, cost, and safety metrics."""

    attempted = len(results)
    completed = sum(result.status == "completed" for result in results)
    errors = sum(result.status == "error" for result in results)
    timeouts = sum(result.status == "timed_out" for result in results)
    scored = sum(result.top1_correct is not None for result in results)
    correct = sum(result.top1_correct is True for result in results)
    durations = [result.duration_ms for result in results]
    known_costs = [result.cost for result in results if result.cost is not None]
    total_cost = sum(known_costs) if known_costs else None
    blocked_calls = sum(result.unsafe_operation_count for result in results)
    budget_exceeded = sum(
        result.harness_status == IncidentStatus.BUDGET_EXCEEDED.value
        for result in results
    )
    total_tool_calls = sum(result.tool_call_count for result in results)
    total_sql_calls = sum(result.sql_call_count for result in results)
    return BenchmarkRunSummary(
        config=config,
        results=results,
        attempted=attempted,
        completed=completed,
        error_count=errors,
        timed_out_count=timeouts,
        scored=scored,
        correct=correct,
        top1_accuracy_scored=(correct / scored) if scored else None,
        top1_accuracy_attempted=(correct / attempted) if attempted else None,
        total_tool_calls=total_tool_calls,
        average_tool_calls=(total_tool_calls / attempted) if attempted else 0.0,
        total_sql_calls=total_sql_calls,
        average_sql_calls=(total_sql_calls / attempted) if attempted else 0.0,
        total_duration_ms=sum(durations),
        average_latency_ms=(sum(durations) / attempted) if attempted else 0.0,
        p50_latency_ms=_percentile(durations, 50),
        p95_latency_ms=_percentile(durations, 95),
        total_cost=total_cost,
        average_known_cost=(total_cost / len(known_costs)) if known_costs else None,
        cost_known_cases=len(known_costs),
        blocked_calls=blocked_calls,
        unsafe_attempts=blocked_calls,
        budget_exceeded=budget_exceeded,
        timeouts=timeouts,
        errors=errors,
    )


def write_run_summary(config: BenchmarkRunConfig, summary: BenchmarkRunSummary) -> Path:
    path = _run_directory(config) / "summary.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(summary.model_dump_json(indent=2), encoding="utf-8")
    return path


def write_results_jsonl(
    config: BenchmarkRunConfig,
    results: Sequence[BenchmarkCaseResult],
) -> Path:
    path = _run_directory(config) / "results.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(result.model_dump_json() + "\n" for result in results),
        encoding="utf-8",
    )
    return path


def _prepare_run_directory(config: BenchmarkRunConfig) -> Path:
    directory = _run_directory(config)
    if directory.exists() and not config.overwrite:
        raise FileExistsError(
            f"benchmark_run_id already exists: {config.benchmark_run_id}; "
            "use overwrite=True to opt in"
        )
    if directory.exists():
        if not directory.is_dir():
            raise FileExistsError(f"benchmark run path is not a directory: {directory}")
        shutil.rmtree(directory)
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def run_batch(
    config: BenchmarkRunConfig,
    cases_directory: Path = Path("benchmark/cases"),
    executor: HarnessExecutor | None = None,
) -> BenchmarkRunSummary:
    """Run selected cases sequentially with per-case failure isolation."""

    _validate_runtime_config(config)
    if len(set(config.case_ids)) != len(config.case_ids):
        raise ValueError("benchmark case_ids must not contain duplicates")
    _prepare_run_directory(config)
    active_executor = executor
    build_error: str | None = None
    if active_executor is None:
        try:
            active_executor = build_harness_executor(config)
        except Exception as exc:  # noqa: BLE001 - keep a build failure case-local
            build_error = f"{type(exc).__name__}: {exc}"
    results: list[BenchmarkCaseResult] = []
    for index, case_id in enumerate(config.case_ids, start=1):
        started = perf_counter()
        try:
            manifest = load_case_manifest(case_id, directory=cases_directory)
            validate_case_manifest(manifest)
        except Exception as exc:  # noqa: BLE001 - loading one case cannot abort the batch
            result = _error_result(
                None,
                case_id,
                status="error",
                duration_ms=(perf_counter() - started) * 1000,
                message=f"{type(exc).__name__}: {exc}",
            )
            results.append(_persist_case_id_error(config, case_id, result))
            continue
        if build_error is not None:
            result = _error_result(
                manifest,
                manifest.case_id,
                status="error",
                duration_ms=(perf_counter() - started) * 1000,
                message=f"Harness build failed: {build_error}",
            )
            results.append(_persist_case_artifacts(config, manifest, result, {
                "schema_version": 1,
                "partial": True,
                "status": "error",
                "error": result.error_message,
            }))
            continue
        results.append(
            run_case(
                config,
                manifest,
                active_executor,
                run_id=f"{config.benchmark_run_id}-{index:05d}",
                case_index=index,
                cases_directory=cases_directory,
            )
        )
    summary = build_run_summary(config, results)
    write_results_jsonl(config, results)
    write_run_summary(config, summary)
    return summary


SEED_CASE_IDS: tuple[str, ...] = tuple(
    f"F{fault_number:02d}-001" for fault_number in range(1, 13)
)
DEFAULT_SMOKE_CASE_IDS: tuple[str, ...] = (
    "F01-001",
    "F02-001",
    "F06-001",
    "F11-001",
    "F12-001",
)


def run_seed_orchestration(
    config: BenchmarkRunConfig,
    *,
    cases_directory: Path = Path("benchmark/cases"),
    executor: HarnessExecutor | None = None,
) -> BenchmarkRunSummary:
    """Run all canonical seed manifests in stable F01-F12 order."""

    return run_batch(
        config.model_copy(update={"case_ids": list(SEED_CASE_IDS)}),
        cases_directory=cases_directory,
        executor=executor,
    )


def run_real_harness_smoke(
    config: BenchmarkRunConfig,
    *,
    cases_directory: Path = Path("benchmark/cases"),
    executor: HarnessExecutor | None = None,
) -> BenchmarkRunSummary:
    """Run the five-case real Harness smoke without a live model provider."""

    smoke_config = config.model_copy(
        update={
            "case_ids": list(DEFAULT_SMOKE_CASE_IDS),
            "model_name": "deterministic-smoke",
            "model_provider": "mock",
        }
    )
    active_executor = executor
    if active_executor is None:
        active_executor = build_harness_executor(
            smoke_config,
            model_client_factory=_smoke_model_client_factory,
        )
    return run_batch(
        smoke_config if executor is None else config.model_copy(
            update={"case_ids": list(DEFAULT_SMOKE_CASE_IDS)}
        ),
        cases_directory=cases_directory,
        executor=active_executor,
    )


def _smoke_model_client_factory(runtime_input: HarnessRuntimeInput) -> ModelClient:
    """Deterministic model behavior based only on the runtime alert semantics."""

    alert = runtime_input.alert
    if alert.metric == "daily_active_users" and alert.change_rate <= -0.4:
        root_cause = "metric_definition_change"
        second_sql = "SELECT definition_hash, query FROM metric_versions LIMIT 1"
    elif alert.metric == "daily_active_users" and alert.change_rate < 0:
        root_cause = "missing_partition"
        second_sql = "SELECT row_count, status FROM partition_metadata LIMIT 1"
    elif alert.metric == "ai_task_count" and alert.change_rate > 0:
        root_cause = "duplicate_batch"
        second_sql = "SELECT status, error_type FROM pipeline_runs LIMIT 1"
    elif alert.metric == "average_session_duration":
        root_cause = "unit_error"
        second_sql = "SELECT schema_json FROM schema_snapshots LIMIT 1"
    elif alert.metric == "ai_task_count":
        root_cause = "field_drift"
        second_sql = "SELECT schema_json FROM schema_snapshots LIMIT 1"
    else:
        root_cause = "ab_split_anomaly"
        second_sql = "SELECT control_ratio, treatment_ratio FROM experiment_configs LIMIT 1"
    plan = _smoke_plan(alert, root_cause, second_sql)
    return MockModelClient(plan, model="deterministic-smoke")


def _smoke_plan(alert: Alert, root_cause: str, second_sql: str) -> InvestigationPlan:
    date_literal = alert.observed_at[:10]
    candidates = [
        root_cause,
        "missing_partition" if root_cause != "missing_partition" else "data_delay",
        "data_delay" if root_cause not in {"missing_partition", "data_delay"} else "null_value_anomaly",
    ]
    first_sql = (
        "SELECT COUNT(*) AS event_count FROM events "
        f"WHERE CAST(event_time AS DATE) = DATE '{date_literal}'"
    )
    return InvestigationPlan.model_validate(
        {
            "incident_id": alert.incident_id,
            "hypotheses": [
                {
                    "hypothesis_id": f"H{index:02d}",
                    "root_cause_type": candidate,
                    "description": f"Candidate explanation: {candidate}.",
                    "initial_confidence": 0.60 if index == 1 else 0.20,
                }
                for index, candidate in enumerate(candidates, start=1)
            ],
            "steps": [
                {
                    "step_id": "S01",
                    "purpose": "Inspect business activity for the alert date.",
                    "hypothesis_id": "H01",
                    "tool": "sql_query",
                    "arguments": {"sql": first_sql},
                    "expected_evidence": ["business activity observation"],
                    "stop_condition": "retain the business observation",
                },
                {
                    "step_id": "S02",
                    "purpose": "Inspect an independent operational or metadata signal.",
                    "hypothesis_id": "H01",
                    "tool": "sql_query",
                    "arguments": {"sql": second_sql},
                    "expected_evidence": ["independent metadata observation"],
                    "stop_condition": "validate the strongest candidate",
                },
            ],
        }
    )


__all__ = [
    "DEFAULT_SMOKE_CASE_IDS",
    "SEED_CASE_IDS",
    "BenchmarkCaseResult",
    "BenchmarkRunConfig",
    "BenchmarkRunSummary",
    "BenchmarkTimeoutError",
    "CaseRunStatus",
    "CurrentHarnessExecutor",
    "HarnessExecutionOutput",
    "HarnessRuntimeConfig",
    "HarnessRuntimeInput",
    "build_harness_executor",
    "build_run_summary",
    "build_runtime_input",
    "execute_with_timeout",
    "load_case_manifest",
    "load_case_manifests",
    "load_selected_cases",
    "materialize_case",
    "materialize_case_environment",
    "run_batch",
    "run_case",
    "run_real_harness_smoke",
    "run_seed_orchestration",
    "validate_case_manifest",
    "write_case_result",
    "write_results_jsonl",
    "write_run_summary",
    "write_trace",
]
