"""Interpret runtime tool observations as hypothesis evidence.

Tool execution and root-cause validation are deliberately separate concerns.
This module is the narrow adapter between them: it looks at the structured
result returned by a tool, applies deterministic observation rules, and emits
independent decisions for each evidence reference.

In particular, a successful SQL call is not evidence by itself. SQL evidence
is created only when the returned values match a concrete diagnostic rule and
the observation is bound to the current incident scope.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from numbers import Real
from typing import Any, cast

import sqlglot
from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator
from sqlglot import exp
from sqlglot.errors import SqlglotError

from agents.planner import InvestigationStep
from config.faults import EvidenceSourceType
from harness.hypothesis import EvidenceReference, HypothesisState
from tools.executor import ToolExecutionResult
from validators.sql_result import SqlResultValidation


class EvidencePolarity(StrEnum):
    """How one observed result relates to the active hypothesis."""

    SUPPORTS = "supports"
    CONTRADICTS = "contradicts"
    NEUTRAL = "neutral"


class IncidentEvidenceContext(BaseModel):
    """Runtime incident scope used to bind evidence to the active alert."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    incident_id: str = Field(min_length=1)
    metric_id: str = Field(min_length=1)
    observed_at: datetime
    target_date: date
    device_type: str | None = None
    region: str | None = None

    @model_validator(mode="after")
    def target_date_matches_observation(self) -> IncidentEvidenceContext:
        if self.observed_at.date() != self.target_date:
            raise ValueError("target_date must match observed_at date")
        return self

    @classmethod
    def from_alert(cls, alert: Mapping[str, Any] | object) -> IncidentEvidenceContext:
        """Build scope from an Alert/state alert, never from benchmark metadata."""

        if isinstance(alert, Mapping):
            payload = alert
        elif hasattr(alert, "model_dump"):
            dumped = alert.model_dump(mode="python")
            if not isinstance(dumped, Mapping):
                raise TypeError("alert.model_dump() must return a mapping")
            payload = dumped
        else:
            raise TypeError("alert must be an Alert or mapping")

        incident_id = payload.get("incident_id")
        metric_id = payload.get("metric")
        if not isinstance(incident_id, str) or not incident_id.strip():
            raise ValueError("runtime alert incident_id is required")
        if not isinstance(metric_id, str) or not metric_id.strip():
            raise ValueError("runtime alert metric is required")
        observed_at = _parse_observed_datetime(payload.get("observed_at"))
        return cls(
            incident_id=incident_id,
            metric_id=metric_id,
            observed_at=observed_at,
            target_date=observed_at.date(),
            device_type=_optional_string(
                payload.get("device_type", payload.get("segment"))
            ),
            region=_optional_string(payload.get("region")),
        )


class EvidenceDecision(BaseModel):
    """One evidence reference and its independent hypothesis polarity."""

    model_config = ConfigDict(extra="forbid")

    evidence: EvidenceReference
    polarity: EvidencePolarity
    reason: str = Field(min_length=1)


class EvidenceInterpretation(BaseModel):
    """Serializable result containing one decision per evidence reference."""

    model_config = ConfigDict(extra="forbid")

    decisions: list[EvidenceDecision] = Field(default_factory=list)
    neutral_reason: str | None = None

    @property
    def polarity(self) -> EvidencePolarity:
        """Compatibility view for callers that only produced one decision."""

        if len(self.decisions) == 1:
            return self.decisions[0].polarity
        return EvidencePolarity.NEUTRAL

    @property
    def evidence(self) -> EvidenceReference | None:
        """Compatibility view for callers that only produced one decision."""

        if len(self.decisions) == 1:
            return self.decisions[0].evidence
        return None


_CANONICAL_SOURCE_TYPES = frozenset(item.value for item in EvidenceSourceType)
_SUCCESS_STATUS = "success"
_MISSING_PARTITION = "missing_partition"
_METRIC_DEFINITION_CHANGE = "metric_definition_change"
_DUPLICATE_BATCH = "duplicate_batch"
_JOIN_FILTER = "join_filter"
_DQ_HYPOTHESIS_COMPATIBILITY = {
    "check_null_rate": frozenset({"null_value_anomaly"}),
    "check_duplicate_rate": frozenset({"duplicate_batch", "join_explosion"}),
    "check_freshness": frozenset({"data_delay"}),
    "detect_schema_drift": frozenset({"schema_change", "field_drift"}),
    "detect_distribution_drift": frozenset({"ab_split_anomaly", "field_drift"}),
}
_DATE_LITERAL_PATTERN = re.compile(
    r"(?:\bDATE\s*)?['\"](\d{4}-\d{2}-\d{2})(?:[T ][^'\"]*)?['\"]",
    re.IGNORECASE,
)
_ANDROID_FILTER_PATTERN = re.compile(
    r"\b(?:device_type|segment)\s*=\s*['\"]android['\"]",
    re.IGNORECASE,
)
_METRIC_FILTER_PATTERN = re.compile(
    r"\bmetric_id\s*=\s*['\"]([^'\"]+)['\"]",
    re.IGNORECASE,
)
_VERSION_TIME_COLUMNS = (
    "effective_at",
    "valid_from",
    "changed_at",
    "created_at",
    "updated_at",
)
_RESULT_DATE_COLUMNS = (
    "metric_date",
    "event_date",
    "target_date",
    "date",
    "cast_date",
)
_F02_TOTAL_COLUMNS = (
    "rows_on_date",
    "row_count",
    "ai_task_count",
    "event_count",
    "raw_event_count",
    "total_count",
)
_F02_DISTINCT_COLUMNS = (
    "distinct_event_ids",
    "distinct_event_id_count",
    "distinct_ai_task_count",
    "distinct_event_count",
)
_F02_DUPLICATE_COLUMNS = (
    "duplicate_event_id_rows",
    "repeated_event_id_rows",
    "repeated_row_excess",
    "duplicate_count",
    "duplicate_rows",
    "duplicate_event_count",
)
_DEVICE_FILTER_PATTERN = re.compile(
    r"\b(?:device_type|segment)\s*=\s*['\"]([^'\"]+)['\"]",
    re.IGNORECASE,
)
_REGION_FILTER_PATTERN = re.compile(
    r"\bregion\s*=\s*['\"]([^'\"]+)['\"]",
    re.IGNORECASE,
)


class RuntimeEvidenceInterpreter:
    """Turn structured tool output into deterministic runtime evidence."""

    def __init__(
        self,
        context: IncidentEvidenceContext,
    ) -> None:
        self.context = context
        self.incident_id = context.incident_id

    def interpret(
        self,
        *,
        hypothesis: HypothesisState,
        step: InvestigationStep,
        tool_result: ToolExecutionResult,
    ) -> EvidenceInterpretation:
        """Interpret one typed tool result without mutating runtime state."""

        if tool_result.evidence:
            return self._interpret_canonical_evidence(
                hypothesis=hypothesis,
                step=step,
                tool_result=tool_result,
            )

        if tool_result.tool_name != "sql_query":
            return self._neutral(
                "the tool result did not contain a recognized abnormal finding"
            )

        return self._interpret_sql(
            hypothesis=hypothesis,
            step=step,
            tool_result=tool_result,
        )

    def _interpret_canonical_evidence(
        self,
        *,
        hypothesis: HypothesisState,
        step: InvestigationStep,
        tool_result: ToolExecutionResult,
    ) -> EvidenceInterpretation:
        """Decide every canonical DQ reference independently and fail closed."""

        result = _mapping(tool_result.result)
        if not tool_result.success:
            return self._neutral_for_references(
                tool_result.evidence, "the data-quality tool did not succeed"
            )
        if result.get("status") != _SUCCESS_STATUS:
            return self._neutral_for_references(
                tool_result.evidence, "the data-quality result status was not success"
            )
        if result.get("passed") is not False:
            return self._neutral_for_references(
                tool_result.evidence,
                "passed data-quality checks are neutral by default",
            )
        if result.get("check_name") != tool_result.tool_name:
            return self._neutral_for_references(
                tool_result.evidence, "data-quality check name did not match the tool"
            )
        if self.context is None:
            return self._neutral_for_references(
                tool_result.evidence, "incident scope was not provided"
            )

        decisions: list[EvidenceDecision] = []
        for reference in tool_result.evidence:
            if reference.source_type not in _CANONICAL_SOURCE_TYPES:
                decisions.append(
                    EvidenceDecision(
                        evidence=reference,
                        polarity=EvidencePolarity.NEUTRAL,
                        reason="evidence source type is not a canonical DQ source",
                    )
                )
                continue
            if hypothesis.root_cause_type not in _DQ_HYPOTHESIS_COMPATIBILITY.get(
                tool_result.tool_name, frozenset()
            ):
                reason = (
                    f"{tool_result.tool_name} is not compatible with "
                    f"{hypothesis.root_cause_type}"
                )
                decisions.append(
                    EvidenceDecision(
                        evidence=reference,
                        polarity=EvidencePolarity.NEUTRAL,
                        reason=reason,
                    )
                )
                continue
            compatible, reason = self._dq_scope_compatible(
                hypothesis=hypothesis,
                tool_result=tool_result,
                reference=reference,
            )
            if not compatible:
                decisions.append(
                    EvidenceDecision(
                        evidence=reference,
                        polarity=EvidencePolarity.NEUTRAL,
                        reason=reason,
                    )
                )
                continue
            proven, reason = _dq_abnormality_proven(
                tool_result.tool_name,
                result=result,
                reference=reference,
            )
            decisions.append(
                EvidenceDecision(
                    evidence=reference,
                    polarity=(
                        EvidencePolarity.SUPPORTS
                        if proven
                        else EvidencePolarity.NEUTRAL
                    ),
                    reason=(
                        "the failed DQ result proves a structured anomaly "
                        "compatible with the active hypothesis and incident "
                        f"scope: {reason}"
                        if proven
                        else reason
                    ),
                )
            )

        if not decisions:
            return self._neutral("the DQ result contained no evidence references")
        return EvidenceInterpretation(
            decisions=decisions,
            neutral_reason=(
                "data-quality evidence was not a scoped failed check"
                if all(
                    decision.polarity is EvidencePolarity.NEUTRAL
                    for decision in decisions
                )
                else None
            ),
        )

    def _dq_scope_compatible(
        self,
        *,
        hypothesis: HypothesisState,
        tool_result: ToolExecutionResult,
        reference: EvidenceReference,
    ) -> tuple[bool, str]:
        """Check only runtime DQ fields that prove metric/scope compatibility."""

        assert self.context is not None
        observation = _mapping(reference.observation)
        result = _mapping(tool_result.result)
        table = observation.get("table", result.get("table"))
        column = observation.get("column", result.get("column"))
        details = _mapping(observation.get("details"))

        if tool_result.tool_name == "check_null_rate":
            if (
                hypothesis.root_cause_type != "null_value_anomaly"
                or self.context.metric_id != "daily_active_users"
                or table != "events"
                or column != "user_id"
            ):
                return False, "null-rate table, column, or metric is outside the incident scope"
            scope = _mapping(details.get("scope"))
            if not _scope_covers_date(scope, self.context.target_date):
                return False, "null-rate scope does not cover the incident target date"
            if not _scope_matches_segment(scope, self.context.device_type):
                return False, "null-rate scope does not contain the incident segment"
            return True, "events.user_id null rate is measured in the target window"

        if tool_result.tool_name == "check_duplicate_rate":
            return False, (
                "duplicate-rate check is not incident-scoped in the current "
                "tool contract"
            )

        if tool_result.tool_name == "check_freshness":
            if (
                self.context.metric_id != "daily_active_users"
                or table != "events"
                or column != "event_time"
            ):
                return False, "freshness table, timestamp, or metric is outside the incident scope"
            scope = _mapping(details.get("scope"))
            reference_time = _parse_date_like(details.get("reference_time"))
            if reference_time is None:
                return False, "freshness evidence has no usable reference_time"
            if scope and not _scope_covers_date(scope, self.context.target_date):
                return False, "freshness current window does not cover the incident target date"
            if scope and not _scope_matches_segment(scope, self.context.device_type):
                return False, "freshness scope does not contain the incident segment"
            if not scope and abs((reference_time - self.context.target_date).days) > 1:
                return False, "freshness reference_time is outside the incident target window"
            return True, "events freshness is measured against the incident target window"

        if tool_result.tool_name == "detect_schema_drift":
            expected_metric = (
                "daily_active_users"
                if hypothesis.root_cause_type == "schema_change"
                else "ai_task_count"
            )
            if self.context.metric_id != expected_metric or table != "events":
                return False, "schema-drift table or metric is outside the incident scope"
            current_date = _parse_date_like(details.get("current_effective_at"))
            previous_date = _parse_date_like(details.get("previous_effective_at"))
            if current_date is None or previous_date is None:
                return False, "schema-drift evidence has no usable snapshot dates"
            if not _dates_near_or_cover(
                (previous_date, current_date), self.context.target_date
            ):
                return False, "schema-drift snapshots are outside the incident target window"
            return True, "events schema snapshots cover the incident target window"

        if tool_result.tool_name == "detect_distribution_drift":
            current_window = _mapping(details.get("current_window"))
            if not _scope_covers_date(current_window, self.context.target_date):
                return False, "distribution current window does not cover the incident target date"
            if hypothesis.root_cause_type == "field_drift":
                compatible = (
                    self.context.metric_id == "ai_task_count"
                    and table == "events"
                    and column == "event_name"
                )
            else:
                compatible = (
                    self.context.metric_id == "conversion_rate"
                    and table in {"events", "experiment_assignments"}
                    and isinstance(column, str)
                )
            if not compatible:
                return False, "distribution table, column, or metric is outside the incident scope"
            return True, "distribution current window covers the incident target date"

        return False, "the DQ tool has no safe scope rule"

    def _interpret_sql(
        self,
        *,
        hypothesis: HypothesisState,
        step: InvestigationStep,
        tool_result: ToolExecutionResult,
    ) -> EvidenceInterpretation:
        """Apply SQL rules only after validating the complete result envelope."""

        if not tool_result.success:
            return self._neutral("SQL execution did not succeed")
        validation = tool_result.sql_validation
        if validation is None:
            return self._neutral("SQL has no result-validation record")
        if not validation.passed:
            return self._neutral("SQL result validation failed")
        if not _validation_is_usable(validation):
            return self._neutral("SQL result was not marked usable")

        result = _mapping(tool_result.result)
        if result.get("status") != _SUCCESS_STATUS:
            return self._neutral("SQL result status was not success")
        columns = _string_list(result.get("columns"))
        rows = _rows(result.get("rows"), columns)
        row_count = _nonnegative_int(result.get("row_count"))
        truncated = result.get("truncated")
        if (
            not columns
            or not rows
            or row_count is None
            or row_count == 0
            or truncated is not False
            or len(rows) != row_count
        ):
            return self._neutral("SQL result was empty, truncated, or incomplete")

        metric_version = self._interpret_metric_versions(
            hypothesis=hypothesis,
            step=step,
            tool_result=tool_result,
            columns=columns,
            rows=rows,
        )
        if metric_version is not None:
            return metric_version

        scoped_business = self._interpret_scoped_business_observation(
            hypothesis=hypothesis,
            step=step,
            tool_result=tool_result,
            rows=rows,
        )
        if scoped_business is not None:
            return scoped_business

        business = self._interpret_business_observation(
            hypothesis=hypothesis,
            step=step,
            tool_result=tool_result,
            rows=rows,
        )
        if business is not None:
            return business

        operational = self._interpret_partition_observation(
            hypothesis=hypothesis,
            step=step,
            tool_result=tool_result,
            columns=columns,
            rows=rows,
        )
        if operational is not None:
            return operational

        return self._neutral("the returned SQL values matched no evidence rule")

    def _interpret_scoped_business_observation(
        self,
        *,
        hypothesis: HypothesisState,
        step: InvestigationStep,
        tool_result: ToolExecutionResult,
        rows: list[dict[str, Any]],
    ) -> EvidenceInterpretation | None:
        if hypothesis.root_cause_type not in {_DUPLICATE_BATCH, _JOIN_FILTER}:
            return None
        if self.context is None:
            return self._neutral("incident scope was not provided")

        sql = _sql_text(step)
        query = _parse_sql_query(sql)
        if query is None:
            return self._neutral("SQL could not be parsed as one read-only query")
        if not _sql_dimensions_match(self.context, sql):
            return self._neutral(
                "SQL device or region filters did not match the incident scope"
            )
        row = _target_scoped_row(self.context, step, rows)
        if row is None:
            return self._neutral("SQL result did not prove the incident target date")

        if hypothesis.root_cause_type == _DUPLICATE_BATCH:
            return self._interpret_duplicate_identity_counts(
                step=step,
                tool_result=tool_result,
                query=query,
                row=row,
            )
        return self._interpret_join_filter_survivor_counts(
            step=step,
            tool_result=tool_result,
            query=query,
            row=row,
        )

    def _interpret_duplicate_identity_counts(
        self,
        *,
        step: InvestigationStep,
        tool_result: ToolExecutionResult,
        query: exp.Select,
        row: Mapping[str, Any],
    ) -> EvidenceInterpretation | None:
        if self.context is None or self.context.metric_id != "ai_task_count":
            return self._neutral("F02 SQL observation is outside the metric scope")
        if not _f02_query_scope_matches(
            query,
            total_column=_first_present(row, *_F02_TOTAL_COLUMNS),
            distinct_column=_first_present(row, *_F02_DISTINCT_COLUMNS),
            duplicate_column=_first_present(row, *_F02_DUPLICATE_COLUMNS),
        ):
            return self._neutral(
                "F02 SQL did not prove the scoped event identity count relation"
            )

        total_column = _first_present(row, *_F02_TOTAL_COLUMNS)
        distinct_column = _first_present(row, *_F02_DISTINCT_COLUMNS)
        if total_column is None or distinct_column is None:
            return None
        total = _count(row[total_column])
        distinct = _count(row[distinct_column])
        if total is None or distinct is None or distinct > total:
            return self._neutral("F02 identity counts were not valid nonnegative counts")

        duplicate_column = _first_present(row, *_F02_DUPLICATE_COLUMNS)
        duplicate = None if duplicate_column is None else _count(row[duplicate_column])
        if duplicate_column is not None and duplicate is None:
            return self._neutral("F02 duplicate excess was not a valid count")
        expected_duplicate = total - distinct
        if duplicate is not None and duplicate != expected_duplicate:
            return self._neutral(
                "F02 duplicate excess was inconsistent with total minus distinct"
            )
        if total == distinct or (duplicate is not None and duplicate == 0):
            return self._neutral("F02 identity counts showed no duplicate excess")

        description = (
            "Target-date AI-task events contain duplicate identities: "
            f"{total_column}={total}, {distinct_column}={distinct}, "
            f"duplicate_excess={expected_duplicate}."
        )
        return self._make_interpretation(
            step_id=step.step_id,
            tool_result=tool_result,
            source_type=EvidenceSourceType.BUSINESS_DATA.value,
            rule="f02_duplicate_identity_counts",
            polarity=EvidencePolarity.SUPPORTS,
            description=description,
            row=row,
            scope_check=(
                "ai_task_count, target date, events table, run_ai_task filter, "
                "and identity-count relation matched"
            ),
        )

    def _interpret_join_filter_survivor_counts(
        self,
        *,
        step: InvestigationStep,
        tool_result: ToolExecutionResult,
        query: exp.Select,
        row: Mapping[str, Any],
    ) -> EvidenceInterpretation | None:
        if self.context is None or self.context.metric_id != "daily_active_users":
            return self._neutral("F07 SQL observation is outside the metric scope")
        if not {"event_users", "subscribed_users"}.issubset(row):
            return None
        if not _f07_query_scope_matches(query):
            return self._neutral(
                "F07 SQL did not prove the events-to-subscriptions survivor relation"
            )

        event_users = _count(row["event_users"])
        subscribed_users = _count(row["subscribed_users"])
        if (
            event_users is None
            or subscribed_users is None
            or subscribed_users > event_users
        ):
            return self._neutral("F07 survivor values were not valid user counts")
        if subscribed_users == event_users:
            return self._neutral("F07 join relation removed no event users")

        description = (
            "Target-date subscription matching reduces the event-user population: "
            f"event_users={event_users}, subscribed_users={subscribed_users}."
        )
        return self._make_interpretation(
            step_id=step.step_id,
            tool_result=tool_result,
            source_type=EvidenceSourceType.BUSINESS_DATA.value,
            rule="f07_join_filter_survivor_counts",
            polarity=EvidencePolarity.SUPPORTS,
            description=description,
            row=row,
            scope_check=(
                "daily_active_users, target date, events population, and "
                "LEFT JOIN subscription survivors matched"
            ),
        )

    def _interpret_business_observation(
        self,
        *,
        hypothesis: HypothesisState,
        step: InvestigationStep,
        tool_result: ToolExecutionResult,
        rows: list[dict[str, Any]],
    ) -> EvidenceInterpretation | None:
        if self.context is None:
            return self._neutral("incident scope was not provided")
        row = rows[0]

        android_column = _first_present(row, "android_event_count", "android_events")
        if android_column is None and _step_mentions_android(step):
            android_column = _first_present(row, "event_count", "events")
        if android_column is not None and hypothesis.root_cause_type == _MISSING_PARTITION:
            if not _f01_business_scope_matches(self.context, step, row):
                return self._neutral(
                    "F01 business observation is outside the target date or Android scope"
                )
            android_count = _number(row[android_column])
            if android_count is None:
                return self._neutral("F01 Android event count was not numeric")
            description = (
                "Business activity query returned "
                f"{android_column}={_display(android_count)}."
            )
            polarity = (
                EvidencePolarity.SUPPORTS
                if android_count == 0
                else EvidencePolarity.CONTRADICTS
            )
            return self._make_interpretation(
                step_id=step.step_id,
                tool_result=tool_result,
                source_type=EvidenceSourceType.BUSINESS_DATA.value,
                rule="f01_android_event_count",
                polarity=polarity,
                description=description,
                row=row,
                scope_check="target date and Android query/result scope matched",
            )

        required = {"raw_event_count", "raw_user_count", "daily_active_users"}
        if required.issubset(row) and hypothesis.root_cause_type == _METRIC_DEFINITION_CHANGE:
            if not _f11_business_scope_matches(self.context, step, row):
                return self._neutral(
                    "F11 business observation is outside the target metric or date scope"
                )
            raw_event_count = _number(row["raw_event_count"])
            raw_user_count = _number(row["raw_user_count"])
            daily_active_users = _number(row["daily_active_users"])
            if (
                raw_event_count is None
                or raw_user_count is None
                or daily_active_users is None
            ):
                return self._neutral("F11 business values were not numeric")
            if raw_event_count <= 0 or daily_active_users >= raw_user_count:
                return self._neutral(
                    "F11 business values did not show raw activity exceeding the metric"
                )
            description = (
                "Business activity remains present while the materialized "
                "metric is lower: "
                f"raw_event_count={_display(raw_event_count)}, "
                f"raw_user_count={_display(raw_user_count)}, "
                f"daily_active_users={_display(daily_active_users)}."
            )
            return self._make_interpretation(
                step_id=step.step_id,
                tool_result=tool_result,
                source_type=EvidenceSourceType.BUSINESS_DATA.value,
                rule="f11_metric_divergence",
                polarity=EvidencePolarity.SUPPORTS,
                description=description,
                row=row,
                scope_check="target metric and target-date business query/result matched",
            )
        return None

    def _interpret_partition_observation(
        self,
        *,
        hypothesis: HypothesisState,
        step: InvestigationStep,
        tool_result: ToolExecutionResult,
        columns: list[str],
        rows: list[dict[str, Any]],
    ) -> EvidenceInterpretation | None:
        if hypothesis.root_cause_type != _MISSING_PARTITION:
            return None
        if self.context is None:
            return self._neutral("incident scope was not provided")
        if not {"row_count", "status", "partition_value"}.issubset(columns):
            return None
        if not _step_mentions_partition_metadata(step, rows):
            return None
        if self.context.metric_id != "daily_active_users":
            return self._neutral("F01 partition observation is outside the metric scope")
        if self.context.device_type is not None and self.context.device_type.lower() != "android":
            return self._neutral("F01 partition observation is outside the incident segment")

        matching_row = next(
            (
                candidate
                for candidate in rows
                if _partition_scope_matches(candidate, self.context.target_date)
            ),
            None,
        )
        if matching_row is None:
            return self._neutral(
                "partition metadata did not contain the target-date Android partition"
            )
        row_count = _number(matching_row.get("row_count"))
        status = matching_row.get("status")
        if row_count is None or not isinstance(status, str):
            return self._neutral("partition metadata values were not usable")

        normalized_status = status.strip().lower()
        description = (
            "partition_metadata reports "
            f"partition_value={matching_row['partition_value']}, "
            f"row_count={_display(row_count)}, status={normalized_status}."
        )
        if row_count == 0 and normalized_status == "missing":
            polarity = EvidencePolarity.SUPPORTS
        elif row_count > 0 and normalized_status in {"ready", "success"}:
            polarity = EvidencePolarity.CONTRADICTS
        else:
            return self._neutral("partition metadata matched no safe F01 state")
        return self._make_interpretation(
            step_id=step.step_id,
            tool_result=tool_result,
            source_type=EvidenceSourceType.OPERATIONAL_METADATA.value,
            rule="f01_partition_state",
            polarity=polarity,
            description=description,
            row=matching_row,
            scope_check="target date and Android partition_value matched",
        )

    def _interpret_metric_versions(
        self,
        *,
        hypothesis: HypothesisState,
        step: InvestigationStep,
        tool_result: ToolExecutionResult,
        columns: list[str],
        rows: list[dict[str, Any]],
    ) -> EvidenceInterpretation | None:
        if not _step_mentions_metric_versions(step, columns):
            return None
        if self.context is None:
            return self._neutral("incident scope was not provided")
        if "metric_id" not in columns:
            return self._neutral("metric_versions did not return metric_id")
        returned_metric_ids = {row.get("metric_id") for row in rows}
        if returned_metric_ids != {self.context.metric_id}:
            return self._neutral(
                "metric_versions returned a metric_id outside the incident scope"
            )
        query_metric_ids = set(_METRIC_FILTER_PATTERN.findall(_sql_text(step)))
        if query_metric_ids and query_metric_ids != {self.context.metric_id}:
            return self._neutral("metric_versions query filtered a different metric_id")

        comparable_columns = [
            column
            for column in ("version", "definition_hash", "query")
            if column in columns
        ]
        if len(rows) < 2 or not comparable_columns:
            return self._neutral("metric_versions did not return comparable version data")
        changes = [
            column
            for column in comparable_columns
            if any(
                rows[index].get(column) != rows[index - 1].get(column)
                for index in range(1, len(rows))
            )
        ]
        if not changes:
            return self._neutral("metric_versions returned no version, hash, or query change")

        time_columns = [column for column in _VERSION_TIME_COLUMNS if column in columns]
        time_scope_reason = "metric_versions has no time column; metric_id scope was enforced"
        if time_columns:
            time_values: list[date] = []
            for row in rows:
                for column in time_columns:
                    parsed = _parse_date_like(row.get(column))
                    if parsed is None:
                        return self._neutral(
                            "metric_versions time column contained no usable date"
                        )
                    time_values.append(parsed)
            if not _dates_near_or_cover(time_values, self.context.target_date):
                return self._neutral(
                    "metric_versions change is outside the incident target-date window"
                )
            time_scope_reason = "metric_versions change date covered the incident target window"

        if hypothesis.root_cause_type != _METRIC_DEFINITION_CHANGE:
            return self._neutral(
                "metric_versions showed a change, but it does not support the active hypothesis"
            )

        projected_columns = ["metric_id", *comparable_columns, *time_columns]
        before = _project_row(rows[0], projected_columns)
        after = _project_row(rows[-1], projected_columns)
        description = (
            "metric_versions changed in returned observations: "
            f"before={before}; after={after}; changed_fields={changes}."
        )
        return self._make_interpretation(
            step_id=step.step_id,
            tool_result=tool_result,
            source_type=EvidenceSourceType.METRIC_VERSION.value,
            rule="f11_metric_version_change",
            polarity=EvidencePolarity.SUPPORTS,
            description=description,
            row={
                "before": before,
                "after": after,
                "changed_fields": changes,
            },
            scope_check=time_scope_reason,
        )

    def _make_interpretation(
        self,
        *,
        step_id: str,
        tool_result: ToolExecutionResult,
        source_type: str,
        rule: str,
        polarity: EvidencePolarity,
        description: str,
        row: Mapping[str, Any],
        scope_check: str,
    ) -> EvidenceInterpretation:
        query_id = tool_result.query_id or "no-query-id"
        evidence_id = self._evidence_id(
            step_id=step_id,
            query_id=query_id,
            rule=rule,
        )
        observation = _observation_payload(
            tool_result,
            context=self.context,
            step_id=step_id,
            row=row,
            rule=rule,
            scope_check=scope_check,
        )
        evidence = EvidenceReference(
            evidence_id=evidence_id,
            source_type=source_type,
            description=description,
            query_id=tool_result.query_id,
            observation=observation,
        )
        return EvidenceInterpretation(
            decisions=[
                EvidenceDecision(
                    evidence=evidence,
                    polarity=polarity,
                    reason=description,
                )
            ]
        )

    def _evidence_id(self, *, step_id: str, query_id: str, rule: str) -> str:
        metric_id = self.context.metric_id if self.context is not None else "unknown-metric"
        target_date = (
            self.context.target_date.isoformat()
            if self.context is not None
            else "unknown-date"
        )
        identity = (
            f"{self.incident_id}\x1f{metric_id}\x1f{target_date}\x1f"
            f"{step_id}\x1f{query_id}\x1f{rule}"
        )
        return "runner-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()

    @staticmethod
    def _neutral(reason: str) -> EvidenceInterpretation:
        return EvidenceInterpretation(neutral_reason=reason)

    @staticmethod
    def _neutral_for_references(
        references: Sequence[EvidenceReference],
        reason: str,
    ) -> EvidenceInterpretation:
        return EvidenceInterpretation(
            decisions=[
                EvidenceDecision(
                    evidence=reference,
                    polarity=EvidencePolarity.NEUTRAL,
                    reason=reason,
                )
                for reference in references
            ],
            neutral_reason=reason,
        )


def _validation_is_usable(validation: SqlResultValidation) -> bool:
    return validation.evidence.usable and not validation.evidence.truncated


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        return []
    return value


def _rows(value: object, columns: Sequence[str]) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    output: list[dict[str, Any]] = []
    for raw_row in value:
        if isinstance(raw_row, Mapping):
            if all(column in raw_row for column in columns):
                output.append({column: raw_row[column] for column in columns})
            continue
        if isinstance(raw_row, list) and len(raw_row) == len(columns):
            output.append(dict(zip(columns, raw_row, strict=True)))
    return output


def _nonnegative_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _number(value: object) -> Real | Decimal | None:
    if isinstance(value, bool) or not isinstance(value, (Real, Decimal)):
        return None
    return value


def _count(value: object) -> int | None:
    number = _number(value)
    if number is None or number < 0:
        return None
    integer = int(number)
    return integer if number == integer else None


def _finite_number(value: object) -> float | None:
    number = _number(value)
    if number is None:
        return None
    try:
        converted = float(number)
    except (OverflowError, ValueError):
        return None
    return converted if math.isfinite(converted) else None


def _structured_value(
    observation: Mapping[str, Any],
    details: Mapping[str, Any],
    result: Mapping[str, Any],
    observation_key: str,
    detail_key: str | None = None,
) -> object:
    """Read canonical observation fields before their detail-level aliases."""

    if observation_key in observation:
        return observation[observation_key]
    if detail_key is not None and detail_key in details:
        return details[detail_key]
    return result.get(observation_key)


def _dq_abnormality_proven(
    tool_name: str,
    *,
    result: Mapping[str, Any],
    reference: EvidenceReference,
) -> tuple[bool, str]:
    """Require structured abnormal values before admitting DQ support."""

    if reference.source_type not in _CANONICAL_SOURCE_TYPES:
        return False, "evidence source type is not a canonical DQ source"

    observation = _mapping(reference.observation)
    if observation.get("check_name") != tool_name:
        return False, "canonical DQ observation check name did not match the tool"
    if observation.get("status") != _SUCCESS_STATUS:
        return False, "canonical DQ observation status was not success"
    if observation.get("passed") is not False:
        return False, "canonical DQ observation was not a failed check"
    if result.get("status") != _SUCCESS_STATUS or result.get("passed") is not False:
        return False, "the failed DQ result envelope was not valid"

    details = _mapping(observation.get("details"))
    if tool_name == "check_null_rate":
        total_rows = observation.get("total_rows", details.get("total_rows"))
        if not isinstance(total_rows, int) or isinstance(total_rows, bool) or total_rows <= 0:
            return False, "null-rate observation has no rows; null rate is undefined"
        null_rate = _finite_number(
            _structured_value(observation, details, result, "observed_value", "null_rate")
        )
        if null_rate is None:
            return False, "null-rate observation has no finite null_rate"
        threshold = _finite_number(
            _structured_value(observation, details, result, "threshold")
        )
        if threshold is None:
            return False, "null-rate observation has no finite threshold"
        if null_rate <= threshold:
            return False, "null rate does not exceed the configured threshold"
        return True, f"null rate {null_rate} exceeds threshold {threshold}"

    if tool_name == "check_freshness":
        timestamp_rows = observation.get(
            "timestamp_rows", details.get("timestamp_rows")
        )
        if (
            not isinstance(timestamp_rows, int)
            or isinstance(timestamp_rows, bool)
            or timestamp_rows <= 0
        ):
            return False, "freshness observation has no timestamp rows"
        freshness_age = _finite_number(
            _structured_value(
                observation,
                details,
                result,
                "observed_value",
                "freshness_age_seconds",
            )
        )
        if freshness_age is None:
            return False, "freshness observation has no finite freshness age"
        max_age = _finite_number(
            _structured_value(
                observation,
                details,
                result,
                "threshold",
                "max_age_seconds",
            )
        )
        if max_age is None:
            return False, "freshness observation has no finite max age"
        if freshness_age < 0:
            return False, "freshness age is negative; the timestamp is future-dated"
        if freshness_age <= max_age:
            return False, "freshness age does not exceed the configured max age"
        return True, f"freshness age {freshness_age} exceeds max age {max_age}"

    if tool_name == "check_duplicate_rate":
        return False, (
            "duplicate-rate check is not incident-scoped in the current "
            "tool contract"
        )

    if tool_name == "detect_schema_drift":
        if not _has_actual_schema_change(details):
            return False, "schema-drift result reported no actual schema change"
        return True, "schema-drift details contain an actual schema change"

    if tool_name == "detect_distribution_drift":
        observed = _finite_number(
            _structured_value(observation, details, result, "observed_value")
        )
        if observed is None:
            return False, "distribution observation has no finite observed drift value"
        threshold = _finite_number(
            _structured_value(observation, details, result, "threshold")
        )
        if threshold is None:
            return False, "distribution observation has no finite threshold"
        if observed <= threshold:
            return False, "observed distribution drift does not exceed the threshold"
        return True, f"observed distribution drift {observed} exceeds threshold {threshold}"

    return False, "the DQ tool has no safe abnormality rule"


def _has_actual_schema_change(details: Mapping[str, Any]) -> bool:
    """Recognize only the change lists emitted by the schema drift tool."""

    for key in ("added_columns", "removed_columns", "type_changes"):
        value = details.get(key)
        if not isinstance(value, list) or not value:
            continue
        if key == "type_changes":
            if any(isinstance(item, Mapping) and bool(item) for item in value):
                return True
        elif any(isinstance(item, str) and item.strip() for item in value):
            return True
    return False


def _first_present(row: Mapping[str, Any], *names: str) -> str | None:
    return next((name for name in names if name in row), None)


def _step_mentions_android(step: InvestigationStep) -> bool:
    return "android" in _sql_text(step).lower()


def _step_mentions_partition_metadata(
    step: InvestigationStep,
    rows: Sequence[Mapping[str, Any]],
) -> bool:
    return "partition_metadata" in _sql_text(step).lower() or any(
        "partition_value" in row for row in rows
    )


def _step_mentions_metric_versions(
    step: InvestigationStep,
    columns: Sequence[str],
) -> bool:
    return (
        "metric_versions" in _sql_text(step).lower()
        and bool(
            set(columns).intersection(
                {"metric_id", "version", "definition_hash", "query"}
            )
        )
    )


def _sql_text(step: InvestigationStep) -> str:
    sql = step.arguments.get("sql")
    return sql if isinstance(sql, str) else ""


def _sql_dimensions_match(context: IncidentEvidenceContext, sql: str) -> bool:
    device_filters = _DEVICE_FILTER_PATTERN.findall(sql)
    region_filters = _REGION_FILTER_PATTERN.findall(sql)
    device_mentioned = re.search(r"\b(?:device_type|segment)\b", sql, re.IGNORECASE)
    region_mentioned = re.search(r"\bregion\b", sql, re.IGNORECASE)
    if device_mentioned is not None and not device_filters:
        return False
    if region_mentioned is not None and not region_filters:
        return False
    return _dimension_filter_matches(
        device_filters, context.device_type
    ) and _dimension_filter_matches(region_filters, context.region)


def _dimension_filter_matches(filters: Sequence[str], expected: str | None) -> bool:
    normalized = {value.strip().lower() for value in filters if value.strip()}
    if normalized:
        return expected is not None and normalized == {expected.strip().lower()}
    return expected is None


def _target_scoped_row(
    context: IncidentEvidenceContext,
    step: InvestigationStep,
    rows: Sequence[Mapping[str, Any]],
) -> Mapping[str, Any] | None:
    sql = _sql_text(step)
    has_result_dates = any(
        any(column in row for column in _RESULT_DATE_COLUMNS) for row in rows
    )
    if not has_result_dates:
        if len(rows) != 1 or not _sql_targets_exact_date(sql, context.target_date):
            return None
        return rows[0]

    if not _sql_covers_date(sql, context.target_date):
        return None
    matching: list[Mapping[str, Any]] = []
    for row in rows:
        raw_dates = [row[column] for column in _RESULT_DATE_COLUMNS if column in row]
        if not raw_dates:
            return None
        parsed_dates = [_parse_date_like(value) for value in raw_dates]
        if any(value is None for value in parsed_dates):
            return None
        if all(value == context.target_date for value in parsed_dates):
            matching.append(row)
    return matching[0] if len(matching) == 1 else None


def _sql_covers_date(sql: str, target_date: date) -> bool:
    dates = _sql_dates(sql)
    return bool(dates) and min(dates) <= target_date <= max(dates)


def _sql_targets_exact_date(sql: str, target_date: date) -> bool:
    target = re.escape(target_date.isoformat())
    date_equality = re.compile(
        rf"\bcast\s*\(\s*(?:\w+\.)?event_time\s+as\s+date\s*\)"
        rf"\s*=\s*(?:date\s*)?['\"]{target}['\"]",
        re.IGNORECASE,
    )
    if date_equality.search(sql):
        return True

    next_date = re.escape((target_date + timedelta(days=1)).isoformat())
    lower_bound = re.compile(
        rf"\b(?:\w+\.)?event_time\s*>=\s*(?:timestamp\s*)?"
        rf"['\"]{target}(?:[ T]00:00:00)?['\"]",
        re.IGNORECASE,
    )
    upper_bound = re.compile(
        rf"\b(?:\w+\.)?event_time\s*<\s*(?:timestamp\s*)?"
        rf"['\"]{next_date}(?:[ T]00:00:00)?['\"]",
        re.IGNORECASE,
    )
    return lower_bound.search(sql) is not None and upper_bound.search(sql) is not None


def _parse_sql_query(sql: str) -> exp.Select | None:
    try:
        expressions = [item for item in sqlglot.parse(sql, read="duckdb") if item]
    except (SqlglotError, TypeError, ValueError):
        return None
    if len(expressions) != 1 or not isinstance(expressions[0], exp.Select):
        return None
    return expressions[0]


def _f02_query_scope_matches(
    query: exp.Select,
    *,
    total_column: str | None,
    distinct_column: str | None,
    duplicate_column: str | None,
) -> bool:
    if total_column is None or distinct_column is None:
        return False
    tables = list(query.find_all(exp.Table))
    table_and_population_match = (
        len(tables) == 1
        and tables[0].name.lower() == "events"
        and not list(query.find_all(exp.Join))
        and _has_string_equality(query, column="event_name", value="run_ai_task")
    )
    total_expression = _projection_expression(query, total_column)
    distinct_expression = _projection_expression(query, distinct_column)
    total_expression_matches = _is_event_count(total_expression)
    distinct_expression_matches = _is_distinct_column_count(
        distinct_expression, column="event_id"
    )
    if duplicate_column is None:
        duplicate_expression_matches = True
    else:
        duplicate_expression = _projection_expression(query, duplicate_column)
        duplicate_expression_matches = (
            isinstance(duplicate_expression, exp.Sub)
            and _is_event_count(duplicate_expression.this)
            and _is_distinct_column_count(
                duplicate_expression.expression, column="event_id"
            )
        )
    return (
        table_and_population_match
        and total_expression_matches
        and distinct_expression_matches
        and duplicate_expression_matches
    )


def _f07_query_scope_matches(query: exp.Select) -> bool:
    tables = list(query.find_all(exp.Table))
    joins = list(query.find_all(exp.Join))
    from_clause = query.args.get("from_")
    source = from_clause.this if isinstance(from_clause, exp.From) else None
    if (
        len(tables) != 2
        or {table.name.lower() for table in tables} != {"events", "subscriptions"}
        or not isinstance(source, exp.Table)
        or source.name.lower() != "events"
        or len(joins) != 1
    ):
        return False
    join = joins[0]
    joined_table = join.this
    if (
        not isinstance(joined_table, exp.Table)
        or joined_table.name.lower() != "subscriptions"
        or str(join.args.get("side", "")).upper() != "LEFT"
    ):
        return False

    event_alias = source.alias_or_name.lower()
    subscription_alias = joined_table.alias_or_name.lower()
    join_condition_matches = _equality_matches_columns(
        join.args.get("on"),
        left_table=event_alias,
        left_column="user_id",
        right_table=subscription_alias,
        right_column="user_id",
    )
    event_count = _is_distinct_column_count(
        _projection_expression(query, "event_users"),
        table=event_alias,
        column="user_id",
    )
    subscription_count = _is_distinct_column_count(
        _projection_expression(query, "subscribed_users"),
        table=subscription_alias,
        column="user_id",
    )
    where = query.args.get("where")
    right_table_filtered = isinstance(where, exp.Where) and any(
        column.table.lower() == subscription_alias
        for column in where.find_all(exp.Column)
    )
    return (
        join_condition_matches
        and event_count
        and subscription_count
        and not right_table_filtered
    )


def _projection_expression(query: exp.Select, alias: str) -> exp.Expression | None:
    for projection in query.expressions:
        if isinstance(projection, exp.Alias) and projection.alias.lower() == alias.lower():
            return projection.this
    return None


def _is_event_count(expression: exp.Expression | None) -> bool:
    if not isinstance(expression, exp.Count):
        return False
    counted = expression.this
    return isinstance(counted, exp.Star) or (
        isinstance(counted, exp.Column) and counted.name.lower() == "event_id"
    )


def _is_distinct_column_count(
    expression: exp.Expression | None,
    *,
    column: str,
    table: str | None = None,
) -> bool:
    if not isinstance(expression, exp.Count) or not isinstance(
        expression.this, exp.Distinct
    ):
        return False
    values = expression.this.expressions
    if len(values) != 1 or not isinstance(values[0], exp.Column):
        return False
    observed = values[0]
    return observed.name.lower() == column.lower() and (
        table is None or observed.table.lower() == table.lower()
    )


def _has_string_equality(query: exp.Select, *, column: str, value: str) -> bool:
    for equality in query.find_all(exp.EQ):
        for possible_column, possible_value in (
            (equality.this, equality.expression),
            (equality.expression, equality.this),
        ):
            if (
                isinstance(possible_column, exp.Column)
                and possible_column.name.lower() == column.lower()
                and isinstance(possible_value, exp.Literal)
                and possible_value.is_string
                and possible_value.this.lower() == value.lower()
            ):
                return True
    return False


def _equality_matches_columns(
    expression: exp.Expression | None,
    *,
    left_table: str,
    left_column: str,
    right_table: str,
    right_column: str,
) -> bool:
    if not isinstance(expression, exp.EQ):
        return False
    expected = {
        (left_table.lower(), left_column.lower()),
        (right_table.lower(), right_column.lower()),
    }
    observed = {
        (item.table.lower(), item.name.lower())
        for item in (expression.this, expression.expression)
        if isinstance(item, exp.Column)
    }
    return observed == expected


def _project_row(
    row: Mapping[str, Any], columns: Sequence[str]
) -> dict[str, JsonValue]:
    return cast(dict[str, JsonValue], {column: row.get(column) for column in columns})


def _display(value: object) -> str:
    return str(value)


def _observation_payload(
    tool_result: ToolExecutionResult,
    *,
    context: IncidentEvidenceContext | None,
    step_id: str,
    row: Mapping[str, Any],
    rule: str,
    scope_check: str,
) -> dict[str, JsonValue]:
    payload: dict[str, JsonValue] = {
        "incident_id": context.incident_id if context is not None else None,
        "metric_id": context.metric_id if context is not None else None,
        "target_date": (
            context.target_date.isoformat() if context is not None else None
        ),
        "step_id": step_id,
        "rule": rule,
        "scope_check": scope_check,
        "tool_name": tool_result.tool_name,
        "query_id": tool_result.query_id,
        "observed_row": cast(dict[str, JsonValue], dict(row)),
        "result": cast(JsonValue, tool_result.result),
    }
    if tool_result.sql_validation is not None:
        payload["sql_validation"] = cast(
            JsonValue,
            tool_result.sql_validation.model_dump(mode="json"),
        )
    return payload


def _parse_observed_datetime(value: object) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime.combine(value, datetime.min.time())
    if not isinstance(value, str) or not value.strip():
        raise ValueError("runtime alert observed_at is required")
    normalized = value.strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        try:
            return datetime.combine(date.fromisoformat(normalized), datetime.min.time())
        except ValueError as exc:
            raise ValueError("runtime alert observed_at must be ISO-8601") from exc


def _optional_string(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _parse_date_like(value: object) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str) or len(value) < 10:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _row_dates(row: Mapping[str, Any], names: Sequence[str]) -> list[date]:
    values: list[date] = []
    for name in names:
        if name in row:
            parsed = _parse_date_like(row[name])
            if parsed is not None:
                values.append(parsed)
    return values


def _sql_dates(sql: str) -> list[date]:
    values: list[date] = []
    for value in _DATE_LITERAL_PATTERN.findall(sql):
        parsed = _parse_date_like(value)
        if parsed is not None:
            values.append(parsed)
    return values


def _f01_business_scope_matches(
    context: IncidentEvidenceContext,
    step: InvestigationStep,
    row: Mapping[str, Any],
) -> bool:
    if context.metric_id != "daily_active_users":
        return False
    row_dates = _row_dates(row, ("metric_date", "target_date", "event_date", "date"))
    sql_dates = _sql_dates(_sql_text(step))
    if any(value != context.target_date for value in row_dates):
        return False
    if context.target_date not in {*row_dates, *sql_dates}:
        return False

    row_segments = [
        str(row[name]).strip().lower()
        for name in ("device_type", "segment")
        if name in row and row[name] is not None
    ]
    if any(segment != "android" for segment in row_segments):
        return False
    if not row_segments and not _ANDROID_FILTER_PATTERN.search(_sql_text(step)):
        return False
    return context.device_type is None or context.device_type.lower() == "android"


def _f11_business_scope_matches(
    context: IncidentEvidenceContext,
    step: InvestigationStep,
    row: Mapping[str, Any],
) -> bool:
    if context.metric_id != "daily_active_users":
        return False
    if "metric_id" in row and row["metric_id"] != context.metric_id:
        return False
    dates = _row_dates(row, ("metric_date", "target_date", "event_date", "date"))
    sql = _sql_text(step)
    if any(value != context.target_date for value in dates):
        return False
    if context.target_date not in {*dates, *_sql_dates(sql)}:
        return False
    return (
        re.search(r"\bdaily_active_users\b", sql, re.IGNORECASE) is not None
        and "daily_metrics" in sql.lower()
        and "events" in sql.lower()
    )


def _partition_scope_matches(row: Mapping[str, Any], target_date: date) -> bool:
    value = row.get("partition_value")
    if not isinstance(value, str):
        return False
    parts = value.strip().split("/", 1)
    if len(parts) != 2:
        return False
    partition_date = _parse_date_like(parts[0])
    return partition_date == target_date and parts[1].strip().lower() == "android"


def _scope_covers_date(scope: Mapping[str, Any], target_date: date) -> bool:
    start = _parse_date_like(scope.get("start"))
    end = _parse_date_like(scope.get("end"))
    return start is not None and end is not None and start <= target_date < end


def _scope_matches_segment(scope: Mapping[str, Any], device_type: str | None) -> bool:
    if device_type is None:
        return True
    equals = _mapping(scope.get("equals"))
    raw_value = equals.get("device_type", equals.get("segment"))
    values = raw_value if isinstance(raw_value, list) else [raw_value]
    return any(
        isinstance(value, str) and value.strip().lower() == device_type.lower()
        for value in values
    )


def _dates_near_or_cover(values: Sequence[date], target_date: date) -> bool:
    if not values:
        return False
    if min(values) <= target_date <= max(values):
        return True
    return any(abs((value - target_date).days) <= 1 for value in values)


DQ_HYPOTHESIS_COMPATIBILITY = _DQ_HYPOTHESIS_COMPATIBILITY


__all__ = [
    "DQ_HYPOTHESIS_COMPATIBILITY",
    "EvidenceDecision",
    "EvidenceInterpretation",
    "EvidencePolarity",
    "IncidentEvidenceContext",
    "RuntimeEvidenceInterpreter",
]
