"""Server-time eligibility for accounts that confirmed contest participation."""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from kf_oj.contracts.contests import ContestAgentAccess
from kf_oj.modules.contests.models import ContestEditorProblem, ContestEntrant, ScheduledContest


class AgentUnavailableDuringContest(RuntimeError):
    pass


def _registered_contests(account_id: UUID):
    return (
        select(ScheduledContest.key)
        .join(ContestEntrant, ContestEntrant.contest_key == ScheduledContest.key)
        .where(ContestEntrant.account_id == account_id)
    )


async def require_agent_access(db: AsyncSession, account_id: UUID) -> None:
    now = datetime.now(UTC)
    active = _registered_contests(account_id).where(
        ScheduledContest.starts_at <= now,
        ScheduledContest.ends_at > now,
    )
    if await db.scalar(select(active.exists())):
        raise AgentUnavailableDuringContest


async def blocks_semantic_completion(db: AsyncSession, account_id: UUID, version_id: UUID) -> bool:
    now = datetime.now(UTC)
    restricted = (
        _registered_contests(account_id)
        .join(ContestEditorProblem, ContestEditorProblem.contest_key == ScheduledContest.key)
        .where(
            ContestEditorProblem.version_id == version_id,
            ScheduledContest.semantic_completion_enabled.is_(False),
            ScheduledContest.starts_at <= now,
            ScheduledContest.ends_at > now,
        )
    )
    return bool(await db.scalar(select(restricted.exists())))


async def agent_access(db: AsyncSession, account_id: UUID) -> ContestAgentAccess:
    now = datetime.now(UTC)
    end, start = (
        await db.execute(
            _registered_contests(account_id)
            .with_only_columns(
                func.max(ScheduledContest.ends_at).filter(ScheduledContest.starts_at <= now),
                func.min(ScheduledContest.starts_at).filter(ScheduledContest.starts_at > now),
                maintain_column_froms=True,
            )
            .where(ScheduledContest.ends_at > now)
        )
    ).one()
    return ContestAgentAccess(blocked_until=end, next_start_at=start, checked_at=now)
