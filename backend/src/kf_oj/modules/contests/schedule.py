"""Publish contest windows and allow authenticated accounts to confirm participation."""

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import async_sessionmaker

from kf_oj.contracts.contests import ContestSchedule
from kf_oj.modules.contests.models import ContestEntrant, ScheduledContest
from kf_oj.modules.identity.public import AdministrationPort, Principal


class ScheduleUnavailable(LookupError):
    pass


class ScheduleConflict(ValueError):
    pass


class ContestSchedules:
    def __init__(self, sessions: async_sessionmaker, identity: AdministrationPort) -> None:
        self.sessions = sessions
        self.identity = identity

    async def list(self) -> tuple[ContestSchedule, ...]:
        async with self.sessions() as db:
            rows = await db.scalars(
                select(ScheduledContest)
                .order_by(ScheduledContest.starts_at.desc(), ScheduledContest.key)
                .limit(100)
            )
            return tuple(ContestSchedule.model_validate(row, from_attributes=True) for row in rows)

    async def detail(self, key: str) -> ContestSchedule:
        async with self.sessions() as db:
            row = await db.get(ScheduledContest, key)
            if row is None:
                raise ScheduleUnavailable
            return ContestSchedule.model_validate(row, from_attributes=True)

    async def publish(self, principal: Principal, body: ContestSchedule) -> ContestSchedule:
        async with self.sessions.begin() as db:
            await self.identity.require_administrator(db, principal)
            if body.key in {"agent-access", "my-rating"}:
                raise ScheduleConflict("contest key is reserved")
            if body.ends_at <= datetime.now(UTC):
                raise ScheduleConflict("contest has already ended")
            values = body.model_dump()
            values["title"] = body.title.strip()
            created = await db.scalar(
                insert(ScheduledContest)
                .values(**values)
                .on_conflict_do_nothing()
                .returning(ScheduledContest.key)
            )
            if created is None:
                raise ScheduleConflict("contest key already exists")
            return ContestSchedule.model_validate(values)

    async def registration(self, principal: Principal, key: str, *, confirm=False) -> bool:
        async with self.sessions.begin() as db:
            await self.identity.require_active_session(db, principal)
            contest = await db.get(ScheduledContest, key)
            if contest is None:
                raise ScheduleUnavailable
            existing = await db.get(ContestEntrant, (principal.account_id, key))
            if existing is not None:
                return True
            if not confirm:
                return False
            if contest.ends_at <= datetime.now(UTC):
                raise ScheduleConflict("contest has already ended")
            await db.execute(
                insert(ContestEntrant)
                .values(account_id=principal.account_id, contest_key=key)
                .on_conflict_do_nothing()
            )
            return True
