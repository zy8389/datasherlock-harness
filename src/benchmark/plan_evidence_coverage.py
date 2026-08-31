"""Offline evidence-source coverage audit for frozen Planner traces."""

from __future__ import annotations

import argparse
import hashlib
from collections import Counter
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from agents.planner import InvestigationPlan, infer_step_evidence_source
from benchmark.ablation import AblationCaseResult
from config.faults import EvidenceSourceType, load_fault_catalog
from tools.registry import build_default_tool_registry


class PlanCoverageAuditError(ValueError):
    """Raised when a frozen trace cannot support a deterministic audit."""


class CasePlanCoverage(BaseModel):
    """Evidence-source coverage for one declared golden candidate."""

    model_config = ConfigDict(extra="forbid")

    case_id: str
    fault_id: str
    golden_root_cause: str
    golden_hypothesis_id: str
    required_sources: list[str]
    planned_sources: list[str]
    missing_sources: list[str]
    source_by_step: dict[str, str | None]
    coverage_complete: bool


class FaultPlanCoverage(BaseModel):
    """Aggregate coverage for one fault family."""

    model_config = ConfigDict(extra="forbid")

    fault_id: str
    root_cause_type: str
    candidates: int = Field(ge=0)
    coverage_complete: int = Field(ge=0)
    coverage_incomplete: int = Field(ge=0)
    missing_source_counts: dict[str, int]


class FrozenPlanCoverageAudit(BaseModel):
    """Serializable aggregate for one immutable Full Harness result file."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    raw_artifact: str
    raw_sha256: str
    record_count: int = Field(ge=0)
    golden_hypothesis_present_cases: int = Field(ge=0)
    golden_hypothesis_missing_cases: int = Field(ge=0)
    declared_multisource_golden_candidates: int = Field(ge=0)
    coverage_complete: int = Field(ge=0)
    coverage_incomplete: int = Field(ge=0)
    missing_source_counts: dict[str, int]
    undeclared_source_contract_faults: list[str]
    cases: list[CasePlanCoverage]
    per_fault: list[FaultPlanCoverage]


def audit_frozen_plan_evidence_coverage(
    results_path: str | Path,
    *,
    run_id: str,
) -> FrozenPlanCoverageAudit:
    """Audit planned source paths without executing a model, tool, or Harness."""

    path = Path(results_path)
    raw = path.read_bytes()
    records = [
        AblationCaseResult.model_validate_json(line)
        for line in raw.splitlines()
        if line.strip()
    ]
    if not records:
        raise PlanCoverageAuditError(f"no result records found in {path}")
    if {str(record.variant) for record in records} != {"full_harness"}:
        raise PlanCoverageAuditError(
            "audit input must contain only full_harness records"
        )
    case_ids = [record.case_id for record in records]
    if len(case_ids) != len(set(case_ids)):
        raise PlanCoverageAuditError("audit input contains duplicate case IDs")

    catalog = load_fault_catalog()
    registry = build_default_tool_registry()
    source_names = [source.value for source in EvidenceSourceType]
    missing_counts: Counter[str] = Counter()
    per_fault_cases: dict[str, list[CasePlanCoverage]] = {}
    audited_cases: list[CasePlanCoverage] = []
    golden_present = 0

    for record in records:
        fault = catalog.by_id(record.fault_id)
        if record.expected_root_cause != fault.root_cause_type:
            raise PlanCoverageAuditError(
                f"{record.case_id} expected root cause differs from {record.fault_id}"
            )
        planner = record.trace_payload.get("planner")
        if not isinstance(planner, dict) or not isinstance(planner.get("plan"), dict):
            raise PlanCoverageAuditError(
                f"{record.case_id} has no complete frozen Planner plan"
            )
        plan = InvestigationPlan.model_validate(planner["plan"])
        golden = [
            hypothesis
            for hypothesis in plan.hypotheses
            if hypothesis.root_cause_type == record.expected_root_cause
        ]
        if len(golden) > 1:
            raise PlanCoverageAuditError(
                f"{record.case_id} proposes the golden root cause more than once"
            )
        if not golden:
            continue
        golden_present += 1

        required = set(fault.evidence_source_types)
        if len(required) < 2:
            continue
        hypothesis = golden[0]
        source_by_step: dict[str, str | None] = {}
        planned: set[EvidenceSourceType] = set()
        for step in plan.steps:
            if step.hypothesis_id != hypothesis.hypothesis_id:
                continue
            source = infer_step_evidence_source(step, registry)
            source_by_step[step.step_id] = source.value if source is not None else None
            if source is not None:
                planned.add(source)

        missing = required.difference(planned)
        for source in missing:
            missing_counts[source.value] += 1
        case = CasePlanCoverage(
            case_id=record.case_id,
            fault_id=record.fault_id,
            golden_root_cause=record.expected_root_cause,
            golden_hypothesis_id=hypothesis.hypothesis_id,
            required_sources=sorted(source.value for source in required),
            planned_sources=sorted(source.value for source in planned),
            missing_sources=sorted(source.value for source in missing),
            source_by_step=source_by_step,
            coverage_complete=not missing,
        )
        audited_cases.append(case)
        per_fault_cases.setdefault(record.fault_id, []).append(case)

    per_fault: list[FaultPlanCoverage] = []
    for fault in catalog.faults:
        cases = per_fault_cases.get(fault.id, [])
        if len(set(fault.evidence_source_types)) < 2:
            continue
        fault_missing = Counter(
            source for case in cases for source in case.missing_sources
        )
        per_fault.append(
            FaultPlanCoverage(
                fault_id=fault.id,
                root_cause_type=fault.root_cause_type,
                candidates=len(cases),
                coverage_complete=sum(case.coverage_complete for case in cases),
                coverage_incomplete=sum(not case.coverage_complete for case in cases),
                missing_source_counts={
                    source: fault_missing[source] for source in source_names
                },
            )
        )

    return FrozenPlanCoverageAudit(
        run_id=run_id,
        raw_artifact=path.as_posix(),
        raw_sha256=hashlib.sha256(raw).hexdigest(),
        record_count=len(records),
        golden_hypothesis_present_cases=golden_present,
        golden_hypothesis_missing_cases=len(records) - golden_present,
        declared_multisource_golden_candidates=len(audited_cases),
        coverage_complete=sum(case.coverage_complete for case in audited_cases),
        coverage_incomplete=sum(not case.coverage_complete for case in audited_cases),
        missing_source_counts={
            source: missing_counts[source] for source in source_names
        },
        undeclared_source_contract_faults=[
            f"{fault.id} {fault.root_cause_type}"
            for fault in catalog.faults
            if not fault.evidence_source_types
        ],
        cases=audited_cases,
        per_fault=per_fault,
    )


def render_frozen_plan_coverage_markdown(audit: FrozenPlanCoverageAudit) -> str:
    """Render the deterministic audit as a reviewable Markdown report."""

    lines = [
        "# Frozen Planner Evidence-Source Coverage Audit",
        "",
        "## Scope",
        "",
        (
            "This is a read-only static analysis of the immutable Full Harness Planner "
            "traces. It does not execute a model, tool, Harness step, or benchmark case, "
            "and it does not rewrite historical outcomes. Ground Truth is not injected "
            "into any runtime component; the frozen result's expected label is used only "
            "to select the golden candidate for offline measurement."
        ),
        "",
        "| Identity | Value |",
        "| --- | --- |",
        f"| Run ID | `{audit.run_id}` |",
        f"| Raw artifact | `{audit.raw_artifact}` |",
        f"| Raw SHA256 | `{audit.raw_sha256}` |",
        f"| Full Harness records | {audit.record_count} |",
        "",
        "## Aggregate Coverage",
        "",
        "| Funnel stage | Count |",
        "| --- | ---: |",
        (
            "| Golden hypothesis present | "
            f"{audit.golden_hypothesis_present_cases} / {audit.record_count} |"
        ),
        (
            "| Golden hypothesis missing | "
            f"{audit.golden_hypothesis_missing_cases} / {audit.record_count} |"
        ),
        (
            "| Declared multi-source golden candidates | "
            f"{audit.declared_multisource_golden_candidates} |"
        ),
        f"| Coverage complete | {audit.coverage_complete} |",
        f"| Coverage incomplete | {audit.coverage_incomplete} |",
        "",
        (
            "A source counts only when one distinct step resolves to one canonical source. "
            "Queries over unknown assets and SQL that mixes source classes count as "
            "unclassified, never as independent coverage."
        ),
        "",
        "## Missing Sources",
        "",
        "| Source | Missing cases |",
        "| --- | ---: |",
    ]
    lines.extend(
        f"| `{source}` | {count} |"
        for source, count in audit.missing_source_counts.items()
    )
    lines.extend(
        [
            "",
            "## Per-Fault Coverage",
            "",
            "| Fault | Root cause | Candidates | Complete | Incomplete | Missing sources |",
            "| --- | --- | ---: | ---: | ---: | --- |",
        ]
    )
    for fault in audit.per_fault:
        missing = (
            ", ".join(
                f"{source}={count}"
                for source, count in fault.missing_source_counts.items()
                if count
            )
            or "None"
        )
        lines.append(
            f"| `{fault.fault_id}` | `{fault.root_cause_type}` | {fault.candidates} | "
            f"{fault.coverage_complete} | {fault.coverage_incomplete} | {missing} |"
        )

    lines.extend(
        [
            "",
            "## Case Detail",
            "",
            "| Case | Fault | Required | Planned | Missing | Complete |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
    )
    for case in audit.cases:
        lines.append(
            f"| `{case.case_id}` | `{case.fault_id}` | "
            f"{', '.join(case.required_sources)} | "
            f"{', '.join(case.planned_sources) or 'None'} | "
            f"{', '.join(case.missing_sources) or 'None'} | "
            f"{'YES' if case.coverage_complete else 'NO'} |"
        )

    lines.extend(
        [
            "",
            "## Undeclared Source-Contract Debt",
            "",
            (
                "These catalog families declare no `evidence_source_types`. This PR does "
                "not guess or add contracts for them; they receive only the universal "
                "requirement that every proposed hypothesis has at least one investigation "
                "step."
            ),
            "",
        ]
    )
    lines.extend(f"- `{fault}`" for fault in audit.undeclared_source_contract_faults)
    lines.extend(
        [
            "",
            "## Interpretation",
            "",
            (
                "The report measures whether a frozen plan was capable of reaching every "
                "catalog-declared independent source. It does not claim that a planned "
                "query would produce supporting evidence, raise confidence, or pass the "
                "Validator. Those remain runtime admission and authorization decisions."
            ),
            "",
            (
                "This audit analyzes the immutable post-PR14 frozen run. Later PRs do not "
                "rewrite its traces, scores, errors, or abstentions."
            ),
            "",
        ]
    )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("results", type=Path)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    audit = audit_frozen_plan_evidence_coverage(args.results, run_id=args.run_id)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        render_frozen_plan_coverage_markdown(audit),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()


__all__ = [
    "CasePlanCoverage",
    "FaultPlanCoverage",
    "FrozenPlanCoverageAudit",
    "PlanCoverageAuditError",
    "audit_frozen_plan_evidence_coverage",
    "render_frozen_plan_coverage_markdown",
]
