"""Only initial queued evaluation/outbox writes; execution belongs to Coordinator."""

import hashlib
import json
from uuid import uuid4

from sqlalchemy import func, select, text

from kf_oj.contracts.judging import JobManifest
from kf_oj.contracts.submissions import CreateSubmission, SubmissionCreated
from kf_oj.infra.artifacts.repository import ArtifactRepository
from kf_oj.infra.messaging.models import Outbox
from kf_oj.modules.identity.public import Principal
from kf_oj.modules.problems.public import ProblemPort, ProblemUnavailable
from kf_oj.modules.submissions.models import Evaluation, Submission
from kf_oj.modules.submissions.public import (
    IdempotencyConflict,
    SubmissionBusy,
    SubmissionUnavailable,
)


class SubmissionService:
    def __init__(self, sessions, problems: ProblemPort, artifacts: ArtifactRepository, admission):
        self.sessions, self.problems = sessions, problems
        self.artifacts, self.admission = artifacts, admission

    @staticmethod
    def _response(submission, evaluation):
        return SubmissionCreated(
            submission_id=submission.id,
            evaluation_id=evaluation.id,
            problem_version_id=submission.problem_version_id,
            language=submission.language,
            state=evaluation.state,
            created_at=submission.created_at,
        )

    async def create(self, principal: Principal, request: CreateSubmission) -> SubmissionCreated:
        # Validate even trusted non-HTTP callers. No path or argv is accepted.
        request = CreateSubmission.model_validate(request.model_dump())
        source = request.source.encode("utf-8")
        request_digest = hashlib.sha256(
            json.dumps(
                [
                    str(request.problem_version_id),
                    request.language.value,
                    hashlib.sha256(source).hexdigest(),
                ],
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        if self.admission is None:
            raise SubmissionUnavailable("judge admission unavailable")
        async with self.sessions.begin() as session:
            # Bounded pending capacity and per-account idempotency serialize across API processes.
            # Two-key namespace is separate from artifact/slug advisory locks.
            await session.execute(text("SELECT pg_advisory_xact_lock(1262907470, 0)"))
            existing = (
                await session.execute(
                    select(Submission, Evaluation)
                    .join(Evaluation, Evaluation.submission_id == Submission.id)
                    .where(
                        Submission.account_id == principal.account_id,
                        Submission.idempotency_key == request.idempotency_key,
                    )
                )
            ).one_or_none()
            if existing is not None:
                if existing[0].request_digest != request_digest:
                    raise IdempotencyConflict("idempotency key belongs to another request")
                return self._response(*existing)
            pending = ("queued", "claimed", "compiling", "running", "cancelling")
            total = await session.scalar(
                select(func.count()).select_from(Evaluation).where(Evaluation.state.in_(pending))
            )
            owned = await session.scalar(
                select(func.count())
                .select_from(Submission)
                .join(Evaluation, Evaluation.submission_id == Submission.id)
                .where(Submission.account_id == principal.account_id, Evaluation.state.in_(pending))
            )
            if total >= 64 or owned >= 8:
                raise SubmissionBusy("pending submission capacity exhausted")
            try:
                version = await self.problems.for_submission(session, request.problem_version_id)
            except ProblemUnavailable:
                raise SubmissionUnavailable("problem version unavailable") from None
            if request.language not in version.languages:
                raise SubmissionUnavailable("language unavailable")
            # Bytes fsync before metadata/ref transaction. Rollback may leave collectable bytes,
            # but cannot leave an evaluation that refers to non-durable bytes.
            reference = await self.artifacts.put(session, source)
            submission_id, evaluation_id = uuid4(), uuid4()
            manifest = JobManifest(
                schema_version=1,
                kind="judge.manifest",
                evaluation_id=evaluation_id,
                submission_id=submission_id,
                problem_version_id=version.version_id,
                language=request.language,
                toolchain=version.toolchain,
                source=reference,
                cases=version.cases,
                comparison="exact_bytes",
                compile_limits=version.compile_limits,
                run_limits=version.run_limits,
                job_wall_time_us=version.job_wall_time_us,
            )
            self.admission.admit(manifest, 1)
            submission = Submission(
                id=submission_id,
                account_id=principal.account_id,
                problem_id=version.problem_id,
                problem_version_id=version.version_id,
                language=request.language.value,
                source_digest=reference.digest,
                idempotency_key=request.idempotency_key,
                request_digest=request_digest,
            )
            session.add(submission)
            await session.flush()
            await session.execute(
                Evaluation.__table__.insert().values(
                    id=evaluation_id,
                    submission_id=submission_id,
                    problem_version_id=version.version_id,
                )
            )
            session.add(Outbox(id=uuid4(), evaluation_id=evaluation_id))
            await session.flush()
            evaluation = await session.get(Evaluation, evaluation_id)
            response = self._response(submission, evaluation)
        # The transaction commit must succeed before any success is returned.
        return response
