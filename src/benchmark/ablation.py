"""Four-architecture benchmark ablation with a shared fairness envelope.

This module owns experiment orchestration and scoring only.  It deliberately
keeps case manifests and expected labels in the outer runner; adapters receive
the same sanitized alert, metric semantics, taxonomy, and a byte-identical
copy of the materialized database for each variant.
"""

from __future__ import annotations

import asyncio
import csv
import hashlib
import json
import math
import multiprocessing as mp
import pickle
import queue
import shutil
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from pathlib import Path
from time import perf_counter
from typing import Any, Literal, Protocol, cast

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    field_validator,
    model_validator,
)

from agents.planner import (
    Alert,
    InvestigationPlan,
    InvestigationStep,
    MetricContext,
    Planner,
    load_metric_context,
)
from benchmark.case_generator import (
    load_case_manifest,
    load_case_manifests,
    materialize_case,
    validate_case_manifest,
)
from benchmark.cases import CaseManifest
from benchmark.evidence_interpreter import (
    EvidencePolarity,
    IncidentEvidenceContext,
    RuntimeEvidenceInterpreter,
)
from benchmark.runner import (
    BenchmarkRunConfig,
    CurrentHarnessExecutor,
    HarnessExecutionOutput,
    HarnessRuntimeInput,
    build_harness_executor,
    build_runtime_input,
)
from config.faults import DEFAULT_FAULT_CATALOG_PATH, load_fault_catalog
from config.model_settings import ModelSettings
from data.generator import write_outputs
from harness.graph import HarnessGraph
from harness.guardrails import (
    GuardrailEvent,
    GuardrailPolicy,
    GuardrailRuntime,
    GuardrailUsage,
)
from harness.hypothesis import HypothesisManager
from harness.state import IncidentState, IncidentStatus
from llm.base import ModelClient
from llm.factory import create_model_client
from llm.mock_client import MockModelClient
from llm.models import ModelUsage
from tools.executor import ToolExecutionResult, ToolExecutor
from tools.registry import build_default_tool_registry

VariantName = Literal[
    "single_prompt",
    "react",
    "state_graph_no_validator",
    "full_harness",
]
RunStatus = Literal["completed", "error", "timed_out"]
RunKind = Literal["full", "smoke"]

VARIANT_ORDER: tuple[VariantName, ...] = (
    "single_prompt",
    "react",
    "state_graph_no_validator",
    "full_harness",
)
CANONICAL_CASE_IDS: tuple[str, ...] = tuple(
    f"F{fault:02d}-{variant:03d}" for fault in range(1, 13) for variant in range(1, 6)
)
CANONICAL_ROOT_CAUSES: tuple[str, ...] = tuple(
    fault.root_cause_type
    for fault in load_fault_catalog(DEFAULT_FAULT_CATALOG_PATH).faults
)

_RUNTIME_INPUT_GT_KEYS = frozenset(
    {
        "case_id",
        "fault_id",
        "expected_evidence",
        "expected_root_cause",
        "ground_truth_case",
        "root_cause_type",
        "source_seed_case_id",
        "manifest",
    }
)
_TRACE_GT_KEYS = frozenset(
    {
        "case_id",
        "fault_id",
        "expected_root_cause",
        "ground_truth_case",
        "source_seed_case_id",
        "manifest",
    }
)
_UNSAFE_GUARDRAIL_REASONS = frozenset(
    {"unsafe_sql", "non_read_only_tool", "unsafe_tool"}
)
_SAFE_RUN_ID = r"^[A-Za-z0-9][A-Za-z0-9._-]*$"


class AblationConfig(BaseModel):
    """Validated configuration shared by all four architecture variants."""

    model_config = ConfigDict(extra="forbid", protected_namespaces=())

    case_ids: list[str] = Field(min_length=1)
    model_provider: Literal["mock", "openai"] = "mock"
    model_name: str = Field(min_length=1)
    model_base_url: str | None = None
    model_timeout_seconds: float = Field(default=60.0, gt=0)
    model_retries: int = Field(default=2, ge=0)
    model_retry_base_delay_seconds: float = Field(default=0.5, ge=0)
    per_case_timeout_seconds: float = Field(default=30.0, gt=0)
    max_agent_rounds: int = Field(default=20, gt=0)
    max_tool_calls: int = Field(default=20, gt=0)
    max_sql_calls: int = Field(default=15, gt=0)
    max_result_rows: int = Field(default=1000, gt=0)
    max_duplicate_calls: int = Field(default=1, gt=0)
    max_planner_retries: int = Field(default=2, ge=0)
    input_cost_per_token: float | None = Field(default=None, ge=0)
    output_cost_per_token: float | None = Field(default=None, ge=0)
    output_dir: Path = Path("experiments/ablation/results")
    run_id: str = "four-architecture-ablation"
    run_kind: RunKind = "full"
    resume: bool = False
    overwrite: bool = False
    mock_plan: dict[str, Any] | None = None

    @field_validator("case_ids")
    @classmethod
    def validate_case_ids(cls, values: list[str]) -> list[str]:
        if len(values) != len(set(values)):
            raise ValueError("case_ids must be unique")
        invalid = [value for value in values if value not in CANONICAL_CASE_IDS]
        if invalid:
            raise ValueError(
                "case_ids must use canonical F01-001 through F12-005 IDs: "
                + ", ".join(invalid)
            )
        return values

    @field_validator("model_name", "run_id")
    @classmethod
    def reject_blank_values(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("configuration values must not be blank")
        return value.strip()

    @field_validator("run_id")
    @classmethod
    def validate_run_id(cls, value: str) -> str:
        import re

        if re.fullmatch(_SAFE_RUN_ID, value) is None:
            raise ValueError(
                "run_id must contain only letters, numbers, '.', '_' or '-'"
            )
        return value

    @field_validator("model_base_url")
    @classmethod
    def normalize_base_url(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        return value.strip()

    @model_validator(mode="after")
    def validate_budget_relationships(self) -> AblationConfig:
        if self.model_provider == "mock" and self.model_name not in {
            "mock",
            "mock-model",
            "deterministic",
            "deterministic-smoke",
        }:
            raise ValueError(f"unsupported mock model_name: {self.model_name!r}")
        if self.max_duplicate_calls > self.max_tool_calls:
            raise ValueError("max_duplicate_calls cannot exceed max_tool_calls")
        return self

    @property
    def is_full_selection(self) -> bool:
        return tuple(self.case_ids) == CANONICAL_CASE_IDS

    def public_dict(self) -> dict[str, Any]:
        """Return config data safe to persist; credentials are never a field."""

        return cast(dict[str, Any], self.model_dump(mode="json"))


class AblationRuntimeInput(BaseModel):
    """The only input an ablation adapter is allowed to receive."""

    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(min_length=1)
    database_path: Path
    alert: Alert
    metric_context: MetricContext
    allowed_root_causes: list[str] = Field(min_length=1)


class RankedRootCauseResponse(BaseModel):
    """Single Prompt output contract; vocabulary is checked by the scorer."""

    model_config = ConfigDict(extra="forbid")

    ranked_root_causes: list[str] = Field(default_factory=list, max_length=3)


class ReactAction(BaseModel):
    """Public action-observation protocol without private chain-of-thought."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["tool", "final"]
    tool: str | None = None
    arguments: dict[str, JsonValue] = Field(default_factory=dict)
    ranked_root_causes: list[str] = Field(default_factory=list, max_length=3)

    @model_validator(mode="after")
    def validate_action(self) -> ReactAction:
        if self.type == "tool" and (not self.tool or not self.arguments):
            raise ValueError("tool actions require tool and arguments")
        if self.type == "final" and self.tool is not None:
            raise ValueError("final actions must not include a tool")
        return self


class AblationExecutionOutput(BaseModel):
    """Comparable observable result returned by every architecture adapter."""

    model_config = ConfigDict(extra="forbid", protected_namespaces=())

    variant: VariantName
    completion_status: str = Field(min_length=1)
    ranked_root_causes: list[str] = Field(default_factory=list, max_length=3)
    tool_call_count: int = Field(default=0, ge=0)
    sql_call_count: int = Field(default=0, ge=0)
    model_usage: ModelUsage | None = None
    known_cost: float | None = Field(default=None, ge=0)
    latency_ms: float = Field(default=0, ge=0)
    guardrail_events: list[dict[str, JsonValue]] = Field(default_factory=list)
    trace_payload: dict[str, JsonValue] = Field(default_factory=dict)
    error: str | None = None


class AblationCaseResult(BaseModel):
    """One outer-scored case/variant pair, including failed attempts."""

    model_config = ConfigDict(extra="forbid", protected_namespaces=())

    case_id: str
    fault_id: str
    variant: VariantName
    status: RunStatus
    completion_status: str
    ranked_root_causes: list[str] = Field(default_factory=list, max_length=3)
    expected_root_cause: str
    top1_correct: bool
    top3_correct: bool
    invalid_prediction: bool = False
    tool_call_count: int = Field(default=0, ge=0)
    sql_call_count: int = Field(default=0, ge=0)
    total_tool_attempts: int = Field(default=0, ge=0)
    invalid_sql_attempts: int = Field(default=0, ge=0)
    unsafe_operation_attempts: int = Field(default=0, ge=0)
    duplicate_operation_attempts: int = Field(default=0, ge=0)
    blocked_calls: int = Field(default=0, ge=0)
    budget_exceeded: bool = False
    model_usage: ModelUsage | None = None
    known_cost: float | None = Field(default=None, ge=0)
    latency_ms: float = Field(default=0, ge=0)
    abstention: bool = False
    error: str | None = None
    trace_payload: dict[str, JsonValue] = Field(default_factory=dict)


class AblationMetrics(BaseModel):
    """Shared aggregate metrics with explicit attempted denominators."""

    model_config = ConfigDict(extra="forbid", protected_namespaces=())

    variant: VariantName
    attempted: int = Field(ge=0)
    completed: int = Field(ge=0)
    errors: int = Field(ge=0)
    timeouts: int = Field(ge=0)
    abstentions: int = Field(ge=0)
    invalid_predictions: int = Field(ge=0)
    top1_correct: int = Field(ge=0)
    top3_correct: int = Field(ge=0)
    top1_accuracy: float = Field(ge=0, le=1)
    top3_accuracy: float = Field(ge=0, le=1)
    total_tool_calls: int = Field(ge=0)
    average_tool_calls: float = Field(ge=0)
    total_sql_calls: int = Field(ge=0)
    average_sql_calls: float = Field(ge=0)
    total_tool_attempts: int = Field(ge=0)
    total_sql_attempts: int = Field(ge=0)
    invalid_sql_attempts: int = Field(ge=0)
    invalid_sql_rate: float = Field(ge=0, le=1)
    unsafe_operation_attempts: int = Field(ge=0)
    unsafe_operation_rate: float = Field(ge=0, le=1)
    duplicate_operation_attempts: int = Field(ge=0)
    duplicate_operation_rate: float = Field(ge=0, le=1)
    blocked_calls: int = Field(ge=0)
    budget_exceeded: int = Field(ge=0)
    mean_latency_ms: float = Field(ge=0)
    p50_latency_ms: float = Field(ge=0)
    p95_latency_ms: float = Field(ge=0)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)
    known_cost_total: float | None = Field(default=None, ge=0)
    known_average_cost: float | None = Field(default=None, ge=0)
    known_cost_cases: int = Field(ge=0)


class FairnessValidation(BaseModel):
    """Machine-checkable fairness and completeness audit."""

    model_config = ConfigDict(extra="forbid", protected_namespaces=())

    case_count: int = Field(ge=0)
    variant_count: int = Field(ge=0)
    expected_pairs: int = Field(ge=0)
    attempted_pairs: int = Field(ge=0)
    complete_pair_matrix: bool
    same_db_hash: bool
    same_model_fingerprint: bool
    gt_runtime_leakage: bool
    duplicate_pairs: int = Field(default=0, ge=0)
    missing_pairs: list[str] = Field(default_factory=list)
    model_fingerprint: str
    db_hashes: dict[str, dict[str, str]] = Field(default_factory=dict)


class AblationRun:
    """In-memory return value for one persisted run."""

    def __init__(
        self,
        *,
        run_dir: Path,
        results: list[AblationCaseResult],
        metrics: dict[VariantName, AblationMetrics],
        fairness: FairnessValidation,
    ) -> None:
        self.run_dir = run_dir
        self.results = results
        self.metrics = metrics
        self.fairness = fairness


class ModelClientFactory(Protocol):
    def __call__(self, runtime_input: Any) -> ModelClient: ...


def _model_fingerprint(config: AblationConfig) -> str:
    material = {
        "provider": config.model_provider,
        "model": config.model_name,
        "base_url": config.model_base_url,
        "timeout_seconds": config.model_timeout_seconds,
        "retries": config.model_retries,
        "retry_base_delay_seconds": config.model_retry_base_delay_seconds,
        "principal_generation_settings": {},
    }
    return hashlib.sha256(
        json.dumps(material, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _config_fingerprint(config: AblationConfig) -> str:
    material = config.public_dict()
    material.pop("resume", None)
    material.pop("overwrite", None)
    return hashlib.sha256(
        json.dumps(material, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def full_run_blocker(
    config: AblationConfig,
    *,
    model_client_factory: ModelClientFactory | None = None,
) -> str | None:
    """Return a safe preflight blocker for a real configured-model run."""

    if config.run_kind != "full" or model_client_factory is not None:
        return None
    if config.model_provider == "openai":
        settings = ModelSettings(
            model_provider=config.model_provider,
            openai_model=config.model_name,
            openai_base_url=config.model_base_url,
            llm_timeout_seconds=config.model_timeout_seconds,
            llm_max_retries=config.model_retries,
            llm_retry_base_delay_seconds=config.model_retry_base_delay_seconds,
        )
        if settings.openai_api_key is None:
            return "OPENAI_API_KEY is not configured"
    return None


def _cost_from_usage(usage: ModelUsage | None, config: AblationConfig) -> float | None:
    if usage is None or usage.input_tokens is None or usage.output_tokens is None:
        return None
    if config.input_cost_per_token is None or config.output_cost_per_token is None:
        return None
    return (
        usage.input_tokens * config.input_cost_per_token
        + usage.output_tokens * config.output_cost_per_token
    )


def _aggregate_usage(usages: Sequence[ModelUsage]) -> ModelUsage | None:
    if not usages:
        return None

    def total(field: str) -> int | None:
        values = [getattr(usage, field) for usage in usages]
        if any(value is None for value in values):
            return None
        return sum(cast(int, value) for value in values)

    return ModelUsage(
        input_tokens=total("input_tokens"),
        output_tokens=total("output_tokens"),
        total_tokens=total("total_tokens"),
    )


def _assert_no_ground_truth_fields(
    value: object,
    *,
    forbidden_keys: frozenset[str] = _RUNTIME_INPUT_GT_KEYS,
) -> None:
    """Reject Ground Truth keys anywhere in a serialized runtime payload."""

    if isinstance(value, Mapping):
        leaked = forbidden_keys.intersection(value.keys())
        if leaked:
            raise ValueError(
                "Ground Truth field(s) leaked into runtime payload: "
                + ", ".join(sorted(leaked))
            )
        for child in value.values():
            _assert_no_ground_truth_fields(child, forbidden_keys=forbidden_keys)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for child in value:
            _assert_no_ground_truth_fields(child, forbidden_keys=forbidden_keys)


def serialize_runtime_input(runtime_input: AblationRuntimeInput) -> str:
    """Serialize and audit the exact payload visible to a variant adapter."""

    payload = runtime_input.model_dump(mode="json")
    _assert_no_ground_truth_fields(payload)
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _runtime_input(
    manifest: CaseManifest,
    database_path: Path,
    *,
    run_id: str,
) -> AblationRuntimeInput:
    """Build runtime input through the canonical benchmark sanitization API."""

    benchmark_config = BenchmarkRunConfig(
        case_ids=[manifest.case_id],
        harness_version="current-main",
        model_name="mock-model",
        output_dir=database_path.parent,
        benchmark_run_id="runtime",
        model_provider="mock",
    )
    sanitized: HarnessRuntimeInput = build_runtime_input(
        manifest,
        database_path,
        run_id=run_id,
        config=benchmark_config,
    )
    alert = sanitized.alert
    context = load_metric_context(alert.metric)
    runtime = AblationRuntimeInput(
        run_id=run_id,
        database_path=database_path,
        alert=alert,
        metric_context=context,
        allowed_root_causes=list(CANONICAL_ROOT_CAUSES),
    )
    serialize_runtime_input(runtime)
    return runtime


def _mock_response_factory(
    config: AblationConfig,
) -> Callable[[type[Any], str, str], Any]:
    """Return an offline response factory that contains no case answers."""

    def response(
        response_model: type[Any], _system_prompt: str, _user_prompt: str
    ) -> Any:
        if response_model is RankedRootCauseResponse:
            return RankedRootCauseResponse(
                ranked_root_causes=[
                    "missing_partition",
                    "data_delay",
                    "null_value_anomaly",
                ]
            )
        if response_model is ReactAction:
            return ReactAction(
                type="final",
                ranked_root_causes=[
                    "missing_partition",
                    "data_delay",
                    "null_value_anomaly",
                ],
            )
        if response_model is InvestigationPlan:
            if config.mock_plan is not None:
                return InvestigationPlan.model_validate(config.mock_plan)
            from benchmark.runner import _default_mock_plan

            return _default_mock_plan(
                Alert.model_validate(
                    {
                        "incident_id": "offline-smoke",
                        "metric": "daily_active_users",
                        "observed_at": "2026-01-30",
                        "expected_value": 1.0,
                        "observed_value": 0.5,
                        "change_rate": -0.5,
                        "severity": "high",
                    }
                )
            )
        raise TypeError(f"unsupported offline response model: {response_model!r}")

    return response


class AblationVariantExecutor:
    """Dispatch one variant while constructing no case-aware runtime state."""

    def __init__(
        self,
        config: AblationConfig,
        variant: VariantName,
        *,
        model_client_factory: ModelClientFactory | None = None,
    ) -> None:
        self.config = config.model_copy(deep=True)
        self.variant = variant
        self.model_client_factory = model_client_factory

    def __call__(self, runtime_input: AblationRuntimeInput) -> AblationExecutionOutput:
        serialize_runtime_input(runtime_input)
        if self.variant == "single_prompt":
            return self._single_prompt(runtime_input)
        if self.variant == "react":
            return self._react(runtime_input)
        if self.variant == "state_graph_no_validator":
            return self._state_graph_no_validator(runtime_input)
        return self._full_harness(runtime_input)

    def _model_client(self, runtime_input: Any) -> ModelClient:
        if self.model_client_factory is not None:
            return self.model_client_factory(runtime_input)
        if self.config.model_provider == "mock":
            return MockModelClient(
                _mock_response_factory(self.config),
                model=self.config.model_name,
            )
        settings = ModelSettings(
            model_provider=self.config.model_provider,
            openai_model=self.config.model_name,
            openai_base_url=self.config.model_base_url,
            llm_timeout_seconds=self.config.model_timeout_seconds,
            llm_max_retries=self.config.model_retries,
            llm_retry_base_delay_seconds=self.config.model_retry_base_delay_seconds,
        )
        return create_model_client(settings)

    @staticmethod
    def _call_model(
        client: ModelClient,
        *,
        system_prompt: str,
        user_prompt: str,
        response_model: type[Any],
    ) -> Any:
        return asyncio.run(
            client.generate_structured(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                response_model=response_model,
            )
        )

    def _single_prompt(
        self, runtime_input: AblationRuntimeInput
    ) -> AblationExecutionOutput:
        started = perf_counter()
        usages: list[ModelUsage] = []
        try:
            client = self._model_client(runtime_input)
            result = self._call_model(
                client,
                system_prompt=(
                    "You are a single-pass incident classifier. Return only the "
                    "strict JSON schema with up to three ranked canonical root causes. "
                    "Do not call tools or invent evidence."
                ),
                user_prompt=json.dumps(
                    {
                        "alert": runtime_input.alert.model_dump(mode="json"),
                        "metric_context": runtime_input.metric_context.planner_payload(),
                        "allowed_root_causes": runtime_input.allowed_root_causes,
                    },
                    sort_keys=True,
                ),
                response_model=RankedRootCauseResponse,
            )
            usages.append(result.usage)
            parsed = RankedRootCauseResponse.model_validate(result.parsed)
            return _output(
                variant="single_prompt",
                completion_status="completed",
                ranked=parsed.ranked_root_causes,
                usage=_aggregate_usage(usages),
                config=self.config,
                latency_ms=(perf_counter() - started) * 1000,
                trace={"schema_version": 1, "model_calls": 1, "tool_trace": []},
            )
        except Exception as exc:  # noqa: BLE001 - adapter boundary is observable
            return _output(
                variant="single_prompt",
                completion_status="error",
                usage=_aggregate_usage(usages),
                config=self.config,
                latency_ms=(perf_counter() - started) * 1000,
                trace={"schema_version": 1, "model_calls": 1, "tool_trace": []},
                error=f"{type(exc).__name__}: {exc}",
            )

    def _react(self, runtime_input: AblationRuntimeInput) -> AblationExecutionOutput:
        started = perf_counter()
        registry = build_default_tool_registry()
        policy = GuardrailPolicy(
            max_agent_rounds=self.config.max_agent_rounds,
            max_tool_calls=self.config.max_tool_calls,
            max_sql_calls=self.config.max_sql_calls,
            max_result_rows=self.config.max_result_rows,
            max_duplicate_calls=self.config.max_duplicate_calls,
        )
        guardrails = GuardrailRuntime(policy=policy, registry=registry)
        usage = GuardrailUsage()
        tool_executor = ToolExecutor(runtime_input.database_path, registry=registry)
        usages: list[ModelUsage] = []
        events: list[GuardrailEvent] = []
        tool_trace: list[dict[str, JsonValue]] = []
        status = "unresolved"
        error: str | None = None
        try:
            client = self._model_client(runtime_input)
            observations: list[dict[str, JsonValue]] = []
            for round_index in range(1, self.config.max_agent_rounds + 1):
                action_result = self._call_model(
                    client,
                    system_prompt=(
                        "You are a bounded ReAct diagnostician. On each turn return "
                        "one public JSON action: type=tool with a registered read-only "
                        "tool and arguments, or type=final with up to three ranked "
                        "canonical root causes. Never provide private chain-of-thought."
                    ),
                    user_prompt=json.dumps(
                        {
                            "alert": runtime_input.alert.model_dump(mode="json"),
                            "metric_context": runtime_input.metric_context.planner_payload(),
                            "allowed_root_causes": runtime_input.allowed_root_causes,
                            "observations": observations,
                        },
                        sort_keys=True,
                    ),
                    response_model=ReactAction,
                )
                usages.append(action_result.usage)
                action = ReactAction.model_validate(action_result.parsed)
                if action.type == "final":
                    status = "completed"
                    return _output(
                        variant="react",
                        completion_status=status,
                        ranked=action.ranked_root_causes,
                        usage=_aggregate_usage(usages),
                        config=self.config,
                        latency_ms=(perf_counter() - started) * 1000,
                        events=events,
                        trace={
                            "schema_version": 1,
                            "model_calls": len(usages),
                            "tool_trace": tool_trace,
                            "guardrail_usage": usage.model_dump(mode="json"),
                        },
                    )

                step = InvestigationStep.model_validate(
                    {
                        "step_id": f"react-{round_index:03d}",
                        "purpose": "Execute the bounded public ReAct tool action.",
                        "hypothesis_id": "react",
                        "tool": action.tool,
                        "arguments": action.arguments,
                        "expected_evidence": ["runtime tool observation"],
                        "stop_condition": "use the observation to choose the next action",
                    }
                )
                decision = guardrails.preflight(usage, step)
                if decision.allowed:
                    guardrails.record_allowed(usage, decision)
                else:
                    guardrails.record_blocked(usage)
                events.append(
                    guardrails.event(
                        usage,
                        decision,
                        event_type="preflight",
                        incident_id=runtime_input.alert.incident_id,
                        trace_id=f"react-round-{round_index:03d}",
                        step_id=step.step_id,
                        sequence=len(events) + 1,
                    )
                )
                if not decision.allowed:
                    status = (
                        "budget_exceeded"
                        if decision.reason
                        in {
                            "agent_round_budget_exceeded",
                            "tool_call_budget_exceeded",
                            "sql_call_budget_exceeded",
                        }
                        else "tool_failed"
                    )
                    break
                result = tool_executor.execute_step(
                    step,
                    incident_id=runtime_input.alert.incident_id,
                    trace_id=f"react-round-{round_index:03d}",
                    metric_id=runtime_input.metric_context.metric_id,
                    timeout_seconds=decision.timeout_seconds,
                    max_rows=decision.max_rows,
                )
                payload = cast(dict[str, JsonValue], result.model_dump(mode="json"))
                tool_trace.append(payload)
                observations.append(payload)
                for reason, message in guardrails.postflight(payload):
                    events.append(
                        guardrails.event(
                            usage,
                            decision,
                            event_type="postflight",
                            incident_id=runtime_input.alert.incident_id,
                            trace_id=f"react-round-{round_index:03d}",
                            step_id=step.step_id,
                            sequence=len(events) + 1,
                            reason=reason,
                            message=message,
                        )
                    )
                if not result.success:
                    status = "tool_failed"
                    break
            else:
                status = "budget_exceeded"
        except Exception as exc:  # noqa: BLE001 - adapter boundary is observable
            status = "error"
            error = f"{type(exc).__name__}: {exc}"
        return _output(
            variant="react",
            completion_status=status,
            usage=_aggregate_usage(usages),
            config=self.config,
            latency_ms=(perf_counter() - started) * 1000,
            events=events,
            trace={
                "schema_version": 1,
                "model_calls": len(usages),
                "tool_trace": tool_trace,
                "guardrail_usage": usage.model_dump(mode="json"),
            },
            error=error,
        )

    def _state_graph_no_validator(
        self,
        runtime_input: AblationRuntimeInput,
    ) -> AblationExecutionOutput:
        started = perf_counter()
        usages: list[ModelUsage] = []
        try:
            registry = build_default_tool_registry()
            client = self._model_client(runtime_input)
            planner = Planner(
                client,
                tool_registry=registry,
                max_retries=self.config.max_planner_retries,
            )
            executor = ToolExecutor(runtime_input.database_path, registry=registry)
            graph = HarnessGraph(
                planner=planner,
                tool_executor=executor,
                hypothesis_manager=HypothesisManager(),
                guardrail_runtime=GuardrailRuntime(
                    policy=GuardrailPolicy(
                        max_agent_rounds=self.config.max_agent_rounds,
                        max_tool_calls=self.config.max_tool_calls,
                        max_sql_calls=self.config.max_sql_calls,
                        max_result_rows=self.config.max_result_rows,
                        max_duplicate_calls=self.config.max_duplicate_calls,
                    ),
                    registry=registry,
                ),
                # A guard object is supplied only to make an accidental
                # validator call fail loudly. This adapter never calls it.
                root_cause_validator=_ValidatorMustNotBeCalled(),
            )
            state = IncidentState(
                alert=cast(
                    dict[str, JsonValue], runtime_input.alert.model_dump(mode="json")
                )
            )
            plan_result = graph.prepare_plan(
                state,
                metric_context=runtime_input.metric_context,
            )
            if plan_result.model_result is not None:
                usages.append(plan_result.model_result.usage)
            graph.transition(state, IncidentStatus.EXECUTING)
            self._run_unvalidated_graph(graph, state)
            if state.root_cause is not None:
                raise AssertionError(
                    "state_graph_no_validator mutated state.root_cause"
                )
            ranked = _rank_hypotheses(graph.hypothesis_manager.hypotheses())
            return _output(
                variant="state_graph_no_validator",
                completion_status=(
                    "unresolved"
                    if state.status is IncidentStatus.HYPOTHESIS_TESTING
                    else state.status.value.lower()
                ),
                ranked=ranked,
                usage=_aggregate_usage(usages),
                config=self.config,
                latency_ms=(perf_counter() - started) * 1000,
                tool_call_count=state.guardrail_usage.tool_calls,
                sql_call_count=state.guardrail_usage.sql_calls,
                events=state.guardrail_events,
                trace={
                    "schema_version": 1,
                    "model_calls": len(usages),
                    "state": state.to_dict(),
                    "planner": plan_result.model_dump(
                        mode="json", exclude={"model_result"}
                    ),
                },
            )
        except Exception as exc:  # noqa: BLE001 - adapter boundary is observable
            return _output(
                variant="state_graph_no_validator",
                completion_status="error",
                usage=_aggregate_usage(usages),
                config=self.config,
                latency_ms=(perf_counter() - started) * 1000,
                trace={"schema_version": 1, "model_calls": len(usages)},
                error=f"{type(exc).__name__}: {exc}",
            )

    @staticmethod
    def _run_unvalidated_graph(graph: HarnessGraph, state: IncidentState) -> None:
        steps = [InvestigationStep.model_validate(step) for step in state.plan]
        interpreter = RuntimeEvidenceInterpreter(
            context=IncidentEvidenceContext.from_alert(state.alert)
        )
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
            result = ToolExecutionResult.model_validate(state.tool_trace[-1])
            hypothesis = graph.hypothesis_manager.get_hypothesis(step.hypothesis_id)
            interpretation = interpreter.interpret(
                hypothesis=hypothesis,
                step=step,
                tool_result=result,
            )
            for decision in interpretation.decisions:
                if decision.polarity is EvidencePolarity.NEUTRAL:
                    continue
                evidence = decision.evidence
                graph.register_evidence(state, evidence)
                graph.attach_evidence(
                    state,
                    step.hypothesis_id,
                    evidence.evidence_id,
                    supports=decision.polarity is EvidencePolarity.SUPPORTS,
                )
            if index < len(steps) and state.status is IncidentStatus.HYPOTHESIS_TESTING:
                graph.request_more_evidence(state)
                continue
            break

    def _full_harness(
        self, runtime_input: AblationRuntimeInput
    ) -> AblationExecutionOutput:
        started = perf_counter()
        try:
            benchmark_config = BenchmarkRunConfig(
                case_ids=["F01-001"],
                harness_version="current-main",
                model_name=self.config.model_name,
                output_dir=runtime_input.database_path.parent,
                benchmark_run_id="runtime",
                model_provider=self.config.model_provider,
                mock_plan=self.config.mock_plan,
                checkpoint_enabled=False,
                max_planner_retries=self.config.max_planner_retries,
                max_agent_rounds=self.config.max_agent_rounds,
                max_tool_calls=self.config.max_tool_calls,
                max_sql_calls=self.config.max_sql_calls,
                max_result_rows=self.config.max_result_rows,
                max_duplicate_calls=self.config.max_duplicate_calls,
                input_cost_per_token=self.config.input_cost_per_token,
                output_cost_per_token=self.config.output_cost_per_token,
            )
            factory = self.model_client_factory or self._model_client
            executor: CurrentHarnessExecutor = build_harness_executor(
                benchmark_config,
                model_client_factory=factory,
            )
            harness_input = HarnessRuntimeInput(
                run_id=runtime_input.run_id,
                database_path=runtime_input.database_path,
                alert=runtime_input.alert,
            )
            output: HarnessExecutionOutput = executor(harness_input)
            state_payload = output.trace_payload.get("state", {})
            ranked = _rank_state_payload(
                state_payload,
                predicted=output.predicted_root_cause,
            )
            return _output(
                variant="full_harness",
                completion_status=output.harness_status,
                ranked=ranked,
                usage=output.model_usage,
                config=self.config,
                latency_ms=(perf_counter() - started) * 1000,
                tool_call_count=output.tool_call_count,
                sql_call_count=output.sql_call_count,
                events=_events_from_trace(output.trace_payload),
                trace=cast(dict[str, Any], output.trace_payload),
            )
        except Exception as exc:  # noqa: BLE001 - adapter boundary is observable
            return _output(
                variant="full_harness",
                completion_status="error",
                config=self.config,
                latency_ms=(perf_counter() - started) * 1000,
                trace={"schema_version": 1},
                error=f"{type(exc).__name__}: {exc}",
            )


class SinglePromptAdapter(AblationVariantExecutor):
    def __init__(
        self,
        config: AblationConfig,
        *,
        model_client_factory: ModelClientFactory | None = None,
    ) -> None:
        super().__init__(
            config, "single_prompt", model_client_factory=model_client_factory
        )


class ReActAdapter(AblationVariantExecutor):
    def __init__(
        self,
        config: AblationConfig,
        *,
        model_client_factory: ModelClientFactory | None = None,
    ) -> None:
        super().__init__(config, "react", model_client_factory=model_client_factory)


class StateGraphNoValidatorAdapter(AblationVariantExecutor):
    def __init__(
        self,
        config: AblationConfig,
        *,
        model_client_factory: ModelClientFactory | None = None,
    ) -> None:
        super().__init__(
            config,
            "state_graph_no_validator",
            model_client_factory=model_client_factory,
        )


class FullHarnessAdapter(AblationVariantExecutor):
    def __init__(
        self,
        config: AblationConfig,
        *,
        model_client_factory: ModelClientFactory | None = None,
    ) -> None:
        super().__init__(
            config, "full_harness", model_client_factory=model_client_factory
        )


class _ValidatorMustNotBeCalled:
    def validate(self, *_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError(
            "RootCauseValidator is not part of the no-validator variant"
        )


def _rank_hypotheses(hypotheses: Sequence[Any]) -> list[str]:
    ordered = sorted(
        enumerate(hypotheses),
        key=lambda item: (-float(item[1].confidence), item[0]),
    )
    return [str(item[1].root_cause_type) for item in ordered[:3]]


def _rank_state_payload(payload: object, *, predicted: str | None) -> list[str]:
    if not isinstance(payload, Mapping):
        return [predicted] if predicted else []
    hypotheses = payload.get("hypotheses", [])
    labels: list[str] = []
    if isinstance(hypotheses, Sequence):
        records = [item for item in hypotheses if isinstance(item, Mapping)]
        records.sort(key=lambda item: -float(item.get("confidence", 0)))
        labels.extend(
            str(item["root_cause_type"])
            for item in records
            if isinstance(item.get("root_cause_type"), str)
        )
    if predicted:
        labels = [predicted, *labels]
    return list(dict.fromkeys(labels))[:3]


def _events_from_trace(payload: Mapping[str, Any]) -> tuple[GuardrailEvent, ...]:
    raw: object = payload.get("guardrail_events")
    if raw is None and isinstance(payload.get("state"), Mapping):
        raw = payload["state"].get("guardrail_events")
    if not isinstance(raw, Sequence):
        return ()
    return tuple(
        GuardrailEvent.model_validate(item) for item in raw if isinstance(item, Mapping)
    )


def _output(
    *,
    variant: VariantName,
    completion_status: str,
    config: AblationConfig,
    latency_ms: float,
    ranked: Sequence[str] = (),
    usage: ModelUsage | None = None,
    tool_call_count: int | None = None,
    sql_call_count: int | None = None,
    events: Sequence[GuardrailEvent] = (),
    trace: Mapping[str, Any] | None = None,
    error: str | None = None,
) -> AblationExecutionOutput:
    event_payloads = [
        cast(dict[str, JsonValue], event.model_dump(mode="json")) for event in events
    ]
    trace_payload = dict(trace or {})
    trace_payload.setdefault("model_fingerprint", _model_fingerprint(config))
    trace_payload.setdefault("model_provider", config.model_provider)
    trace_payload.setdefault("model_name", config.model_name)
    if event_payloads:
        trace_payload.setdefault("guardrail_events", event_payloads)
    _assert_no_ground_truth_fields(trace_payload, forbidden_keys=_TRACE_GT_KEYS)
    return AblationExecutionOutput(
        variant=variant,
        completion_status=completion_status,
        ranked_root_causes=list(ranked)[:3],
        tool_call_count=(
            sum(
                1
                for event in event_payloads
                if event.get("event_type") == "preflight"
                and event.get("allowed") is True
            )
            if tool_call_count is None
            else tool_call_count
        ),
        sql_call_count=(
            sum(
                1
                for event in event_payloads
                if event.get("event_type") == "preflight"
                and event.get("allowed") is True
                and event.get("tool_name") == "sql_query"
            )
            if sql_call_count is None
            else sql_call_count
        ),
        model_usage=usage,
        known_cost=_cost_from_usage(usage, config),
        latency_ms=max(0.0, latency_ms),
        guardrail_events=event_payloads,
        trace_payload=cast(dict[str, JsonValue], trace_payload),
        error=error,
    )


def _metrics_rate(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 0.0


def _nearest_rank(values: Sequence[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(percentile * len(ordered)) - 1))
    return ordered[index]


def _sum_usage(results: Sequence[AblationCaseResult], field: str) -> int | None:
    usages = [result.model_usage for result in results]
    if not usages:
        return None
    values = [getattr(usage, field) for usage in usages if usage is not None]
    if len(values) != len(usages) or any(value is None for value in values):
        return None
    return sum(cast(int, value) for value in values)


def _trace_events(result: AblationCaseResult) -> list[Mapping[str, Any]]:
    payload = result.trace_payload
    raw = payload.get("guardrail_events") if isinstance(payload, Mapping) else None
    if isinstance(raw, Sequence):
        return [item for item in raw if isinstance(item, Mapping)]
    state = payload.get("state") if isinstance(payload, Mapping) else None
    if isinstance(state, Mapping) and isinstance(
        state.get("guardrail_events"), Sequence
    ):
        return [item for item in state["guardrail_events"] if isinstance(item, Mapping)]
    return []


def _trace_tool_results(result: AblationCaseResult) -> list[Mapping[str, Any]]:
    payload = result.trace_payload
    candidates: object = (
        payload.get("tool_trace") if isinstance(payload, Mapping) else None
    )
    if candidates is None and isinstance(payload.get("state"), Mapping):
        candidates = payload["state"].get("tool_trace")
    if isinstance(candidates, Sequence):
        return [item for item in candidates if isinstance(item, Mapping)]
    return []


def score_execution(
    output: AblationExecutionOutput | None,
    manifest: CaseManifest,
    *,
    status: RunStatus = "completed",
    error: str | None = None,
    latency_ms: float | None = None,
) -> AblationCaseResult:
    """Score one output without exposing Ground Truth to the adapter."""

    if output is None:
        output = AblationExecutionOutput(
            variant=cast(VariantName, "single_prompt"),
            completion_status="error" if status == "error" else "timed_out",
            latency_ms=latency_ms or 0,
            error=error,
        )
    adapter_status = output.completion_status.lower()
    effective_status: RunStatus = (
        "error"
        if status == "completed"
        and adapter_status in {"error", "tool_failed", "validation_failed"}
        else status
    )
    labels = list(output.ranked_root_causes)
    valid_labels: list[str] = []
    for label in labels:
        if label in CANONICAL_ROOT_CAUSES and label not in valid_labels:
            valid_labels.append(label)
    invalid_prediction = any(label not in CANONICAL_ROOT_CAUSES for label in labels)
    top1 = bool(
        effective_status == "completed"
        and labels
        and labels[0] == manifest.root_cause_type
    )
    top3 = bool(
        effective_status == "completed" and manifest.root_cause_type in valid_labels[:3]
    )
    events = _trace_events(
        AblationCaseResult(
            case_id=manifest.case_id,
            fault_id=manifest.fault_id,
            variant=output.variant,
            status=effective_status,
            completion_status=output.completion_status,
            ranked_root_causes=labels,
            expected_root_cause=manifest.root_cause_type,
            top1_correct=top1,
            top3_correct=top3,
            trace_payload=output.trace_payload,
        )
    )
    tool_results = _trace_tool_results(
        AblationCaseResult(
            case_id=manifest.case_id,
            fault_id=manifest.fault_id,
            variant=output.variant,
            status=effective_status,
            completion_status=output.completion_status,
            ranked_root_causes=labels,
            expected_root_cause=manifest.root_cause_type,
            top1_correct=top1,
            top3_correct=top3,
            trace_payload=output.trace_payload,
        )
    )
    preflight_events = [
        item for item in events if item.get("event_type") == "preflight"
    ]
    total_tool_attempts = len(preflight_events) or len(tool_results)
    invalid_sql = sum(
        1
        for item in preflight_events
        if item.get("tool_name") == "sql_query"
        and item.get("reason") in {"unsafe_sql", "invalid_tool_contract"}
    )
    invalid_sql += sum(
        1
        for item in tool_results
        if item.get("tool_name") == "sql_query"
        and item.get("success") is False
        and isinstance(item.get("error"), Mapping)
        and item["error"].get("type")
        in {"validation", "execution", "timeout", "tool_contract"}
    )
    unsafe = sum(
        1
        for item in preflight_events
        if not item.get("allowed", True)
        and item.get("reason") in _UNSAFE_GUARDRAIL_REASONS
    )
    duplicate = sum(
        1
        for item in preflight_events
        if not item.get("allowed", True) and item.get("reason") == "duplicate_tool_call"
    )
    blocked = sum(1 for item in preflight_events if not item.get("allowed", True))
    budget = output.completion_status == "budget_exceeded" or any(
        item.get("reason")
        in {
            "agent_round_budget_exceeded",
            "tool_call_budget_exceeded",
            "sql_call_budget_exceeded",
        }
        for item in preflight_events
    )
    return AblationCaseResult(
        case_id=manifest.case_id,
        fault_id=manifest.fault_id,
        variant=output.variant,
        status=effective_status,
        completion_status=output.completion_status,
        ranked_root_causes=labels,
        expected_root_cause=manifest.root_cause_type,
        top1_correct=top1,
        top3_correct=top3,
        invalid_prediction=invalid_prediction,
        tool_call_count=output.tool_call_count,
        sql_call_count=output.sql_call_count,
        total_tool_attempts=total_tool_attempts,
        invalid_sql_attempts=invalid_sql,
        unsafe_operation_attempts=unsafe,
        duplicate_operation_attempts=duplicate,
        blocked_calls=blocked,
        budget_exceeded=budget,
        model_usage=output.model_usage,
        known_cost=output.known_cost,
        latency_ms=latency_ms if latency_ms is not None else output.latency_ms,
        abstention=not labels,
        error=error or output.error,
        trace_payload=output.trace_payload,
    )


def compute_metrics(
    results: Sequence[AblationCaseResult],
    variant: VariantName,
) -> AblationMetrics:
    """Compute all metrics using all attempted pairs as accuracy denominator."""

    selected = [result for result in results if result.variant == variant]
    attempted = len(selected)
    latencies = [result.latency_ms for result in selected]
    tool_attempts = sum(result.total_tool_attempts for result in selected)
    sql_attempts = sum(
        result.sql_call_count + result.invalid_sql_attempts for result in selected
    )
    known_costs = [
        result.known_cost for result in selected if result.known_cost is not None
    ]
    input_tokens = _sum_usage(selected, "input_tokens")
    output_tokens = _sum_usage(selected, "output_tokens")
    total_tokens = _sum_usage(selected, "total_tokens")
    known_total = (
        sum(known_costs) if len(known_costs) == attempted and attempted else None
    )
    return AblationMetrics(
        variant=variant,
        attempted=attempted,
        completed=sum(result.status == "completed" for result in selected),
        errors=sum(result.status == "error" for result in selected),
        timeouts=sum(result.status == "timed_out" for result in selected),
        abstentions=sum(result.abstention for result in selected),
        invalid_predictions=sum(result.invalid_prediction for result in selected),
        top1_correct=sum(result.top1_correct for result in selected),
        top3_correct=sum(result.top3_correct for result in selected),
        top1_accuracy=_metrics_rate(
            sum(result.top1_correct for result in selected), attempted
        ),
        top3_accuracy=_metrics_rate(
            sum(result.top3_correct for result in selected), attempted
        ),
        total_tool_calls=sum(result.tool_call_count for result in selected),
        average_tool_calls=_metrics_rate(
            sum(result.tool_call_count for result in selected), attempted
        ),
        total_sql_calls=sum(result.sql_call_count for result in selected),
        average_sql_calls=_metrics_rate(
            sum(result.sql_call_count for result in selected), attempted
        ),
        total_tool_attempts=tool_attempts,
        total_sql_attempts=sql_attempts,
        invalid_sql_attempts=sum(result.invalid_sql_attempts for result in selected),
        invalid_sql_rate=_metrics_rate(
            sum(result.invalid_sql_attempts for result in selected),
            sum(
                result.sql_call_count + result.invalid_sql_attempts
                for result in selected
            ),
        ),
        unsafe_operation_attempts=sum(
            result.unsafe_operation_attempts for result in selected
        ),
        unsafe_operation_rate=_metrics_rate(
            sum(result.unsafe_operation_attempts for result in selected), tool_attempts
        ),
        duplicate_operation_attempts=sum(
            result.duplicate_operation_attempts for result in selected
        ),
        duplicate_operation_rate=_metrics_rate(
            sum(result.duplicate_operation_attempts for result in selected),
            tool_attempts,
        ),
        blocked_calls=sum(result.blocked_calls for result in selected),
        budget_exceeded=sum(result.budget_exceeded for result in selected),
        mean_latency_ms=_metrics_rate(sum(latencies), attempted),
        p50_latency_ms=_nearest_rank(latencies, 0.50),
        p95_latency_ms=_nearest_rank(latencies, 0.95),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
        known_cost_total=known_total,
        known_average_cost=(
            sum(known_costs) / len(known_costs) if known_costs else None
        ),
        known_cost_cases=len(known_costs),
    )


def _variant_adapter(
    config: AblationConfig,
    variant: VariantName,
    factory: ModelClientFactory | None,
) -> AblationVariantExecutor:
    adapter_type: type[AblationVariantExecutor] = {
        "single_prompt": SinglePromptAdapter,
        "react": ReActAdapter,
        "state_graph_no_validator": StateGraphNoValidatorAdapter,
        "full_harness": FullHarnessAdapter,
    }[variant]
    return adapter_type(config, model_client_factory=factory)


def _variant_process_worker(
    executor: AblationVariantExecutor,
    runtime_payload: dict[str, Any],
    result_queue: Any,
) -> None:
    try:
        runtime = AblationRuntimeInput.model_validate(runtime_payload)
        output = executor(runtime)
        result_queue.put({"ok": True, "output": output.model_dump(mode="json")})
    except BaseException as exc:  # noqa: BLE001 - process boundary reports all failures
        try:
            result_queue.put(
                {"ok": False, "error_type": type(exc).__name__, "error": str(exc)}
            )
        except (BrokenPipeError, EOFError, OSError):
            pass


def _terminate_process(process: Any) -> None:
    if process.is_alive():
        process.terminate()
        process.join(1.0)
    if process.is_alive():
        process.kill()
        process.join(1.0)


def _execute_thread_with_timeout(
    executor: AblationVariantExecutor,
    runtime: AblationRuntimeInput,
    timeout_seconds: float,
) -> AblationExecutionOutput:
    pool = ThreadPoolExecutor(max_workers=1)
    future = pool.submit(executor, runtime)
    try:
        output = future.result(timeout=timeout_seconds)
        return (
            output
            if isinstance(output, AblationExecutionOutput)
            else AblationExecutionOutput.model_validate(output)
        )
    except FutureTimeoutError as exc:
        future.cancel()
        raise TimeoutError(
            f"case execution exceeded {timeout_seconds:g} seconds"
        ) from exc
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


def execute_variant_with_timeout(
    executor: AblationVariantExecutor,
    runtime: AblationRuntimeInput,
    timeout_seconds: float,
) -> AblationExecutionOutput:
    """Use killable process isolation for production adapters when possible."""

    try:
        pickle.dumps(executor)
    except (pickle.PicklingError, TypeError, AttributeError):
        return _execute_thread_with_timeout(executor, runtime, timeout_seconds)
    context = mp.get_context("spawn")
    result_queue = context.Queue()
    process = context.Process(
        target=_variant_process_worker,
        args=(executor, runtime.model_dump(mode="json"), result_queue),
    )
    process.start()
    message: dict[str, Any] | None = None
    deadline = perf_counter() + timeout_seconds
    try:
        while message is None:
            remaining = deadline - perf_counter()
            if remaining <= 0:
                _terminate_process(process)
                raise TimeoutError(
                    f"case execution exceeded {timeout_seconds:g} seconds"
                )
            try:
                candidate = result_queue.get(timeout=min(0.05, remaining))
            except queue.Empty:
                if not process.is_alive():
                    raise RuntimeError(
                        f"variant worker exited without a result (exit code {process.exitcode})"
                    )
                continue
            if not isinstance(candidate, dict):
                raise TypeError("variant worker returned an invalid result envelope")
            message = candidate
        process.join(1.0)
        if process.is_alive():
            _terminate_process(process)
    finally:
        if process.is_alive():
            _terminate_process(process)
        result_queue.close()
        result_queue.join_thread()
    assert message is not None
    if not message.get("ok"):
        raise RuntimeError(
            f"{message.get('error_type', 'WorkerError')}: {message.get('error', '')}"
        )
    return AblationExecutionOutput.model_validate(message["output"])


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _run_dir(config: AblationConfig) -> Path:
    return (config.output_dir / config.run_id).resolve()


def _case_input_payload(
    *,
    manifest: CaseManifest,
    index: int,
    runtime_alert: Mapping[str, Any],
    db_paths: Mapping[str, Path],
    model_fingerprint: str,
) -> dict[str, Any]:
    payload = {
        "case_id": manifest.case_id,
        "case_index": index,
        "runtime_alert": dict(runtime_alert),
        "model_fingerprint": model_fingerprint,
        "database_sha256": {
            variant: _sha256(path) for variant, path in db_paths.items()
        },
        "database_paths": {variant: str(path) for variant, path in db_paths.items()},
    }
    _assert_no_ground_truth_fields(payload["runtime_alert"])
    return payload


def validate_fairness(
    case_inputs: Sequence[Mapping[str, Any]],
    results: Sequence[AblationCaseResult],
    *,
    model_fingerprint: str,
    expected_variants: Sequence[VariantName] = VARIANT_ORDER,
) -> FairnessValidation:
    """Validate equal inputs, complete pair matrix, and trace isolation."""

    expected_keys = {
        (str(case["case_id"]), variant)
        for case in case_inputs
        for variant in expected_variants
    }
    observed_keys = [(result.case_id, result.variant) for result in results]
    counts: dict[tuple[str, VariantName], int] = defaultdict(int)
    for key in observed_keys:
        counts[key] += 1
    missing = sorted(
        f"{case}/{variant}"
        for case, variant in expected_keys
        if not counts[(case, variant)]
    )
    duplicate_pairs = sum(max(0, count - 1) for count in counts.values())
    hash_map = {
        str(case["case_id"]): cast(dict[str, str], dict(case["database_sha256"]))
        for case in case_inputs
    }
    same_hash = all(len(set(hashes.values())) == 1 for hashes in hash_map.values())
    leakage = False
    for result in results:
        try:
            _assert_no_ground_truth_fields(
                result.trace_payload,
                forbidden_keys=_TRACE_GT_KEYS,
            )
        except ValueError:
            leakage = True
            break
    if not leakage:
        for case in case_inputs:
            try:
                _assert_no_ground_truth_fields(case.get("runtime_alert", {}))
            except ValueError:
                leakage = True
                break
    observed_model_fingerprints = {
        fingerprint
        for result in results
        if isinstance(result.trace_payload, Mapping)
        for fingerprint in [result.trace_payload.get("model_fingerprint")]
        if isinstance(fingerprint, str)
    }
    recorded_model_fingerprints = {
        fingerprint
        for case in case_inputs
        for fingerprint in [case.get("model_fingerprint")]
        if isinstance(fingerprint, str)
    }
    same_model = (
        bool(model_fingerprint)
        and recorded_model_fingerprints.issubset({model_fingerprint})
        and observed_model_fingerprints.issubset({model_fingerprint})
    )
    return FairnessValidation(
        case_count=len(case_inputs),
        variant_count=len(expected_variants),
        expected_pairs=len(expected_keys),
        attempted_pairs=len(results),
        complete_pair_matrix=not missing
        and duplicate_pairs == 0
        and len(results) == len(expected_keys),
        same_db_hash=same_hash,
        same_model_fingerprint=same_model,
        gt_runtime_leakage=leakage,
        duplicate_pairs=duplicate_pairs,
        missing_pairs=missing,
        model_fingerprint=model_fingerprint,
        db_hashes=hash_map,
    )


def _load_manifests(
    config: AblationConfig, cases_directory: Path
) -> list[CaseManifest]:
    if config.is_full_selection:
        manifests = load_case_manifests(cases_directory)
    else:
        manifests = [
            load_case_manifest(case_id, directory=cases_directory)
            for case_id in config.case_ids
        ]
    by_id = {manifest.case_id: manifest for manifest in manifests}
    selected = [by_id[case_id] for case_id in config.case_ids]
    return [validate_case_manifest(manifest) for manifest in selected]


class AblationRunner:
    """Materialize, execute, resume, score, and report one ablation run."""

    def __init__(
        self,
        config: AblationConfig,
        *,
        cases_directory: str | Path = Path("benchmark/cases"),
        model_client_factory: ModelClientFactory | None = None,
    ) -> None:
        self.config = config.model_copy(deep=True)
        self.cases_directory = Path(cases_directory)
        self.model_client_factory = model_client_factory
        self.run_dir = _run_dir(config)

    def run(self) -> AblationRun:
        blocker = full_run_blocker(
            self.config,
            model_client_factory=self.model_client_factory,
        )
        if blocker is not None:
            raise RuntimeError(f"FULL RUN = BLOCKED: {blocker}")
        manifests = _load_manifests(self.config, self.cases_directory)
        case_inputs = self._prepare_case_inputs(manifests)
        existing = self._load_existing_results()
        results = list(existing.values())
        for case_payload, manifest in zip(case_inputs, manifests, strict=True):
            for variant in VARIANT_ORDER:
                key = (manifest.case_id, variant)
                if key in existing:
                    continue
                database_path = Path(case_payload["database_paths"][variant])
                runtime = _runtime_input(
                    manifest,
                    database_path,
                    run_id=f"{self.config.run_id}-case-{case_payload['case_index']:05d}",
                )
                started = perf_counter()
                try:
                    adapter = _variant_adapter(
                        self.config,
                        variant,
                        self.model_client_factory,
                    )
                    output = execute_variant_with_timeout(
                        adapter,
                        runtime,
                        self.config.per_case_timeout_seconds,
                    )
                    result = score_execution(
                        output,
                        manifest,
                        latency_ms=(perf_counter() - started) * 1000,
                    )
                except TimeoutError as exc:
                    result = score_execution(
                        None,
                        manifest,
                        status="timed_out",
                        error=str(exc),
                        latency_ms=(perf_counter() - started) * 1000,
                    )
                    result = result.model_copy(
                        update={"variant": variant, "fault_id": manifest.fault_id}
                    )
                except Exception as exc:  # noqa: BLE001 - one pair must not erase a run
                    result = score_execution(
                        None,
                        manifest,
                        status="error",
                        error=f"{type(exc).__name__}: {exc}",
                        latency_ms=(perf_counter() - started) * 1000,
                    )
                    result = result.model_copy(
                        update={"variant": variant, "fault_id": manifest.fault_id}
                    )
                self._append_result(result)
                existing[key] = result
                results.append(result)
        fairness = validate_fairness(
            case_inputs,
            results,
            model_fingerprint=_model_fingerprint(self.config),
        )
        metrics = {
            variant: compute_metrics(results, variant) for variant in VARIANT_ORDER
        }
        recomputed = recompute_report(self.run_dir)
        if recomputed != metrics:
            raise RuntimeError(
                "raw JSONL metric recomputation does not match in-memory metrics"
            )
        self._write_summaries(case_inputs, results, fairness, metrics)
        return AblationRun(
            run_dir=self.run_dir, results=results, metrics=metrics, fairness=fairness
        )

    def _prepare_case_inputs(
        self, manifests: Sequence[CaseManifest]
    ) -> list[dict[str, Any]]:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        config_path = self.run_dir / "config.json"
        expected_config = {
            "config": self.config.public_dict(),
            "config_fingerprint": _config_fingerprint(self.config),
            "model_fingerprint": _model_fingerprint(self.config),
            "variants": list(VARIANT_ORDER),
        }
        if config_path.exists():
            stored = json.loads(config_path.read_text(encoding="utf-8"))
            mismatch = (
                stored.get("config_fingerprint")
                != expected_config["config_fingerprint"]
                or stored.get("model_fingerprint")
                != expected_config["model_fingerprint"]
                or stored.get("variants") != expected_config["variants"]
                or stored.get("config", {}).get("case_ids") != self.config.case_ids
            )
            if mismatch and not self.config.overwrite:
                raise ValueError(
                    "cannot resume ablation run: config/model/variant fingerprint mismatch"
                )
            if self.config.overwrite:
                # The target is the validated run directory for this run ID.
                shutil.rmtree(self.run_dir)
                self.run_dir.mkdir(parents=True, exist_ok=True)
                config_path.write_text(
                    json.dumps(expected_config, indent=2, sort_keys=True),
                    encoding="utf-8",
                )
            elif not self.config.resume:
                raise FileExistsError(
                    f"run already exists; set resume=true: {self.run_dir}"
                )
        else:
            config_path.write_text(
                json.dumps(expected_config, indent=2, sort_keys=True), encoding="utf-8"
            )
        input_path = self.run_dir / "case_inputs.jsonl"
        if input_path.exists() and self.config.resume:
            payloads = [
                json.loads(line)
                for line in input_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            if [payload["case_id"] for payload in payloads] == [
                manifest.case_id for manifest in manifests
            ] and all(
                set(payload.get("database_paths", {})) == set(VARIANT_ORDER)
                and set(payload.get("database_sha256", {})) == set(VARIANT_ORDER)
                and payload.get("model_fingerprint") == _model_fingerprint(self.config)
                and isinstance(payload.get("runtime_alert"), Mapping)
                and all(
                    Path(path).is_file()
                    and _sha256(Path(path)) == payload["database_sha256"][variant]
                    for variant, path in payload["database_paths"].items()
                )
                for payload in payloads
            ):
                for payload in payloads:
                    _assert_no_ground_truth_fields(payload["runtime_alert"])
                return payloads
        if input_path.exists() and not self.config.resume and not self.config.overwrite:
            raise FileExistsError(
                f"case inputs already exist; set resume=true: {input_path}"
            )
        if self.config.overwrite and input_path.exists():
            input_path.unlink()
        payloads = []
        for index, manifest in enumerate(manifests, start=1):
            result = materialize_case(manifest, directory=self.cases_directory)
            base_dir = self.run_dir / ".runtime" / f"case-{index:05d}" / "base"
            base_dir.mkdir(parents=True, exist_ok=True)
            base_db = base_dir / "datasherlock.duckdb"
            write_outputs(base_dir, result.tables)
            db_paths: dict[str, Path] = {}
            for variant in VARIANT_ORDER:
                variant_dir = self.run_dir / ".runtime" / f"case-{index:05d}" / variant
                variant_dir.mkdir(parents=True, exist_ok=True)
                variant_db = variant_dir / "datasherlock.duckdb"
                shutil.copy2(base_db, variant_db)
                db_paths[variant] = variant_db
            sanitized_alert = build_runtime_input(
                manifest,
                db_paths[VARIANT_ORDER[0]],
                run_id=f"{self.config.run_id}-case-{index:05d}",
            ).alert.model_dump(mode="json")
            payloads.append(
                _case_input_payload(
                    manifest=manifest,
                    index=index,
                    runtime_alert=sanitized_alert,
                    db_paths=db_paths,
                    model_fingerprint=_model_fingerprint(self.config),
                )
            )
        input_path.write_text(
            "".join(json.dumps(payload, sort_keys=True) + "\n" for payload in payloads),
            encoding="utf-8",
        )
        return payloads

    def _load_existing_results(
        self,
    ) -> dict[tuple[str, VariantName], AblationCaseResult]:
        existing: dict[tuple[str, VariantName], AblationCaseResult] = {}
        for variant in VARIANT_ORDER:
            path = self.run_dir / variant / "results.jsonl"
            if not path.exists():
                continue
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                result = AblationCaseResult.model_validate_json(line)
                key = (result.case_id, result.variant)
                if key in existing:
                    raise ValueError(f"duplicate persisted ablation pair: {key}")
                existing[key] = result
        return existing

    def _append_result(self, result: AblationCaseResult) -> None:
        directory = self.run_dir / result.variant
        directory.mkdir(parents=True, exist_ok=True)
        with (directory / "results.jsonl").open(
            "a", encoding="utf-8", newline="\n"
        ) as file:
            file.write(result.model_dump_json() + "\n")

    def _write_summaries(
        self,
        case_inputs: Sequence[Mapping[str, Any]],
        results: Sequence[AblationCaseResult],
        fairness: FairnessValidation,
        metrics: Mapping[VariantName, AblationMetrics],
    ) -> None:
        (self.run_dir / "fairness.json").write_text(
            fairness.model_dump_json(indent=2), encoding="utf-8"
        )
        for variant in VARIANT_ORDER:
            directory = self.run_dir / variant
            directory.mkdir(parents=True, exist_ok=True)
            (directory / "summary.json").write_text(
                metrics[variant].model_dump_json(indent=2), encoding="utf-8"
            )
        comparison = {
            "schema_version": 1,
            "run_kind": self.config.run_kind,
            "model_fingerprint": _model_fingerprint(self.config),
            "fairness": fairness.model_dump(mode="json"),
            "variants": {
                variant: metrics[variant].model_dump(mode="json")
                for variant in VARIANT_ORDER
            },
            "per_fault": _per_fault_metrics(results),
        }
        (self.run_dir / "comparison.json").write_text(
            json.dumps(comparison, indent=2, sort_keys=True), encoding="utf-8"
        )
        columns = [
            "Variant",
            "Top-1",
            "Top-3",
            "Invalid SQL rate",
            "Unsafe operation rate",
            "Duplicate operation rate",
            "Avg tool calls",
            "Avg SQL calls",
            "Mean latency",
            "P50 latency",
            "P95 latency",
            "Known avg cost",
            "Errors",
            "Timeouts",
            "Abstentions",
        ]
        with (self.run_dir / "comparison.csv").open(
            "w", encoding="utf-8", newline=""
        ) as file:
            writer = csv.DictWriter(file, fieldnames=columns)
            writer.writeheader()
            for variant in VARIANT_ORDER:
                metric = metrics[variant]
                writer.writerow(
                    {
                        "Variant": variant,
                        "Top-1": metric.top1_accuracy,
                        "Top-3": metric.top3_accuracy,
                        "Invalid SQL rate": metric.invalid_sql_rate,
                        "Unsafe operation rate": metric.unsafe_operation_rate,
                        "Duplicate operation rate": metric.duplicate_operation_rate,
                        "Avg tool calls": metric.average_tool_calls,
                        "Avg SQL calls": metric.average_sql_calls,
                        "Mean latency": metric.mean_latency_ms,
                        "P50 latency": metric.p50_latency_ms,
                        "P95 latency": metric.p95_latency_ms,
                        "Known avg cost": metric.known_average_cost,
                        "Errors": metric.errors,
                        "Timeouts": metric.timeouts,
                        "Abstentions": metric.abstentions,
                    }
                )
        (self.run_dir / "report.md").write_text(
            render_report(self.config, metrics, fairness, _per_fault_metrics(results)),
            encoding="utf-8",
        )


def _per_fault_metrics(
    results: Sequence[AblationCaseResult],
) -> dict[str, dict[str, dict[str, Any]]]:
    grouped: dict[str, dict[VariantName, list[AblationCaseResult]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for result in results:
        grouped[result.fault_id][result.variant].append(result)
    return {
        fault_id: {
            variant: compute_metrics(items, variant).model_dump(mode="json")
            for variant, items in variants.items()
        }
        for fault_id, variants in sorted(grouped.items())
    }


def render_report(
    config: AblationConfig,
    metrics: Mapping[VariantName, AblationMetrics],
    fairness: FairnessValidation,
    per_fault: Mapping[str, Mapping[str, Mapping[str, Any]]],
) -> str:
    """Render the stable report with definitions and measured comparisons."""

    lines = [
        "# Four-Architecture Ablation",
        "",
        (
            "This is a deterministic wiring smoke and must not be read as scientific "
            "ablation accuracy."
            if config.run_kind == "smoke"
            else "This report contains the configured full ablation run."
        ),
        "",
        f"Run ID: `{config.run_id}`",
        f"Model: `{config.model_provider}/{config.model_name}`",
        f"Attempted pairs: {fairness.attempted_pairs}/{fairness.expected_pairs}",
        "",
        "## Aggregate Metrics",
        "",
        "| Variant | Top-1 | Top-3 | Invalid SQL rate | Unsafe operation rate | Duplicate operation rate | Avg tool calls | Avg SQL calls | Mean latency | P50 latency | P95 latency | Known avg cost | Errors | Timeouts | Abstentions |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for variant in VARIANT_ORDER:
        metric = metrics[variant]
        values = [
            variant,
            f"{metric.top1_accuracy:.4f}",
            f"{metric.top3_accuracy:.4f}",
            f"{metric.invalid_sql_rate:.4f}",
            f"{metric.unsafe_operation_rate:.4f}",
            f"{metric.duplicate_operation_rate:.4f}",
            f"{metric.average_tool_calls:.3f}",
            f"{metric.average_sql_calls:.3f}",
            f"{metric.mean_latency_ms:.2f}",
            f"{metric.p50_latency_ms:.2f}",
            f"{metric.p95_latency_ms:.2f}",
            "unknown"
            if metric.known_average_cost is None
            else f"{metric.known_average_cost:.8f}",
            str(metric.errors),
            str(metric.timeouts),
            str(metric.abstentions),
        ]
        lines.append("| " + " | ".join(values) + " |")
    lines.extend(
        [
            "",
            "## Definitions",
            "",
            "Top-1 and Top-3 use all attempted case/variant pairs as the denominator; errors, timeouts, unresolved cases, and abstentions are incorrect.",
            "Top-3 uses the first three valid, deduplicated canonical labels. Unknown labels remain invalid and are never coerced.",
            "Invalid SQL rate is invalid SQL attempts divided by SQL attempts. SQL parser, read-only, validation, and execution failures count; a valid empty result does not.",
            "Unsafe operation rate and duplicate operation rate use actual GuardrailRuntime preflight reasons divided by total tool attempts.",
            "Known cost is null/unknown unless both input and output token counts and both configured rates are available; unknown is never reported as zero.",
            "",
            "## Component Comparisons",
            "",
            "Single Prompt to ReAct measures the cost and gain of iterative tool use.",
            "ReAct to State Graph No Validator measures state and hypothesis orchestration without an authoritative validator gate.",
            "State Graph No Validator to Full Harness measures the validator gate while reusing the production Harness adapter.",
            "Single Prompt to Full Harness reports the measured end-to-end tradeoff in accuracy, safety, tool use, latency, and cost.",
            "Interpretation is limited to measured differences; no unsupported causal claim is made.",
            "",
            "## Per-Fault Results",
            "",
            "| Fault | Variant | Top-1 | Top-3 | Attempted | Errors | Timeouts |",
            "|---|---|---:|---:|---:|---:|---:|",
        ]
    )
    for fault_id, variants in sorted(per_fault.items()):
        for variant in VARIANT_ORDER:
            metric = variants.get(variant)
            if metric is None:
                continue
            lines.append(
                f"| {fault_id} | {variant} | {metric['top1_accuracy']:.4f} | {metric['top3_accuracy']:.4f} | {metric['attempted']} | {metric['errors']} | {metric['timeouts']} |"
            )
    return "\n".join(lines) + "\n"


def recompute_report(run_dir: str | Path) -> dict[VariantName, AblationMetrics]:
    """Recompute metrics directly from raw JSONL artifacts."""

    root = Path(run_dir)
    results: list[AblationCaseResult] = []
    for variant in VARIANT_ORDER:
        path = root / variant / "results.jsonl"
        if path.exists():
            results.extend(
                AblationCaseResult.model_validate_json(line)
                for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            )
    return {variant: compute_metrics(results, variant) for variant in VARIANT_ORDER}


def load_ablation_config(
    path: str | Path,
    *,
    smoke: bool = False,
    run_id: str | None = None,
) -> AblationConfig:
    """Load YAML config without ever persisting or printing credentials."""

    payload = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    if not isinstance(payload, Mapping):
        raise TypeError("ablation config must be a YAML object")
    values = dict(payload)
    if smoke:
        values["case_ids"] = ["F01-001", "F03-001", "F06-001", "F11-001", "F12-001"]
        values["model_provider"] = "mock"
        values["model_name"] = "deterministic-smoke"
        values["run_kind"] = "smoke"
    if run_id is not None:
        values["run_id"] = run_id
    if "model_name" not in values:
        settings = ModelSettings()
        values["model_provider"] = values.get("model_provider", settings.model_provider)
        values["model_name"] = settings.openai_model or "mock-model"
        values["model_base_url"] = settings.openai_base_url
        values["model_timeout_seconds"] = settings.llm_timeout_seconds
        values["model_retries"] = settings.llm_max_retries
        values["model_retry_base_delay_seconds"] = settings.llm_retry_base_delay_seconds
    return AblationConfig.model_validate(values)


__all__ = [
    "CANONICAL_CASE_IDS",
    "CANONICAL_ROOT_CAUSES",
    "VARIANT_ORDER",
    "AblationCaseResult",
    "AblationConfig",
    "AblationExecutionOutput",
    "AblationMetrics",
    "AblationRun",
    "AblationRunner",
    "AblationRuntimeInput",
    "FairnessValidation",
    "FullHarnessAdapter",
    "RankedRootCauseResponse",
    "ReActAdapter",
    "SinglePromptAdapter",
    "StateGraphNoValidatorAdapter",
    "compute_metrics",
    "execute_variant_with_timeout",
    "full_run_blocker",
    "load_ablation_config",
    "recompute_report",
    "render_report",
    "score_execution",
    "serialize_runtime_input",
    "validate_fairness",
]
