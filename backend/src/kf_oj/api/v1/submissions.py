from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response

from kf_oj.api.dependencies import current_principal, unsafe_principal
from kf_oj.contracts.judging_v2 import Language
from kf_oj.contracts.submission_performance import SubmissionPerformance
from kf_oj.contracts.submissions import (
    CancellationAccepted,
    CreateSubmission,
    SubmissionCreated,
    SubmissionDetail,
    SubmissionPage,
    SubmissionSource,
)
from kf_oj.infra.artifacts.store import ArtifactError
from kf_oj.modules.identity.public import Principal
from kf_oj.modules.submissions.public import (
    CancelUnavailable,
    IdempotencyConflict,
    SubmissionBusy,
    SubmissionUnavailable,
)

router = APIRouter(prefix="/submissions", tags=["submissions"])


@router.post("", status_code=201, response_model=SubmissionCreated)
async def create(
    body: CreateSubmission,
    request: Request,
    response: Response,
    principal: Annotated[Principal, Depends(unsafe_principal)],
):
    if request.headers.get("content-type", "").split(";", 1)[0] != "application/json":
        raise HTTPException(415, "JSON request required")
    service = getattr(request.app.state, "submissions", None)
    if service is None:
        raise HTTPException(503, "submission service unavailable")
    try:
        result = await service.create(principal, body)
    except IdempotencyConflict:
        raise HTTPException(409, "idempotency request conflict") from None
    except SubmissionBusy:
        raise HTTPException(429, "pending submission capacity exhausted") from None
    except SubmissionUnavailable:
        raise HTTPException(404, "submission capability unavailable") from None
    except ArtifactError:
        raise HTTPException(503, "source storage unavailable") from None
    except ValueError:
        raise HTTPException(422, "submission outside admission policy") from None
    response.headers["Cache-Control"] = "no-store"
    return result


def queries(request: Request):
    service = getattr(request.app.state, "submission_queries", None)
    if service is None:
        raise HTTPException(503, "submission service unavailable")
    return service


@router.get("", response_model=SubmissionPage)
async def history(
    request: Request,
    principal: Annotated[Principal, Depends(current_principal)],
    after: Annotated[str | None, Query(max_length=128)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    problem_version_id: UUID | None = None,
    language: Language | None = None,
):
    try:
        return await queries(request).page(principal, after, limit, problem_version_id, language)
    except ValueError:
        raise HTTPException(422, "invalid history request") from None


@router.get("/{submission_id}", response_model=SubmissionDetail)
async def detail(
    submission_id: UUID,
    request: Request,
    principal: Annotated[Principal, Depends(current_principal)],
):
    try:
        return await queries(request).detail(principal, submission_id)
    except SubmissionUnavailable:
        raise HTTPException(404, "submission unavailable") from None
    except ValueError:
        raise HTTPException(503, "stored result unavailable") from None


@router.get("/{submission_id}/performance", response_model=SubmissionPerformance)
async def performance(
    submission_id: UUID,
    request: Request,
    principal: Annotated[Principal, Depends(current_principal)],
):
    service = getattr(request.app.state, "submission_performance", None)
    if service is None:
        raise HTTPException(503, "performance service unavailable")
    try:
        return await service.detail(principal, submission_id)
    except SubmissionUnavailable:
        raise HTTPException(404, "submission unavailable") from None
    except ValueError:
        raise HTTPException(503, "performance unavailable") from None


@router.get("/{submission_id}/source", response_model=SubmissionSource)
async def source(
    submission_id: UUID,
    request: Request,
    principal: Annotated[Principal, Depends(current_principal)],
):
    service = queries(request)
    try:
        result = await service.detail(principal, submission_id)
        data = await service.owned_source(principal, submission_id)
        return SubmissionSource(
            submission_id=submission_id, language=result.language, source=data.decode("utf-8")
        )
    except SubmissionUnavailable:
        raise HTTPException(404, "submission unavailable") from None
    except ValueError:
        raise HTTPException(503, "stored source unavailable") from None


@router.post("/{submission_id}/cancel", status_code=202, response_model=CancellationAccepted)
async def cancel(
    submission_id: UUID,
    request: Request,
    principal: Annotated[Principal, Depends(unsafe_principal)],
):
    try:
        return await queries(request).cancel(principal, submission_id)
    except SubmissionUnavailable:
        raise HTTPException(404, "submission unavailable") from None
    except CancelUnavailable:
        raise HTTPException(503, "cancellation service unavailable") from None
