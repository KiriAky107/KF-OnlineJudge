from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from kf_oj.api.dependencies import current_principal, unsafe_principal
from kf_oj.contracts.contests import ContestAgentAccess, ContestRegistration, ContestSchedule
from kf_oj.modules.contests.public import agent_access
from kf_oj.modules.contests.schedule import ContestSchedules, ScheduleConflict, ScheduleUnavailable
from kf_oj.modules.identity.public import AdministrationDenied, AuthenticationFailed, Principal

router = APIRouter(tags=["contest-schedule"])


def service(request: Request) -> ContestSchedules:
    schedules = getattr(request.app.state, "contest_schedules", None)
    if schedules is None:
        raise HTTPException(503, "contest service unavailable")
    return schedules


Schedules = Annotated[ContestSchedules, Depends(service)]
Reader = Annotated[Principal, Depends(current_principal)]
Writer = Annotated[Principal, Depends(unsafe_principal)]


@router.get("/contests", response_model=tuple[ContestSchedule, ...])
async def contests(schedules: Schedules):
    return await schedules.list()


@router.get("/contests/agent-access", response_model=ContestAgentAccess)
async def learning_agent_access(response: Response, principal: Reader, schedules: Schedules):
    response.headers["Cache-Control"] = "no-store"
    async with schedules.sessions() as db:
        return await agent_access(db, principal.account_id)


@router.get("/contests/{key}", response_model=ContestSchedule)
async def schedule(key: str, schedules: Schedules):
    try:
        return await schedules.detail(key)
    except ScheduleUnavailable:
        raise HTTPException(404, "contest unavailable") from None


@router.post("/admin/contest-schedules", response_model=ContestSchedule, status_code=201)
async def publish(body: ContestSchedule, principal: Writer, schedules: Schedules):
    try:
        return await schedules.publish(principal, body)
    except AdministrationDenied:
        raise HTTPException(403, "contest scheduling requires administrator access") from None
    except AuthenticationFailed:
        raise HTTPException(401, "authentication required") from None
    except ScheduleConflict as error:
        raise HTTPException(409, str(error)) from None


async def registration_result(
    response: Response,
    schedules: ContestSchedules,
    principal: Principal,
    key: str,
    *,
    confirm=False,
) -> ContestRegistration:
    response.headers["Cache-Control"] = "no-store"
    try:
        return ContestRegistration(
            confirmed=await schedules.registration(principal, key, confirm=confirm)
        )
    except AuthenticationFailed:
        raise HTTPException(401, "authentication required") from None
    except ScheduleUnavailable:
        raise HTTPException(404, "contest unavailable") from None
    except ScheduleConflict as error:
        raise HTTPException(409, str(error)) from None


@router.get("/contests/{key}/registration", response_model=ContestRegistration)
async def registration(key: str, response: Response, principal: Reader, schedules: Schedules):
    return await registration_result(response, schedules, principal, key)


@router.put("/contests/{key}/registration", response_model=ContestRegistration)
async def confirm(key: str, response: Response, principal: Writer, schedules: Schedules):
    return await registration_result(response, schedules, principal, key, confirm=True)
