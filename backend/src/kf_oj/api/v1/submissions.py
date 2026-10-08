from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from kf_oj.api.dependencies import unsafe_principal
from kf_oj.contracts.submissions import CreateSubmission, SubmissionCreated
from kf_oj.infra.artifacts.store import ArtifactError
from kf_oj.modules.identity.public import Principal
from kf_oj.modules.submissions.public import (
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
