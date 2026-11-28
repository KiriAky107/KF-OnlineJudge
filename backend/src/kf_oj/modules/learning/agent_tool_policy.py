"""Authorize tool calls and share their limits across learning processes."""

from dataclasses import dataclass
from datetime import timedelta
from typing import Protocol
from uuid import UUID, uuid4

from sqlalchemy import case, func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kf_oj.contracts.agent_runtime import RunState
from kf_oj.modules.identity.public import ActiveSessionPort, AuthenticationFailed, Principal
from kf_oj.modules.learning.agent_models import AgentRunRow, AgentSessionRow, AgentTaskRow
from kf_oj.modules.learning.agent_tool_models import AgentToolAttemptRow, ReviewEvaluationBudgetRow
from kf_oj.runtime.tools import AgentToolGrant, Tool, ToolBudgetExceeded, ToolContext, ToolDenied

REVIEW_EVALUATION_READS_PER_WINDOW = 6


async def reserve_review_evaluation_read(
    db: AsyncSession, owner_id: UUID, problem_version_id: UUID
) -> bool:
    budget = ReviewEvaluationBudgetRow
    now = func.statement_timestamp()
    reset = budget.window_started_at <= now - timedelta(hours=1)
    # Unique scope and the row locked by the upsert apply the budget across workers.
    charged = await db.scalar(
        insert(budget)
        .values(
            owner_id=owner_id, problem_version_id=problem_version_id, window_started_at=now, reads=1
        )
        .on_conflict_do_update(
            index_elements=[budget.owner_id, budget.problem_version_id],
            set_={
                "window_started_at": case((reset, now), else_=budget.window_started_at),
                "reads": case((reset, 1), else_=budget.reads + 1),
            },
            where=or_(reset, budget.reads < REVIEW_EVALUATION_READS_PER_WINDOW),
        )
        .returning(budget.reads)
    )
    return charged is not None


class AgentToolGrants(Protocol):
    async def for_current_skill(
        self, db: AsyncSession, principal: Principal, version_id: UUID, configuration_digest: str
    ) -> AgentToolGrant | None: ...


@dataclass(frozen=True, slots=True)
class AgentToolLimits:
    run_attempts: int = 12
    account_attempts_per_hour: int = 60
    service_attempts_per_hour: int = 600
    account_in_flight: int = 1
    service_in_flight: int = 8
    run_seconds: int = 300

    def __post_init__(self) -> None:
        for value in (
            self.run_attempts,
            self.account_attempts_per_hour,
            self.service_attempts_per_hour,
            self.account_in_flight,
            self.service_in_flight,
            self.run_seconds,
        ):
            if type(value) is not int or value < 1:
                raise ValueError("invalid Agent tool budget")


class PostgresToolPolicy:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        principal: Principal,
        identity: ActiveSessionPort,
        grants: AgentToolGrants,
        limits: AgentToolLimits,
    ) -> None:
        self._sessions = sessions
        self._principal = principal
        self._identity = identity
        self._grants = grants
        self._limits = limits

    async def _require_current(
        self, db: AsyncSession, context: ToolContext, tool: Tool
    ) -> AgentRunRow:
        try:
            await self._identity.require_active_session(db, self._principal)
        except AuthenticationFailed:
            raise ToolDenied("Agent tool unavailable") from None
        run = await db.scalar(
            select(AgentRunRow)
            .join(AgentSessionRow, AgentSessionRow.id == AgentRunRow.session_id)
            .outerjoin(AgentTaskRow, AgentTaskRow.run_id == AgentRunRow.id)
            .where(
                AgentRunRow.id == context.run_id,
                AgentSessionRow.owner_id == self._principal.account_id,
                AgentRunRow.revision == context.run_revision,
                AgentRunRow.state == RunState.RUNNING.value,
                or_(
                    AgentTaskRow.run_id.is_(None),
                    AgentTaskRow.lease_expires_at > func.clock_timestamp(),
                ),
            )
            .with_for_update(read=True, of=AgentRunRow)
        )
        if run is None:
            raise ToolDenied("Agent tool unavailable")
        grant = await self._grants.for_current_skill(
            db, self._principal, run.skill_version_id, run.configuration_digest
        )
        if grant is None or tool.key not in grant.tools or tool.scope not in grant.scopes:
            raise ToolDenied("Agent tool unavailable")
        return run

    async def reserve(self, context: ToolContext, tool: Tool) -> UUID:
        async with self._sessions.begin() as db:
            run = await self._require_current(db, context, tool)
            # One short lock orders checks and inserts across callers sharing the service budget.
            await db.execute(text("SELECT pg_advisory_xact_lock(1262907471, 0)"))
            now = await db.scalar(select(func.clock_timestamp()))
            assert now is not None
            expires = now + timedelta(seconds=tool.timeout_seconds + 5)
            if expires > run.created_at + timedelta(seconds=self._limits.run_seconds):
                raise ToolBudgetExceeded("Agent run has reached its tool deadline")
            run_count = await db.scalar(
                select(func.count())
                .select_from(AgentToolAttemptRow)
                .where(AgentToolAttemptRow.run_id == run.id)
            )
            if run_count >= self._limits.run_attempts:
                raise ToolBudgetExceeded("Agent run tool attempts exhausted")
            owned = AgentToolAttemptRow.owner_id == self._principal.account_id
            active = AgentToolAttemptRow.finished_at.is_(None) & (
                AgentToolAttemptRow.expires_at > now
            )
            hourly = AgentToolAttemptRow.started_at > now - timedelta(hours=1)
            service_hour, account_hour, service_active, account_active = (
                await db.execute(
                    select(
                        func.count(),
                        func.count().filter(owned),
                        func.count().filter(active),
                        func.count().filter(active, owned),
                    )
                    .select_from(AgentToolAttemptRow)
                    .where(hourly)
                )
            ).one()
            limits = self._limits
            if (
                service_hour >= limits.service_attempts_per_hour
                or account_hour >= limits.account_attempts_per_hour
                or service_active >= limits.service_in_flight
                or account_active >= limits.account_in_flight
            ):
                raise ToolBudgetExceeded("Agent tool shared budget exhausted")
            attempt_id = uuid4()
            db.add(
                AgentToolAttemptRow(
                    id=attempt_id,
                    run_id=run.id,
                    owner_id=self._principal.account_id,
                    started_at=now,
                    expires_at=expires,
                )
            )
            return attempt_id

    async def require_current(self, context: ToolContext, tool: Tool) -> None:
        async with self._sessions.begin() as db:
            run = await self._require_current(db, context, tool)
            now = await db.scalar(select(func.clock_timestamp()))
            assert now is not None
            if now >= run.created_at + timedelta(seconds=self._limits.run_seconds):
                raise ToolBudgetExceeded("Agent run has reached its tool deadline")

    async def release(self, reservation_id: UUID) -> None:
        async with self._sessions.begin() as db:
            await db.execute(
                update(AgentToolAttemptRow)
                .where(
                    AgentToolAttemptRow.id == reservation_id,
                    AgentToolAttemptRow.owner_id == self._principal.account_id,
                    AgentToolAttemptRow.finished_at.is_(None),
                )
                .values(
                    finished_at=func.greatest(
                        AgentToolAttemptRow.started_at, func.clock_timestamp()
                    )
                )
            )
