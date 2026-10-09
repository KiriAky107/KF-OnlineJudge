import base64
import json
from datetime import datetime
from uuid import UUID

from sqlalchemy import select, tuple_
from sqlalchemy.ext.asyncio import async_sessionmaker

from kf_oj.contracts.judging import MAX_SOURCE_BYTES, ArtifactReference
from kf_oj.contracts.judging_versions import decode_report
from kf_oj.contracts.submissions import (
    CancellationAccepted,
    PublicCaseResult,
    PublicJudgement,
    SubmissionDetail,
    SubmissionPage,
    SubmissionSummary,
)
from kf_oj.infra.artifacts.models import Artifact
from kf_oj.infra.artifacts.repository import ArtifactRepository
from kf_oj.infra.artifacts.store import ArtifactError
from kf_oj.modules.identity.public import Principal
from kf_oj.modules.submissions.models import Evaluation, Submission
from kf_oj.modules.submissions.public import (
    CancellationPort,
    CancelUnavailable,
    SubmissionUnavailable,
)


def cursor_encode(submission: Submission) -> str:
    return (
        base64.urlsafe_b64encode(
            f"{submission.created_at.isoformat()}|{submission.id}".encode("ascii")
        )
        .decode("ascii")
        .rstrip("=")
    )


def cursor_decode(value: str) -> tuple[datetime, UUID]:
    try:
        if not 1 <= len(value) <= 128:
            raise ValueError
        date, identifier = (
            base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
            .decode("ascii")
            .split("|")
        )
        timestamp = datetime.fromisoformat(date)
        if timestamp.utcoffset() is None:
            raise ValueError
        return timestamp, UUID(identifier)
    except (ValueError, UnicodeError):
        raise ValueError("invalid history cursor") from None


def summary(submission: Submission, evaluation: Evaluation) -> SubmissionSummary:
    terminal = evaluation.state in {"finished", "failed", "cancelled"}
    return SubmissionSummary(
        submission_id=submission.id,
        evaluation_id=evaluation.id,
        problem_version_id=submission.problem_version_id,
        language=submission.language,
        state=evaluation.state,
        created_at=submission.created_at,
        updated_at=evaluation.updated_at,
        attempts=evaluation.attempts_count,
        verdict=evaluation.verdict if terminal else None,
        failure_code=evaluation.failure_code,
    )


class SubmissionQueries:
    def __init__(
        self,
        sessions: async_sessionmaker,
        artifacts: ArtifactRepository,
        cancellation: CancellationPort | None = None,
    ):
        self.sessions, self.artifacts = sessions, artifacts
        self.cancellation = cancellation

    def owned(self, principal: Principal, submission_id: UUID):
        return (
            select(Submission, Evaluation)
            .join(Evaluation, Evaluation.submission_id == Submission.id)
            .where(Submission.id == submission_id, Submission.account_id == principal.account_id)
        )

    async def page(self, principal: Principal, after: str | None, limit: int) -> SubmissionPage:
        if not 1 <= limit <= 100:
            raise ValueError("invalid history limit")
        query = (
            select(Submission, Evaluation)
            .join(Evaluation, Evaluation.submission_id == Submission.id)
            .where(Submission.account_id == principal.account_id)
            .order_by(Submission.created_at.desc(), Submission.id.desc())
            .limit(limit + 1)
        )
        if after is not None:
            query = query.where(tuple_(Submission.created_at, Submission.id) < cursor_decode(after))
        async with self.sessions() as session:
            rows = (await session.execute(query)).all()
        return SubmissionPage(
            items=tuple(summary(s, e) for s, e in rows[:limit]),
            next_after=cursor_encode(rows[limit - 1][0]) if len(rows) > limit else None,
        )

    async def detail(self, principal: Principal, submission_id: UUID) -> SubmissionDetail:
        async with self.sessions() as session:
            row = (await session.execute(self.owned(principal, submission_id))).one_or_none()
        if row is None:
            raise SubmissionUnavailable("submission unavailable")
        submission, evaluation = row
        result = None
        if evaluation.state in {"finished", "failed", "cancelled"} and evaluation.final_report:
            # A single joined snapshot, strict validation and explicit projection prevent leaking
            # internal bindings, artifacts, hidden case identifiers or partial attempt reports.
            report = decode_report(
                json.dumps(
                    evaluation.final_report, ensure_ascii=False, separators=(",", ":")
                ).encode()
            )
            if str(report.binding.evaluation_id) != str(evaluation.id):
                raise ValueError("stored result binding invalid")
            if report.verdict.value != evaluation.verdict:
                raise ValueError("stored result verdict invalid")
            result = PublicJudgement(
                compilation_status=report.compilation.status,
                diagnostics=report.compilation.diagnostics,
                diagnostics_truncated=report.compilation.diagnostics_truncated,
                cpu_time_us=report.run_cpu_time_us,
                memory_peak_bytes=report.run_memory_peak_bytes,
                cleanup_complete=report.cleanup_complete,
                cases=tuple(
                    PublicCaseResult(position=i, **c.model_dump(exclude={"case_id"}))
                    for i, c in enumerate(report.cases)
                ),
            )
        return SubmissionDetail(**summary(submission, evaluation).model_dump(), result=result)

    async def cancel(self, principal: Principal, submission_id: UUID) -> CancellationAccepted:
        async with self.sessions() as session:
            row = (await session.execute(self.owned(principal, submission_id))).one_or_none()
        if row is None:
            raise SubmissionUnavailable("submission unavailable")
        submission, evaluation = row
        if self.cancellation is None or not await self.cancellation.cancel(evaluation.id):
            raise CancelUnavailable("cancellation service unavailable")
        return CancellationAccepted(submission_id=submission.id, evaluation_id=evaluation.id)

    async def owned_source(self, principal: Principal, submission_id: UUID) -> bytes:
        async with self.sessions() as session:
            submission = await session.scalar(
                select(Submission).where(
                    Submission.id == submission_id, Submission.account_id == principal.account_id
                )
            )
            if submission is None:
                raise SubmissionUnavailable("submission unavailable")
            metadata = await session.get(Artifact, submission.source_digest)
            if (
                metadata is None
                or metadata.state != "ready"
                or not 1 <= metadata.bytes <= MAX_SOURCE_BYTES
            ):
                raise SubmissionUnavailable("submission source unavailable")
            reference = ArtifactReference(digest=metadata.digest, bytes=metadata.bytes)
            try:
                return await self.artifacts.read(session, reference)
            except ArtifactError:
                raise SubmissionUnavailable("submission source unavailable") from None
