"""Only initial queued evaluation/outbox writes; execution belongs to Coordinator."""

import hashlib
import json
from uuid import uuid4

from sqlalchemy import func, select, text

from kf_oj.contracts.judging import ArtifactReference, TestCase
from kf_oj.contracts.judging_v2 import admit_manifest
from kf_oj.contracts.judging_versions import decode_report
from kf_oj.contracts.self_tests import CreateSelfTest, SelfTestDetail
from kf_oj.infra.artifacts.repository import ArtifactRepository
from kf_oj.infra.messaging.models import Outbox
from kf_oj.modules.identity.public import Principal
from kf_oj.modules.learning.public import linked_public_sample_diagnostic
from kf_oj.modules.problems.public import ProblemPort, ProblemUnavailable, public_diagnostic_sample
from kf_oj.modules.submissions.models import (
    Evaluation,
    SelfTest,
    SelfTestPublicComparison,
    Submission,
)
from kf_oj.modules.submissions.public import (
    IdempotencyConflict,
    SubmissionBusy,
    SubmissionUnavailable,
)


class SelfTestService:
    def __init__(self, sessions, problems: ProblemPort, artifacts: ArtifactRepository, admission):
        self.sessions, self.problems = sessions, problems
        self.artifacts, self.admission = artifacts, admission

    @staticmethod
    def _response(submission, evaluation):
        terminal = evaluation.state in {"finished", "failed", "cancelled"}
        values = {}
        if terminal and evaluation.final_report:
            report = decode_report(json.dumps(evaluation.final_report, ensure_ascii=False).encode())
            if (
                report.binding.evaluation_id != evaluation.id
                or report.binding.attempt_id != evaluation.current_attempt_id
                or report.binding.fencing_version != evaluation.fencing_version
                or any(case.case_id != submission.id for case in report.cases)
            ):
                raise ValueError("stored self-test result binding invalid")
            output = report.self_test_output
            values = dict(
                compile_diagnostics=report.compilation.diagnostics,
                compile_diagnostics_truncated=report.compilation.diagnostics_truncated,
                cpu_time_us=report.run_cpu_time_us,
                memory_peak_bytes=report.run_memory_peak_bytes,
            )
            if output:
                values.update(output.model_dump())
        return SelfTestDetail(
            self_test_id=submission.id,
            problem_version_id=submission.problem_version_id,
            language=submission.language,
            state=evaluation.state,
            created_at=submission.created_at,
            verdict=evaluation.verdict if terminal else None,
            failure_code=evaluation.failure_code if terminal else None,
            **values,
        )

    async def detail(self, principal, self_test_id):
        async with self.sessions() as session:
            row = (
                await session.execute(
                    select(SelfTest, Evaluation)
                    .join(Evaluation, Evaluation.self_test_id == SelfTest.id)
                    .where(SelfTest.id == self_test_id, SelfTest.account_id == principal.account_id)
                )
            ).one_or_none()
            if row is None:
                raise SubmissionUnavailable("self-test unavailable")
            return self._response(*row)

    @staticmethod
    async def _sample_metadata(session, principal, request: CreateSelfTest, stdin: bytes) -> dict:
        ordinal = request.public_sample_ordinal
        if ordinal is None:
            return {}
        try:
            sample = await public_diagnostic_sample(session, request.problem_version_id, ordinal)
        except ProblemUnavailable:
            return {}
        if sample.input_bytes != stdin:
            return {}
        reference = request.diagnostic_request_id
        if reference is not None and not await linked_public_sample_diagnostic(
            session, principal, reference, sample, request.language.value
        ):
            reference = None
        return {
            "ordinal": ordinal,
            "expected_digest": hashlib.sha256(sample.expected_bytes).hexdigest(),
            "diagnostic_request_id": reference,
        }

    async def create(self, principal: Principal, request: CreateSelfTest) -> SelfTestDetail:
        # Validate even trusted non-HTTP callers. No path or argv is accepted.
        request = CreateSelfTest.model_validate(
            request.model_dump(exclude={"language"}) | {"language": request.language}
        )
        source = request.source.encode("utf-8")
        input_bytes = request.stdin.encode("utf-8")
        source_digest = hashlib.sha256(source).hexdigest()
        input_digest = hashlib.sha256(input_bytes).hexdigest()
        request_fields = [
            str(request.problem_version_id),
            request.language.value,
            source_digest,
            input_digest,
        ]
        if request.public_sample_ordinal is not None:
            request_fields.append(str(request.public_sample_ordinal))
            request_fields.append(str(request.diagnostic_request_id or ""))
        request_digest = hashlib.sha256(
            json.dumps(request_fields, separators=(",", ":")).encode()
        ).hexdigest()
        if self.admission is None:
            raise SubmissionUnavailable("judge admission unavailable")
        async with self.sessions.begin() as session:
            # Bounded pending capacity and per-account idempotency serialize across API processes.
            # Two-key namespace is separate from artifact/slug advisory locks.
            await session.execute(text("SELECT pg_advisory_xact_lock(1262907470, 0)"))
            existing = (
                await session.execute(
                    select(SelfTest, Evaluation)
                    .join(Evaluation, Evaluation.self_test_id == SelfTest.id)
                    .where(
                        SelfTest.account_id == principal.account_id,
                        SelfTest.idempotency_key == request.idempotency_key,
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
            owned_self = await session.scalar(
                select(func.count())
                .select_from(SelfTest)
                .join(Evaluation, Evaluation.self_test_id == SelfTest.id)
                .where(SelfTest.account_id == principal.account_id, Evaluation.state.in_(pending))
            )
            if total >= 64 or owned + owned_self >= 8:
                raise SubmissionBusy("pending submission capacity exhausted")
            try:
                version = await self.problems.for_self_test(session, request.problem_version_id)
            except ProblemUnavailable:
                raise SubmissionUnavailable("problem version unavailable") from None
            if request.language not in version.languages:
                raise SubmissionUnavailable("language unavailable")
            sample_metadata = await self._sample_metadata(session, principal, request, input_bytes)
            # Bytes fsync before metadata/ref transaction. Rollback may leave collectable bytes,
            # but cannot leave an evaluation that refers to non-durable bytes.
            payloads = {
                source_digest: source,
                input_digest: input_bytes,
                hashlib.sha256(b"").hexdigest(): b"",
            }
            refs = {
                digest: await self.artifacts.put(session, payloads[digest])
                for digest in sorted(payloads)
            }
            reference = refs[source_digest]
            stdin = refs[input_digest]
            submission_id, evaluation_id = uuid4(), uuid4()
            manifest = self_test_manifest(
                version, evaluation_id, submission_id, request.language, reference, stdin
            )
            admit_manifest(self.admission, manifest, 1)
            submission = SelfTest(
                id=submission_id,
                account_id=principal.account_id,
                problem_id=version.problem_id,
                problem_version_id=version.version_id,
                language=request.language.value,
                source_digest=reference.digest,
                stdin_digest=stdin.digest,
                stdin_bytes=stdin.bytes,
                idempotency_key=request.idempotency_key,
                request_digest=request_digest,
            )
            session.add(submission)
            await session.flush()
            if sample_metadata:
                session.add(SelfTestPublicComparison(self_test_id=submission_id, **sample_metadata))
            await session.execute(
                Evaluation.__table__.insert().values(
                    id=evaluation_id,
                    self_test_id=submission_id,
                    problem_version_id=version.version_id,
                )
            )
            session.add(Outbox(id=uuid4(), evaluation_id=evaluation_id))
            await session.flush()
            evaluation = await session.get(Evaluation, evaluation_id)
            response = self._response(submission, evaluation)
        # The transaction commit must succeed before any success is returned.
        return response


def self_test_manifest(version, evaluation_id, self_test_id, language, source, stdin):
    empty = ArtifactReference(digest=hashlib.sha256(b"").hexdigest(), bytes=0)
    case = TestCase(case_id=self_test_id, input=stdin, expected=empty)
    return version.manifest(
        evaluation_id, self_test_id, language, source, cases=(case,), self_test=True
    )
