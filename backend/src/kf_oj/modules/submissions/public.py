from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from kf_oj.contracts.judging import ArtifactReference, Verdict
from kf_oj.contracts.learning_model import ChatSubmissionDetails, ReviewEvaluationCategory
from kf_oj.contracts.submission_evidence import SubmissionCodeEvidence, SubmissionSourceExcerpt


class CancelUnavailable(ValueError):
    pass


class CancellationPort(Protocol):
    async def cancel(self, evaluation_id: UUID) -> bool: ...


class SubmissionUnavailable(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class DiagnosticSourceReference:
    submission_id: UUID
    problem_version_id: UUID
    language: str
    artifact: ArtifactReference


async def owned_diagnostic_source(
    session, account_id: UUID, submission_id: UUID, version_id: UUID
) -> DiagnosticSourceReference:
    """Project sealed metadata; never read source here or grant execution on its own."""
    from sqlalchemy import select

    from kf_oj.contracts.judging import MAX_SOURCE_BYTES
    from kf_oj.infra.artifacts.models import Artifact

    from .models import Submission

    row = (
        await session.execute(
            select(Submission.language, Artifact.digest, Artifact.bytes)
            .join(Artifact, Artifact.digest == Submission.source_digest)
            .where(
                Submission.id == submission_id,
                Submission.account_id == account_id,
                Submission.problem_version_id == version_id,
                Submission.language.in_(("c17", "cpp20")),
                Artifact.state == "ready",
                Artifact.bytes.between(1, MAX_SOURCE_BYTES),
            )
        )
    ).one_or_none()
    if row is None:
        raise SubmissionUnavailable("diagnostic source unavailable")
    language, digest, size = row
    return DiagnosticSourceReference(
        submission_id, version_id, language, ArtifactReference(digest=digest, bytes=size)
    )


async def reviewed_diagnostic_reference(
    session,
    *,
    submission_id: UUID,
    evaluation_id: UUID,
    version_id: UUID,
    language: str,
    accepted_report_sha256: str,
    source: ArtifactReference,
) -> bool:
    """Require the coordinator's clean final acceptance for the exact source."""
    from sqlalchemy import and_, select

    from kf_oj.infra.artifacts.models import Artifact

    from .models import Evaluation, JudgeAttempt, Submission

    return bool(
        await session.scalar(
            select(Submission.id)
            .join(Evaluation, Evaluation.submission_id == Submission.id)
            .join(
                JudgeAttempt,
                and_(
                    JudgeAttempt.evaluation_id == Evaluation.id,
                    JudgeAttempt.id == Evaluation.current_attempt_id,
                ),
            )
            .join(Artifact, Artifact.digest == Submission.source_digest)
            .where(
                Submission.id == submission_id,
                Submission.problem_version_id == version_id,
                Submission.language == language,
                Evaluation.id == evaluation_id,
                Evaluation.state == "finished",
                Evaluation.verdict == Verdict.ACCEPTED,
                Evaluation.failure_code.is_(None),
                Evaluation.final_report.is_not(None),
                Evaluation.final_report_digest == accepted_report_sha256,
                JudgeAttempt.report_digest == accepted_report_sha256,
                JudgeAttempt.cleanup_complete.is_(True),
                JudgeAttempt.revoked_at.is_not(None),
                Artifact.digest == source.digest,
                Artifact.bytes == source.bytes,
                Artifact.state == "ready",
            )
        )
    )


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


async def recent_practice_versions(
    session, principal, *, since: datetime, as_of: datetime | None = None
) -> tuple[UUID, ...]:
    from .learning_queries import recent_versions

    return await recent_versions(session, principal, since=since, as_of=as_of)


async def owned_review_evaluation_category(
    session,
    principal,
    *,
    submission_id: UUID,
    evaluation_id: UUID,
    problem_version_id: UUID,
    as_of: datetime,
) -> ReviewEvaluationCategory | None:
    from .learning_queries import review_evaluation_category

    return await review_evaluation_category(
        session,
        principal,
        submission_id=submission_id,
        evaluation_id=evaluation_id,
        problem_version_id=problem_version_id,
        as_of=as_of,
    )


async def owned_guided_submission_details(
    session,
    principal,
    *,
    submission_id: UUID,
    problem_version_id: UUID,
    as_of: datetime,
) -> ChatSubmissionDetails | None:
    from .learning_queries import guided_submission_details

    return await guided_submission_details(
        session,
        principal,
        submission_id=submission_id,
        problem_version_id=problem_version_id,
        as_of=as_of,
    )


async def accepted_versions(
    session, principal, version_ids: set[UUID], *, as_of: datetime | None = None
) -> set[UUID]:
    from .learning_queries import accepted

    return await accepted(session, principal, version_ids, as_of=as_of)


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


async def team_training_verdict_totals(
    session, cohort, version_ids: set[UUID], *, as_of: datetime
) -> dict[UUID, dict[str, int]]:
    """Return verdict totals over a server-selected learning cohort."""
    from .learning_queries import team_verdict_totals

    return await team_verdict_totals(session, cohort, version_ids, as_of=as_of)


async def training_acceptance_totals(session, cohort) -> dict[str, tuple[int, int, int]]:
    """Count assigned, attempted and passed exercises in a fixed observation window."""
    from .learning_queries import acceptance_totals

    return await acceptance_totals(session, cohort)


async def public_self_test_comparison_totals(
    session, start: datetime, end: datetime
) -> dict[bool, tuple[int, int, int]]:
    """Count byte-comparable public sample runs without exposing source or output."""
    from .learning_queries import self_test_comparison_totals

    return await self_test_comparison_totals(session, start, end)


@dataclass(frozen=True)
class LearningProgress:
    status: str
    evidence: tuple[LearningRecord, ...]


class SubmissionEvidencePort(Protocol):
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
