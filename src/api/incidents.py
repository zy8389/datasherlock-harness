"""Revision-protected incident, approval, and sandbox repair API routes."""

import os
from collections.abc import Callable
from datetime import UTC, datetime
from functools import lru_cache
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, JsonValue

from api.auth import (
    ApprovalAuthenticationConfigurationError,
    ApprovalAuthenticationError,
    ApprovalAuthenticator,
    ApprovalAuthorizationError,
    ApprovalPrincipal,
)
from harness.approval import ApprovalFlow, ApprovalFlowError
from harness.checkpoint import (
    CheckpointConflictError,
    CheckpointStoreError,
    IncidentAlreadyExistsError,
    IncidentAuditEvent,
    IncidentCheckpoint,
    IncidentCheckpointRepository,
    IncidentCheckpointStore,
    IncidentNotFoundError,
)
from harness.post_validation import PostRepairValidator
from harness.postgres_checkpoint import PostgresCheckpointStore
from harness.repair import ApprovalDecision, ApprovalOutcome
from harness.repair_proposal import RepairProposalBuilder, RepairProposalBuildError
from harness.repair_workflow import RepairWorkflowError, RepairWorkflowService
from harness.root_cause import (
    RootCauseConfirmationError,
    RootCauseConfirmationService,
)
from harness.sandbox_repair import SandboxRepairExecutor
from harness.state import IncidentState


class CreateIncidentRequest(BaseModel):
    """The immutable alert payload used to open a new investigation."""

    model_config = ConfigDict(extra="forbid")

    alert: dict[str, JsonValue] = Field(min_length=1)


class RepairProposalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=1)


class RootCauseConfirmationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=1)


class ApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    expected_revision: int = Field(ge=1)
    decision_id: str = Field(min_length=1)
    outcome: ApprovalOutcome
    comment: str | None = None


class SandboxRepairRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=1)
    allowed_relative_error: float = Field(default=0.05, ge=0, le=1)
    regression_metric_ids: tuple[str, ...] = ()
    max_regression_ratio: float = Field(default=0.05, ge=0, le=1)


StoreProvider = Callable[[], IncidentCheckpointRepository]
WorkflowProvider = Callable[[], RepairWorkflowService]


@lru_cache
def get_incident_store() -> IncidentCheckpointRepository:
    """Build the configured durable checkpoint backend when an incident route is used."""

    backend = os.getenv("INCIDENT_CHECKPOINT_BACKEND", "postgres").lower()
    if backend == "postgres":
        return PostgresCheckpointStore.from_environment()
    if backend == "file":
        return IncidentCheckpointStore(
            os.getenv("INCIDENT_CHECKPOINT_ROOT", "data/incidents")
        )
    raise RuntimeError("INCIDENT_CHECKPOINT_BACKEND must be 'postgres' or 'file'")


@lru_cache
def get_repair_workflow() -> RepairWorkflowService:
    """Build the executor from service-owned paths, never request body values."""

    source_database = os.getenv(
        "DUCKDB_PATH", "/workspace/data/processed/datasherlock.duckdb"
    )
    repair_source_database = os.getenv("REPAIR_SOURCE_DUCKDB_PATH")
    if not repair_source_database:
        raise RuntimeError("REPAIR_SOURCE_DUCKDB_PATH must be configured")
    executor = SandboxRepairExecutor(
        source_database,
        os.getenv("SANDBOX_ROOT", "data/sandboxes"),
        repair_source_database_path=repair_source_database,
    )
    return RepairWorkflowService(executor, PostRepairValidator(source_database))


def create_incident_router(
    *,
    store_provider: StoreProvider = get_incident_store,
    workflow_provider: WorkflowProvider = get_repair_workflow,
    approval_flow: ApprovalFlow | None = None,
    repair_proposal_builder: RepairProposalBuilder | None = None,
    root_cause_confirmation_service: RootCauseConfirmationService | None = None,
    approval_authenticator: ApprovalAuthenticator | None = None,
) -> APIRouter:
    """Create routes with injectable state and execution dependencies for tests."""

    router = APIRouter(prefix="/incidents", tags=["incidents"])
    flow = approval_flow or ApprovalFlow()
    proposal_builder = repair_proposal_builder or RepairProposalBuilder()
    root_cause_service = (
        root_cause_confirmation_service or RootCauseConfirmationService()
    )
    authenticator = approval_authenticator or ApprovalAuthenticator.from_environment()

    def resolve_store() -> IncidentCheckpointRepository:
        return store_provider()

    def resolve_approval_principal(
        authorization: Annotated[str | None, Header()] = None,
    ) -> ApprovalPrincipal:
        try:
            return authenticator.require_approval(authorization)
        except ApprovalAuthenticationConfigurationError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=str(exc),
            ) from exc
        except ApprovalAuthenticationError as exc:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=str(exc),
                headers={"WWW-Authenticate": "Bearer"},
            ) from exc
        except ApprovalAuthorizationError as exc:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc

    @router.post("", response_model=IncidentCheckpoint, status_code=status.HTTP_201_CREATED)
    def create_incident(
        request: CreateIncidentRequest,
        store: Annotated[IncidentCheckpointRepository, Depends(resolve_store)],
    ) -> IncidentCheckpoint:
        state = IncidentState(alert=request.alert)
        try:
            checkpoint = store.create(state)
            store.append_event(
                IncidentAuditEvent(
                    incident_id=checkpoint.incident_id,
                    event_type="incident_created",
                    revision=checkpoint.revision,
                    details={"status": checkpoint.state.status.value},
                )
            )
            return checkpoint
        except IncidentAlreadyExistsError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        except CheckpointStoreError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    @router.get("/{incident_id}", response_model=IncidentCheckpoint)
    def get_incident(
        incident_id: str,
        store: Annotated[IncidentCheckpointRepository, Depends(resolve_store)],
    ) -> IncidentCheckpoint:
        return _load_checkpoint(store, incident_id)

    @router.post("/{incident_id}/root-cause", response_model=IncidentCheckpoint)
    def confirm_root_cause(
        incident_id: str,
        request: RootCauseConfirmationRequest,
        store: Annotated[IncidentCheckpointRepository, Depends(resolve_store)],
    ) -> IncidentCheckpoint:
        current = _load_checkpoint(store, incident_id)
        try:
            root_cause_service.confirm(current.state)
            checkpoint = store.save(
                current.state, expected_revision=request.expected_revision
            )
            root_cause = checkpoint.state.root_cause
            assert root_cause is not None
            _append_event(
                store,
                checkpoint,
                "root_cause_confirmed",
                {
                    "root_cause_type": root_cause["root_cause_type"],
                    "evidence_ids": [
                        evidence["evidence_id"] for evidence in checkpoint.state.evidence
                    ],
                },
            )
            return checkpoint
        except (
            CheckpointConflictError,
            RootCauseConfirmationError,
        ) as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    @router.post("/{incident_id}/repair-proposals", response_model=IncidentCheckpoint)
    def submit_repair_proposal(
        incident_id: str,
        request: RepairProposalRequest,
        store: Annotated[IncidentCheckpointRepository, Depends(resolve_store)],
    ) -> IncidentCheckpoint:
        current = _load_checkpoint(store, incident_id)
        try:
            proposal = proposal_builder.build(current.state)
            flow.propose(current.state, proposal)
            flow.request_approval(current.state)
            checkpoint = store.save(
                current.state, expected_revision=request.expected_revision
            )
            _append_event(
                store,
                checkpoint,
                "repair_proposal_submitted",
                {
                    "proposal_id": proposal.proposal_id,
                    "proposal_hash": proposal.proposal_hash,
                    "action": proposal.action.value,
                },
            )
            return checkpoint
        except (
            ApprovalFlowError,
            CheckpointConflictError,
            RepairProposalBuildError,
        ) as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    @router.post("/{incident_id}/approval", response_model=IncidentCheckpoint)
    def approve_repair_proposal(
        incident_id: str,
        request: ApprovalRequest,
        store: Annotated[IncidentCheckpointRepository, Depends(resolve_store)],
        principal: Annotated[ApprovalPrincipal, Depends(resolve_approval_principal)],
    ) -> IncidentCheckpoint:
        current = _load_checkpoint(store, incident_id)
        if current.state.repair_proposal is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="incident has no repair proposal",
            )
        try:
            decision = ApprovalDecision.for_proposal(
                current.state.repair_proposal,
                decision_id=request.decision_id,
                outcome=request.outcome,
                reviewer=principal.subject,
                comment=request.comment,
                decided_at=datetime.now(UTC),
            )
            flow.record_decision(current.state, decision)
            checkpoint = store.save(
                current.state, expected_revision=request.expected_revision
            )
            _append_event(
                store,
                checkpoint,
                "repair_approval_recorded",
                {
                    "decision_id": decision.decision_id,
                    "outcome": decision.outcome.value,
                    "reviewer": decision.reviewer,
                    "identity_source": principal.identity_source,
                    "permissions": sorted(principal.permissions),
                },
            )
            return checkpoint
        except (ApprovalFlowError, CheckpointConflictError, ValueError) as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    @router.post("/{incident_id}/sandbox-repair", response_model=IncidentCheckpoint)
    def execute_sandbox_repair(
        incident_id: str,
        request: SandboxRepairRequest,
        store: Annotated[IncidentCheckpointRepository, Depends(resolve_store)],
    ) -> IncidentCheckpoint:
        current = _load_checkpoint(store, incident_id)
        try:
            workflow = workflow_provider()
        except (RuntimeError, ValueError) as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"sandbox repair unavailable: {exc}",
            ) from exc
        try:
            workflow.execute_approved_repair(
                current.state,
                allowed_relative_error=request.allowed_relative_error,
                regression_metric_ids=request.regression_metric_ids,
                max_regression_ratio=request.max_regression_ratio,
            )
            checkpoint = store.save(
                current.state, expected_revision=request.expected_revision
            )
            _append_event(
                store,
                checkpoint,
                "sandbox_repair_completed",
                {
                    "status": checkpoint.state.status.value,
                    "sandbox_run_id": (
                        checkpoint.state.sandbox_run.run_id
                        if checkpoint.state.sandbox_run is not None
                        else None
                    ),
                },
            )
            return checkpoint
        except (
            ApprovalFlowError,
            CheckpointConflictError,
            RepairWorkflowError,
        ) as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    @router.get("/{incident_id}/audit", response_model=list[IncidentAuditEvent])
    def get_incident_audit(
        incident_id: str,
        store: Annotated[IncidentCheckpointRepository, Depends(resolve_store)],
    ) -> list[IncidentAuditEvent]:
        _load_checkpoint(store, incident_id)
        try:
            return store.read_events(incident_id)
        except CheckpointStoreError as exc:
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)) from exc

    return router


def _load_checkpoint(
    store: IncidentCheckpointRepository, incident_id: str
) -> IncidentCheckpoint:
    try:
        return store.load(incident_id)
    except IncidentNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except CheckpointStoreError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


def _append_event(
    store: IncidentCheckpointRepository,
    checkpoint: IncidentCheckpoint,
    event_type: str,
    details: dict[str, JsonValue],
) -> None:
    store.append_event(
        IncidentAuditEvent(
            incident_id=checkpoint.incident_id,
            event_type=event_type,
            revision=checkpoint.revision,
            details=details,
        )
    )


__all__ = [
    "ApprovalRequest",
    "CreateIncidentRequest",
    "RepairProposalRequest",
    "RootCauseConfirmationRequest",
    "SandboxRepairRequest",
    "create_incident_router",
    "get_incident_store",
    "get_repair_workflow",
]
