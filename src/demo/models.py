"""Typed public views for the canonical incident demo API."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator


class DemoCaseSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_id: str
    metric: str
    observed_at: str
    observed_value: float
    expected_value: float
    change_rate: float
    severity: str
    interactive_supported: bool
    repair_supported: bool


class DemoCaseList(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cases: list[DemoCaseSummary]


class DemoAlertView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    incident_id: str
    metric: str
    observed_at: str
    expected_value: float
    observed_value: float
    change_rate: float
    severity: str


class DemoPlanStepView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    step_id: str
    hypothesis_id: str
    purpose: str
    tool: str
    expected_evidence: list[str]
    execution_status: Literal["completed", "pending"]
    sql: str | None = None


class DemoToolTraceView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    position: int = Field(ge=1)
    tool: str
    success: bool
    query_id: str | None = None
    validation: dict[str, JsonValue] | None = None
    row_count: int | None = Field(default=None, ge=0)
    result_summary: str
    error: dict[str, str] | None = None
    raw_result: JsonValue | None = None


class DemoHypothesisView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    hypothesis_id: str
    root_cause_type: str
    description: str
    status: str
    confidence: float = Field(ge=0, le=1)
    supporting_evidence_ids: list[str]
    contradicting_evidence_ids: list[str]


class DemoEvidenceView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    evidence_id: str
    source_type: str
    finding: str
    query_id: str | None = None
    observation: dict[str, JsonValue]


class DemoRootCauseView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    root_cause_type: str
    confidence: float = Field(ge=0, le=1)
    affected_assets: list[str]
    supporting_evidence_ids: list[str]
    independent_source_types: list[str]


class DemoProposalView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    proposal_id: str
    action: str
    affected_assets: list[str]
    risk: str
    rationale: str
    evidence_bindings: list[str]
    parameters: dict[str, JsonValue]
    valid_until: datetime


class DemoApprovalView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision_id: str
    reviewer: str
    outcome: str
    comment: str | None = None
    decided_at: datetime


class DemoRepairView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    action: str
    status: str
    handler_invocation_count: int = Field(ge=0)
    source_hash_before: str | None = None
    source_hash_after: str | None = None
    sandbox_hash_before: str | None = None
    sandbox_hash_after: str | None = None
    changed_row_counts: dict[str, int]
    operation_details: dict[str, JsonValue]
    error: str | None = None


class DemoPostValidationView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    validation_id: str
    sandbox_run_id: str
    metric_id: str
    observed_before: float
    observed_after: float
    target_met: bool
    regressions: list[str]
    status: str
    summary: str
    validated_at: datetime


class DemoIncidentSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    incident_id: str
    case_id: str
    created_at: datetime
    updated_at: datetime
    status: str
    final_status: str | None


class DemoIncidentList(BaseModel):
    model_config = ConfigDict(extra="forbid")

    incidents: list[DemoIncidentSummary]


class DemoIncidentView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    incident_id: str
    case: DemoCaseSummary
    created_at: datetime
    updated_at: datetime
    status: str
    final_status: str | None
    execution_mode: Literal["deterministic-smoke"] = "deterministic-smoke"
    model_calls: Literal[0] = 0
    alert: DemoAlertView
    plan: list[DemoPlanStepView]
    tool_trace: list[DemoToolTraceView]
    hypotheses: list[DemoHypothesisView]
    evidence: list[DemoEvidenceView]
    root_cause: DemoRootCauseView | None
    repair_proposal: DemoProposalView | None
    approval: DemoApprovalView | None
    repair: DemoRepairView | None
    post_validation: DemoPostValidationView | None
    can_approve: bool
    terminal: bool
    final_report: dict[str, JsonValue] | None


class DemoIncidentStartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    case_id: str = Field(min_length=1)


class DemoApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    reviewer: str = Field(min_length=1)
    outcome: Literal["approved", "rejected"]
    comment: str | None = None

    @model_validator(mode="after")
    def require_rejection_comment(self) -> DemoApprovalRequest:
        if self.outcome == "rejected" and not self.comment:
            raise ValueError("rejected approval requires a non-empty comment")
        return self


class DemoBenchmarkRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    variant: str
    display_name: str
    top_1: float
    top_3: float
    invalid_sql_rate: float
    unsafe_rate: float
    duplicate_rate: float
    avg_tool_calls: float
    avg_sql_calls: float
    mean_latency_ms: float
    p50_latency_ms: float
    p95_latency_ms: float
    errors: int
    timeouts: int
    abstentions: int


class DemoBenchmarkSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    source_commit: str
    historical: Literal[True] = True
    post_pr20_rerun: Literal[False] = False
    rows: list[DemoBenchmarkRow]


__all__ = [
    "DemoApprovalRequest",
    "DemoBenchmarkSnapshot",
    "DemoCaseList",
    "DemoIncidentList",
    "DemoIncidentStartRequest",
    "DemoIncidentView",
]
