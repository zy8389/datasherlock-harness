from __future__ import annotations

import copy
from collections.abc import Mapping
from enum import StrEnum
from typing import Final, TypeAlias, cast

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from harness.guardrails import GuardrailEvent, GuardrailUsage

JsonObject: TypeAlias = dict[str, JsonValue]


class IncidentStatus(StrEnum):
    RECEIVED = "RECEIVED"
    TRIAGE = "TRIAGE"
    PLANNING = "PLANNING"
    EXECUTING = "EXECUTING"
    VALIDATING = "VALIDATING"
    HYPOTHESIS_TESTING = "HYPOTHESIS_TESTING"
    ROOT_CAUSE_FOUND = "ROOT_CAUSE_FOUND"
    FIX_PROPOSED = "FIX_PROPOSED"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    SANDBOX_REPAIR = "SANDBOX_REPAIR"
    POST_VALIDATION = "POST_VALIDATION"
    RESOLVED = "RESOLVED"
    REJECTED = "REJECTED"
    UNRESOLVED = "UNRESOLVED"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
    TOOL_FAILED = "TOOL_FAILED"
    VALIDATION_FAILED = "VALIDATION_FAILED"

    @property
    def is_terminal(self) -> bool:
        return self in TERMINAL_INCIDENT_STATUSES


TERMINAL_INCIDENT_STATUSES: Final[frozenset[IncidentStatus]] = frozenset(
    {
        IncidentStatus.RESOLVED,
        IncidentStatus.REJECTED,
        IncidentStatus.UNRESOLVED,
        IncidentStatus.BUDGET_EXCEEDED,
        IncidentStatus.TOOL_FAILED,
        IncidentStatus.VALIDATION_FAILED,
    }
)


class IncidentState(BaseModel):
    """Serializable snapshot of an incident investigation."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    alert: JsonObject = Field(default_factory=dict)
    plan: list[JsonObject] = Field(default_factory=list)
    hypotheses: list[JsonObject] = Field(default_factory=list)
    evidence: list[JsonObject] = Field(default_factory=list)
    tool_trace: list[JsonObject] = Field(default_factory=list)
    planner_metadata: JsonObject | None = None
    root_cause: JsonObject | None = None
    fix_proposal: JsonObject | None = None
    approval: JsonObject | None = None
    repair_result: JsonObject | None = None
    status: IncidentStatus = IncidentStatus.RECEIVED
    final_status: IncidentStatus | None = None
    rejected_hypotheses: list[JsonObject] = Field(default_factory=list)
    retry_count: int = Field(default=0, ge=0)
    token_cost: float = Field(default=0.0, ge=0)
    current_conclusion: str | None = None
    guardrail_usage: GuardrailUsage = Field(default_factory=GuardrailUsage)
    guardrail_events: list[GuardrailEvent] = Field(default_factory=list)

    def to_dict(self) -> JsonObject:
        """Return a detached, JSON-compatible checkpoint payload."""

        return cast(JsonObject, self.model_dump(mode="json"))

    @classmethod
    def from_dict(cls, payload: Mapping[str, JsonValue]) -> IncidentState:
        """Restore a state from a checkpoint payload."""

        return cls.model_validate(copy.deepcopy(dict(payload)))

    def to_json(self) -> str:
        return self.model_dump_json()

    @classmethod
    def from_json(cls, payload: str | bytes | bytearray) -> IncidentState:
        return cls.model_validate_json(payload)
