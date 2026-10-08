from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request

from kf_oj.contracts.problems import ProblemDetail, ProblemPage
from kf_oj.modules.problems.public import ProblemUnavailable
from kf_oj.modules.problems.service import ProblemService

router = APIRouter(prefix="/problems", tags=["problems"])


def service(request: Request) -> ProblemService:
    component = getattr(request.app.state, "problems", None)
    if component is None:
        raise HTTPException(503, "problem service unavailable")
    return component


@router.get("", response_model=ProblemPage)
async def list_problems(
    request: Request,
    after: Annotated[str | None, Query(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> ProblemPage:
    return await service(request).list(after=after, limit=limit)


@router.get("/versions/{version_id}", response_model=ProblemDetail)
async def problem_version(version_id: UUID, request: Request) -> ProblemDetail:
    try:
        return await service(request).detail(version_id)
    except ProblemUnavailable:
        raise HTTPException(404, "problem version unavailable") from None
