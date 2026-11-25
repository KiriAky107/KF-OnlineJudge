"""PostgreSQL queue and lease transitions for learning workflows."""

from dataclasses import dataclass, field
from datetime import timedelta
from uuid import UUID

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kf_oj.contracts.agent_runtime import AgentRun, RunState
from kf_oj.modules.contests.public import AgentUnavailableDuringContest, require_agent_access
from kf_oj.modules.identity.public import AcceptedSession
from kf_oj.modules.learning.agent_model_budget import finish_model_attempt
from kf_oj.modules.learning.agent_models import (
    AgentRunRow,
    AgentSessionRow,
    AgentStepRow,
    AgentTaskRow,
    AgentWorkflowRow,
)
from kf_oj.modules.learning.agent_store import (
    _run_record,
    begin_timing,
    clear_partial_answer,
    finish_timing,
)
from kf_oj.runtime.state import check_run_transition
from kf_oj.runtime.storage import AgentRecordUnavailable


@dataclass(frozen=True, slots=True)
class AgentLease:
    run: AgentRun
    owner_id: UUID
    receipt: AcceptedSession = field(repr=False)
    attempt: int


def _move(run: AgentRunRow, following: RunState) -> None:
    check_run_transition(RunState(run.state), following)
    run.state = following.value
    run.revision += 1


class AgentQueue:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    @staticmethod
    async def _request_cancel(db: AsyncSession, run: AgentRunRow) -> None:
        await clear_partial_answer(db, run.id)
        _move(run, RunState.CANCEL_REQUESTED)

    @staticmethod
    async def _close_cancelled(db: AsyncSession, run: AgentRunRow, task: AgentTaskRow) -> None:
        if run.state == RunState.CANCEL_REQUESTED:
            await finish_model_attempt(db, run.id, usage=None)
        _move(run, RunState.CANCELLED)
        task.lease_expires_at = None
        await finish_timing(db, run.id)
        await db.execute(
            update(AgentStepRow)
            .where(AgentStepRow.run_id == run.id, AgentStepRow.state == "running")
            .values(state="cancelled")
        )

    async def claim(self, *, duration_seconds: int = 45) -> AgentLease | None:
        if not 1 <= duration_seconds <= 120:
            raise ValueError("invalid learning lease duration")
        async with self._sessions.begin() as db:
            now = await db.scalar(select(func.clock_timestamp()))
            expired = AgentTaskRow.lease_expires_at <= now
            available = and_(
                AgentRunRow.state.in_((RunState.QUEUED.value, RunState.RETRY_WAIT.value)),
                AgentTaskRow.available_at <= now,
            )
            found = (
                await db.execute(
                    select(AgentRunRow, AgentTaskRow, AgentSessionRow.owner_id)
                    .join(AgentTaskRow, AgentTaskRow.run_id == AgentRunRow.id)
                    .join(AgentSessionRow, AgentSessionRow.id == AgentRunRow.session_id)
                    .where(
                        or_(
                            available,
                            and_(
                                AgentRunRow.state.in_(
                                    (RunState.RUNNING.value, RunState.CANCEL_REQUESTED.value)
                                ),
                                expired,
                            ),
                        )
                    )
                    .order_by(AgentTaskRow.available_at, AgentRunRow.id)
                    .limit(1)
                    .with_for_update(of=AgentRunRow, skip_locked=True)
                )
            ).one_or_none()
            if found is None:
                return None
            run, task, owner = found
            if run.state == RunState.CANCEL_REQUESTED:
                await self._close_cancelled(db, run, task)
                return None
            if run.state == RunState.RUNNING:
                await clear_partial_answer(db, run.id)
            if task.attempts >= 3:
                _move(run, RunState.FAILED)
                task.lease_expires_at = None
                await finish_timing(db, run.id)
                reason = "learning_attempts_exhausted"
                await db.execute(
                    update(AgentStepRow)
                    .where(AgentStepRow.run_id == run.id, AgentStepRow.state == "running")
                    .values(state="failed", error_code=reason)
                )
                await db.execute(
                    update(AgentWorkflowRow)
                    .where(AgentWorkflowRow.run_id == run.id)
                    .values(failure_code=reason)
                )
                return None
            if run.state == RunState.RUNNING:
                run.revision += 1
                await db.execute(
                    update(AgentStepRow)
                    .where(AgentStepRow.run_id == run.id, AgentStepRow.state == "running")
                    .values(state="failed", error_code="learning_execution_interrupted")
                )
            else:
                if run.state == RunState.RETRY_WAIT:
                    _move(run, RunState.QUEUED)
                _move(run, RunState.RUNNING)
            if task.attempts == 0:
                await begin_timing(db, run.id, now)
            task.attempts += 1
            task.lease_expires_at = now + timedelta(seconds=duration_seconds)
            return AgentLease(
                _run_record(run),
                owner,
                AcceptedSession(task.authorization_version, task.session_proof),
                task.attempts,
            )

    async def renew(self, lease: AgentLease, *, duration_seconds: int = 45) -> bool:
        if not 1 <= duration_seconds <= 120:
            raise ValueError("invalid learning lease duration")
        async with self._sessions.begin() as db:
            run = await db.get(AgentRunRow, lease.run.run_id, with_for_update=True)
            if run is None or run.state != RunState.RUNNING or run.revision != lease.run.revision:
                return False
            task = await db.get(AgentTaskRow, run.id)
            now = await db.scalar(select(func.clock_timestamp()))
            if task is None or task.lease_expires_at is None or task.lease_expires_at <= now:
                return False
            try:
                await require_agent_access(db, lease.owner_id)
            except AgentUnavailableDuringContest:
                await self._request_cancel(db, run)
                return False
            task.lease_expires_at = now + timedelta(seconds=duration_seconds)
            return True

    async def defer(self, db: AsyncSession, running: AgentRun) -> AgentRun | None:
        run = await db.get(AgentRunRow, running.run_id, with_for_update=True)
        if run is None or run.state != RunState.RUNNING or run.revision != running.revision:
            return None
        task = await db.get(AgentTaskRow, run.id)
        now = await db.scalar(select(func.clock_timestamp()))
        if (
            task is None
            or task.attempts >= 3
            or task.lease_expires_at is None
            or task.lease_expires_at <= now
        ):
            return None
        await clear_partial_answer(db, run.id)
        _move(run, RunState.RETRY_WAIT)
        task.available_at = now + timedelta(seconds=3 * task.attempts)
        task.lease_expires_at = None
        return _run_record(run)

    async def cancel(self, db: AsyncSession, owner: UUID, run_id: UUID) -> AgentRun:
        found = (
            await db.execute(
                select(AgentRunRow, AgentTaskRow)
                .join(AgentTaskRow, AgentTaskRow.run_id == AgentRunRow.id)
                .join(AgentSessionRow, AgentSessionRow.id == AgentRunRow.session_id)
                .where(AgentRunRow.id == run_id, AgentSessionRow.owner_id == owner)
                .with_for_update(of=AgentRunRow)
            )
        ).one_or_none()
        if found is None:
            raise AgentRecordUnavailable("learning run unavailable")
        run, task = found
        if run.state in (RunState.QUEUED, RunState.RETRY_WAIT):
            await self._close_cancelled(db, run, task)
        elif run.state == RunState.RUNNING:
            await self._request_cancel(db, run)
        return _run_record(run)

    async def acknowledge_cancelled(self, lease: AgentLease) -> bool:
        async with self._sessions.begin() as db:
            run = await db.get(AgentRunRow, lease.run.run_id, with_for_update=True)
            if (
                run is None
                or run.state != RunState.CANCEL_REQUESTED
                or run.revision != lease.run.revision + 1
            ):
                return False
            task = await db.get(AgentTaskRow, run.id)
            assert task is not None
            await self._close_cancelled(db, run, task)
            return True
