from collections.abc import Awaitable
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from kf_oj.api.dependencies import current_principal, unsafe_principal
from kf_oj.contracts.teams import (
    CreateTeam,
    TeamMembershipView,
    TeamMembersPage,
    TeamPage,
    TeamView,
    UpdateTeamMember,
)
from kf_oj.modules.identity.public import AuthenticationFailed, Principal
from kf_oj.modules.teams.public import TeamConflict, TeamPermissionDenied, TeamUnavailable
from kf_oj.modules.teams.service import TeamService

router = APIRouter(prefix="/teams", tags=["teams"])
Reader = Annotated[Principal, Depends(current_principal)]
Writer = Annotated[Principal, Depends(unsafe_principal)]


def service(request: Request) -> TeamService:
    value = getattr(request.app.state, "teams", None)
    if value is None:
        raise HTTPException(503, "teams unavailable")
    return value


Teams = Annotated[TeamService, Depends(service)]


async def _action[Result](operation: Awaitable[Result]) -> Result:
    try:
        return await operation
    except AuthenticationFailed:
        raise HTTPException(401, "authentication required") from None
    except TeamPermissionDenied:
        raise HTTPException(403, "team role required") from None
    except TeamUnavailable:
        raise HTTPException(404, "team resource unavailable") from None
    except TeamConflict:
        raise HTTPException(409, "team request conflicts with existing state") from None


@router.get("", response_model=TeamPage)
async def team_page(teams: Teams, search: Annotated[str, Query(max_length=30)] = ""):
    return await teams.page(search.strip())


@router.post("", response_model=TeamView, status_code=201)
async def create_team(body: CreateTeam, principal: Writer, teams: Teams):
    return await _action(teams.create(principal, body))


@router.get("/captained", response_model=TeamPage)
async def my_captained_teams(
    principal: Reader, teams: Teams, search: Annotated[str, Query(max_length=30)] = ""
):
    return await _action(teams.captained_page(principal, search.strip()))


@router.get("/{team_id}", response_model=TeamView)
async def team_profile(team_id: UUID, teams: Teams):
    return await _action(teams.profile(team_id))


@router.get("/{team_id}/membership", response_model=TeamMembershipView)
async def my_team_membership(team_id: UUID, principal: Reader, teams: Teams):
    return await _action(teams.membership(principal, team_id))


@router.put("/{team_id}/membership", response_model=TeamMembershipView)
async def request_team_membership(team_id: UUID, principal: Writer, teams: Teams):
    return await _action(teams.apply(principal, team_id))


@router.delete("/{team_id}/membership", status_code=204)
async def leave_team(team_id: UUID, principal: Writer, teams: Teams) -> None:
    await _action(teams.leave(principal, team_id))


@router.get("/{team_id}/members", response_model=TeamMembersPage)
async def team_members(
    team_id: UUID,
    principal: Reader,
    teams: Teams,
    role: Literal["member", "pending"] = "pending",
    offset: Annotated[int, Query(ge=0)] = 0,
):
    return await _action(teams.members(principal, team_id, role, offset))


@router.put("/{team_id}/members/{account_id}", response_model=TeamMembershipView)
async def manage_team_member(
    team_id: UUID, account_id: UUID, body: UpdateTeamMember, principal: Writer, teams: Teams
):
    return await _action(teams.moderate(principal, team_id, account_id, body))
