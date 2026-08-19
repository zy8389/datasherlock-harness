"""Structured investigation planning for metric anomaly incidents.

The canonical Planner depends on the provider-neutral ``ModelClient``
interface. A callable generator remains supported only as a compatibility
adapter for the first offline Planner tests.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Mapping
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
    ModelCallResult,
    ModelClient,
    ModelClientError,
    ModelResponseError,
    is_model_client,
)

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


PLANNER_SYSTEM_PROMPT: Final[str] = """You are the DataSherlock investigation Planner.
Turn one structured anomaly alert and its canonical metric context into a bounded
investigation plan. You propose candidate explanations and evidence-producing
checks; you do not declare a root cause, write data, generate a repair, or
invent metric semantics that are absent from the supplied context.

Output only one valid JSON object. Do not wrap it in Markdown fences. The object
must match the supplied InvestigationPlan schema exactly: unknown fields are not
allowed. Produce 3 to 5 distinct hypotheses. Produce no more than 10 steps.
Every step must contain purpose, hypothesis_id, tool, arguments,
expected_evidence, and stop_condition. Use read-only SQL, metadata, data
quality, or pipeline inspection tools only. Each step must say what observation
would stop that branch or move the investigation to the next branch.
"""


def build_planner_prompt(
    alert_or_request: Alert | PlannerInput | Mapping[str, Any],
    metric_context: MetricContext | Mapping[str, Any] | None = None,
) -> str:
    """Build the complete, model-independent Prompt for one planning request.

    ``alert_or_request`` may be a :class:`PlannerInput`, an :class:`Alert`, or
    an alert mapping.  The second argument is required unless a complete
    PlannerInput is supplied.
    """

    request = _coerce_request(alert_or_request, metric_context)
    return f"{PLANNER_SYSTEM_PROMPT}\n\n{_build_planner_user_prompt(request)}"


def _build_planner_user_prompt(request: PlannerInput) -> str:
    """Build the user message separately from the system instructions."""

    applicable_faults = _fault_context(request.metric_context.metric_id)
    fault_text = json.dumps(applicable_faults, ensure_ascii=False, indent=2)
    input_text = json.dumps(request.model_dump(mode="json"), ensure_ascii=False, indent=2)
    schema_text = json.dumps(InvestigationPlan.model_json_schema(), ensure_ascii=False, indent=2)
    return (
        "Canonical input:\n"
        f"{input_text}\n\n"
        "Relevant canonical fault vocabulary (candidate context only; do not "
        "treat any item as confirmed):\n"
        f"{fault_text}\n\n"
        "Required JSON Schema:\n"
        f"{schema_text}\n\n"
        "Return the InvestigationPlan JSON object now."
    )


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
        max_retries: int = 2,
    ) -> None:
        if max_retries < 0:
            raise ValueError("max_retries must be non-negative")
        if not is_model_client(model_client) and not callable(model_client):
            raise TypeError("model_client must implement ModelClient or be callable")
        self.model_client = model_client if is_model_client(model_client) else None
        self._legacy_generator = model_client if callable(model_client) else None
        self.max_retries = max_retries
        self.last_model_result: ModelCallResult[InvestigationPlan] | None = None
        self.last_planner_repair_count = 0

    def plan(
        self,
        alert_or_request: Alert | PlannerInput | Mapping[str, Any],
        metric_context: MetricContext | Mapping[str, Any] | None = None,
    ) -> InvestigationPlan:
        """Synchronously run the canonical async Planner for compatibility."""

        if self._legacy_generator is not None:
            return self._plan_legacy(alert_or_request, metric_context)
        return asyncio.run(self.aplan(alert_or_request, metric_context))

    async def aplan(
        self,
        alert_or_request: Alert | PlannerInput | Mapping[str, Any],
        metric_context: MetricContext | Mapping[str, Any] | None = None,
    ) -> InvestigationPlan:
        """Asynchronously call ModelClient and perform Planner repair retries."""

        if self.model_client is None:
            raise TypeError("aplan requires a ModelClient, not the legacy callable adapter")

        request = _coerce_request(alert_or_request, metric_context)
        self.last_model_result = None
        self.last_planner_repair_count = 0
        user_prompt = _build_planner_user_prompt(request)
        repair_count = 0

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
                plan = InvestigationPlan.model_validate(result.parsed)
                if plan.incident_id != request.alert.incident_id:
                    raise ValueError(
                        "plan.incident_id does not match alert.incident_id "
                        f"({plan.incident_id!r} != {request.alert.incident_id!r})"
                    )
                self.last_planner_repair_count = repair_count
                self.last_model_result = result.model_copy(
                    update={
                        "planner_repair_count": repair_count,
                        "retry_count": result.retry_count + repair_count,
                    }
                )
                return plan
            except ModelResponseError:
                repair_count += 1
                continue
            except ValueError:
                repair_count += 1
                continue
            except ModelClientError:
                # Transport/API failures have already been retried and
                # normalized by ModelClient; they are not Planner repairs.
                self.last_planner_repair_count = repair_count
                return build_fallback_plan(request)

        self.last_planner_repair_count = repair_count
        return build_fallback_plan(request)

    def _plan_legacy(
        self,
        alert_or_request: Alert | PlannerInput | Mapping[str, Any],
        metric_context: MetricContext | Mapping[str, Any] | None,
    ) -> InvestigationPlan:
        """Preserve the first version's sync callable behavior for old tests."""

        request = _coerce_request(alert_or_request, metric_context)
        prompt = build_planner_prompt(request)
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
                return plan
            except Exception as exc:  # noqa: BLE001 - compatibility path mirrors v1 behavior
                _ = exc
        return build_fallback_plan(request)

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


def build_fallback_plan(request: PlannerInput) -> InvestigationPlan:
    """Build a bounded read-only plan without relying on model availability."""

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
    return InvestigationPlan(
        incident_id=request.alert.incident_id,
        hypotheses=hypotheses,
        steps=steps,
    )


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
            "expected_evidence": fault.expected_evidence,
            "expected_direction": fault.expected_direction,
        }
        for fault in catalog.faults
        if metric_id in fault.affected_metrics
    ]


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
    metric = request.metric_context.metric_id
    source_table = request.metric_context.source_tables[0] if request.metric_context.source_tables else "source_table"
    observed_at = request.alert.observed_at
    tool, arguments = _tool_for_root_cause(
        fault.root_cause_type,
        metric=metric,
        source_table=source_table,
        entity_column=request.metric_context.entity_column,
        observed_at=observed_at,
    )
    return InvestigationStep(
        step_id=f"S{index:02d}",
        purpose=f"为 {hypothesis.hypothesis_id} 检查 {fault.root_cause_type} 的可观察信号。",
        hypothesis_id=hypothesis.hypothesis_id,
        tool=tool,
        arguments=cast(JsonObject, arguments),
        expected_evidence=list(fault.expected_evidence),
        stop_condition=(
            f"若 {fault.root_cause_type} 的关键证据未出现，则降低 {hypothesis.hypothesis_id} "
            "的优先级并继续下一条假设；若出现，保留证据并进入独立交叉验证。"
        ),
    )


def _tool_for_root_cause(
    root_cause_type: str,
    *,
    metric: str,
    source_table: str,
    entity_column: str | None,
    observed_at: str,
) -> tuple[str, JsonObject]:
    date_args: JsonObject = {"metric": metric, "observed_at": observed_at}
    if root_cause_type == "missing_partition":
        return "get_partition_status", {"table": source_table, "date": observed_at}
    if root_cause_type == "data_delay":
        return "check_freshness", {"table": source_table, "expected_date": observed_at}
    if root_cause_type == "null_value_anomaly":
        return "check_null_rate", {
            "table": source_table,
            "column": entity_column or "user_id",
            "date": observed_at,
        }
    if root_cause_type == "duplicate_batch":
        return "check_duplicate_rate", {
            "table": source_table,
            "keys": ["event_id"],
            "date": observed_at,
        }
    if root_cause_type == "join_explosion":
        return "validate_join_cardinality", {
            "left_table": source_table,
            "right_table": "experiment_assignments",
            "keys": [entity_column or "user_id"],
        }
    if root_cause_type in {"field_drift", "schema_change"}:
        return "detect_schema_drift", {"table": source_table, "observed_at": observed_at}
    if root_cause_type == "unit_error":
        return "detect_distribution_drift", {
            "table": source_table,
            "column": "duration_seconds",
            "observed_at": observed_at,
        }
    if root_cause_type == "ab_split_anomaly":
        return "drill_down_by_dimension", {"metric": metric, "dimension": "variant"}
    if root_cause_type == "timezone_error":
        return "compare_time_windows", {
            **date_args,
            "dimension": "region",
            "baseline_period": "previous_7_days",
        }
    if root_cause_type == "join_filter":
        return "validate_join_cardinality", {
            "left_table": source_table,
            "right_table": "subscriptions",
            "keys": [entity_column or "user_id"],
        }
    if root_cause_type == "metric_definition_change":
        return "get_data_lineage", {"metric": metric}
    return "compare_time_windows", {**date_args, "baseline_period": "previous_7_days"}


__all__ = [
    "PLANNER_ALERT_EXAMPLES",
    "PLANNER_SYSTEM_PROMPT",
    "Alert",
    "Hypothesis",
    "InvestigationPlan",
    "InvestigationStep",
    "MetricContext",
    "Planner",
    "PlannerInput",
    "build_fallback_plan",
    "build_planner_prompt",
    "load_metric_context",
    "parse_investigation_plan",
]
