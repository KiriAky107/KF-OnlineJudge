"""Administrator import contract; configured service availability is explicit."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.exc import DBAPIError
from starlette.requests import ClientDisconnect

from kf_oj.api.admin_dependencies import maintenance_reader, maintenance_writer
from kf_oj.contracts.admin_problems import (
    CreateProblemImport,
    ProblemImportCapabilities,
    ProblemImportDetail,
    ProblemImportPage,
    ProblemImportSummary,
)
from kf_oj.infra.artifacts.store import ArtifactError
from kf_oj.modules.identity.public import MaintenanceDenied, Principal
from kf_oj.modules.problems.imports import ImportConflict, ImportMissing, ImportUnavailable

router = APIRouter(prefix="/admin/problem-imports", tags=["problem-maintenance"])


@router.get("/capabilities", response_model=ProblemImportCapabilities)
async def capabilities(
    request: Request, _: Annotated[Principal, Depends(maintenance_reader)]
) -> ProblemImportCapabilities:
    service = getattr(request.app.state, "problem_imports", None)
    if service is None:
        return ProblemImportCapabilities(uploads_available=False, validation_available=False)
    return await service.capabilities()


def service(request):
    result = getattr(request.app.state, "problem_imports", None)
    if result is None:
        raise HTTPException(503, "problem imports unavailable")
    return result


async def operation(awaitable):
    try:
        return await awaitable
    except MaintenanceDenied:
        raise HTTPException(403, "problem maintenance required") from None
    except ImportConflict:
        raise HTTPException(409, "import request conflict") from None
    except ImportMissing:
        raise HTTPException(404, "import unavailable") from None
    except TimeoutError:
        raise HTTPException(408, "upload deadline exceeded") from None
    except (ImportUnavailable, ArtifactError, OSError, DBAPIError):
        raise HTTPException(503, "problem imports unavailable") from None
    except ClientDisconnect:
        raise HTTPException(400, "upload interrupted") from None


@router.post("", status_code=201, response_model=ProblemImportSummary)
async def create(
    body: CreateProblemImport,
    request: Request,
    principal: Annotated[Principal, Depends(maintenance_writer)],
):
    if request.headers.get("content-type", "").split(";", 1)[0] != "application/json":
        raise HTTPException(415, "JSON request required")
    return await operation(service(request).create(principal, body))


@router.get("", response_model=ProblemImportPage)
async def page(
    request: Request,
    principal: Annotated[Principal, Depends(maintenance_reader)],
    after: UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
):
    return await operation(service(request).list(principal, after, limit))


@router.get("/{operation_id}", response_model=ProblemImportDetail)
async def detail(
    operation_id: UUID,
    request: Request,
    principal: Annotated[Principal, Depends(maintenance_reader)],
):
    return await operation(service(request).detail(principal, operation_id))


@router.put(
    "/{operation_id}/archive",
    response_model=ProblemImportSummary,
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {"application/zip": {"schema": {"type": "string", "format": "binary"}}},
        }
    },
)
async def upload(
    operation_id: UUID,
    request: Request,
    principal: Annotated[Principal, Depends(maintenance_writer)],
):
    if request.url.path != f"/api/v1/admin/problem-imports/{operation_id}/archive":
        raise HTTPException(422, "canonical operation UUID required")
    if request.headers.get("content-type", "").split(";", 1)[0] != "application/zip":
        raise HTTPException(415, "ZIP request required")
    if request.headers.get("content-encoding") not in (None, "identity"):
        raise HTTPException(415, "encoded upload unavailable")
    length = request.headers.get("content-length")
    if length is not None:
        try:
            if not length.isascii() or not length.isdecimal() or len(length) > 12:
                raise ValueError
            length = int(length)
        except ValueError:
            raise HTTPException(413, "invalid archive length") from None

    async def chunks():
        while True:
            message = await request.receive()
            if message["type"] == "http.disconnect":
                raise ClientDisconnect()
            if message["type"] != "http.request":
                raise ImportConflict("invalid upload frame")
            yield message.get("body", b"")
            if not message.get("more_body", False):
                return

    return await operation(service(request).upload(principal, operation_id, chunks(), length))


@router.post("/{operation_id}/discard", response_model=ProblemImportSummary)
async def discard(
    operation_id: UUID,
    request: Request,
    principal: Annotated[Principal, Depends(maintenance_writer)],
):
    return await operation(service(request).discard(principal, operation_id))
