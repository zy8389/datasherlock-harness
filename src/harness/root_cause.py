"""Derive a root cause exclusively from trace-bound diagnostic evidence."""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Iterable
from typing import Final

from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError

from config.faults import (
    DEFAULT_FAULT_CATALOG_PATH,
    EvidenceSourceType,
    load_fault_catalog,
)
from harness.repair import RepairEvidence
from harness.repair_proposal import MissingPartitionContext
from harness.state import IncidentState, IncidentStatus

_CONFIRMABLE_STATUSES: Final[frozenset[IncidentStatus]] = frozenset(
    {
        IncidentStatus.TRIAGE,
        IncidentStatus.PLANNING,
        IncidentStatus.EXECUTING,
        IncidentStatus.VALIDATING,
        IncidentStatus.HYPOTHESIS_TESTING,
    }
)


class RootCauseConfirmationError(ValueError):
    """Raised when trace-bound diagnostics cannot establish one root cause."""


class DiagnosticEvidenceBinding(BaseModel):
    """Server-owned classification attached when a diagnostic tool succeeds.

    This type is deliberately not an API request model. It is passed by the
    trusted investigation orchestration when it dispatches a specific tool
    step, then persisted beside the result that actually produced the evidence.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    root_cause_type: str = Field(min_length=1)
    source_type: EvidenceSourceType
    asset: str = Field(min_length=1)
    repair_context: dict[str, JsonValue] = Field(default_factory=dict)


class RootCauseConfirmationService:
    """Promote only one catalog-valid, trace-backed diagnostic candidate."""

    def __init__(self, *, fault_catalog_path: str = str(DEFAULT_FAULT_CATALOG_PATH)) -> None:
        catalog = load_fault_catalog(fault_catalog_path)
        self._faults = {fault.root_cause_type: fault for fault in catalog.faults}

    def confirm(self, state: IncidentState) -> IncidentState:
        """Derive and persist the sole supported root cause in the checkpoint."""

        if state.status not in _CONFIRMABLE_STATUSES:
            raise RootCauseConfirmationError(
                "root cause confirmation requires an active diagnostic incident"
            )
        metric_id = state.alert.get("metric")
        if not isinstance(metric_id, str) or not metric_id.strip():
            raise RootCauseConfirmationError("incident alert must contain a metric")

        traces = _index_traces(state)
        candidates: dict[str, list[tuple[RepairEvidence, DiagnosticEvidenceBinding]]] = (
            defaultdict(list)
        )
        for stored in _index_evidence(state).values():
            evidence, binding = self._promote_evidence(stored, traces)
            candidates[binding.root_cause_type].append((evidence, binding))

        valid_candidates = [
            candidate
            for root_cause_type, records in candidates.items()
            if (
                candidate := self._validate_candidate(
                    metric_id, root_cause_type, records
                )
            )
            is not None
        ]
        if not valid_candidates:
            raise RootCauseConfirmationError(
                "no trace-bound evidence satisfies a catalog root cause contract"
            )
        if len(valid_candidates) != 1:
            raise RootCauseConfirmationError(
                "trace-bound evidence supports multiple root causes; confirmation is ambiguous"
            )

        root_cause_type, evidence, repair_context = valid_candidates[0]
        fault = self._faults[root_cause_type]
        state.evidence = [item.model_dump(mode="json") for item in evidence]
        state.root_cause = {
            "root_cause_type": root_cause_type,
            "confidence": min(0.95, 0.8 + 0.05 * (len(evidence) - 2)),
            "affected_assets": fault.affected_assets,
            "repair_context": repair_context,
        }
        state.status = IncidentStatus.ROOT_CAUSE_FOUND
        state.final_status = None
        return state

    def _validate_candidate(
        self,
        metric_id: str,
        root_cause_type: str,
        records: list[tuple[RepairEvidence, DiagnosticEvidenceBinding]],
    ) -> tuple[str, tuple[RepairEvidence, ...], dict[str, JsonValue]] | None:
        fault = self._faults.get(root_cause_type)
        if fault is None or metric_id not in fault.affected_metrics:
            return None
        evidence = tuple(record[0] for record in records)
        if len(evidence) < 2 or len({item.source_type for item in evidence}) < 2:
            return None
        if any(item.asset not in fault.affected_assets for item in evidence):
            return None
        required_sources = set(fault.evidence_source_types)
        if not required_sources.issubset({item.source_type for item in evidence}):
            return None
        repair_context = _common_repair_context(record[1] for record in records)
        if repair_context is None:
            return None
        if root_cause_type == "missing_partition":
            try:
                repair_context = MissingPartitionContext.model_validate(
                    repair_context
                ).model_dump(mode="json")
            except ValidationError:
                return None
        return root_cause_type, evidence, repair_context

    @staticmethod
    def _promote_evidence(
        stored: dict[str, JsonValue], traces: dict[str, dict[str, JsonValue]]
    ) -> tuple[RepairEvidence, DiagnosticEvidenceBinding]:
        evidence_id = stored.get("evidence_id")
        if not isinstance(evidence_id, str) or not evidence_id.strip():
            raise RootCauseConfirmationError("recorded evidence has no evidence_id")
        trace_id = stored.get("tool_trace_id")
        if not isinstance(trace_id, str):
            raise RootCauseConfirmationError(
                f"recorded evidence has no tool trace: {evidence_id}"
            )
        trace = traces.get(trace_id)
        if trace is None or not _is_usable_trace(trace, stored):
            raise RootCauseConfirmationError(
                f"recorded evidence is not backed by a usable tool result: {evidence_id}"
            )
        finding = stored.get("finding")
        if not isinstance(finding, str) or not finding.strip():
            raise RootCauseConfirmationError(
                f"recorded evidence has no finding: {evidence_id}"
            )
        try:
            binding = DiagnosticEvidenceBinding.model_validate(
                {
                    "root_cause_type": stored.get("root_cause_type"),
                    "source_type": stored.get("source_type"),
                    "asset": stored.get("asset"),
                    "repair_context": stored.get("repair_context"),
                }
            )
        except ValidationError as exc:
            raise RootCauseConfirmationError(
                f"recorded evidence has no diagnostic tool binding: {evidence_id}"
            ) from exc
        return (
            RepairEvidence(
                evidence_id=evidence_id,
                source_type=binding.source_type,
                asset=binding.asset,
                finding=finding,
            ),
            binding,
        )


def _common_repair_context(
    bindings: Iterable[DiagnosticEvidenceBinding],
) -> dict[str, JsonValue] | None:
    contexts = [binding.repair_context for binding in bindings]
    serialized_contexts = {
        json.dumps(context, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        for context in contexts
    }
    if len(serialized_contexts) != 1:
        return None
    return dict(contexts[0])


def _index_evidence(state: IncidentState) -> dict[str, dict[str, JsonValue]]:
    records: dict[str, dict[str, JsonValue]] = {}
    for item in state.evidence:
        evidence_id = item.get("evidence_id")
        if not isinstance(evidence_id, str) or not evidence_id.strip():
            continue
        if evidence_id in records:
            raise RootCauseConfirmationError("recorded evidence ids must be unique")
        records[evidence_id] = item
    return records


def _index_traces(state: IncidentState) -> dict[str, dict[str, JsonValue]]:
    traces: dict[str, dict[str, JsonValue]] = {}
    for item in state.tool_trace:
        trace_id = item.get("trace_id")
        if not isinstance(trace_id, str) or not trace_id.strip():
            continue
        if trace_id in traces:
            raise RootCauseConfirmationError("tool trace ids must be unique")
        traces[trace_id] = item
    return traces


def _is_usable_trace(
    trace: dict[str, JsonValue], evidence: dict[str, JsonValue]
) -> bool:
    if trace.get("tool") != "sql_runner":
        return False
    query_id = evidence.get("query_id")
    if not isinstance(query_id, str) or trace.get("query_id") != query_id:
        return False
    response = trace.get("response")
    validation = trace.get("validation")
    if not isinstance(response, dict) or response.get("status") != "success":
        return False
    if not isinstance(validation, dict):
        return False
    result_evidence = validation.get("evidence")
    return isinstance(result_evidence, dict) and result_evidence.get("usable") is True


__all__ = [
    "DiagnosticEvidenceBinding",
    "RootCauseConfirmationError",
    "RootCauseConfirmationService",
]
