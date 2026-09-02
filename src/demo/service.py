"""Backend orchestration for the deterministic canonical incident demo."""

from __future__ import annotations

import csv
import json
import os
from datetime import UTC, date, datetime
from pathlib import Path
from threading import RLock
from typing import Any, Final, cast
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, JsonValue, ValidationError

from benchmark.case_generator import (
    load_case_manifest,
    load_case_manifests,
    materialize_case,
)
from benchmark.runner import (
    BenchmarkRunConfig,
    _smoke_model_client_factory,
    build_harness_executor,
    build_runtime_input,
)
from data.generator import generate_dataset, write_outputs
from demo.models import (
    DemoAlertView,
    DemoApprovalView,
    DemoBenchmarkRow,
    DemoBenchmarkSnapshot,
    DemoCaseList,
    DemoCaseSummary,
    DemoEvidenceView,
    DemoHypothesisView,
    DemoIncidentList,
    DemoIncidentSummary,
    DemoIncidentView,
    DemoPlanStepView,
    DemoPostValidationView,
    DemoProposalView,
    DemoRepairView,
    DemoRootCauseView,
    DemoToolTraceView,
)
from harness.approval import record_approval
from harness.graph import HarnessGraph
from harness.hypothesis import EvidenceReference, HypothesisManager
from harness.post_validation import PostRepairValidator
from harness.repair import (
    ApprovalDecision,
    ApprovalOutcome,
    RepairProposal,
    SandboxRun,
    SandboxRunStatus,
)
from harness.repair_proposal import RepairProposalBuilder
from harness.sandbox_repair import SandboxRepairExecutor
from harness.state import IncidentState, IncidentStatus


def _asset_root() -> Path:
    candidates = (Path.cwd().resolve(), Path(__file__).parents[2].resolve())
    for candidate in candidates:
        if (candidate / "benchmark" / "cases").is_dir() and (
            candidate / "experiments" / "ablation" / "reports"
        ).is_dir():
            return candidate
    return candidates[0]


ROOT: Final[Path] = _asset_root()
DEFAULT_CASES_DIRECTORY: Final[Path] = ROOT / "benchmark" / "cases"
DEFAULT_REPORT_DIRECTORY: Final[Path] = (
    ROOT
    / "experiments"
    / "ablation"
    / "reports"
    / "full-60-4arch-post-pr14-20260831"
)
INTERACTIVE_CASE_IDS: Final[frozenset[str]] = frozenset(
    f"F01-{index:03d}" for index in range(1, 6)
)
_DISPLAY_NAMES: Final[dict[str, str]] = {
    "single_prompt": "Single Prompt",
    "react": "ReAct",
    "state_graph_no_validator": "State Graph No Validator",
    "full_harness": "Full Harness",
}


class DemoServiceError(RuntimeError):
    """Base error for a request that the demo service cannot fulfill."""


class DemoCaseNotFoundError(DemoServiceError):
    """Raised when a requested canonical case does not exist."""


class DemoCaseUnsupportedError(DemoServiceError):
    """Raised when a canonical case has no validated interactive workflow."""


class DemoIncidentNotFoundError(DemoServiceError):
    """Raised when a UUID has no persisted demo session."""


class DemoIncidentConflictError(DemoServiceError):
    """Raised when an incident can no longer accept the requested decision."""


class DemoSessionError(DemoServiceError):
    """Raised when a persisted demo session is malformed."""


class _DemoSession(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    incident_id: str
    case_id: str
    created_at: datetime
    updated_at: datetime
    state: IncidentState


class DemoService:
    """Run the current Harness and expose presentation-safe derived views."""

    def __init__(
        self,
        *,
        workdir: str | Path | None = None,
        cases_directory: str | Path = DEFAULT_CASES_DIRECTORY,
        report_directory: str | Path = DEFAULT_REPORT_DIRECTORY,
    ) -> None:
        configured_workdir = workdir or os.getenv("DEMO_WORKDIR", "data/demo")
        self.workdir = Path(configured_workdir).resolve()
        self.cases_directory = Path(cases_directory).resolve()
        self.report_directory = Path(report_directory).resolve()
        self.incidents_directory = self.workdir / "incidents"
        self.incidents_directory.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()

    def list_cases(self) -> DemoCaseList:
        manifests = load_case_manifests(self.cases_directory)
        return DemoCaseList(
            cases=[self._case_summary(manifest) for manifest in manifests]
        )

    def list_incidents(self) -> DemoIncidentList:
        sessions = [
            self._read_session(path.parent.name)
            for path in self.incidents_directory.glob("*/session.json")
        ]
        sessions.sort(key=lambda item: item.updated_at, reverse=True)
        return DemoIncidentList(
            incidents=[
                DemoIncidentSummary(
                    incident_id=item.incident_id,
                    case_id=item.case_id,
                    created_at=item.created_at,
                    updated_at=item.updated_at,
                    status=item.state.status.value,
                    final_status=(
                        item.state.final_status.value
                        if item.state.final_status is not None
                        else None
                    ),
                )
                for item in sessions
            ]
        )

    def start_incident(self, case_id: str) -> DemoIncidentView:
        manifest = self._load_manifest(case_id)
        if manifest.case_id not in INTERACTIVE_CASE_IDS:
            raise DemoCaseUnsupportedError(
                f"interactive diagnosis is not enabled for {manifest.case_id}"
            )

        incident_id = str(uuid4())
        incident_directory = self._incident_directory(incident_id)
        fault_directory = incident_directory / "fault"
        baseline_directory = incident_directory / "baseline"
        baseline = generate_dataset(
            manifest.baseline_user_count,
            manifest.baseline_days,
            manifest.baseline_event_count,
            manifest.baseline_seed,
            datetime.combine(manifest.baseline_start_date, datetime.min.time()),
        )
        fault = materialize_case(manifest, baseline_tables=baseline)
        write_outputs(fault_directory, fault.tables)
        write_outputs(baseline_directory, baseline)

        config = BenchmarkRunConfig(
            case_ids=[manifest.case_id],
            harness_version="current-main",
            model_name="deterministic-smoke",
            model_provider="mock",
            output_dir=incident_directory / "runner-output",
        )
        runtime_input = build_runtime_input(
            manifest,
            fault_directory / "datasherlock.duckdb",
            run_id=incident_id,
            config=config,
        )
        diagnosis = build_harness_executor(
            config,
            model_client_factory=_smoke_model_client_factory,
        ).execute(runtime_input)
        state = IncidentState.from_dict(diagnosis.trace_payload["state"])
        if (
            state.status is not IncidentStatus.ROOT_CAUSE_FOUND
            or state.root_cause is None
            or state.root_cause.get("root_cause_type") != "missing_partition"
        ):
            raise DemoServiceError(
                f"{manifest.case_id} did not reach an authorized F01 diagnosis"
            )

        manager = HypothesisManager()
        graph = HarnessGraph(hypothesis_manager=manager)
        graph.restore_runtime(state)
        proposal = RepairProposalBuilder(hypothesis_manager=manager).build(state)
        graph.propose_fix(state, proposal.model_dump(mode="json"))
        now = datetime.now(UTC)
        session = _DemoSession(
            incident_id=incident_id,
            case_id=manifest.case_id,
            created_at=now,
            updated_at=now,
            state=state,
        )
        self._write_session(session)
        return self._incident_view(session)

    def get_incident(self, incident_id: str) -> DemoIncidentView:
        return self._incident_view(self._read_session(incident_id))

    def decide_incident(
        self,
        incident_id: str,
        *,
        reviewer: str,
        outcome: str,
        comment: str | None = None,
    ) -> DemoIncidentView:
        with self._lock:
            session = self._read_session(incident_id)
            state = session.state
            if state.status is not IncidentStatus.AWAITING_APPROVAL:
                raise DemoIncidentConflictError(
                    f"incident is already in {state.status.value}"
                )
            if state.fix_proposal is None:
                raise DemoSessionError("incident has no persisted repair proposal")
            try:
                proposal = RepairProposal.model_validate(state.fix_proposal)
                approval_outcome = ApprovalOutcome(outcome)
                decision = ApprovalDecision.for_proposal(
                    proposal,
                    decision_id=f"AD-{uuid4()}",
                    reviewer=reviewer,
                    outcome=approval_outcome,
                    comment=comment,
                )
            except (TypeError, ValueError, ValidationError) as exc:
                raise DemoServiceError(str(exc)) from exc

            graph = HarnessGraph(hypothesis_manager=HypothesisManager())
            graph.restore_runtime(state)
            record_approval(graph, state, proposal, decision)
            if approval_outcome is ApprovalOutcome.REJECTED:
                session.updated_at = datetime.now(UTC)
                self._write_session(session)
                return self._incident_view(session)

            incident_directory = self._incident_directory(incident_id)
            fault_database = incident_directory / "fault" / "datasherlock.duckdb"
            baseline_database = incident_directory / "baseline" / "datasherlock.duckdb"
            executor = SandboxRepairExecutor(
                fault_database,
                incident_directory / "sandbox",
                repair_source_database_path=baseline_database,
            )
            placeholder = SandboxRun(
                run_id=f"SR-{uuid4()}",
                incident_id=proposal.incident_id,
                proposal_id=proposal.proposal_id,
                proposal_hash=proposal.proposal_hash,
                approval_decision_id=decision.decision_id,
                action=proposal.action,
                sandbox_path="placeholder",
            )
            pending_run = SandboxRun.for_approved_proposal(
                proposal,
                decision,
                run_id=placeholder.run_id,
                sandbox_path=str(executor.sandbox_path_for(placeholder)),
            )
            graph.record_pending_repair_run(state, pending_run)
            session.updated_at = datetime.now(UTC)
            self._write_session(session)

            repaired = executor.execute(proposal, pending_run)
            graph.record_repair_result(state, result=repaired.model_dump(mode="json"))
            session.updated_at = datetime.now(UTC)
            self._write_session(session)
            if repaired.status is not SandboxRunStatus.SUCCEEDED:
                return self._incident_view(session)

            alert_date = date.fromisoformat(str(state.alert["observed_at"])[:10])
            validation = PostRepairValidator(fault_database).validate(
                proposal,
                repaired,
                metric_id=str(state.alert["metric"]),
                metric_date=alert_date,
                expected_value=float(state.alert["expected_value"]),
                validation_id=f"PV-{uuid4()}",
            )
            graph.record_post_validation_result(
                state,
                validated=validation.status.value == "passed",
                result=validation.model_dump(mode="json"),
            )
            session.updated_at = datetime.now(UTC)
            self._write_session(session)
            return self._incident_view(session)

    def benchmark_snapshot(self) -> DemoBenchmarkSnapshot:
        config_path = self.report_directory / "public-config.json"
        comparison_path = self.report_directory / "comparison.csv"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        with comparison_path.open(encoding="utf-8", newline="") as handle:
            source_rows = list(csv.DictReader(handle))
        rows_by_variant = {row["Variant"]: row for row in source_rows}
        rows = [
            self._benchmark_row(variant, rows_by_variant[variant])
            for variant in config["variant_order"]
        ]
        return DemoBenchmarkSnapshot(
            run_id=str(config["run_id"]),
            source_commit=str(config["source_commit"]),
            rows=rows,
        )

    def _load_manifest(self, case_id: str) -> Any:
        try:
            return load_case_manifest(case_id, directory=self.cases_directory)
        except (FileNotFoundError, ValueError) as exc:
            raise DemoCaseNotFoundError(f"unknown canonical case: {case_id}") from exc

    def _incident_directory(self, incident_id: str) -> Path:
        try:
            normalized = str(UUID(incident_id))
        except (TypeError, ValueError, AttributeError) as exc:
            raise DemoIncidentNotFoundError("incident ID must be a UUID") from exc
        target = (self.incidents_directory / normalized).resolve()
        if target.parent != self.incidents_directory.resolve():
            raise DemoIncidentNotFoundError("incident path is outside the demo workspace")
        return target

    def _read_session(self, incident_id: str) -> _DemoSession:
        path = self._incident_directory(incident_id) / "session.json"
        if not path.is_file():
            raise DemoIncidentNotFoundError(f"unknown demo incident: {incident_id}")
        try:
            return _DemoSession.model_validate_json(path.read_bytes())
        except (OSError, ValidationError, ValueError) as exc:
            raise DemoSessionError(f"invalid demo session: {incident_id}") from exc

    def _write_session(self, session: _DemoSession) -> None:
        directory = self._incident_directory(session.incident_id)
        directory.mkdir(parents=True, exist_ok=True)
        destination = directory / "session.json"
        temporary = directory / "session.json.tmp"
        payload = session.model_dump_json(indent=2).encode("utf-8")
        with temporary.open("wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)

    def _incident_view(self, session: _DemoSession) -> DemoIncidentView:
        state = session.state
        case = self._case_summary(self._load_manifest(session.case_id))
        proposal = self._proposal_view(state)
        approval = self._approval_view(state)
        repair = self._repair_view(state)
        post_validation = self._post_validation_view(state)
        evidence = self._evidence_views(state)
        root_cause = self._root_cause_view(state, proposal)
        terminal = state.status.is_terminal
        final_status = (
            state.final_status.value if state.final_status is not None else None
        )
        alert = DemoAlertView.model_validate(
            {key: state.alert[key] for key in DemoAlertView.model_fields}
        )
        final_report: dict[str, JsonValue] | None = None
        if terminal:
            final_report = cast(
                dict[str, JsonValue],
                {
                    "incident_id": session.incident_id,
                    "case_id": session.case_id,
                    "final_status": final_status or state.status.value,
                    "alert": alert.model_dump(mode="json"),
                    "root_cause": (
                        root_cause.model_dump(mode="json") if root_cause else None
                    ),
                    "evidence": [item.model_dump(mode="json") for item in evidence],
                    "approval": approval.model_dump(mode="json") if approval else None,
                    "repair": repair.model_dump(mode="json") if repair else None,
                    "post_validation": (
                        post_validation.model_dump(mode="json")
                        if post_validation
                        else None
                    ),
                },
            )
        return DemoIncidentView(
            incident_id=session.incident_id,
            case=case,
            created_at=session.created_at,
            updated_at=session.updated_at,
            status=state.status.value,
            final_status=final_status,
            alert=alert,
            plan=self._plan_views(state),
            tool_trace=self._tool_trace_views(state),
            hypotheses=self._hypothesis_views(state),
            evidence=evidence,
            root_cause=root_cause,
            repair_proposal=proposal,
            approval=approval,
            repair=repair,
            post_validation=post_validation,
            can_approve=state.status is IncidentStatus.AWAITING_APPROVAL,
            terminal=terminal,
            final_report=final_report,
        )

    @staticmethod
    def _case_summary(manifest: Any) -> DemoCaseSummary:
        alert = manifest.original_alert
        interactive = manifest.case_id in INTERACTIVE_CASE_IDS
        return DemoCaseSummary(
            case_id=manifest.case_id,
            metric=alert.metric,
            observed_at=alert.observed_at,
            observed_value=alert.observed_value,
            expected_value=alert.expected_value,
            change_rate=alert.change_rate,
            severity=alert.severity,
            interactive_supported=interactive,
            repair_supported=interactive,
        )

    @staticmethod
    def _plan_views(state: IncidentState) -> list[DemoPlanStepView]:
        completed = len(state.tool_trace)
        result: list[DemoPlanStepView] = []
        for index, payload in enumerate(state.plan):
            arguments = payload.get("arguments", {})
            sql = arguments.get("sql") if isinstance(arguments, dict) else None
            result.append(
                DemoPlanStepView(
                    step_id=str(payload["step_id"]),
                    hypothesis_id=str(payload["hypothesis_id"]),
                    purpose=str(payload["purpose"]),
                    tool=str(payload["tool"]),
                    expected_evidence=[str(item) for item in payload["expected_evidence"]],
                    execution_status="completed" if index < completed else "pending",
                    sql=str(sql) if isinstance(sql, str) else None,
                )
            )
        return result

    @staticmethod
    def _tool_trace_views(state: IncidentState) -> list[DemoToolTraceView]:
        result: list[DemoToolTraceView] = []
        for position, payload in enumerate(state.tool_trace, start=1):
            raw_result = payload.get("result")
            row_count = (
                raw_result.get("row_count")
                if isinstance(raw_result, dict)
                else None
            )
            if not isinstance(row_count, int) or isinstance(row_count, bool):
                row_count = None
            summary = (
                f"Read-only query returned {row_count} row(s)."
                if row_count is not None
                else ("Tool call succeeded." if payload.get("success") else "Tool call failed.")
            )
            validation = payload.get("sql_validation")
            error = payload.get("error")
            result.append(
                DemoToolTraceView(
                    position=position,
                    tool=str(payload.get("tool_name", "unknown")),
                    success=bool(payload.get("success")),
                    query_id=(
                        str(payload["query_id"])
                        if isinstance(payload.get("query_id"), str)
                        else None
                    ),
                    validation=(validation if isinstance(validation, dict) else None),
                    row_count=row_count,
                    result_summary=summary,
                    error=error if isinstance(error, dict) else None,
                    raw_result=cast(JsonValue | None, raw_result),
                )
            )
        return result

    @staticmethod
    def _hypothesis_views(state: IncidentState) -> list[DemoHypothesisView]:
        return [
            DemoHypothesisView(
                hypothesis_id=str(payload["hypothesis_id"]),
                root_cause_type=str(payload["root_cause_type"]),
                description=str(payload["description"]),
                status=str(payload["status"]),
                confidence=float(payload["confidence"]),
                supporting_evidence_ids=[
                    str(item) for item in payload.get("supporting_evidence_ids", [])
                ],
                contradicting_evidence_ids=[
                    str(item) for item in payload.get("contradicting_evidence_ids", [])
                ],
            )
            for payload in state.hypotheses
        ]

    @staticmethod
    def _evidence_views(state: IncidentState) -> list[DemoEvidenceView]:
        result: list[DemoEvidenceView] = []
        for payload in state.evidence:
            try:
                reference = EvidenceReference.model_validate(payload)
            except (TypeError, ValidationError, ValueError):
                continue
            result.append(
                DemoEvidenceView(
                    evidence_id=reference.evidence_id,
                    source_type=reference.source_type,
                    finding=reference.description,
                    query_id=reference.query_id,
                    observation=reference.observation,
                )
            )
        return result

    @staticmethod
    def _proposal_view(state: IncidentState) -> DemoProposalView | None:
        if state.fix_proposal is None:
            return None
        proposal = RepairProposal.model_validate(state.fix_proposal)
        return DemoProposalView(
            proposal_id=proposal.proposal_id,
            action=proposal.action.value,
            affected_assets=list(proposal.affected_assets),
            risk=proposal.risk.value,
            rationale=proposal.rationale,
            evidence_bindings=list(proposal.supporting_evidence_ids),
            parameters=proposal.parameters,
            valid_until=proposal.valid_until,
        )

    @staticmethod
    def _root_cause_view(
        state: IncidentState,
        proposal: DemoProposalView | None,
    ) -> DemoRootCauseView | None:
        if state.root_cause is None:
            return None
        return DemoRootCauseView(
            root_cause_type=str(state.root_cause["root_cause_type"]),
            confidence=float(state.root_cause["confidence"]),
            affected_assets=proposal.affected_assets if proposal else [],
            supporting_evidence_ids=[
                str(item)
                for item in state.root_cause.get("supporting_evidence_ids", [])
            ],
            independent_source_types=[
                str(item)
                for item in state.root_cause.get("independent_source_types", [])
            ],
        )

    @staticmethod
    def _approval_view(state: IncidentState) -> DemoApprovalView | None:
        if state.approval is None:
            return None
        return DemoApprovalView(
            decision_id=str(state.approval["decision_id"]),
            reviewer=str(state.approval["reviewer"]),
            outcome=str(state.approval["outcome"]),
            comment=(
                str(state.approval["comment"])
                if state.approval.get("comment") is not None
                else None
            ),
            decided_at=cast(datetime | str, state.approval["decided_at"]),
        )

    @staticmethod
    def _repair_payload(state: IncidentState) -> dict[str, JsonValue] | None:
        payload = state.repair_result
        if not isinstance(payload, dict):
            return None
        nested = payload.get("sandbox_run")
        if isinstance(nested, dict):
            return nested
        if isinstance(payload.get("run_id"), str):
            return payload
        return None

    @classmethod
    def _repair_view(cls, state: IncidentState) -> DemoRepairView | None:
        payload = cls._repair_payload(state)
        if payload is None:
            return None
        return DemoRepairView(
            run_id=str(payload["run_id"]),
            action=str(payload["action"]),
            status=str(payload["status"]),
            handler_invocation_count=int(payload.get("handler_invocation_count", 0)),
            source_hash_before=cast(str | None, payload.get("source_hash_before")),
            source_hash_after=cast(str | None, payload.get("source_hash_after")),
            sandbox_hash_before=cast(str | None, payload.get("sandbox_hash_before")),
            sandbox_hash_after=cast(str | None, payload.get("sandbox_hash_after")),
            changed_row_counts=cast(dict[str, int], payload.get("changed_row_counts", {})),
            operation_details=cast(
                dict[str, JsonValue], payload.get("operation_details", {})
            ),
            error=cast(str | None, payload.get("error")),
        )

    @staticmethod
    def _post_validation_view(
        state: IncidentState,
    ) -> DemoPostValidationView | None:
        if not isinstance(state.repair_result, dict):
            return None
        payload = state.repair_result.get("post_validation")
        if not isinstance(payload, dict):
            return None
        return DemoPostValidationView(
            validation_id=str(payload["validation_id"]),
            sandbox_run_id=str(payload["sandbox_run_id"]),
            metric_id=str(payload["metric_id"]),
            observed_before=float(payload["observed_before"]),
            observed_after=float(payload["observed_after"]),
            target_met=bool(payload["target_met"]),
            regressions=[str(item) for item in payload.get("regressions", [])],
            status=str(payload["status"]),
            summary=str(payload["summary"]),
            validated_at=cast(datetime | str, payload["validated_at"]),
        )

    @staticmethod
    def _benchmark_row(variant: str, row: dict[str, str]) -> DemoBenchmarkRow:
        return DemoBenchmarkRow(
            variant=variant,
            display_name=_DISPLAY_NAMES.get(variant, variant.replace("_", " ").title()),
            top_1=float(row["Top-1"]),
            top_3=float(row["Top-3"]),
            invalid_sql_rate=float(row["Invalid SQL rate"]),
            unsafe_rate=float(row["Unsafe operation rate"]),
            duplicate_rate=float(row["Duplicate operation rate"]),
            avg_tool_calls=float(row["Avg tool calls"]),
            avg_sql_calls=float(row["Avg SQL calls"]),
            mean_latency_ms=float(row["Mean latency"]),
            p50_latency_ms=float(row["P50 latency"]),
            p95_latency_ms=float(row["P95 latency"]),
            errors=int(row["Errors"]),
            timeouts=int(row["Timeouts"]),
            abstentions=int(row["Abstentions"]),
        )


__all__ = [
    "DemoCaseNotFoundError",
    "DemoCaseUnsupportedError",
    "DemoIncidentConflictError",
    "DemoIncidentNotFoundError",
    "DemoService",
    "DemoServiceError",
]
