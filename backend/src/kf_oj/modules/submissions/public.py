from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from kf_oj.contracts.judging import Verdict
from kf_oj.contracts.submission_evidence import SubmissionCodeEvidence, SubmissionSourceExcerpt
from kf_oj.contracts.submissions import SubmissionDetail


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
    session,
    principal,
    *,
    since: datetime,
    as_of: datetime | None = None,
    limit: int = 500,
    problem_ids: tuple[UUID, ...] = (),
    submission_id: UUID | None = None,
    anchor_submission_id: UUID | None = None,
) -> LearningHistory:
    from .learning_queries import records

    return await records(
        session,
        principal,
        since=since,
        as_of=as_of,
        limit=limit,
        problem_ids=problem_ids,
        submission_id=submission_id,
        anchor_submission_id=anchor_submission_id,
    )


async def recent_practice_versions(session, principal, *, since: datetime) -> tuple[UUID, ...]:
    from .learning_queries import recent_versions

    return await recent_versions(session, principal, since=since)


async def accepted_versions(session, principal, version_ids: set[UUID]) -> set[UUID]:
    from .learning_queries import accepted

    return await accepted(session, principal, version_ids)


@dataclass(frozen=True, slots=True)
class CatalogStatistics:
    total: int
    verdict_counts: dict[Verdict, int]


async def catalog_statistics(
    session, problem_ids: tuple[UUID, ...]
) -> dict[UUID, CatalogStatistics]:
    from .queries import catalog_counts

    return await catalog_counts(session, problem_ids)


@dataclass(frozen=True, slots=True)
class LearningPractice:
    last_attempt_at: datetime
    last_accepted_at: datetime | None
    first_accepted_at: datetime | None = None


async def learning_practice(
    session, principal, version_ids: set[UUID], *, as_of: datetime
) -> dict[UUID, LearningPractice]:
    from .learning_queries import practice

    return await practice(session, principal, version_ids, as_of=as_of)


async def learning_progress(
    session, principal, version_ids: set[UUID], *, since: datetime, as_of: datetime | None = None
) -> dict[UUID, LearningProgress]:
    from .learning_queries import progress

    return await progress(session, principal, version_ids, since=since, as_of=as_of)


@dataclass(frozen=True)
class LearningProgress:
    status: str
    evidence: tuple[LearningRecord, ...]


class SubmissionEvidencePort(Protocol):
    async def detail(self, principal, submission_id: UUID) -> SubmissionDetail: ...

    async def owned_source_excerpt(
        self, principal, submission_id: UUID, start_line: int, end_line: int
    ) -> SubmissionSourceExcerpt: ...

    async def learning_code_evidence(
        self,
        session,
        principal,
        *,
        since: datetime,
        as_of: datetime,
        problem_ids: tuple[UUID, ...],
        submission_id: UUID | None,
        comparison: bool,
        collect_control_flow: bool = False,
    ) -> SubmissionCodeEvidence | None: ...
