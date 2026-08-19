"""Structured investigation planning for metric anomaly incidents.

The canonical Planner depends on the provider-neutral ``ModelClient``
interface. A callable generator remains supported only as a compatibility
adapter for the first offline Planner tests.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Mapping
from datetime import date, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Final, TypeAlias, cast

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    field_validator,
    model_validator,
)

from config.faults import (
    DEFAULT_FAULT_CATALOG_PATH,
    FaultDefinition,
    load_fault_catalog,
)
from config.metrics import DEFAULT_METRICS_PATH, load_metrics_config
from llm.base import (
    ModelAuthenticationError,
    ModelCallResult,
    ModelClient,
    ModelClientError,
    ModelConfigurationError,
    ModelProviderError,
    ModelRateLimitError,
    ModelRequestError,
    ModelResponseError,
    ModelTimeoutError,
    ModelTransportError,
    is_model_client,
)
from tools.registry import (
    ToolArgumentsError,
    ToolRegistry,
    build_default_tool_registry,
)
from tools.sql_runner import SqlRunnerError, validate_readonly_sql

JsonObject: TypeAlias = dict[str, JsonValue]
PlanGenerator: TypeAlias = Callable[[str], str]

MIN_HYPOTHESES: Final = 3
MAX_HYPOTHESES: Final = 5
MAX_STEPS: Final = 10

# These operations are intentionally excluded from an investigation plan.
# Investigation is read-only; repair belongs to a later, approval-gated
# harness state.
UNSAFE_INVESTIGATION_TOOLS: Final[frozenset[str]] = frozenset(
    {
        "apply_patch_in_sandbox",
        "generate_sql_patch",
        "rerun_pipeline_in_sandbox",
        "validate_repaired_metric",
    }
)


class PlannerValidationError(ValueError):
    """Planner output is structurally valid but violates runtime constraints."""


class PlannerFallbackReason(StrEnum):
    """Stable reason codes recorded when the deterministic plan is used."""

    MODEL_TIMEOUT = "MODEL_TIMEOUT"
    MODEL_RATE_LIMIT = "MODEL_RATE_LIMIT"
    MODEL_TRANSPORT_ERROR = "MODEL_TRANSPORT_ERROR"
    MODEL_AUTHENTICATION_ERROR = "MODEL_AUTHENTICATION_ERROR"
    MODEL_REQUEST_ERROR = "MODEL_REQUEST_ERROR"
    MODEL_PROVIDER_ERROR = "MODEL_PROVIDER_ERROR"
    MODEL_CONFIGURATION_ERROR = "MODEL_CONFIGURATION_ERROR"
    MODEL_RESPONSE_INVALID = "MODEL_RESPONSE_INVALID"
    PLANNER_VALIDATION_FAILED = "PLANNER_VALIDATION_FAILED"
    RETRY_EXHAUSTED = "RETRY_EXHAUSTED"


class Alert(BaseModel):
    """The structured anomaly alert consumed by the Planner."""

    # Alert producers may attach routing labels or detector metadata.  The
    # required fields remain explicit while preserving that useful context.
    model_config = ConfigDict(extra="allow", str_strip_whitespace=True)

    incident_id: str = Field(min_length=1)
    metric: str = Field(min_length=1)
    observed_at: str = Field(min_length=1)
    expected_value: float
    observed_value: float
    change_rate: float
    severity: str = Field(min_length=1)

    @field_validator("observed_at", mode="before")
    @classmethod
    def normalize_observed_at(cls, value: object) -> str:
        """Serialize date-like alert timestamps consistently for tool args."""

        if hasattr(value, "isoformat"):
            return cast(str, value.isoformat())
        if not isinstance(value, str):
            raise TypeError("observed_at must be a string or date-like value")
        return value


class MetricContext(BaseModel):
    """Canonical metric semantics and the context available to the Planner."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    metric_id: str = Field(min_length=1)
    name: str | None = None
    description: str | None = None
    aggregation: str | None = None
    formula: str | None = None
    query: str | None = None
    unit: str | None = None
    source_tables: list[str] = Field(default_factory=list)
    time_column: str | None = None
    entity_column: str | None = None
    group_by: list[str] = Field(default_factory=list)
    filters: dict[str, JsonValue] = Field(default_factory=dict)
    dependencies: list[str] = Field(default_factory=list)
    dimensions: list[str] = Field(default_factory=list)
    related_faults: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def normalize_metric_id_aliases(cls, value: object) -> object:
        """Accept the ``id``/``metric`` names used by config and alert payloads."""

        if not isinstance(value, Mapping):
            return value
        payload = dict(value)
        if "metric_id" not in payload:
            for alias in ("id", "metric"):
                if alias in payload:
                    payload["metric_id"] = payload.pop(alias)
                    break
        return payload

    @field_validator(
        "name",
        "description",
        "aggregation",
        "formula",
        "query",
        "unit",
        "time_column",
        "entity_column",
        mode="before",
    )
    @classmethod
    def normalize_optional_strings(cls, value: object) -> object:
        """Treat empty optional strings as absent context."""

        if value == "":
            return None
        return value


class PlannerInput(BaseModel):
    """Planner request consisting of an alert and canonical metric context."""

    model_config = ConfigDict(extra="forbid")

    alert: Alert
    metric_context: MetricContext

    @model_validator(mode="before")
    @classmethod
    def normalize_metric_context_aliases(cls, value: object) -> object:
        """Accept ``id``/``metric`` aliases when adapting metrics.yaml data."""

        if not isinstance(value, Mapping):
            return value
        payload = dict(value)
        context = payload.get("metric_context")
        if isinstance(context, Mapping):
            normalized_context = dict(context)
            if "metric_id" not in normalized_context:
                for alias in ("id", "metric"):
                    if alias in normalized_context:
                        normalized_context["metric_id"] = normalized_context.pop(alias)
                        break
            payload["metric_context"] = normalized_context
        return payload

    @model_validator(mode="after")
    def validate_metric_match(self) -> PlannerInput:
        """Prevent a plan from being generated for the wrong metric context."""

        if self.alert.metric != self.metric_context.metric_id:
            raise ValueError(
                "alert.metric must match metric_context.metric_id "
                f"({self.alert.metric!r} != {self.metric_context.metric_id!r})"
            )
        return self


class Hypothesis(BaseModel):
    """One candidate root-cause explanation, not a final conclusion."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    hypothesis_id: str = Field(min_length=1)
    root_cause_type: str = Field(min_length=1)
    description: str = Field(min_length=1)
    initial_confidence: float = Field(ge=0, le=1)


class InvestigationStep(BaseModel):
    """One bounded, evidence-producing action in an investigation plan."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    step_id: str = Field(min_length=1)
    purpose: str = Field(min_length=1)
    hypothesis_id: str = Field(min_length=1)
    tool: str = Field(min_length=1)
    arguments: JsonObject
    expected_evidence: list[str] = Field(min_length=1)
    stop_condition: str = Field(min_length=1)

    @field_validator("tool")
    @classmethod
    def reject_repair_tools(cls, value: str) -> str:
        """Keep planning strictly inside the read-only investigation phase."""

        if value in UNSAFE_INVESTIGATION_TOOLS:
            raise ValueError(f"repair tool is not allowed in an investigation plan: {value}")
        return value


class InvestigationPlan(BaseModel):
    """Strict JSON output contract for the Planner."""

    model_config = ConfigDict(extra="forbid")

    incident_id: str = Field(min_length=1)
    hypotheses: list[Hypothesis] = Field(
        min_length=MIN_HYPOTHESES,
        max_length=MAX_HYPOTHESES,
    )
    steps: list[InvestigationStep] = Field(min_length=1, max_length=MAX_STEPS)

    @model_validator(mode="after")
    def validate_references_and_uniqueness(self) -> InvestigationPlan:
        """Ensure every step points to one of the proposed hypotheses."""

        hypothesis_ids = [hypothesis.hypothesis_id for hypothesis in self.hypotheses]
        if len(hypothesis_ids) != len(set(hypothesis_ids)):
            raise ValueError("hypothesis_id values must be unique")

        root_cause_types = [hypothesis.root_cause_type for hypothesis in self.hypotheses]
        if len(root_cause_types) != len(set(root_cause_types)):
            raise ValueError("root_cause_type values must be unique")

        known_ids = set(hypothesis_ids)
        unknown_ids = {
            step.hypothesis_id for step in self.steps if step.hypothesis_id not in known_ids
        }
        if unknown_ids:
            raise ValueError(
                "steps reference unknown hypotheses: " + ", ".join(sorted(unknown_ids))
            )
        step_ids = [step.step_id for step in self.steps]
        if len(step_ids) != len(set(step_ids)):
            raise ValueError("step_id values must be unique")
        return self


class PlannerRunResult(BaseModel):
    """Auditable outcome of one Planner run, including fallback metadata."""

    model_config = ConfigDict(extra="forbid", protected_namespaces=())

    plan: InvestigationPlan
    model_result: ModelCallResult[InvestigationPlan] | None = None
    fallback_used: bool = False
    fallback_reason: PlannerFallbackReason | None = None
    planner_repair_count: int = Field(default=0, ge=0)
    transport_retry_count: int = Field(default=0, ge=0)
    model_latency_ms: float | None = Field(default=None, ge=0)
    provider: str | None = None
    model: str | None = None


def validate_plan_tools(plan: InvestigationPlan, tool_registry: ToolRegistry) -> None:
    """Validate tool names, arguments, and read-only SQL semantics at runtime."""

    for step in plan.steps:
        if not tool_registry.contains(step.tool):
            raise PlannerValidationError(f"unknown tool in investigation plan: {step.tool}")
        try:
            tool_registry.validate_arguments(step.tool, step.arguments)
        except ToolArgumentsError as exc:
            raise PlannerValidationError(str(exc)) from exc

        definition = tool_registry.get(step.tool)
        if not definition.read_only:
            raise PlannerValidationError(
                f"non-read-only tool is not allowed in an investigation plan: {step.tool}"
            )
        if step.tool == "sql_query":
            sql = step.arguments.get("sql")
            try:
                validate_readonly_sql(cast(str, sql))
            except (SqlRunnerError, TypeError, ValueError) as exc:
                raise PlannerValidationError(
                    f"sql_query arguments contain unsafe SQL: {exc}"
                ) from exc


def validate_plan_semantics(
    plan: InvestigationPlan,
    request: PlannerInput,
    tool_registry: ToolRegistry,
) -> None:
    """Apply runtime constraints that cannot be expressed by Pydantic alone."""

    if plan.incident_id != request.alert.incident_id:
        raise PlannerValidationError(
            "plan.incident_id does not match alert.incident_id "
            f"({plan.incident_id!r} != {request.alert.incident_id!r})"
        )
    validate_root_cause_types(plan)
    validate_plan_tools(plan, tool_registry)


def validate_root_cause_types(plan: InvestigationPlan) -> None:
    """Reject hypotheses outside the canonical closed-set fault taxonomy."""

    allowed = set(_canonical_root_cause_types())
    invalid = sorted(
        {
            hypothesis.root_cause_type
            for hypothesis in plan.hypotheses
            if hypothesis.root_cause_type not in allowed
        }
    )
    if invalid:
        raise PlannerValidationError(
            "non-canonical root_cause_type value(s): " + ", ".join(invalid)
        )


PLANNER_SYSTEM_PROMPT: Final[str] = """You are the DataSherlock investigation Planner.
Turn one structured anomaly alert and its canonical metric context into a bounded
investigation plan. You propose candidate explanations and evidence-producing
checks; you do not declare a root cause, write data, generate a repair, or
invent metric semantics that are absent from the supplied context.

Output only one valid JSON object as the InvestigationPlan structured response.
Produce 3 to 5 distinct
hypotheses and no more than 10 steps. Every step must contain purpose,
hypothesis_id, tool, arguments, expected_evidence, and stop_condition. Use only
the tools listed under Available tools. Never invent a tool name. Investigation
must remain read-only: do not declare a final root cause, write data, or produce
repair actions. The JSON object must contain the fields "incident_id",
"hypotheses", and "steps". Each step must contain "purpose", "hypothesis_id",
"tool", "arguments", "expected_evidence", and "stop_condition". Each step
must say what observation would stop that branch or move the investigation to
the next branch.
"""


def build_planner_prompt(
    alert_or_request: Alert | PlannerInput | Mapping[str, Any],
    metric_context: MetricContext | Mapping[str, Any] | None = None,
    *,
    tool_registry: ToolRegistry | None = None,
) -> str:
    """Build the complete, model-independent Prompt for one planning request.

    ``alert_or_request`` may be a :class:`PlannerInput`, an :class:`Alert`, or
    an alert mapping.  The second argument is required unless a complete
    PlannerInput is supplied.
    """

    request = _coerce_request(alert_or_request, metric_context)
    registry = tool_registry if tool_registry is not None else build_default_tool_registry()
    return f"{PLANNER_SYSTEM_PROMPT}\n\n{_build_planner_user_prompt(request, registry)}"


def _build_planner_user_prompt(
    request: PlannerInput,
    tool_registry: ToolRegistry,
    *,
    include_schema: bool = False,
) -> str:
    """Build the user message, including only tools in the injected registry."""

    applicable_faults = _fault_context(request.metric_context.metric_id)
    fault_text = json.dumps(applicable_faults, ensure_ascii=False, indent=2)
    canonical_root_causes = json.dumps(
        _canonical_root_cause_types(), ensure_ascii=False
    )
    input_text = json.dumps(request.model_dump(mode="json"), ensure_ascii=False, indent=2)
    parts = [
        (
            "Canonical input:\n"
            f"{input_text}\n\n"
            "Relevant canonical fault vocabulary (candidate context only; do not "
            "treat any item as confirmed):\n"
            f"{fault_text}\n\n"
            "Canonical root_cause_type vocabulary (closed-set candidate labels; "
            "do not treat any item as confirmed):\n"
            f"{canonical_root_causes}\n\n"
        ),
        _available_tools_text(tool_registry),
        (
            "\nRules:\n"
            "- Only use tools listed above.\n"
            "- Every investigation step must reference one available tool.\n"
            "- Every SQL query must be read-only and will be checked by SQL Runner.\n"
            "- For root_cause_type, use only a canonical value from the supplied "
            "fault vocabulary; do not invent new values.\n"
            "- Do not declare a final root cause or generate repair actions.\n"
        ),
    ]
    if include_schema:
        schema_text = json.dumps(
            InvestigationPlan.model_json_schema(), ensure_ascii=False, indent=2
        )
        parts.append(f"\nLegacy JSON Schema:\n{schema_text}\n")
    parts.append("\nReturn the InvestigationPlan JSON object now.")
    return "".join(parts)


def _available_tools_text(tool_registry: ToolRegistry) -> str:
    """Render registry metadata for the Planner without maintaining a second list."""

    sections = ["Available tools:\n"]
    for definition in tool_registry.definitions():
        argument_schema = json.dumps(
            definition.argument_schema, ensure_ascii=False, indent=2
        )
        sections.append(
            f"Tool: {definition.name}\n"
            f"Description: {definition.description}\n"
            f"Arguments:\n{argument_schema}\n"
            f"Read-only: {str(definition.read_only).lower()}\n"
        )
    return "\n".join(sections)


def _build_legacy_prompt(request: PlannerInput, tool_registry: ToolRegistry) -> str:
    """Keep the full schema only for the old raw-string callable adapter."""

    return f"{PLANNER_SYSTEM_PROMPT}\n\n{_build_planner_user_prompt(request, tool_registry, include_schema=True)}"


# A few stable fixtures are kept close to the Planner contract so downstream
# integrations can smoke-test prompt and schema behavior without a live model.
PLANNER_ALERT_EXAMPLES: Final[tuple[JsonObject, ...]] = (
    {
        "incident_id": "INC-DAU-001",
        "metric": "daily_active_users",
        "observed_at": "2026-01-30",
        "expected_value": 10000,
        "observed_value": 7600,
        "change_rate": -0.24,
        "severity": "high",
    },
    {
        "incident_id": "INC-AI-001",
        "metric": "ai_task_count",
        "observed_at": "2026-01-30",
        "expected_value": 42000,
        "observed_value": 58800,
        "change_rate": 0.40,
        "severity": "high",
    },
    {
        "incident_id": "INC-CONV-001",
        "metric": "conversion_rate",
        "observed_at": "2026-01-30",
        "expected_value": 0.10,
        "observed_value": 0.065,
        "change_rate": -0.35,
        "severity": "medium",
    },
)


class Planner:
    """Generate and validate an investigation plan with safe recovery paths.

    The canonical integration accepts a :class:`ModelClient` and uses
    :meth:`aplan`. The synchronous :meth:`plan` method and callable-generator
    adapter remain for compatibility with the first Planner implementation and
    its offline tests.
    """

    def __init__(
        self,
        model_client: ModelClient | PlanGenerator,
        *,
        tool_registry: ToolRegistry | None = None,
        max_retries: int = 2,
    ) -> None:
        if max_retries < 0:
            raise ValueError("max_retries must be non-negative")
        if not is_model_client(model_client) and not callable(model_client):
            raise TypeError("model_client must implement ModelClient or be callable")
        self.model_client = model_client if is_model_client(model_client) else None
        self._legacy_generator = model_client if callable(model_client) else None
        self.tool_registry = (
            tool_registry if tool_registry is not None else build_default_tool_registry()
        )
        self.max_retries = max_retries
        self.last_model_result: ModelCallResult[InvestigationPlan] | None = None
        self.last_planner_repair_count = 0

    def plan(
        self,
        alert_or_request: Alert | PlannerInput | Mapping[str, Any],
        metric_context: MetricContext | Mapping[str, Any] | None = None,
    ) -> InvestigationPlan:
        """Synchronously run the canonical async Planner for compatibility."""

        return self.run(alert_or_request, metric_context).plan

    def run(
        self,
        alert_or_request: Alert | PlannerInput | Mapping[str, Any],
        metric_context: MetricContext | Mapping[str, Any] | None = None,
    ) -> PlannerRunResult:
        """Synchronously return a complete, auditable Planner run result."""

        if self._legacy_generator is not None:
            return self._run_legacy(alert_or_request, metric_context)
        return asyncio.run(self.arun(alert_or_request, metric_context))

    async def aplan(
        self,
        alert_or_request: Alert | PlannerInput | Mapping[str, Any],
        metric_context: MetricContext | Mapping[str, Any] | None = None,
    ) -> InvestigationPlan:
        """Compatibility wrapper returning only the generated investigation plan."""

        return (await self.arun(alert_or_request, metric_context)).plan

    async def arun(
        self,
        alert_or_request: Alert | PlannerInput | Mapping[str, Any],
        metric_context: MetricContext | Mapping[str, Any] | None = None,
    ) -> PlannerRunResult:
        """Call ModelClient and return plan plus fallback and retry audit data."""

        if self.model_client is None:
            raise TypeError("arun requires a ModelClient, not the legacy callable adapter")

        request = _coerce_request(alert_or_request, metric_context)
        self.last_model_result = None
        self.last_planner_repair_count = 0
        user_prompt = _build_planner_user_prompt(request, self.tool_registry)
        repair_count = 0
        transport_retry_count = 0
        last_repair_reason: PlannerFallbackReason | None = None
        last_provider: str | None = None
        last_model: str | None = None
        last_latency_ms: float | None = None

        for attempt in range(self.max_retries + 1):
            attempt_prompt = user_prompt
            if attempt:
                attempt_prompt += (
                    "\n\nRetry this response. The previous response was not accepted "
                    "as valid InvestigationPlan JSON. Return only corrected JSON."
                )
            try:
                result = await self.model_client.generate_structured(
                    system_prompt=PLANNER_SYSTEM_PROMPT,
                    user_prompt=attempt_prompt,
                    response_model=InvestigationPlan,
                )
                transport_retry_count += result.transport_retry_count
                last_provider = result.provider
                last_model = result.model
                last_latency_ms = result.latency_ms
                plan = InvestigationPlan.model_validate(result.parsed)
                validate_plan_semantics(plan, request, self.tool_registry)
                self.last_planner_repair_count = repair_count
                annotated_result = ModelCallResult[InvestigationPlan].model_validate(
                    result.model_dump(mode="python")
                ).model_copy(
                    update={
                        "parsed": plan,
                        "transport_retry_count": transport_retry_count,
                        "planner_repair_count": repair_count,
                        "retry_count": transport_retry_count + repair_count,
                    }
                )
                self.last_model_result = annotated_result
                return PlannerRunResult(
                    plan=plan,
                    model_result=annotated_result,
                    planner_repair_count=repair_count,
                    transport_retry_count=transport_retry_count,
                    model_latency_ms=annotated_result.latency_ms,
                    provider=annotated_result.provider,
                    model=annotated_result.model,
                )
            except ModelResponseError as exc:
                transport_retry_count += exc.transport_retry_count
                last_provider = exc.provider or last_provider
                last_model = exc.model or last_model
                if exc.latency_ms is not None:
                    last_latency_ms = exc.latency_ms
                last_repair_reason = PlannerFallbackReason.MODEL_RESPONSE_INVALID
                repair_count += 1
                continue
            except PlannerValidationError:
                last_repair_reason = PlannerFallbackReason.PLANNER_VALIDATION_FAILED
                repair_count += 1
                continue
            except ValueError:
                last_repair_reason = PlannerFallbackReason.MODEL_RESPONSE_INVALID
                repair_count += 1
                continue
            except ModelTimeoutError as exc:
                return self._fallback_result(
                    request,
                    PlannerFallbackReason.MODEL_TIMEOUT,
                    repair_count,
                    error=exc,
                    transport_retry_count=transport_retry_count,
                    provider=last_provider,
                    model=last_model,
                    latency_ms=last_latency_ms,
                )
            except ModelRateLimitError as exc:
                return self._fallback_result(
                    request,
                    PlannerFallbackReason.MODEL_RATE_LIMIT,
                    repair_count,
                    error=exc,
                    transport_retry_count=transport_retry_count,
                    provider=last_provider,
                    model=last_model,
                    latency_ms=last_latency_ms,
                )
            except ModelAuthenticationError as exc:
                return self._fallback_result(
                    request,
                    PlannerFallbackReason.MODEL_AUTHENTICATION_ERROR,
                    repair_count,
                    error=exc,
                    transport_retry_count=transport_retry_count,
                    provider=last_provider,
                    model=last_model,
                    latency_ms=last_latency_ms,
                )
            except ModelRequestError as exc:
                return self._fallback_result(
                    request,
                    PlannerFallbackReason.MODEL_REQUEST_ERROR,
                    repair_count,
                    error=exc,
                    transport_retry_count=transport_retry_count,
                    provider=last_provider,
                    model=last_model,
                    latency_ms=last_latency_ms,
                )
            except ModelProviderError as exc:
                return self._fallback_result(
                    request,
                    PlannerFallbackReason.MODEL_PROVIDER_ERROR,
                    repair_count,
                    error=exc,
                    transport_retry_count=transport_retry_count,
                    provider=last_provider,
                    model=last_model,
                    latency_ms=last_latency_ms,
                )
            except ModelConfigurationError as exc:
                return self._fallback_result(
                    request,
                    PlannerFallbackReason.MODEL_CONFIGURATION_ERROR,
                    repair_count,
                    error=exc,
                    transport_retry_count=transport_retry_count,
                    provider=last_provider,
                    model=last_model,
                    latency_ms=last_latency_ms,
                )
            except ModelTransportError as exc:
                return self._fallback_result(
                    request,
                    PlannerFallbackReason.MODEL_TRANSPORT_ERROR,
                    repair_count,
                    error=exc,
                    transport_retry_count=transport_retry_count,
                    provider=last_provider,
                    model=last_model,
                    latency_ms=last_latency_ms,
                )
            except ModelClientError as exc:
                return self._fallback_result(
                    request,
                    PlannerFallbackReason.MODEL_TRANSPORT_ERROR,
                    repair_count,
                    error=exc,
                    transport_retry_count=transport_retry_count,
                    provider=last_provider,
                    model=last_model,
                    latency_ms=last_latency_ms,
                )

        reason = last_repair_reason or PlannerFallbackReason.MODEL_RESPONSE_INVALID
        if repair_count > 1 and reason == PlannerFallbackReason.MODEL_RESPONSE_INVALID:
            reason = PlannerFallbackReason.RETRY_EXHAUSTED
        return self._fallback_result(
            request,
            reason,
            repair_count,
            transport_retry_count=transport_retry_count,
            provider=last_provider,
            model=last_model,
            latency_ms=last_latency_ms,
        )

    def _fallback_result(
        self,
        request: PlannerInput,
        reason: PlannerFallbackReason,
        repair_count: int,
        *,
        error: ModelClientError | None = None,
        transport_retry_count: int = 0,
        provider: str | None = None,
        model: str | None = None,
        latency_ms: float | None = None,
    ) -> PlannerRunResult:
        """Build and record one explicitly audited deterministic fallback."""

        plan = build_fallback_plan(request, tool_registry=self.tool_registry)
        self.last_planner_repair_count = repair_count
        self.last_model_result = None
        error_retry_count = error.transport_retry_count if error is not None else 0
        error_provider = error.provider if error is not None else None
        error_model = error.model if error is not None else None
        error_latency_ms = error.latency_ms if error is not None else None
        return PlannerRunResult(
            plan=plan,
            fallback_used=True,
            fallback_reason=reason,
            planner_repair_count=repair_count,
            transport_retry_count=max(transport_retry_count, error_retry_count),
            model_latency_ms=(
                latency_ms if latency_ms is not None else error_latency_ms
            ),
            provider=provider or error_provider,
            model=model or error_model,
        )

    def _plan_legacy(
        self,
        alert_or_request: Alert | PlannerInput | Mapping[str, Any],
        metric_context: MetricContext | Mapping[str, Any] | None,
    ) -> InvestigationPlan:
        """Preserve the first version's sync callable behavior for old tests."""

        return self._run_legacy(alert_or_request, metric_context).plan

    def _run_legacy(
        self,
        alert_or_request: Alert | PlannerInput | Mapping[str, Any],
        metric_context: MetricContext | Mapping[str, Any] | None,
    ) -> PlannerRunResult:
        """Run the raw callable adapter while applying current tool semantics."""

        request = _coerce_request(alert_or_request, metric_context)
        prompt = _build_legacy_prompt(request, self.tool_registry)
        repair_count = 0
        for attempt in range(self.max_retries + 1):
            attempt_prompt = prompt
            if attempt:
                attempt_prompt += (
                    "\n\nRetry this response. The previous response was not accepted "
                    "as valid InvestigationPlan JSON. Return only corrected JSON."
                )
            try:
                raw_output = self._legacy_generator(attempt_prompt)  # type: ignore[misc]
                plan = parse_investigation_plan(raw_output)
                if plan.incident_id != request.alert.incident_id:
                    raise ValueError(
                        "plan.incident_id does not match alert.incident_id "
                        f"({plan.incident_id!r} != {request.alert.incident_id!r})"
                    )
                validate_plan_semantics(plan, request, self.tool_registry)
                self.last_model_result = None
                self.last_planner_repair_count = repair_count
                return PlannerRunResult(
                    plan=plan,
                    planner_repair_count=repair_count,
                )
            except Exception as exc:  # noqa: BLE001 - compatibility path mirrors v1 behavior
                _ = exc
                repair_count += 1
        self.last_model_result = None
        self.last_planner_repair_count = repair_count
        return PlannerRunResult(
            plan=build_fallback_plan(request, tool_registry=self.tool_registry),
            fallback_used=True,
            fallback_reason=PlannerFallbackReason.RETRY_EXHAUSTED,
            planner_repair_count=repair_count,
        )

    # Explicit aliases for callers that prefer a verb matching the output.
    generate_plan = plan
    async_generate_plan = aplan


def parse_investigation_plan(raw_output: str | Mapping[str, Any]) -> InvestigationPlan:
    """Parse exactly one JSON object and validate it against the output schema."""

    if isinstance(raw_output, Mapping):
        payload: object = dict(raw_output)
    elif isinstance(raw_output, str):
        try:
            payload = json.loads(raw_output)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Planner output is not valid JSON: {exc.msg}") from exc
    else:
        raise TypeError("Planner output must be a JSON string or object mapping")

    if not isinstance(payload, dict):
        raise TypeError("Planner output must be one JSON object")
    return InvestigationPlan.model_validate(payload)


def load_metric_context(
    metric_id: str,
    path: str | Path = DEFAULT_METRICS_PATH,
) -> MetricContext:
    """Load one metric's canonical semantics into Planner input context."""

    config = load_metrics_config(path)
    try:
        definition = next(metric for metric in config.metrics if metric.id == metric_id)
    except StopIteration as exc:
        raise KeyError(f"unknown metric: {metric_id}") from exc

    source_tables = list(definition.source_tables)
    if definition.source_table and definition.source_table not in source_tables:
        source_tables.insert(0, definition.source_table)
    return MetricContext(
        metric_id=definition.id,
        name=definition.name,
        description=definition.description,
        aggregation=definition.aggregation,
        formula=definition.formula,
        query=definition.query,
        unit=definition.unit,
        source_tables=source_tables,
        time_column=definition.time_column,
        entity_column=definition.entity_column,
        group_by=definition.group_by,
        filters=cast(dict[str, JsonValue], definition.filters),
        dependencies=definition.dependencies,
        dimensions=definition.group_by,
    )


def build_fallback_plan(
    request: PlannerInput,
    *,
    tool_registry: ToolRegistry | None = None,
) -> InvestigationPlan:
    """Build a bounded deterministic plan using only registered read-only tools."""

    registry = tool_registry if tool_registry is not None else build_default_tool_registry()
    faults = _select_fallback_faults(request)
    hypotheses = [
        Hypothesis(
            hypothesis_id=f"H{index:02d}",
            root_cause_type=fault.root_cause_type,
            description=(
                f"{request.metric_context.metric_id} 的异常可能与 {fault.root_cause_type} "
                "有关，当前仅作为待验证候选。"
            ),
            initial_confidence=max(0.15, 0.45 - (index - 1) * 0.10),
        )
        for index, fault in enumerate(faults, start=1)
    ]

    steps = [
        _step_for_fault(request, hypothesis, fault, index)
        for index, (hypothesis, fault) in enumerate(zip(hypotheses, faults), start=1)
    ]
    plan = InvestigationPlan(
        incident_id=request.alert.incident_id,
        hypotheses=hypotheses,
        steps=steps,
    )
    validate_plan_tools(plan, registry)
    return plan


def _coerce_request(
    alert_or_request: Alert | PlannerInput | Mapping[str, Any],
    metric_context: MetricContext | Mapping[str, Any] | None,
) -> PlannerInput:
    if isinstance(alert_or_request, PlannerInput):
        if metric_context is not None:
            raise ValueError("metric_context must be omitted when passing PlannerInput")
        return alert_or_request
    if (
        metric_context is None
        and isinstance(alert_or_request, Mapping)
        and "alert" in alert_or_request
        and "metric_context" in alert_or_request
    ):
        return PlannerInput.model_validate(alert_or_request)
    if metric_context is None:
        raise ValueError("metric_context is required when passing an alert")
    alert = alert_or_request if isinstance(alert_or_request, Alert) else Alert.model_validate(alert_or_request)
    context = (
        metric_context
        if isinstance(metric_context, MetricContext)
        else MetricContext.model_validate(metric_context)
    )
    return PlannerInput(alert=alert, metric_context=context)


def _fault_context(metric_id: str) -> list[JsonObject]:
    try:
        catalog = load_fault_catalog(DEFAULT_FAULT_CATALOG_PATH)
    except (OSError, ValueError):
        return []
    return [
        {
            "fault_id": fault.id,
            "root_cause_type": fault.root_cause_type,
            "affected_assets": fault.affected_assets,
        }
        for fault in catalog.faults
        if metric_id in fault.affected_metrics
    ]


def _canonical_root_cause_types() -> tuple[str, ...]:
    """Load the complete closed-set taxonomy without duplicating its labels."""

    try:
        catalog = load_fault_catalog(DEFAULT_FAULT_CATALOG_PATH)
    except (OSError, ValueError) as exc:
        raise PlannerValidationError(
            f"could not load canonical fault catalog: {exc}"
        ) from exc
    return tuple(fault.root_cause_type for fault in catalog.faults)


def _select_fallback_faults(request: PlannerInput) -> list[FaultDefinition]:
    """Prioritize canonical fault vocabulary while always returning 3 candidates."""

    try:
        catalog_faults = list(load_fault_catalog(DEFAULT_FAULT_CATALOG_PATH).faults)
    except (OSError, ValueError):
        catalog_faults = []

    by_root_cause = {fault.root_cause_type: fault for fault in catalog_faults}
    applicable = [
        fault
        for fault in catalog_faults
        if request.metric_context.metric_id in fault.affected_metrics
    ]
    applicable_roots = {fault.root_cause_type for fault in applicable}
    descending = request.alert.change_rate >= 0
    preferred = (
        [
            "duplicate_batch",
            "join_explosion",
            "unit_error",
            "ab_split_anomaly",
            "field_drift",
            "schema_change",
            "data_delay",
        ]
        if descending
        else [
            "missing_partition",
            "data_delay",
            "null_value_anomaly",
            "join_filter",
            "field_drift",
            "schema_change",
            "metric_definition_change",
            "timezone_error",
        ]
    )

    selected: list[FaultDefinition] = []
    for root_cause in preferred:
        fault = by_root_cause.get(root_cause)
        if (
            fault is not None
            and fault.root_cause_type in applicable_roots
            and fault not in selected
        ):
            selected.append(fault)
    for fault in applicable + catalog_faults:
        if fault not in selected:
            selected.append(fault)
        if len(selected) >= MAX_HYPOTHESES:
            break

    # The catalog is expected to be complete, but keep the fallback contract
    # valid if a deployment supplies a partial catalog.
    if len(selected) < MIN_HYPOTHESES:
        for root_cause in ("data_delay", "schema_change", "metric_definition_change"):
            fault = by_root_cause.get(root_cause)
            if fault is not None and fault not in selected:
                selected.append(fault)
            if len(selected) >= MIN_HYPOTHESES:
                break
    if len(selected) < MIN_HYPOTHESES:
        raise RuntimeError("fault catalog does not provide three fallback hypotheses")
    return selected[:MAX_HYPOTHESES]


def _step_for_fault(
    request: PlannerInput,
    hypothesis: Hypothesis,
    fault: FaultDefinition,
    index: int,
) -> InvestigationStep:
    sql = _sql_for_root_cause(
        fault.root_cause_type,
        metric=request.metric_context.metric_id,
        entity_column=request.metric_context.entity_column,
        observed_at=request.alert.observed_at,
    )
    return InvestigationStep(
        step_id=f"S{index:02d}",
        purpose=f"为 {hypothesis.hypothesis_id} 检查 {fault.root_cause_type} 的可观察信号。",
        hypothesis_id=hypothesis.hypothesis_id,
        tool="sql_query",
        arguments={"sql": sql},
        expected_evidence=list(fault.expected_evidence),
        stop_condition=(
            f"若 {fault.root_cause_type} 的关键证据未出现，则降低 {hypothesis.hypothesis_id} "
            "的优先级并继续下一条假设；若出现，保留证据并进入独立交叉验证。"
        ),
    )


def _sql_for_root_cause(
    root_cause_type: str,
    *,
    metric: str,
    entity_column: str | None,
    observed_at: str,
) -> str:
    """Build deterministic read-only SQL for the canonical operational tables."""

    date_expression = _date_expression(observed_at)
    metric_literal = _sql_literal(metric)
    if root_cause_type == "missing_partition":
        date_prefix = _sql_literal(_date_text(observed_at) + "/")
        return (
            "SELECT table_name, partition_value, row_count, updated_at, status, source_job_id "
            "FROM partition_metadata "
            f"WHERE table_name = 'events' AND partition_value LIKE {date_prefix} || '%' "
            "ORDER BY partition_value, updated_at DESC"
        )
    if root_cause_type == "data_delay":
        date_literal = _sql_literal(_date_text(observed_at))
        return (
            "SELECT target_table, target_partition, status, started_at, finished_at, "
            "error_type, error_message FROM pipeline_runs "
            f"WHERE target_table = 'events' AND target_partition = {date_literal} "
            "ORDER BY started_at DESC"
        )
    if root_cause_type == "null_value_anomaly":
        column = entity_column if entity_column in {"user_id", "event_id"} else "user_id"
        return (
            "SELECT COUNT(*) AS total_rows, "
            f"SUM(CASE WHEN {column} IS NULL THEN 1 ELSE 0 END) AS null_rows "
            "FROM events "
            f"WHERE CAST(event_time AS DATE) = {date_expression}"
        )
    if root_cause_type == "duplicate_batch":
        return (
            "SELECT COUNT(*) AS total_rows, COUNT(DISTINCT event_id) AS distinct_event_ids, "
            "COUNT(DISTINCT batch_id) AS distinct_batch_ids "
            "FROM events "
            f"WHERE CAST(event_time AS DATE) = {date_expression}"
        )
    if root_cause_type == "join_explosion":
        return (
            "SELECT COUNT(*) AS joined_rows, COUNT(DISTINCT e.event_id) AS distinct_event_ids "
            "FROM events AS e INNER JOIN experiment_assignments AS a "
            "ON e.user_id = a.user_id "
            f"WHERE CAST(e.event_time AS DATE) = {date_expression}"
        )
    if root_cause_type == "field_drift":
        return (
            "SELECT event_name, COUNT(*) AS event_count FROM events "
            f"WHERE CAST(event_time AS DATE) = {date_expression} "
            "GROUP BY event_name ORDER BY event_count DESC"
        )
    if root_cause_type == "schema_change":
        return (
            "SELECT table_name, version, schema_json, effective_at FROM schema_snapshots "
            "WHERE table_name = 'events' ORDER BY effective_at DESC, version DESC"
        )
    if root_cause_type == "unit_error":
        return (
            "SELECT COUNT(*) AS total_rows, AVG(duration_seconds) AS average_duration, "
            "MIN(duration_seconds) AS minimum_duration, MAX(duration_seconds) AS maximum_duration "
            "FROM events "
            f"WHERE CAST(event_time AS DATE) = {date_expression}"
        )
    if root_cause_type == "ab_split_anomaly":
        return (
            "SELECT variant, COUNT(*) AS users FROM experiment_assignments "
            "GROUP BY variant ORDER BY variant"
        )
    if root_cause_type == "timezone_error":
        return (
            "SELECT u.region, EXTRACT(HOUR FROM e.event_time) AS event_hour, "
            "COUNT(*) AS event_count FROM events AS e "
            "INNER JOIN users AS u ON e.user_id = u.user_id "
            f"WHERE CAST(e.event_time AS DATE) = {date_expression} "
            "GROUP BY u.region, event_hour ORDER BY u.region, event_hour"
        )
    if root_cause_type == "join_filter":
        return (
            "SELECT COUNT(DISTINCT e.user_id) AS event_users, "
            "COUNT(DISTINCT s.user_id) AS subscribed_users FROM events AS e "
            "LEFT JOIN subscriptions AS s ON e.user_id = s.user_id "
            f"WHERE CAST(e.event_time AS DATE) = {date_expression}"
        )
    if root_cause_type == "metric_definition_change":
        return (
            "SELECT metric_id, version, definition_hash, query, effective_at "
            "FROM metric_versions "
            f"WHERE metric_id = {metric_literal} ORDER BY effective_at DESC, version DESC"
        )
    return (
        "SELECT metric_date, daily_active_users, new_users, paid_users, ai_task_count, "
        "average_session_duration, conversion_rate FROM daily_metrics "
        f"WHERE metric_date = {date_expression}"
    )


def _date_text(value: str) -> str:
    """Normalize date-like alert timestamps for deterministic SQL literals."""

    candidate = value.strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(candidate).date().isoformat()
    except ValueError:
        try:
            return date.fromisoformat(candidate).isoformat()
        except ValueError:
            return value.strip()


def _date_expression(value: str) -> str:
    normalized = _date_text(value)
    try:
        date.fromisoformat(normalized)
    except ValueError:
        return f"TRY_CAST({_sql_literal(normalized)} AS DATE)"
    return f"DATE '{normalized}'"


def _sql_literal(value: str) -> str:
    """Quote deterministic string values before embedding them in fallback SQL."""

    return "'" + value.replace("'", "''") + "'"


__all__ = [
    "PLANNER_ALERT_EXAMPLES",
    "PLANNER_SYSTEM_PROMPT",
    "Alert",
    "Hypothesis",
    "InvestigationPlan",
    "InvestigationStep",
    "MetricContext",
    "Planner",
    "PlannerFallbackReason",
    "PlannerInput",
    "PlannerRunResult",
    "PlannerValidationError",
    "build_fallback_plan",
    "build_planner_prompt",
    "load_metric_context",
    "parse_investigation_plan",
    "validate_plan_semantics",
    "validate_plan_tools",
    "validate_root_cause_types",
]
