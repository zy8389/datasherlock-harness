"""FastAPI routes for the canonical deterministic demo."""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status

from demo.models import (
    DemoApprovalRequest,
    DemoBenchmarkSnapshot,
    DemoCaseList,
    DemoIncidentList,
    DemoIncidentStartRequest,
    DemoIncidentView,
)
from demo.service import (
    DemoCaseNotFoundError,
    DemoCaseUnsupportedError,
    DemoIncidentConflictError,
    DemoIncidentNotFoundError,
    DemoService,
    DemoServiceError,
)

router = APIRouter(prefix="/demo", tags=["demo"])


@lru_cache(maxsize=1)
def get_demo_service() -> DemoService:
    return DemoService()


DemoServiceDependency = Annotated[DemoService, Depends(get_demo_service)]


@router.get("/cases", response_model=DemoCaseList)
def list_cases(service: DemoServiceDependency) -> DemoCaseList:
    return service.list_cases()


@router.get("/incidents", response_model=DemoIncidentList)
def list_incidents(
    service: DemoServiceDependency,
) -> DemoIncidentList:
    return service.list_incidents()


@router.post(
    "/incidents",
    response_model=DemoIncidentView,
    status_code=status.HTTP_201_CREATED,
)
def start_incident(
    request: DemoIncidentStartRequest,
    service: DemoServiceDependency,
) -> DemoIncidentView:
    try:
        return service.start_incident(request.case_id)
    except DemoCaseNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except DemoCaseUnsupportedError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except DemoServiceError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/incidents/{incident_id}", response_model=DemoIncidentView)
def get_incident(
    incident_id: str,
    service: DemoServiceDependency,
) -> DemoIncidentView:
    try:
        return service.get_incident(incident_id)
    except DemoIncidentNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except DemoServiceError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post(
    "/incidents/{incident_id}/approval",
    response_model=DemoIncidentView,
)
def submit_approval(
    incident_id: str,
    request: DemoApprovalRequest,
    service: DemoServiceDependency,
) -> DemoIncidentView:
    try:
        return service.decide_incident(
            incident_id,
            reviewer=request.reviewer,
            outcome=request.outcome,
            comment=request.comment,
        )
    except DemoIncidentNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except DemoIncidentConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except DemoServiceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/benchmark", response_model=DemoBenchmarkSnapshot)
def benchmark_snapshot(
    service: DemoServiceDependency,
) -> DemoBenchmarkSnapshot:
    return service.benchmark_snapshot()


__all__ = ["get_demo_service", "router"]
