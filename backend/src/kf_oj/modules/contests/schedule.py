"""Publish contest windows and allow authenticated accounts to confirm participation."""

from datetime import UTC, datetime

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import async_sessionmaker

from kf_oj.contracts.contests import ContestEditorPolicy, ContestSchedule
from kf_oj.contracts.contests import ContestEditorProblem as PublicEditorProblem
from kf_oj.modules.contests.models import ContestEditorProblem, ContestEntrant, ScheduledContest
from kf_oj.modules.identity.public import AdministrationPort, Principal
from kf_oj.modules.problems.public import editor_problem_titles


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

    async def editor_policy(
        self, principal: Principal, key: str, policy: ContestEditorPolicy | None = None
    ) -> ContestEditorPolicy:
        async with self.sessions.begin() as db:
            await self.identity.require_administrator(db, principal)
            contest = await db.get(ScheduledContest, key, with_for_update=policy is not None)
            if contest is None:
                raise ScheduleUnavailable
            versions = await self._editor_versions(db, key)
            if policy is None:
                return ContestEditorPolicy(
                    semantic_completion_enabled=contest.semantic_completion_enabled,
                    version_ids=versions,
                )
            if policy.version_ids != versions:
                if contest.starts_at <= datetime.now(UTC):
                    raise ScheduleConflict("contest problems are fixed once the contest starts")
                if len(await editor_problem_titles(db, policy.version_ids)) != len(
                    policy.version_ids
                ):
                    raise ScheduleConflict("contest problems must be published and enabled")
                await db.execute(
                    delete(ContestEditorProblem).where(ContestEditorProblem.contest_key == key)
                )
                db.add_all(
                    ContestEditorProblem(contest_key=key, version_id=version_id, position=position)
                    for position, version_id in enumerate(policy.version_ids)
                )
            contest.semantic_completion_enabled = policy.semantic_completion_enabled
            return policy

    async def editor_problems(
        self, principal: Principal, key: str
    ) -> tuple[PublicEditorProblem, ...]:
        async with self.sessions.begin() as db:
            await self.identity.require_active_session(db, principal)
            contest = await db.get(ScheduledContest, key)
            if contest is None or contest.starts_at > datetime.now(UTC):
                return ()
            if await db.get(ContestEntrant, (principal.account_id, key)) is None:
                return ()
            versions = await self._editor_versions(db, key)
            titles = await editor_problem_titles(db, versions)
            return tuple(
                PublicEditorProblem(version_id=v, title=titles[v]) for v in versions if v in titles
            )

    @staticmethod
    async def _editor_versions(db, key):
        return tuple(
            await db.scalars(
                select(ContestEditorProblem.version_id)
                .where(ContestEditorProblem.contest_key == key)
                .order_by(ContestEditorProblem.position)
            )
        )

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
