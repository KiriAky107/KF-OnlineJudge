"""PostgreSQL-backed team membership and captain checks."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kf_oj.contracts.teams import TeamRole
from kf_oj.modules.identity.public import ActiveSessionPort, Principal, enabled_account_ids

from .models import TeamMembership


class TeamUnavailable(LookupError):
    pass


class TeamPermissionDenied(ValueError):
    pass


class TeamConflict(ValueError):
    pass


def active_team_learners(team_id: UUID):
    return select(TeamMembership.account_id, TeamMembership.approved_at).where(
        TeamMembership.team_id == team_id,
        TeamMembership.role == "member",
        TeamMembership.account_id.in_(enabled_account_ids()),
    )


async def require_team_role(
    session: AsyncSession,
    identity: ActiveSessionPort,
    principal: Principal,
    team_id: UUID,
    *,
    captain: bool = False,
) -> TeamRole:
    """Hold the original membership until the caller finishes its transaction."""
    await identity.require_active_session(session, principal)
    allowed = ("captain",) if captain else ("captain", "member")
    role = await session.scalar(
        select(TeamMembership.role)
        .where(
            TeamMembership.team_id == team_id,
            TeamMembership.account_id == principal.account_id,
            TeamMembership.role.in_(allowed),
        )
        .with_for_update(read=True)
    )
    if role is None:
        raise TeamPermissionDenied("team role required")
    return role
