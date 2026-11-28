"""Administrator-only aggregate learning observations."""

from collections.abc import Awaitable
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from kf_oj.api.dependencies import current_principal
from kf_oj.contracts.learning_observations import (
    DiagnosticCohort,
    DiagnosticObservations,
    PracticeObservations,
)
from kf_oj.modules.identity.public import AdministrationDenied, AuthenticationFailed, Principal
from kf_oj.modules.learning.observations import LearningObservations

router = APIRouter(prefix="/admin/learning", tags=["admin-learning"])

Reader = Annotated[Principal, Depends(current_principal)]
Window = Annotated[int, Query(ge=1, le=90)]


def _service(request: Request) -> LearningObservations:
    service = getattr(request.app.state, "learning_observations", None)
    if service is None:
        raise HTTPException(503, "learning observations unavailable")
    return service


Service = Annotated[LearningObservations, Depends(_service)]


async def _read[Result](operation: Awaitable[Result]) -> Result:
    try:
        return await operation
    except AuthenticationFailed:
        raise HTTPException(401, "authentication required") from None
    except AdministrationDenied:
        raise HTTPException(403, "administrator required") from None


@router.get("/diagnostics", response_model=DiagnosticObservations)
async def diagnostic_observations(
    principal: Reader, service: Service, days: Window = 30, cohort: DiagnosticCohort = "all"
):
    return await _read(service.diagnostics(principal, days, cohort))


@router.get("/practice", response_model=PracticeObservations)
async def practice_observations(principal: Reader, service: Service, days: Window = 30):
    return await _read(service.practice(principal, days))
