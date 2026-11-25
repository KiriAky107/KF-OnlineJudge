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
    account_per_hour: int = 12
    service_per_hour: int = 120

    def __post_init__(self) -> None:
        if min(self.account_per_hour, self.service_per_hour) < 1:
            raise ValueError("model request budgets must be positive")


DEFAULT_MODEL_LIMITS = AgentModelLimits()


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

    owned = AgentModelAttemptRow.owner_id == owner_id
    in_flight = AgentModelAttemptRow.finished_at.is_(None) & (AgentModelAttemptRow.expires_at > now)
    used, owner_used, active, owner_active = (
        await db.execute(
            select(
                func.count(),
                func.count().filter(owned),
                func.count().filter(in_flight),
                func.count().filter(in_flight, owned),
            )
            .select_from(AgentModelAttemptRow)
            .where(AgentModelAttemptRow.started_at > now - timedelta(hours=1))
        )
    ).one()
    if (
        used >= limits.service_per_hour
        or owner_used >= limits.account_per_hour
        or active >= 4
        or owner_active >= 1
    ):
        raise ModelBudgetExceeded("learning model shared request budget exhausted")
    db.add(
        AgentModelAttemptRow(run_id=run.id, owner_id=owner_id, started_at=now, expires_at=expires)
    )


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
