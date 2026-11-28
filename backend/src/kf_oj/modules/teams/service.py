"""Public team profiles and membership managed by each team's captain."""

from typing import Literal
from uuid import UUID, uuid5

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kf_oj.contracts.teams import (
    CreateTeam,
    TeamMembershipView,
    TeamMembersPage,
    TeamMemberView,
    TeamPage,
    TeamView,
    UpdateTeamMember,
)
from kf_oj.modules.identity.public import ActiveSessionPort, Principal, enabled_account_names

from .models import Team, TeamMembership
from .public import TeamConflict, TeamUnavailable, require_team_role

_PAGE_SIZE = 50
_MEMBERS = ("captain", "member")


def _view(team: Team, count: int) -> TeamView:
    return TeamView(
        id=team.id,
        name=team.name,
        description=team.description,
        member_count=count,
        created_at=team.created_at,
    )


def _with_count():
    count = (
        select(func.count())
        .where(TeamMembership.team_id == Team.id, TeamMembership.role.in_(_MEMBERS))
        .scalar_subquery()
    )
    return select(Team, count)


async def _profile(session: AsyncSession, team_id: UUID) -> TeamView:
    row = (await session.execute(_with_count().where(Team.id == team_id))).one_or_none()
    if row is None:
        raise TeamUnavailable("team unavailable")
    return _view(*row)


class TeamService:
    def __init__(self, sessions: async_sessionmaker[AsyncSession], identity: ActiveSessionPort):
        self.sessions = sessions
        self.identity = identity

    async def page(self, search: str) -> TeamPage:
        async with self.sessions() as session:
            return await self._page(session, _with_count(), search)

    async def captained_page(self, principal: Principal, search: str) -> TeamPage:
        async with self.sessions.begin() as session:
            await self.identity.require_active_session(session, principal)
            captain = select(TeamMembership.team_id).where(
                TeamMembership.account_id == principal.account_id, TeamMembership.role == "captain"
            )
            return await self._page(session, _with_count().where(Team.id.in_(captain)), search)

    @staticmethod
    async def _page(session: AsyncSession, query, search: str) -> TeamPage:
        if search:
            query = query.where(Team.name.icontains(search, autoescape=True))
        rows = (
            await session.execute(
                query.order_by(Team.created_at.desc(), Team.id.desc()).limit(_PAGE_SIZE + 1)
            )
        ).all()
        return TeamPage(
            items=tuple(_view(*row) for row in rows[:_PAGE_SIZE]),
            truncated=len(rows) > _PAGE_SIZE,
        )

    async def profile(self, team_id: UUID) -> TeamView:
        async with self.sessions() as session:
            return await _profile(session, team_id)

    async def create(self, principal: Principal, body: CreateTeam) -> TeamView:
        team_id = uuid5(principal.account_id, f"team:{body.idempotency_key}")
        async with self.sessions.begin() as session:
            await self.identity.require_active_session(session, principal)
            created = await session.scalar(
                insert(Team)
                .values(id=team_id, name=body.name, description=body.description)
                .on_conflict_do_nothing(index_elements=[Team.id])
                .returning(Team.id)
            )
            if created is not None:
                session.add(
                    TeamMembership(
                        team_id=team_id,
                        account_id=principal.account_id,
                        role="captain",
                        approved_at=func.clock_timestamp(),
                    )
                )
                await session.flush()
            profile = await _profile(session, team_id)
            if (profile.name, profile.description) != (body.name, body.description):
                raise TeamConflict("team creation key reused with different details")
            return profile

    async def membership(self, principal: Principal, team_id: UUID) -> TeamMembershipView:
        async with self.sessions.begin() as session:
            await self.identity.require_active_session(session, principal)
            await self._require_team(session, team_id)
            row = await session.get(TeamMembership, (team_id, principal.account_id))
            return TeamMembershipView(role=row.role if row else None)

    async def apply(self, principal: Principal, team_id: UUID) -> TeamMembershipView:
        async with self.sessions.begin() as session:
            await self.identity.require_active_session(session, principal)
            await self._require_team(session, team_id)
            role = await session.scalar(
                insert(TeamMembership)
                .values(team_id=team_id, account_id=principal.account_id, role="pending")
                .on_conflict_do_update(
                    index_elements=[TeamMembership.team_id, TeamMembership.account_id],
                    set_={"role": TeamMembership.role},
                )
                .returning(TeamMembership.role)
            )
            return TeamMembershipView(role=role)

    async def leave(self, principal: Principal, team_id: UUID) -> None:
        async with self.sessions.begin() as session:
            await self.identity.require_active_session(session, principal)
            await self._require_team(session, team_id)
            row = await session.get(
                TeamMembership, (team_id, principal.account_id), with_for_update=True
            )
            if row is None:
                return
            if row.role == "captain":
                raise TeamConflict("captain role must remain assigned")
            await session.delete(row)

    async def members(
        self, principal: Principal, team_id: UUID, role: Literal["member", "pending"], offset: int
    ) -> TeamMembersPage:
        async with self.sessions.begin() as session:
            await require_team_role(session, self.identity, principal, team_id, captain=True)
            rows = (
                await session.scalars(
                    select(TeamMembership)
                    .where(TeamMembership.team_id == team_id, TeamMembership.role == role)
                    .order_by(TeamMembership.created_at.desc(), TeamMembership.account_id.desc())
                    .offset(offset)
                    .limit(_PAGE_SIZE + 1)
                )
            ).all()
            page = rows[:_PAGE_SIZE]
            names = await enabled_account_names(session, {row.account_id for row in page})
            return TeamMembersPage(
                items=tuple(
                    TeamMemberView(
                        account_id=row.account_id, username=names[row.account_id], role=role
                    )
                    for row in page
                    if row.account_id in names
                ),
                next_offset=offset + _PAGE_SIZE if len(rows) > _PAGE_SIZE else None,
            )

    async def moderate(
        self, principal: Principal, team_id: UUID, account_id: UUID, body: UpdateTeamMember
    ) -> TeamMembershipView:
        async with self.sessions.begin() as session:
            await require_team_role(session, self.identity, principal, team_id, captain=True)
            row = await session.get(TeamMembership, (team_id, account_id), with_for_update=True)
            if row is None:
                if body.decision == "remove":
                    return TeamMembershipView(role=None)
                raise TeamUnavailable("team application unavailable")
            if row.role == "captain":
                raise TeamConflict("captain role must remain assigned")
            if body.decision == "remove":
                await session.delete(row)
                return TeamMembershipView(role=None)
            if account_id not in await enabled_account_names(session, {account_id}):
                raise TeamUnavailable("team application unavailable")
            if row.role == "pending":
                row.role = "member"
                row.approved_at = func.clock_timestamp()
            return TeamMembershipView(role="member")

    @staticmethod
    async def _require_team(session: AsyncSession, team_id: UUID):
        if await session.get(Team, team_id) is None:
            raise TeamUnavailable("team unavailable")
