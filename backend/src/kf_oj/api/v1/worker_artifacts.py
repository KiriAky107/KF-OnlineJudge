from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, Request, Response, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from kf_oj.contracts.judging import JobManifest, LeaseBinding
from kf_oj.modules.submissions.public import WorkerAccessDenied, WorkerReadBusy
from kf_oj.modules.submissions.worker_access import WorkerAccess

router = APIRouter(prefix="/internal/evaluations", tags=["worker artifacts"])
bearer = HTTPBearer(auto_error=False, scheme_name="WorkerLease")


def grant(
    evaluation_id: UUID,
    attempt_id: UUID,
    worker: UUID,
    fence: int,
    credentials: HTTPAuthorizationCredentials | None,
) -> tuple[LeaseBinding, str]:
    if (
        credentials is None
        or credentials.scheme.lower() != "bearer"
        or len(credentials.credentials) != 64
    ):
        raise HTTPException(401, "worker lease required")
    try:
        return LeaseBinding(
            evaluation_id=evaluation_id,
            attempt_id=attempt_id,
            worker_id=worker,
            fencing_version=fence,
        ), credentials.credentials
    except ValueError:
        raise HTTPException(404, "worker grant rejected") from None


def component(request: Request) -> WorkerAccess:
    access = getattr(request.app.state, "worker_access", None)
    if access is None:
        raise HTTPException(503, "worker artifact service unavailable")
    return access


@router.get("/{evaluation_id}/attempts/{attempt_id}/manifest", response_model=JobManifest)
async def manifest(
    evaluation_id: UUID,
    attempt_id: UUID,
    request: Request,
    worker: Annotated[UUID, Header(alias="X-Worker-ID")],
    fence: Annotated[int, Header(alias="X-Fencing-Version", ge=1)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Security(bearer)],
):
    binding, token = grant(evaluation_id, attempt_id, worker, fence, credentials)
    try:
        payload = await component(request).manifest(binding, token)
    except WorkerAccessDenied:
        raise HTTPException(404, "worker grant rejected") from None
    except WorkerReadBusy:
        raise HTTPException(429, "worker reads temporarily limited") from None
    return Response(payload, media_type="application/json", headers={"Cache-Control": "no-store"})


@router.get(
    "/{evaluation_id}/attempts/{attempt_id}/artifacts/{digest}",
    response_class=Response,
    responses={
        200: {
            "content": {
                "application/octet-stream": {"schema": {"type": "string", "format": "binary"}}
            }
        }
    },
)
async def artifact(
    evaluation_id: UUID,
    attempt_id: UUID,
    digest: str,
    request: Request,
    worker: Annotated[UUID, Header(alias="X-Worker-ID")],
    fence: Annotated[int, Header(alias="X-Fencing-Version", ge=1)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Security(bearer)],
):
    binding, token = grant(evaluation_id, attempt_id, worker, fence, credentials)
    try:
        payload = await component(request).artifact(binding, token, digest)
    except WorkerAccessDenied:
        raise HTTPException(404, "worker grant rejected") from None
    except WorkerReadBusy:
        raise HTTPException(429, "worker reads temporarily limited") from None
    return Response(
        payload,
        media_type="application/octet-stream",
        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
    )
