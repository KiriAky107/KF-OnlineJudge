"""Shared rolling request budgets for the optional Responses provider."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from kf_oj.contracts.learning_model import ModelTokenUsage
from kf_oj.modules.learning.agent_models import AgentModelAttemptRow, AgentRunRow, AgentTaskRow
from kf_oj.runtime.model import ModelBudgetExceeded
from kf_oj.runtime.storage import AgentRecordConflict


@dataclass(frozen=True, slots=True)
class AgentModelLimits:
    account_per_hour: int | None = None
    service_per_hour: int | None = None

    def __post_init__(self) -> None:
        if any(
            limit is not None and limit < 1
            for limit in (self.account_per_hour, self.service_per_hour)
        ):
            raise ValueError("model request budgets must be positive")


DEFAULT_MODEL_LIMITS = AgentModelLimits()


async def _within_hourly_limits(
    db: AsyncSession, owner_id: UUID, now: datetime, limits: AgentModelLimits
) -> bool:
    if limits.service_per_hour is None and limits.account_per_hour is None:
        return True
    owned = AgentModelAttemptRow.owner_id == owner_id
    requests = func.sum(AgentModelAttemptRow.provider_requests)
    used, owner_used = (
        await db.execute(
            select(
                func.coalesce(requests, 0),
                func.coalesce(requests.filter(owned), 0),
            )
            .select_from(AgentModelAttemptRow)
            .where(AgentModelAttemptRow.started_at > now - timedelta(hours=1))
        )
    ).one()
    return (limits.service_per_hour is None or used < limits.service_per_hour) and (
        limits.account_per_hour is None or owner_used < limits.account_per_hour
    )


async def reserve_model_attempt(
    db: AsyncSession,
    run: AgentRunRow,
    owner_id: UUID,
    limits: AgentModelLimits,
    request_seconds: int,
) -> None:
    # Use the same application lock namespace as tool budgets, with a separate slot.
    await db.execute(text("SELECT pg_advisory_xact_lock(1262907471, 1)"))
    now: datetime = await db.scalar(select(func.clock_timestamp()))
    lease = await db.get(AgentTaskRow, run.id)
    if lease is None or lease.lease_expires_at is None or lease.lease_expires_at <= now:
        raise AgentRecordConflict("model call requires a current learning lease")
    expires = now + timedelta(seconds=request_seconds + 5)
    if expires > run.created_at + timedelta(minutes=5):
        raise ModelBudgetExceeded("learning run reached its model deadline")

    active, owner_active = (
        await db.execute(
            select(
                func.count(),
                func.count().filter(AgentModelAttemptRow.owner_id == owner_id),
            )
            .select_from(AgentModelAttemptRow)
            .where(
                AgentModelAttemptRow.finished_at.is_(None), AgentModelAttemptRow.expires_at > now
            )
        )
    ).one()
    if (
        active >= 4
        or owner_active >= 1
        or not await _within_hourly_limits(db, owner_id, now, limits)
    ):
        raise ModelBudgetExceeded("learning model shared request budget exhausted")
    db.add(
        AgentModelAttemptRow(run_id=run.id, owner_id=owner_id, started_at=now, expires_at=expires)
    )


async def reserve_model_continuation(
    db: AsyncSession, run_id: UUID, owner_id: UUID, limits: AgentModelLimits
) -> bool:
    await db.execute(text("SELECT pg_advisory_xact_lock(1262907471, 1)"))
    now: datetime = await db.scalar(select(func.clock_timestamp()))
    attempt = await db.get(AgentModelAttemptRow, run_id)
    if (
        attempt is None
        or attempt.owner_id != owner_id
        or attempt.provider_requests != 1
        or attempt.finished_at is not None
        or attempt.expires_at <= now
    ):
        return False
    if not await _within_hourly_limits(db, owner_id, now, limits):
        return False
    attempt.provider_requests = 2
    return True


async def finish_model_attempt(
    db: AsyncSession, run_id: UUID, usage: ModelTokenUsage | None
) -> None:
    await db.execute(
        update(AgentModelAttemptRow)
        .where(
            AgentModelAttemptRow.run_id == run_id,
            AgentModelAttemptRow.finished_at.is_(None),
        )
        .values(
            finished_at=func.greatest(AgentModelAttemptRow.started_at, func.clock_timestamp()),
            reported_usage=usage.model_dump(mode="json") if usage is not None else None,
        )
    )
