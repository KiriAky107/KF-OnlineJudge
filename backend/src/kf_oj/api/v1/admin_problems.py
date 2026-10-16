"""Live administrator projections and actor-bound immutable mutations."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.exc import DBAPIError

from kf_oj.api.admin_dependencies import maintenance_reader, maintenance_writer
from kf_oj.contracts.problem_maintenance import (
    AuditAction,
    ChangeProblemAvailability,
    MaintainedProblem,
    MaintainedProblemPage,
    MaintainedVersionPage,
    MaintenanceAuditPage,
    MaintenanceReceipt,
    PublishCandidate,
)
from kf_oj.infra.artifacts.store import ArtifactError
from kf_oj.modules.identity.public import MaintenanceDenied, Principal
from kf_oj.modules.problems.imports import ImportConflict, ImportMissing, ImportUnavailable
from kf_oj.modules.problems.public import ProblemUnavailable

router = APIRouter(prefix="/admin", tags=["problem-maintenance"])


def service(request, name="problem_maintenance"):
    result = getattr(request.app.state, name, None)
    if result is None:
        raise HTTPException(503, "problem maintenance unavailable")
    return result


async def operation(awaitable):
    try:
        return await awaitable
    except MaintenanceDenied:
        raise HTTPException(403, "problem maintenance required") from None
    except ImportConflict:
        raise HTTPException(409, "maintenance precondition conflict") from None
    except ImportMissing:
        raise HTTPException(404, "maintenance object unavailable") from None
    except (ImportUnavailable, ArtifactError, ProblemUnavailable, OSError, DBAPIError):
        raise HTTPException(503, "problem maintenance unavailable") from None
    except ValueError:
        raise HTTPException(422, "invalid maintenance request") from None


@router.get("/problems", response_model=MaintainedProblemPage)
async def page(
    request: Request,
    principal: Annotated[Principal, Depends(maintenance_reader)],
    after: Annotated[str | None, Query(pattern=r"^[a-z0-9][a-z0-9_.-]{0,63}$")] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    enabled: bool | None = None,
):
    return await operation(
        service(request).page(principal, after=after, limit=limit, enabled=enabled)
    )


@router.get("/problem-slugs/{slug}", response_model=MaintainedProblem)
async def by_slug(
    slug: str, request: Request, principal: Annotated[Principal, Depends(maintenance_reader)]
):
    from pydantic import TypeAdapter

    from kf_oj.contracts.judging import Label

    try:
        TypeAdapter(Label).validate_python(slug)
    except ValueError:
        raise HTTPException(422, "invalid problem slug") from None
    return await operation(service(request).by_slug(principal, slug))


@router.get("/problems/{problem_id}", response_model=MaintainedProblem)
async def detail(
    problem_id: UUID, request: Request, principal: Annotated[Principal, Depends(maintenance_reader)]
):
    return await operation(service(request).detail(principal, problem_id))


@router.get("/problems/{problem_id}/versions", response_model=MaintainedVersionPage)
async def versions(
    problem_id: UUID,
    request: Request,
    principal: Annotated[Principal, Depends(maintenance_reader)],
    before: Annotated[int | None, Query(ge=1)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
):
    return await operation(
        service(request).versions(principal, problem_id, before=before, limit=limit)
    )


@router.get("/problem-maintenance/{operation_id}", response_model=MaintenanceReceipt)
async def receipt(
    operation_id: UUID,
    request: Request,
    principal: Annotated[Principal, Depends(maintenance_reader)],
):
    return await operation(service(request).receipt(principal, operation_id))


@router.post("/problem-publications", response_model=MaintenanceReceipt, status_code=201)
async def publish(
    body: PublishCandidate,
    request: Request,
    principal: Annotated[Principal, Depends(maintenance_writer)],
):
    return await operation(service(request).publish(principal, body))


@router.post("/problems/{problem_id}/disable", response_model=MaintenanceReceipt)
async def disable(
    problem_id: UUID,
    body: ChangeProblemAvailability,
    request: Request,
    principal: Annotated[Principal, Depends(maintenance_writer)],
):
    return await operation(
        service(request).availability(principal, problem_id, body, enabled=False)
    )


@router.post("/problems/{problem_id}/restore", response_model=MaintenanceReceipt)
async def restore(
    problem_id: UUID,
    body: ChangeProblemAvailability,
    request: Request,
    principal: Annotated[Principal, Depends(maintenance_writer)],
):
    return await operation(service(request).availability(principal, problem_id, body, enabled=True))


@router.get("/maintenance-audit", response_model=MaintenanceAuditPage)
async def audit(
    request: Request,
    principal: Annotated[Principal, Depends(maintenance_reader)],
    before: Annotated[str | None, Query(max_length=100)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    action: AuditAction | None = None,
):
    return await operation(
        service(request, "maintenance_audit").page(
            principal, before=before, limit=limit, action=action
        )
    )
