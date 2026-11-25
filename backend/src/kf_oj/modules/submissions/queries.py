import asyncio
import base64
import json
from datetime import UTC, datetime, time, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import Date, and_, cast, func, select, tuple_
from sqlalchemy.ext.asyncio import async_sessionmaker

from kf_oj.contracts.judging import MAX_SOURCE_BYTES, ArtifactReference, Verdict
from kf_oj.contracts.judging_versions import decode_report
from kf_oj.contracts.submission_evidence import (
    CodeSubmissionRef,
    SubmissionCodeEvidence,
    SubmissionSourceExcerpt,
)
from kf_oj.contracts.submissions import (
    CancellationAccepted,
    ProblemSubmissionStatus,
    PublicCaseResult,
    PublicJudgement,
    SubmissionActivity,
    SubmissionActivityDay,
    SubmissionComparison,
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
    CatalogStatistics,
    SubmissionUnavailable,
)
from kf_oj.modules.submissions.source_evidence import (
    compare_owned_sources,
    compiler_evidence,
    source_excerpt,
)


async def catalog_counts(session, problem_ids: tuple[UUID, ...]) -> dict[UUID, CatalogStatistics]:
    if not problem_ids:
        return {}
    verdicts = tuple(
        verdict for verdict in Verdict if verdict not in (Verdict.CANCELLED, Verdict.SYSTEM_ERROR)
    )
    trusted = and_(Evaluation.state == "finished", Evaluation.final_report_digest.is_not(None))
    query = (
        select(
            Submission.problem_id,
            func.count(),
            *(func.count().filter(trusted, Evaluation.verdict == verdict) for verdict in verdicts),
        )
        .select_from(Submission)
        .outerjoin(Evaluation, Evaluation.submission_id == Submission.id)
        .where(Submission.problem_id.in_(problem_ids))
        .group_by(Submission.problem_id)
    )
    return {
        problem_id: CatalogStatistics(total, dict(zip(verdicts, counts, strict=True)))
        for problem_id, total, *counts in await session.execute(query)
    }


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

    async def page(
        self,
        principal: Principal,
        after: str | None,
        limit: int,
        problem_version_id: UUID | None = None,
        language: str | None = None,
    ) -> SubmissionPage:
        if not 1 <= limit <= 100:
            raise ValueError("invalid history limit")
        query = (
            select(Submission, Evaluation)
            .join(Evaluation, Evaluation.submission_id == Submission.id)
            .where(Submission.account_id == principal.account_id)
            .order_by(Submission.created_at.desc(), Submission.id.desc())
            .limit(limit + 1)
        )
        if problem_version_id is not None:
            query = query.where(Submission.problem_version_id == problem_version_id)
        if language is not None:
            query = query.where(Submission.language == language)
        if after is not None:
            query = query.where(tuple_(Submission.created_at, Submission.id) < cursor_decode(after))
        async with self.sessions() as session:
            rows = (await session.execute(query)).all()
        return SubmissionPage(
            items=tuple(summary(s, e) for s, e in rows[:limit]),
            next_after=cursor_encode(rows[limit - 1][0]) if len(rows) > limit else None,
        )

    async def activity(
        self, principal: Principal, *, as_of: datetime | None = None
    ) -> SubmissionActivity:
        zone = ZoneInfo("Asia/Shanghai")
        now = as_of or datetime.now(UTC)
        end = now.astimezone(zone).date()
        start = end - timedelta(days=364)
        day = cast(func.timezone(zone.key, Submission.created_at), Date).label("day")
        query = (
            select(day, func.count())
            .where(
                Submission.account_id == principal.account_id,
                Submission.created_at >= datetime.combine(start, time.min, zone),
                Submission.created_at <= now,
            )
            .group_by(day)
            .order_by(day)
        )
        async with self.sessions() as session:
            rows = (await session.execute(query)).all()
        return SubmissionActivity(
            start=start,
            end=end,
            days=tuple(SubmissionActivityDay(day=day, count=count) for day, count in rows),
        )

    async def detail(self, principal: Principal, submission_id: UUID) -> SubmissionDetail:
        async with self.sessions() as session:
            row = (await session.execute(self.owned(principal, submission_id))).one_or_none()
        if row is None:
            raise SubmissionUnavailable("submission unavailable")
        return self._project_detail(*row)

    async def problem_status(
        self, principal: Principal, version_id: UUID
    ) -> ProblemSubmissionStatus:
        history = (
            select(Submission, Evaluation)
            .join(Evaluation, Evaluation.submission_id == Submission.id)
            .where(
                Submission.account_id == principal.account_id,
                Submission.problem_version_id == version_id,
            )
        )
        solved = (
            history.with_only_columns(Submission.id)
            .where(
                Evaluation.state == "finished",
                Evaluation.verdict == Verdict.ACCEPTED,
                Evaluation.failure_code.is_(None),
                Evaluation.final_report_digest.is_not(None),
            )
            .correlate(None)
            .exists()
        )
        latest = (
            history.add_columns(solved)
            .order_by(Submission.created_at.desc(), Submission.id.desc())
            .limit(1)
        )
        async with self.sessions() as session:
            row = (await session.execute(latest)).one_or_none()
        if row is None:
            return ProblemSubmissionStatus(solved=False, latest=None)
        submission, evaluation, passed = row
        return ProblemSubmissionStatus(solved=passed, latest=summary(submission, evaluation))

    @staticmethod
    def _project_detail(submission: Submission, evaluation: Evaluation) -> SubmissionDetail:
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

    async def compare(
        self, principal: Principal, submission_id: UUID, other_id: UUID
    ) -> SubmissionComparison:
        if submission_id == other_id:
            raise SubmissionUnavailable("submission comparison unavailable")
        async with self.sessions() as session:
            left = (await session.execute(self.owned(principal, submission_id))).one_or_none()
            right = (await session.execute(self.owned(principal, other_id))).one_or_none()
            if left is None or right is None or left[0].problem_id != right[0].problem_id:
                raise SubmissionUnavailable("submission comparison unavailable")
            return SubmissionComparison(
                left=self._project_detail(*left),
                right=self._project_detail(*right),
                left_source=(await self._read_source(session, left[0])).decode("utf-8"),
                right_source=(await self._read_source(session, right[0])).decode("utf-8"),
                same_version=left[0].problem_version_id == right[0].problem_version_id,
            )

    async def cancel(self, principal: Principal, submission_id: UUID) -> CancellationAccepted:
        async with self.sessions() as session:
            row = (await session.execute(self.owned(principal, submission_id))).one_or_none()
        if row is None:
            raise SubmissionUnavailable("submission unavailable")
        submission, evaluation = row
        if self.cancellation is None or not await self.cancellation.cancel(evaluation.id):
            raise CancelUnavailable("cancellation service unavailable")
        return CancellationAccepted(submission_id=submission.id, evaluation_id=evaluation.id)

    async def learning_code_evidence(
        self,
        session,
        principal: Principal,
        *,
        since: datetime,
        as_of: datetime,
        problem_ids: tuple[UUID, ...],
        submission_id: UUID | None,
        comparison: bool,
        collect_control_flow: bool = False,
    ) -> SubmissionCodeEvidence | None:
        history = (
            select(Submission, Evaluation)
            .join(Evaluation, Evaluation.submission_id == Submission.id)
            .where(
                Submission.account_id == principal.account_id,
                Submission.created_at.between(since, as_of),
            )
            .order_by(Submission.created_at.desc(), Submission.id.desc())
        )
        if problem_ids:
            history = history.where(Submission.problem_id.in_(problem_ids))
        selected = history
        if submission_id is not None:
            selected = selected.where(Submission.id == submission_id)
        found = (await session.execute(selected.limit(1))).one_or_none()
        if found is None:
            if submission_id is not None:
                raise SubmissionUnavailable("submission unavailable")
            return None

        submission, evaluation = found
        ref = self._code_ref(submission, evaluation)
        try:
            lines = (await self._read_source(session, submission)).splitlines()
        except SubmissionUnavailable:
            return SubmissionCodeEvidence(
                source=ref,
                source_available=False,
                line_count=None,
                compiler_locations=(),
                diagnostics_truncated=False,
            )

        report = (
            self._project_detail(submission, evaluation).result
            if evaluation.updated_at <= as_of
            else None
        )
        diagnostics = compiler_evidence(
            submission.language,
            report.diagnostics if report else "",
            len(lines),
            report.diagnostics_truncated if report else False,
        )
        diff = None
        if comparison:
            before = (
                await session.execute(
                    history.where(
                        Submission.problem_version_id == submission.problem_version_id,
                        Submission.language == submission.language,
                        tuple_(Submission.created_at, Submission.id)
                        < (submission.created_at, submission.id),
                    ).limit(1)
                )
            ).one_or_none()
            if before is not None:
                earlier, earlier_evaluation = before
                try:
                    source = await self._read_source(session, earlier)
                except SubmissionUnavailable:
                    source = None
                if source is not None:
                    diff = await asyncio.to_thread(
                        compare_owned_sources,
                        self._code_ref(earlier, earlier_evaluation),
                        source.splitlines(),
                        lines,
                        collect_control_flow=collect_control_flow,
                    )
        return SubmissionCodeEvidence(
            source=ref,
            source_available=True,
            line_count=len(lines),
            compiler_locations=diagnostics.locations,
            diagnostics_truncated=diagnostics.truncated,
            comparison=diff,
            compiler_correction=diagnostics.correction
            if evaluation.verdict == "compilation_error"
            and report is not None
            and report.compilation_status == "compilation_error"
            else None,
        )

    @staticmethod
    def _code_ref(submission: Submission, evaluation: Evaluation) -> CodeSubmissionRef:
        return CodeSubmissionRef(
            submission_id=submission.id,
            evaluation_id=evaluation.id,
            problem_version_id=submission.problem_version_id,
            language=submission.language,
        )

    async def owned_source(self, principal: Principal, submission_id: UUID) -> bytes:
        async with self.sessions() as session:
            submission = await session.scalar(
                select(Submission).where(
                    Submission.id == submission_id, Submission.account_id == principal.account_id
                )
            )
            if submission is None:
                raise SubmissionUnavailable("submission unavailable")
            return await self._read_source(session, submission)

    async def owned_source_excerpt(
        self, principal: Principal, submission_id: UUID, start_line: int, end_line: int
    ) -> SubmissionSourceExcerpt:
        async with self.sessions() as session:
            row = (await session.execute(self.owned(principal, submission_id))).one_or_none()
            if row is None:
                raise SubmissionUnavailable("submission unavailable")
            submission, evaluation = row
            data = await self._read_source(session, submission)
        return SubmissionSourceExcerpt(
            source=self._code_ref(submission, evaluation),
            start_line=start_line,
            end_line=end_line,
            text=source_excerpt(data, start_line, end_line),
        )

    async def _read_source(self, session, submission: Submission) -> bytes:
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
