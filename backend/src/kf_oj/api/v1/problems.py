from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request

from kf_oj.contracts.difficulty import DifficultyKey, DifficultyLevel
from kf_oj.contracts.problems import ProblemDetail, ProblemDifficulty, ProblemPage
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
    search: Annotated[str | None, Query(min_length=1, max_length=80)] = None,
    difficulty: ProblemDifficulty | None = None,
    difficulty_key: DifficultyKey | None = None,
    tag: Annotated[str | None, Query(min_length=1, max_length=40)] = None,
) -> ProblemPage:
    return await service(request).list(
        after=after,
        limit=limit,
        search=search.strip() if search else None,
        difficulty=difficulty,
        difficulty_key=difficulty_key,
        tag=tag,
    )


@router.get("/difficulty-levels", response_model=tuple[DifficultyLevel, ...])
async def list_difficulty_levels(request: Request):
    return await service(request).levels()


@router.get("/versions/{version_id}", response_model=ProblemDetail)
async def problem_version(version_id: UUID, request: Request) -> ProblemDetail:
    try:
        detail = await service(request).detail(version_id)
        execution_available = all(
            getattr(request.app.state, name, None) is not None
            for name in ("self_tests", "submissions")
        )
        return detail.model_copy(update={"execution_available": execution_available})
    except ProblemUnavailable:
        raise HTTPException(404, "problem version unavailable") from None
