from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from kf_oj.api.dependencies import current_principal, unsafe_principal
from kf_oj.contracts.self_tests import CreateSelfTest, SelfTestDetail
from kf_oj.infra.artifacts.store import ArtifactError
from kf_oj.modules.identity.public import Principal
from kf_oj.modules.submissions.public import (
    IdempotencyConflict,
    SubmissionBusy,
    SubmissionUnavailable,
)

router = APIRouter(prefix="/self-tests", tags=["self-tests"])


@router.post("", status_code=201, response_model=SelfTestDetail)
async def create(
    body: CreateSelfTest,
    request: Request,
    response: Response,
    principal: Annotated[Principal, Depends(unsafe_principal)],
):
    if request.headers.get("content-type", "").split(";", 1)[0] != "application/json":
        raise HTTPException(415, "JSON request required")
    service = getattr(request.app.state, "self_tests", None)
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


@router.get("/{self_test_id}", response_model=SelfTestDetail)
async def detail(
    self_test_id: UUID,
    request: Request,
    principal: Annotated[Principal, Depends(current_principal)],
):
    service = getattr(request.app.state, "self_tests", None)
    if service is None:
        raise HTTPException(503, "self-test service unavailable")
    try:
        return await service.detail(principal, self_test_id)
    except SubmissionUnavailable:
        raise HTTPException(404, "self-test unavailable") from None
    except ValueError:
        raise HTTPException(503, "stored self-test result unavailable") from None
