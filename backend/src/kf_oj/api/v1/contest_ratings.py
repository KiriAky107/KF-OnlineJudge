from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from kf_oj.api.dependencies import current_principal, unsafe_principal
from kf_oj.contracts.contests import ContestRating, ContestSettlement, SettleContest
from kf_oj.modules.contests.service import ContestRatings, SettlementConflict
from kf_oj.modules.identity.public import AdministrationDenied, Principal

router = APIRouter(tags=["contest-ratings"])


def service(request: Request) -> ContestRatings:
    ratings = getattr(request.app.state, "contest_ratings", None)
    if ratings is None:
        raise HTTPException(503, "contest ratings unavailable")
    return ratings


@router.get("/contests/my-rating", response_model=ContestRating)
async def my_rating(
    response: Response,
    principal: Annotated[Principal, Depends(current_principal)],
    ratings: Annotated[ContestRatings, Depends(service)],
):
    response.headers["Cache-Control"] = "no-store"
    return await ratings.profile(principal)


@router.post("/admin/contest-settlements", response_model=ContestSettlement)
async def settle(
    body: SettleContest,
    response: Response,
    principal: Annotated[Principal, Depends(unsafe_principal)],
    ratings: Annotated[ContestRatings, Depends(service)],
):
    try:
        result = await ratings.settle(principal, body)
    except AdministrationDenied:
        raise HTTPException(403, "contest settlement requires administrator access") from None
    except SettlementConflict as error:
        raise HTTPException(409, str(error)) from None
    except ValueError as error:
        raise HTTPException(422, str(error)) from None
    response.headers["Cache-Control"] = "no-store"
    return result
