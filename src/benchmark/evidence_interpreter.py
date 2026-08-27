"""Interpret runtime tool observations as hypothesis evidence.

Tool execution and root-cause validation are deliberately separate concerns.
This module is the narrow adapter between them: it looks at the structured
result returned by a tool, applies deterministic observation rules, and emits
at most one canonical evidence reference for that observation.

In particular, a successful SQL call is not evidence by itself.  SQL evidence
is created only when the returned values match a concrete diagnostic rule.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from decimal import Decimal
from enum import StrEnum
from numbers import Real
from typing import Any, cast

from pydantic import BaseModel, ConfigDict, Field, JsonValue

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


class EvidenceInterpretation(BaseModel):
    """Serializable result of interpreting one tool observation."""

    model_config = ConfigDict(extra="forbid")

    polarity: EvidencePolarity
    evidence: EvidenceReference | None = None
    reason: str = Field(min_length=1)


_CANONICAL_SOURCE_TYPES = frozenset(item.value for item in EvidenceSourceType)
_SUCCESS_STATUS = "success"
_MISSING_PARTITION = "missing_partition"
_METRIC_DEFINITION_CHANGE = "metric_definition_change"


class RuntimeEvidenceInterpreter:
    """Turn structured tool output into deterministic runtime evidence.

    ``incident_id`` is supplied by the runtime caller only to make generated
    IDs stable within an incident.  No benchmark case or expected answer is
    needed by this interpreter.
    """

    def __init__(self, incident_id: str | None = None) -> None:
        self.incident_id = incident_id or "unknown-incident"

    def interpret(
        self,
        *,
        hypothesis: HypothesisState,
        step: InvestigationStep,
        tool_result: ToolExecutionResult,
    ) -> EvidenceInterpretation:
        """Interpret one typed tool result without mutating runtime state."""

        if tool_result.evidence:
            dq_interpretation = self._interpret_canonical_evidence(
                tool_result=tool_result,
            )
            if dq_interpretation is not None:
                return dq_interpretation

        if tool_result.tool_name != "sql_query":
            return self._neutral("the tool result did not contain a recognized abnormal finding")

        return self._interpret_sql(
            hypothesis=hypothesis,
            step=step,
            tool_result=tool_result,
        )

    def _interpret_canonical_evidence(
        self,
        *,
        tool_result: ToolExecutionResult,
    ) -> EvidenceInterpretation | None:
        """Preserve DQ evidence IDs and source types, while requiring failure."""

        result = _mapping(tool_result.result)
        passed = result.get("passed")
        status = result.get("status")
        if not tool_result.success or status != _SUCCESS_STATUS or passed is not False:
            return None

        canonical = next(
            (
                reference
                for reference in tool_result.evidence
                if reference.source_type in _CANONICAL_SOURCE_TYPES
            ),
            None,
        )
        if canonical is None:
            return None

        return EvidenceInterpretation(
            polarity=EvidencePolarity.SUPPORTS,
            evidence=canonical,
            reason=(
                "the data-quality check explicitly failed and its canonical "
                f"{canonical.source_type} evidence was preserved"
            ),
        )

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

    def _interpret_business_observation(
        self,
        *,
        hypothesis: HypothesisState,
        step: InvestigationStep,
        tool_result: ToolExecutionResult,
        rows: list[dict[str, Any]],
    ) -> EvidenceInterpretation | None:
        row = rows[0]

        android_column = _first_present(
            row,
            "android_event_count",
            "android_events",
        )
        if android_column is None and _step_mentions_android(step):
            android_column = _first_present(row, "event_count", "events")
        if android_column is not None:
            android_count = _number(row[android_column])
            if android_count is not None and hypothesis.root_cause_type == _MISSING_PARTITION:
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
                )

        required = {"raw_event_count", "raw_user_count", "daily_active_users"}
        if required.issubset(row):
            raw_event_count = _number(row["raw_event_count"])
            raw_user_count = _number(row["raw_user_count"])
            daily_active_users = _number(row["daily_active_users"])
            if (
                raw_event_count is not None
                and raw_user_count is not None
                and daily_active_users is not None
                and hypothesis.root_cause_type == _METRIC_DEFINITION_CHANGE
                and raw_event_count > 0
                and daily_active_users < raw_user_count
            ):
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
        if not {"row_count", "status"}.issubset(columns):
            return None
        if not _step_mentions_partition_metadata(step, rows):
            return None
        row = next(
            (
                candidate
                for candidate in rows
                if isinstance(candidate.get("partition_value"), str)
                and "android" in candidate["partition_value"].lower()
            ),
            rows[0],
        )
        row_count = _number(row.get("row_count"))
        status = row.get("status")
        if row_count is None or not isinstance(status, str):
            return None
        if hypothesis.root_cause_type != _MISSING_PARTITION:
            return None

        normalized_status = status.strip().lower()
        description = (
            "partition_metadata reports "
            f"row_count={_display(row_count)} and status={normalized_status}."
        )
        if row_count == 0 and normalized_status == "missing":
            polarity = EvidencePolarity.SUPPORTS
        elif row_count > 0 and normalized_status in {"ready", "success"}:
            polarity = EvidencePolarity.CONTRADICTS
        else:
            return None
        return self._make_interpretation(
            step_id=step.step_id,
            tool_result=tool_result,
            source_type=EvidenceSourceType.OPERATIONAL_METADATA.value,
            rule="f01_partition_state",
            polarity=polarity,
            description=description,
            row=row,
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
        comparable_columns = [
            column
            for column in ("version", "definition_hash", "query")
            if column in columns
        ]
        if len(rows) < 2 or not comparable_columns:
            return None
        changes = [
            column
            for column in comparable_columns
            if any(rows[index].get(column) != rows[index - 1].get(column) for index in range(1, len(rows)))
        ]
        if not changes:
            return self._neutral("metric_versions returned no version, hash, or query change")
        if hypothesis.root_cause_type != _METRIC_DEFINITION_CHANGE:
            return self._neutral(
                "metric_versions showed a change, but it does not support the active hypothesis"
            )

        before = _project_row(rows[0], comparable_columns)
        after = _project_row(rows[-1], comparable_columns)
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
    ) -> EvidenceInterpretation:
        query_id = tool_result.query_id or "no-query-id"
        evidence_id = self._evidence_id(
            step_id=step_id,
            query_id=query_id,
            rule=rule,
        )
        observation = _observation_payload(tool_result, row=row, rule=rule)
        return EvidenceInterpretation(
            polarity=polarity,
            evidence=EvidenceReference(
                evidence_id=evidence_id,
                source_type=source_type,
                description=description,
                query_id=tool_result.query_id,
                observation=observation,
            ),
            reason=description,
        )

    def _evidence_id(
        self,
        *,
        step_id: str,
        query_id: str,
        rule: str,
    ) -> str:
        identity = f"{self.incident_id}\x1f{step_id}\x1f{query_id}\x1f{rule}"
        return "runner-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()

    @staticmethod
    def _neutral(reason: str) -> EvidenceInterpretation:
        return EvidenceInterpretation(polarity=EvidencePolarity.NEUTRAL, reason=reason)


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


def _first_present(row: Mapping[str, Any], *names: str) -> str | None:
    return next((name for name in names if name in row), None)


def _step_mentions_android(step: InvestigationStep) -> bool:
    sql = step.arguments.get("sql")
    return isinstance(sql, str) and "android" in sql.lower()


def _step_mentions_partition_metadata(
    step: InvestigationStep,
    rows: Sequence[Mapping[str, Any]],
) -> bool:
    sql = step.arguments.get("sql")
    if isinstance(sql, str) and "partition_metadata" in sql.lower():
        return True
    return any("partition_value" in row for row in rows)


def _step_mentions_metric_versions(step: InvestigationStep, columns: Sequence[str]) -> bool:
    sql = step.arguments.get("sql")
    return (
        isinstance(sql, str)
        and "metric_versions" in sql.lower()
        and bool(set(columns).intersection({"version", "definition_hash", "query"}))
    )


def _project_row(row: Mapping[str, Any], columns: Sequence[str]) -> dict[str, JsonValue]:
    return cast(dict[str, JsonValue], {column: row.get(column) for column in columns})


def _display(value: object) -> str:
    return str(value)


def _observation_payload(
    tool_result: ToolExecutionResult,
    *,
    row: Mapping[str, Any],
    rule: str,
) -> dict[str, JsonValue]:
    payload: dict[str, JsonValue] = {
        "rule": rule,
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


__all__ = [
    "EvidenceInterpretation",
    "EvidencePolarity",
    "RuntimeEvidenceInterpreter",
]
