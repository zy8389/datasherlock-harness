"""Offline forensic audit for Full Harness benchmark abstentions.

The audit reads immutable result traces and benchmark Ground Truth. Ground
Truth is used only to select the golden runtime hypothesis for evaluation; it
is never passed to the Planner, Harness runtime, or evidence interpreter.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from collections.abc import Mapping, Sequence
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError

from agents.planner import InvestigationPlan
from benchmark.ablation import AblationCaseResult
from benchmark.case_generator import load_case_manifests
from benchmark.evidence_interpreter import (
    EvidencePolarity,
    IncidentEvidenceContext,
    RuntimeEvidenceInterpreter,
)
from config.faults import load_fault_catalog, load_ground_truth_cases
from harness.hypothesis import (
    REJECTED_CONFIDENCE_THRESHOLD,
    REJECTED_EVIDENCE_COUNT,
    SUPPORTED_CONFIDENCE_THRESHOLD,
    SUPPORTED_EVIDENCE_COUNT,
    EvidenceReference,
    HypothesisState,
    HypothesisStatus,
)
from harness.state import IncidentState, IncidentStatus
from tools.executor import ToolExecutionResult
from validators.root_cause_validator import (
    RootCauseValidationError,
    RootCauseValidator,
)

DEFAULT_GROUND_TRUTH_DIRECTORY = Path("benchmark/ground_truth")
DEFAULT_FAULT_CATALOG_PATH = Path("config/fault_catalog.yaml")
DEFAULT_CASES_DIRECTORY = Path("benchmark/cases")
AUDIT_SCHEMA_VERSION: Final[int] = 1


class AbstentionAuditError(ValueError):
    """Raised when a frozen trace cannot support a deterministic audit."""


class AbstentionCause(StrEnum):
    """Canonical earliest-bottleneck taxonomy for one abstention."""

    HYPOTHESIS_MISSING = "HYPOTHESIS_MISSING"
    EVIDENCE_MISSING = "EVIDENCE_MISSING"
    EVIDENCE_NEUTRALIZED = "EVIDENCE_NEUTRALIZED"
    CONFIDENCE_SHORTFALL = "CONFIDENCE_SHORTFALL"
    VALIDATOR_REJECTED = "VALIDATOR_REJECTED"
    CONTRADICTION_BLOCKED = "CONTRADICTION_BLOCKED"
    PLAN_EXHAUSTED = "PLAN_EXHAUSTED"
    OTHER = "OTHER"


class AuditComponent(StrEnum):
    """Owning component for the primary abstention cause."""

    PLANNER = "Planner"
    TOOL_PLAN = "Tool coverage / plan"
    EVIDENCE_INTERPRETER = "EvidenceInterpreter"
    HYPOTHESIS_MANAGER = "HypothesisManager"
    ROOT_CAUSE_VALIDATOR = "RootCauseValidator"
    OTHER = "Other"


TAXONOMY_ORDER: Final[tuple[AbstentionCause, ...]] = tuple(AbstentionCause)
COMPONENT_ORDER: Final[tuple[AuditComponent, ...]] = tuple(AuditComponent)


class AbstentionSignals(BaseModel):
    """Minimal causal signals consumed by the taxonomy classifier."""

    model_config = ConfigDict(extra="forbid")

    golden_hypothesis_present: bool
    supporting_evidence_count: int = Field(ge=0)
    contradicting_evidence_count: int = Field(ge=0)
    golden_neutral_evidence_count: int = Field(ge=0)
    final_confidence: float | None = Field(default=None, ge=0, le=1)
    final_hypothesis_status: HypothesisStatus | None = None
    validator_invoked: bool
    validator_validated: bool | None = None
    plan_exhausted: bool


class EvidenceFunnel(BaseModel):
    """Observed-to-validator evidence counts reconstructed from a trace."""

    model_config = ConfigDict(extra="forbid")

    observations_total: int = Field(ge=0)
    evidence_registered: int = Field(ge=0)
    supports_total: int = Field(ge=0)
    contradicts_total: int = Field(ge=0)
    neutral_total: int = Field(ge=0)
    golden_supports: int = Field(ge=0)
    golden_contradictions: int = Field(ge=0)
    golden_eligible_for_validator: bool | None = None


class ReasonCount(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1)
    count: int = Field(ge=1)


class CaseAbstentionAudit(BaseModel):
    """Machine-readable forensic result for one eligible case."""

    model_config = ConfigDict(extra="forbid")

    case_id: str
    fault_id: str
    golden_root_cause: str
    runtime_status: str
    runtime_error: str | None
    abstained: bool
    primary_abstention_cause: AbstentionCause
    secondary_causes: list[AbstentionCause]
    attributed_component: AuditComponent
    golden_hypothesis_present: bool
    golden_hypothesis_id: str | None
    golden_hypothesis_rank: int | None = Field(default=None, ge=1)
    initial_confidence: float | None = Field(default=None, ge=0, le=1)
    final_confidence: float | None = Field(default=None, ge=0, le=1)
    confidence_delta: float | None
    final_hypothesis_status: HypothesisStatus | None
    evidence_count: int = Field(ge=0)
    supporting_evidence_count: int = Field(ge=0)
    contradicting_evidence_count: int = Field(ge=0)
    independent_source_types: list[str]
    validator_invoked: bool
    validator_invocation_count: int = Field(ge=0)
    validator_validated: bool | None
    validator_missing_evidence: list[str] | None
    validator_contradictions: list[str] | None
    tool_calls: int = Field(ge=0)
    sql_calls: int = Field(ge=0)
    neutral_evidence_count: int = Field(ge=0)
    neutral_reasons: list[str]
    golden_neutral_evidence_count: int = Field(ge=0)
    golden_neutral_reasons: list[str]
    plan_steps: int = Field(ge=0)
    executed_steps: int = Field(ge=0)
    plan_exhausted: bool
    evidence_funnel: EvidenceFunnel
    cause_chain: list[str]


class FaultFamilyAudit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fault_id: str
    no_error_abstentions: int = Field(ge=0)
    dominant_cause: AbstentionCause | None
    cause_counts: dict[str, int]


class AbstentionAuditReport(BaseModel):
    """Complete deterministic audit contract for one frozen run."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = AUDIT_SCHEMA_VERSION
    run_id: str
    variant: str
    raw_result_path: str
    raw_result_sha256: str
    record_count: int = Field(ge=0)
    excluded_runtime_error_cases: int = Field(ge=0)
    eligible_case_count: int = Field(ge=0)
    taxonomy_counts: dict[str, int]
    component_counts: dict[str, int]
    hypothesis_coverage: dict[str, int]
    evidence_coverage: dict[str, int]
    source_type_coverage: dict[str, int]
    initial_confidence_distribution: dict[str, int]
    final_confidence_distribution: dict[str, int]
    validator_summary: dict[str, int]
    neutral_reasons: list[ReasonCount]
    fault_families: list[FaultFamilyAudit]
    worst_fault_families: list[str]
    cases: list[CaseAbstentionAudit]


class _ReplaySummary(BaseModel):
    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    supports_total: int = 0
    contradicts_total: int = 0
    neutral_total: int = 0
    neutral_reasons: list[str] = Field(default_factory=list)
    golden_neutral_total: int = 0
    golden_neutral_reasons: list[str] = Field(default_factory=list)
    admitted_evidence_ids: list[str] = Field(default_factory=list)


def classify_abstention(signals: AbstentionSignals) -> AbstentionCause:
    """Return the earliest supported bottleneck in the abstention chain."""

    if not signals.golden_hypothesis_present:
        return AbstentionCause.HYPOTHESIS_MISSING
    if (
        signals.contradicting_evidence_count > 0
        or signals.final_hypothesis_status is HypothesisStatus.REJECTED
    ):
        return AbstentionCause.CONTRADICTION_BLOCKED
    if signals.supporting_evidence_count < SUPPORTED_EVIDENCE_COUNT:
        if signals.golden_neutral_evidence_count > 0:
            return AbstentionCause.EVIDENCE_NEUTRALIZED
        return AbstentionCause.EVIDENCE_MISSING
    if (
        signals.final_confidence is not None
        and signals.final_confidence < SUPPORTED_CONFIDENCE_THRESHOLD
    ):
        return AbstentionCause.CONFIDENCE_SHORTFALL
    if (
        signals.final_hypothesis_status is HypothesisStatus.SUPPORTED
        and signals.validator_invoked
        and signals.validator_validated is False
    ):
        return AbstentionCause.VALIDATOR_REJECTED
    if signals.plan_exhausted:
        return AbstentionCause.PLAN_EXHAUSTED
    return AbstentionCause.OTHER


def audit_full_harness_abstentions(
    results_path: str | Path,
    *,
    ground_truth_directory: str | Path = DEFAULT_GROUND_TRUTH_DIRECTORY,
    fault_catalog_path: str | Path = DEFAULT_FAULT_CATALOG_PATH,
    cases_directory: str | Path = DEFAULT_CASES_DIRECTORY,
    expected_run_id: str | None = None,
) -> AbstentionAuditReport:
    """Audit no-runtime-error Full Harness abstentions without executing cases."""

    raw_path = Path(results_path)
    raw_bytes = raw_path.read_bytes()
    records = _load_results(raw_bytes)
    if not records:
        raise AbstentionAuditError(f"no result records found in {raw_path}")
    variants = {record.variant for record in records}
    if variants != {"full_harness"}:
        raise AbstentionAuditError(
            "audit input must contain only full_harness records; found "
            + ", ".join(sorted(variants))
        )

    run_id = _load_run_id(raw_path)
    if expected_run_id is not None and run_id != expected_run_id:
        raise AbstentionAuditError(
            f"run_id mismatch: expected {expected_run_id!r}, found {run_id!r}"
        )

    catalog = load_fault_catalog(fault_catalog_path)
    ground_truth_seeds = {
        case.fault_id: case
        for case in load_ground_truth_cases(ground_truth_directory, catalog)
    }
    manifests = {
        manifest.case_id: manifest for manifest in load_case_manifests(cases_directory)
    }
    _validate_result_identity(
        records,
        manifests=manifests,
        ground_truth_seeds=ground_truth_seeds,
        catalog=catalog,
    )

    runtime_errors = [record for record in records if record.status == "error"]
    eligible = [
        record
        for record in records
        if record.status == "completed"
        and record.abstention
        and record.primary_prediction is None
    ]
    cases = [
        _audit_case(record, golden_root_cause=manifests[record.case_id].root_cause_type)
        for record in sorted(eligible, key=lambda item: item.case_id)
    ]

    return AbstentionAuditReport(
        run_id=run_id,
        variant="full_harness",
        raw_result_path=_portable_path(raw_path),
        raw_result_sha256=hashlib.sha256(raw_bytes).hexdigest(),
        record_count=len(records),
        excluded_runtime_error_cases=len(runtime_errors),
        eligible_case_count=len(cases),
        taxonomy_counts=_enum_counts(
            (case.primary_abstention_cause for case in cases), TAXONOMY_ORDER
        ),
        component_counts=_enum_counts(
            (case.attributed_component for case in cases), COMPONENT_ORDER
        ),
        hypothesis_coverage={
            "golden_hypothesis_proposed": sum(
                case.golden_hypothesis_present for case in cases
            ),
            "golden_hypothesis_missing": sum(
                not case.golden_hypothesis_present for case in cases
            ),
        },
        evidence_coverage=_coverage_counts(
            case.supporting_evidence_count
            for case in cases
            if case.golden_hypothesis_present
        ),
        source_type_coverage=_coverage_counts(
            len(case.independent_source_types)
            for case in cases
            if case.golden_hypothesis_present
        ),
        initial_confidence_distribution=_confidence_counts(
            case.initial_confidence
            for case in cases
            if case.golden_hypothesis_present
        ),
        final_confidence_distribution=_confidence_counts(
            case.final_confidence
            for case in cases
            if case.golden_hypothesis_present
        ),
        validator_summary={
            "never_invoked": sum(not case.validator_invoked for case in cases),
            "invoked": sum(case.validator_invoked for case in cases),
            "gate_eligible": sum(
                case.evidence_funnel.golden_eligible_for_validator is True
                for case in cases
            ),
            "rejected": sum(
                case.primary_abstention_cause is AbstentionCause.VALIDATOR_REJECTED
                for case in cases
            ),
            "validated": sum(case.validator_validated is True for case in cases),
        },
        neutral_reasons=_reason_counts(
            reason for case in cases for reason in case.neutral_reasons
        ),
        fault_families=_fault_family_audits(cases),
        worst_fault_families=_worst_fault_families(cases),
        cases=cases,
    )


def _load_results(raw_bytes: bytes) -> list[AblationCaseResult]:
    records: list[AblationCaseResult] = []
    for line_number, line in enumerate(raw_bytes.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            records.append(AblationCaseResult.model_validate_json(line))
        except ValidationError as exc:
            raise AbstentionAuditError(
                f"invalid result record at line {line_number}: {exc}"
            ) from exc
    return records


def _load_run_id(results_path: Path) -> str:
    config_path = results_path.parent.parent / "config.json"
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
        run_id = payload["config"]["run_id"]
    except (FileNotFoundError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise AbstentionAuditError(
            f"cannot read frozen run_id from {config_path}"
        ) from exc
    if not isinstance(run_id, str) or not run_id.strip():
        raise AbstentionAuditError(f"invalid frozen run_id in {config_path}")
    return run_id


def _validate_result_identity(
    records: Sequence[AblationCaseResult],
    *,
    manifests: Mapping[str, Any],
    ground_truth_seeds: Mapping[str, Any],
    catalog: Any,
) -> None:
    case_ids = [record.case_id for record in records]
    duplicates = [case_id for case_id, count in Counter(case_ids).items() if count > 1]
    if duplicates:
        raise AbstentionAuditError(
            "duplicate result case IDs: " + ", ".join(sorted(duplicates))
        )
    unknown = sorted(set(case_ids).difference(manifests))
    if unknown:
        raise AbstentionAuditError(
            "result cases missing from canonical manifests: " + ", ".join(unknown)
        )
    for record in records:
        manifest = manifests[record.case_id]
        seed = ground_truth_seeds.get(record.fault_id)
        if seed is None:
            raise AbstentionAuditError(
                f"{record.case_id} fault family has no Ground Truth seed"
            )
        fault = catalog.by_id(record.fault_id)
        if record.fault_id != manifest.fault_id:
            raise AbstentionAuditError(
                f"{record.case_id} fault_id does not match its canonical manifest"
            )
        if not (
            manifest.root_cause_type
            == seed.root_cause_type
            == fault.root_cause_type
            == record.expected_root_cause
        ):
            raise AbstentionAuditError(
                f"{record.case_id} root cause differs across result, manifest, "
                "Ground Truth seed, or fault catalog"
            )


def _audit_case(
    record: AblationCaseResult,
    *,
    golden_root_cause: str,
) -> CaseAbstentionAudit:
    trace = record.trace_payload
    state_payload = trace.get("state")
    planner_payload = trace.get("planner")
    if not isinstance(state_payload, Mapping) or not isinstance(planner_payload, Mapping):
        raise AbstentionAuditError(f"{record.case_id} has no complete state/planner trace")
    state = IncidentState.model_validate(state_payload)
    plan_payload = planner_payload.get("plan")
    if not isinstance(plan_payload, Mapping):
        raise AbstentionAuditError(f"{record.case_id} planner trace has no plan")
    plan = InvestigationPlan.model_validate(plan_payload)
    if state.plan != [step.model_dump(mode="json") for step in plan.steps]:
        raise AbstentionAuditError(
            f"{record.case_id} state plan differs from the frozen planner plan"
        )
    if len(state.tool_trace) > len(plan.steps):
        raise AbstentionAuditError(f"{record.case_id} executed more steps than planned")

    final_hypotheses = [HypothesisState.model_validate(item) for item in state.hypotheses]
    by_id = {item.hypothesis_id: item for item in final_hypotheses}
    if len(by_id) != len(final_hypotheses):
        raise AbstentionAuditError(f"{record.case_id} has duplicate hypothesis IDs")
    planner_hypotheses = {item.hypothesis_id: item for item in plan.hypotheses}
    if set(planner_hypotheses) != set(by_id):
        raise AbstentionAuditError(
            f"{record.case_id} final hypotheses differ from the frozen planner plan"
        )

    golden_plan_items = [
        (index, item)
        for index, item in enumerate(plan.hypotheses, start=1)
        if item.root_cause_type == golden_root_cause
    ]
    if len(golden_plan_items) > 1:
        raise AbstentionAuditError(
            f"{record.case_id} proposes the golden root cause more than once"
        )
    golden_rank = golden_plan_items[0][0] if golden_plan_items else None
    golden_plan = golden_plan_items[0][1] if golden_plan_items else None
    golden = by_id[golden_plan.hypothesis_id] if golden_plan is not None else None

    replay = _replay_trace(
        record.case_id,
        state=state,
        plan=plan,
        hypotheses=by_id,
        golden_hypothesis_id=golden.hypothesis_id if golden is not None else None,
    )
    registered = _registered_evidence(state.evidence)
    admitted = set(replay.admitted_evidence_ids)
    if admitted != set(registered):
        missing = sorted(admitted.difference(registered))
        unexpected = sorted(set(registered).difference(admitted))
        raise AbstentionAuditError(
            f"{record.case_id} replay/runtime evidence mismatch; "
            f"missing={missing}, unexpected={unexpected}"
        )

    golden_supports = len(golden.supporting_evidence_ids) if golden else 0
    golden_contradictions = len(golden.contradicting_evidence_ids) if golden else 0
    source_types = _supporting_source_types(golden, registered)
    validator_invocation_count = _golden_validator_invocations(
        state=state,
        plan=plan,
        golden_hypothesis_id=golden.hypothesis_id if golden is not None else None,
    )
    validator_invoked = validator_invocation_count > 0
    validator_validated: bool | None = None
    validator_missing: list[str] | None = None
    validator_contradictions: list[str] | None = None
    if golden is not None and validator_invoked:
        try:
            validation = RootCauseValidator().validate(golden, tuple(registered.values()))
        except RootCauseValidationError as exc:
            if golden.status is not HypothesisStatus.REJECTED:
                raise AbstentionAuditError(
                    f"{record.case_id} golden validator replay failed: {exc}"
                ) from exc
            validation = None
        if validation is not None:
            validator_validated = validation.validated
            validator_missing = validation.missing_evidence
            validator_contradictions = validation.contradictions
            if validation.validated and state.root_cause is None:
                raise AbstentionAuditError(
                    f"{record.case_id} validator replay validates an abstained trace"
                )

    plan_exhausted = (
        state.status is IncidentStatus.UNRESOLVED
        and len(state.tool_trace) == len(plan.steps)
    )
    eligible_for_validator = _eligible_for_validator(golden)
    signals = AbstentionSignals(
        golden_hypothesis_present=golden is not None,
        supporting_evidence_count=golden_supports,
        contradicting_evidence_count=golden_contradictions,
        golden_neutral_evidence_count=replay.golden_neutral_total,
        final_confidence=golden.confidence if golden else None,
        final_hypothesis_status=golden.status if golden else None,
        validator_invoked=validator_invoked,
        validator_validated=validator_validated,
        plan_exhausted=plan_exhausted,
    )
    primary = classify_abstention(signals)
    component = _component_for(primary)
    secondary = _secondary_causes(signals, primary)
    initial_confidence = golden_plan.initial_confidence if golden_plan else None
    final_confidence = golden.confidence if golden else None
    confidence_delta = (
        round(final_confidence - initial_confidence, 12)
        if final_confidence is not None and initial_confidence is not None
        else None
    )

    return CaseAbstentionAudit(
        case_id=record.case_id,
        fault_id=record.fault_id,
        golden_root_cause=golden_root_cause,
        runtime_status=record.completion_status,
        runtime_error=record.error,
        abstained=record.abstention,
        primary_abstention_cause=primary,
        secondary_causes=secondary,
        attributed_component=component,
        golden_hypothesis_present=golden is not None,
        golden_hypothesis_id=golden.hypothesis_id if golden else None,
        golden_hypothesis_rank=golden_rank,
        initial_confidence=initial_confidence,
        final_confidence=final_confidence,
        confidence_delta=confidence_delta,
        final_hypothesis_status=golden.status if golden else None,
        evidence_count=len(golden.evidence_ids) if golden else 0,
        supporting_evidence_count=golden_supports,
        contradicting_evidence_count=golden_contradictions,
        independent_source_types=source_types,
        validator_invoked=validator_invoked,
        validator_invocation_count=validator_invocation_count,
        validator_validated=validator_validated,
        validator_missing_evidence=validator_missing,
        validator_contradictions=validator_contradictions,
        tool_calls=record.tool_call_count,
        sql_calls=record.sql_call_count,
        neutral_evidence_count=replay.neutral_total,
        neutral_reasons=replay.neutral_reasons,
        golden_neutral_evidence_count=replay.golden_neutral_total,
        golden_neutral_reasons=replay.golden_neutral_reasons,
        plan_steps=len(plan.steps),
        executed_steps=len(state.tool_trace),
        plan_exhausted=plan_exhausted,
        evidence_funnel=EvidenceFunnel(
            observations_total=len(state.tool_trace),
            evidence_registered=len(registered),
            supports_total=replay.supports_total,
            contradicts_total=replay.contradicts_total,
            neutral_total=replay.neutral_total,
            golden_supports=golden_supports,
            golden_contradictions=golden_contradictions,
            golden_eligible_for_validator=eligible_for_validator,
        ),
        cause_chain=_cause_chain(
            golden_root_cause=golden_root_cause,
            golden=golden,
            golden_rank=golden_rank,
            plan_steps=len(plan.steps),
            executed_steps=len(state.tool_trace),
            supports=golden_supports,
            contradictions=golden_contradictions,
            neutral=replay.golden_neutral_total,
            validator_invoked=validator_invoked,
            validator_validated=validator_validated,
            primary=primary,
            component=component,
        ),
    )


def _replay_trace(
    case_id: str,
    *,
    state: IncidentState,
    plan: InvestigationPlan,
    hypotheses: Mapping[str, HypothesisState],
    golden_hypothesis_id: str | None,
) -> _ReplaySummary:
    interpreter = RuntimeEvidenceInterpreter(
        context=IncidentEvidenceContext.from_alert(state.alert)
    )
    summary = _ReplaySummary()
    for step, result_payload in zip(plan.steps, state.tool_trace, strict=False):
        hypothesis = hypotheses.get(step.hypothesis_id)
        if hypothesis is None:
            raise AbstentionAuditError(
                f"{case_id} executed step references unknown hypothesis {step.hypothesis_id}"
            )
        result = ToolExecutionResult.model_validate(result_payload)
        interpretation = interpreter.interpret(
            hypothesis=hypothesis,
            step=step,
            tool_result=result,
        )
        is_golden = step.hypothesis_id == golden_hypothesis_id
        if interpretation.decisions:
            for decision in interpretation.decisions:
                if decision.polarity is EvidencePolarity.SUPPORTS:
                    summary.supports_total += 1
                    summary.admitted_evidence_ids.append(decision.evidence.evidence_id)
                elif decision.polarity is EvidencePolarity.CONTRADICTS:
                    summary.contradicts_total += 1
                    summary.admitted_evidence_ids.append(decision.evidence.evidence_id)
                else:
                    summary.neutral_total += 1
                    summary.neutral_reasons.append(decision.reason)
                    if is_golden:
                        summary.golden_neutral_total += 1
                        summary.golden_neutral_reasons.append(decision.reason)
        elif interpretation.neutral_reason is not None:
            summary.neutral_total += 1
            summary.neutral_reasons.append(interpretation.neutral_reason)
            if is_golden:
                summary.golden_neutral_total += 1
                summary.golden_neutral_reasons.append(
                    interpretation.neutral_reason
                )
    return summary


def _registered_evidence(
    evidence_payloads: Sequence[Mapping[str, JsonValue]],
) -> dict[str, EvidenceReference]:
    registered: dict[str, EvidenceReference] = {}
    for payload in evidence_payloads:
        try:
            reference = EvidenceReference.model_validate(payload)
        except ValidationError:
            continue
        existing = registered.get(reference.evidence_id)
        if existing is not None and existing != reference:
            raise AbstentionAuditError(
                f"conflicting registered evidence: {reference.evidence_id}"
            )
        registered[reference.evidence_id] = reference
    return registered


def _golden_validator_invocations(
    *,
    state: IncidentState,
    plan: InvestigationPlan,
    golden_hypothesis_id: str | None,
) -> int:
    if golden_hypothesis_id is None:
        return 0
    count = 0
    for step, result_payload in zip(plan.steps, state.tool_trace, strict=False):
        result = ToolExecutionResult.model_validate(result_payload)
        if step.hypothesis_id == golden_hypothesis_id and result.success:
            count += 1
    return count


def _supporting_source_types(
    golden: HypothesisState | None,
    registered: Mapping[str, EvidenceReference],
) -> list[str]:
    if golden is None:
        return []
    return list(
        dict.fromkeys(
            registered[evidence_id].source_type
            for evidence_id in golden.supporting_evidence_ids
            if evidence_id in registered
        )
    )


def _eligible_for_validator(golden: HypothesisState | None) -> bool | None:
    if golden is None:
        return None
    rejected = (
        len(golden.contradicting_evidence_ids) >= REJECTED_EVIDENCE_COUNT
        or golden.confidence <= REJECTED_CONFIDENCE_THRESHOLD
    )
    return (
        golden.status is HypothesisStatus.SUPPORTED
        and not rejected
        and len(golden.supporting_evidence_ids) >= SUPPORTED_EVIDENCE_COUNT
        and golden.confidence >= SUPPORTED_CONFIDENCE_THRESHOLD
    )


def _secondary_causes(
    signals: AbstentionSignals,
    primary: AbstentionCause,
) -> list[AbstentionCause]:
    candidates: list[AbstentionCause] = []
    if signals.golden_hypothesis_present:
        if signals.golden_neutral_evidence_count > 0:
            candidates.append(AbstentionCause.EVIDENCE_NEUTRALIZED)
        if (
            signals.supporting_evidence_count >= SUPPORTED_EVIDENCE_COUNT
            and signals.final_confidence is not None
            and signals.final_confidence < SUPPORTED_CONFIDENCE_THRESHOLD
        ):
            candidates.append(AbstentionCause.CONFIDENCE_SHORTFALL)
        if signals.contradicting_evidence_count > 0:
            candidates.append(AbstentionCause.CONTRADICTION_BLOCKED)
        if (
            signals.final_hypothesis_status is HypothesisStatus.SUPPORTED
            and signals.validator_invoked
            and signals.validator_validated is False
        ):
            candidates.append(AbstentionCause.VALIDATOR_REJECTED)
    if signals.plan_exhausted:
        candidates.append(AbstentionCause.PLAN_EXHAUSTED)
    return [
        cause
        for cause in TAXONOMY_ORDER
        if cause != primary and cause in set(candidates)
    ]


def _component_for(cause: AbstentionCause) -> AuditComponent:
    return {
        AbstentionCause.HYPOTHESIS_MISSING: AuditComponent.PLANNER,
        AbstentionCause.EVIDENCE_MISSING: AuditComponent.TOOL_PLAN,
        AbstentionCause.EVIDENCE_NEUTRALIZED: AuditComponent.EVIDENCE_INTERPRETER,
        AbstentionCause.CONFIDENCE_SHORTFALL: AuditComponent.HYPOTHESIS_MANAGER,
        AbstentionCause.VALIDATOR_REJECTED: AuditComponent.ROOT_CAUSE_VALIDATOR,
        AbstentionCause.CONTRADICTION_BLOCKED: AuditComponent.HYPOTHESIS_MANAGER,
        AbstentionCause.PLAN_EXHAUSTED: AuditComponent.TOOL_PLAN,
        AbstentionCause.OTHER: AuditComponent.OTHER,
    }[cause]


def _cause_chain(
    *,
    golden_root_cause: str,
    golden: HypothesisState | None,
    golden_rank: int | None,
    plan_steps: int,
    executed_steps: int,
    supports: int,
    contradictions: int,
    neutral: int,
    validator_invoked: bool,
    validator_validated: bool | None,
    primary: AbstentionCause,
    component: AuditComponent,
) -> list[str]:
    planner = (
        f"Planner proposed {golden_root_cause} as rank {golden_rank} ({golden.hypothesis_id})."
        if golden is not None
        else f"Planner did not propose the golden root cause {golden_root_cause}."
    )
    hypothesis = (
        f"Golden hypothesis ended {golden.status.value} at confidence "
        f"{golden.confidence:.2f}."
        if golden is not None
        else "No golden hypothesis lifecycle existed."
    )
    validator = (
        f"Golden validator was invoked and validated={validator_validated}."
        if validator_invoked
        else "Golden validator was never invoked."
    )
    return [
        planner,
        f"Tools executed {executed_steps} of {plan_steps} planned steps.",
        f"Golden evidence ended with {supports} supports, {contradictions} contradictions, and {neutral} neutral decisions.",
        hypothesis,
        validator,
        f"Earliest bottleneck: {primary.value}; recommended owner: {component.value}.",
    ]


def _enum_counts(values: Any, members: Sequence[StrEnum]) -> dict[str, int]:
    counts = Counter(values)
    return {member.value: counts[member] for member in members}


def _coverage_counts(values: Any) -> dict[str, int]:
    counts = {"0": 0, "1": 0, ">=2": 0}
    for value in values:
        if value == 0:
            counts["0"] += 1
        elif value == 1:
            counts["1"] += 1
        else:
            counts[">=2"] += 1
    return counts


def _confidence_counts(values: Any) -> dict[str, int]:
    counts = {"<0.50": 0, "0.50-0.59": 0, "0.60-0.74": 0, ">=0.75": 0}
    for value in values:
        if value is None:
            continue
        if value < 0.50:
            counts["<0.50"] += 1
        elif value < 0.60:
            counts["0.50-0.59"] += 1
        elif value < SUPPORTED_CONFIDENCE_THRESHOLD:
            counts["0.60-0.74"] += 1
        else:
            counts[">=0.75"] += 1
    return counts


def _reason_counts(reasons: Any) -> list[ReasonCount]:
    counts = Counter(reasons)
    return [
        ReasonCount(reason=reason, count=count)
        for reason, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    ]


def _fault_family_audits(
    cases: Sequence[CaseAbstentionAudit],
) -> list[FaultFamilyAudit]:
    grouped: dict[str, list[CaseAbstentionAudit]] = {
        f"F{number:02d}": [] for number in range(1, 13)
    }
    for case in cases:
        grouped[case.fault_id].append(case)
    output: list[FaultFamilyAudit] = []
    for fault_id, fault_cases in grouped.items():
        counts = Counter(case.primary_abstention_cause for case in fault_cases)
        dominant = (
            next(
                cause
                for cause in TAXONOMY_ORDER
                if counts[cause] == max(counts.values())
            )
            if fault_cases
            else None
        )
        output.append(
            FaultFamilyAudit(
                fault_id=fault_id,
                no_error_abstentions=len(fault_cases),
                dominant_cause=dominant,
                cause_counts={cause.value: counts[cause] for cause in TAXONOMY_ORDER},
            )
        )
    return output


def _worst_fault_families(cases: Sequence[CaseAbstentionAudit]) -> list[str]:
    counts = Counter(case.fault_id for case in cases)
    return [
        fault_id
        for fault_id, _ in sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:3]
    ]


def _portable_path(path: Path) -> str:
    try:
        return path.resolve().relative_to(Path.cwd().resolve()).as_posix()
    except ValueError:
        return path.resolve().as_posix()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("results", type=Path, help="frozen Full Harness results.jsonl")
    parser.add_argument(
        "--ground-truth-directory",
        type=Path,
        default=DEFAULT_GROUND_TRUTH_DIRECTORY,
    )
    parser.add_argument(
        "--fault-catalog", type=Path, default=DEFAULT_FAULT_CATALOG_PATH
    )
    parser.add_argument(
        "--cases-directory", type=Path, default=DEFAULT_CASES_DIRECTORY
    )
    parser.add_argument("--expected-run-id")
    parser.add_argument("--output", type=Path, help="write the JSON audit artifact")
    parser.add_argument("--json", action="store_true", help="print JSON to stdout")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    report = audit_full_harness_abstentions(
        args.results,
        ground_truth_directory=args.ground_truth_directory,
        fault_catalog_path=args.fault_catalog,
        cases_directory=args.cases_directory,
        expected_run_id=args.expected_run_id,
    )
    payload = report.model_dump_json(indent=2)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload + "\n", encoding="utf-8", newline="\n")
    if args.json:
        print(payload)
    else:
        print(f"run_id: {report.run_id}")
        print(f"raw SHA256: {report.raw_result_sha256}")
        print(f"eligible abstentions: {report.eligible_case_count}")
        for cause, count in report.taxonomy_counts.items():
            print(f"{cause}: {count}")
        if args.output is not None:
            print(f"JSON: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "AUDIT_SCHEMA_VERSION",
    "AbstentionAuditError",
    "AbstentionAuditReport",
    "AbstentionCause",
    "AbstentionSignals",
    "AuditComponent",
    "CaseAbstentionAudit",
    "audit_full_harness_abstentions",
    "classify_abstention",
]
