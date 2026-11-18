from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID


class CancelUnavailable(ValueError):
    pass


class CancellationPort(Protocol):
    async def cancel(self, evaluation_id: UUID) -> bool: ...


class SubmissionUnavailable(ValueError):
    pass


class WorkerAccessDenied(ValueError):
    """Never reveal whether the evaluation, attempt or artifact exists."""


class WorkerReadBusy(ValueError):
    pass


class IdempotencyConflict(ValueError):
    pass


class SubmissionBusy(ValueError):
    pass


@dataclass(frozen=True)
class LearningRecord:
    submission_id: UUID
    evaluation_id: UUID
    problem_id: UUID
    version_id: UUID
    created_at: datetime
    state: str
    verdict: str | None
    failure_code: str | None


@dataclass(frozen=True)
class LearningHistory:
    records: tuple[LearningRecord, ...]
    truncated: bool


async def learning_records(
    session, principal, *, since: datetime, limit: int = 500
) -> LearningHistory:
    from .learning_queries import records

    return await records(session, principal, since=since, limit=limit)


async def accepted_versions(session, principal, version_ids: set[UUID]) -> set[UUID]:
    from .learning_queries import accepted

    return await accepted(session, principal, version_ids)


async def learning_progress(
    session, principal, version_ids: set[UUID], *, since: datetime
) -> dict[UUID, LearningProgress]:
    from .learning_queries import progress

    return await progress(session, principal, version_ids, since=since)


@dataclass(frozen=True)
class LearningProgress:
    status: str
    evidence: tuple[LearningRecord, ...]
