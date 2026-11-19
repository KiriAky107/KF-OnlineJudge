import asyncio
import hashlib
import hmac
import json
import re
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import async_sessionmaker

from kf_oj.contracts.judging import ArtifactReference, LeaseBinding
from kf_oj.contracts.judging_versions import (
    Manifest,
    decode_manifest,
    manifest_bytes,
)
from kf_oj.infra.artifacts.repository import ArtifactRepository
from kf_oj.infra.artifacts.store import ArtifactError
from kf_oj.modules.problems.public import ProblemPort, ProblemUnavailable
from kf_oj.modules.submissions import repository
from kf_oj.modules.submissions.public import WorkerAccessDenied, WorkerReadBusy
from kf_oj.modules.submissions.self_tests import self_test_manifest


class WorkerAccess:
    def __init__(
        self, sessions: async_sessionmaker, problems: ProblemPort, artifacts: ArtifactRepository
    ):
        self.sessions, self.problems, self.artifacts = sessions, problems, artifacts
        self._reads = asyncio.Semaphore(2)

    @asynccontextmanager
    async def _read_slot(self):
        if self._reads.locked():
            raise WorkerReadBusy("worker read capacity exhausted")
        async with self._reads:
            yield

    @staticmethod
    def _token_hash(token: str) -> str:
        if not re.fullmatch(r"[0-9a-f]{64}", token):
            raise WorkerAccessDenied("worker grant rejected")
        return hashlib.sha256(token.encode("ascii")).hexdigest()

    async def _manifest(self, session, binding: LeaseBinding, token: str) -> Manifest:
        row = await repository.leased_submission(session, binding, self._token_hash(token))
        if row is None:
            raise WorkerAccessDenied("worker grant rejected")
        evaluation, attempt, submission, self_test = row
        execution = self_test if self_test is not None else submission
        if execution is None:
            raise WorkerAccessDenied("worker grant rejected")
        submission = execution
        # Use a sanitized decoder at this trust boundary. JSONB snapshots are immutable for
        # runtime roles; use canonical bytes for both retrieval and digest verification.

        try:
            manifest = decode_manifest(json.dumps(attempt.manifest).encode())
            if (
                manifest.self_test != (self_test is not None)
                or manifest.evaluation_id != evaluation.id
                or manifest.submission_id != submission.id
                or manifest.problem_version_id != evaluation.problem_version_id
                or manifest.problem_version_id != submission.problem_version_id
                or manifest.language.value != submission.language
                or manifest.source.digest != submission.source_digest
                or not hmac.compare_digest(
                    hashlib.sha256(manifest_bytes(manifest)).hexdigest(), attempt.manifest_digest
                )
            ):
                raise WorkerAccessDenied("worker snapshot rejected")
            if self_test is not None:
                version = await self.problems.for_self_test(
                    session, manifest.problem_version_id, accepted=True
                )
                stdin = ArtifactReference(
                    digest=self_test.stdin_digest, bytes=self_test.stdin_bytes
                )
                expected = self_test_manifest(
                    version,
                    manifest.evaluation_id,
                    manifest.submission_id,
                    manifest.language,
                    manifest.source,
                    stdin,
                )
            else:
                version = await self.problems.for_accepted_execution(
                    session, manifest.problem_version_id
                )
                expected = version.manifest(
                    manifest.evaluation_id,
                    manifest.submission_id,
                    manifest.language,
                    manifest.source,
                )
            await self.artifacts.check_metadata(session, manifest.source)
            if version.problem_id != submission.problem_id or manifest_bytes(
                manifest
            ) != manifest_bytes(expected):
                raise WorkerAccessDenied("worker snapshot rejected")
            return manifest
        except (ValueError, ProblemUnavailable):
            raise WorkerAccessDenied("worker snapshot rejected") from None

    async def manifest(self, binding: LeaseBinding, token: str) -> bytes:
        async with self._read_slot(), self.sessions() as session:
            manifest = await self._manifest(session, binding, token)
            payload = manifest_bytes(manifest)
            # READ COMMITTED: recheck after all preparation and just before releasing bytes.
            if (
                await repository.leased_submission(session, binding, self._token_hash(token))
                is None
            ):
                raise WorkerAccessDenied("worker grant rejected")
            return payload

    async def artifact(self, binding: LeaseBinding, token: str, digest: str) -> bytes:
        async with self._read_slot(), self.sessions() as session:
            manifest = await self._manifest(session, binding, token)
            references = (manifest.source,) + tuple(
                reference for case in manifest.cases for reference in (case.input, case.expected)
            )
            reference = next((ref for ref in references if ref.digest == digest), None)
            if reference is None:
                raise WorkerAccessDenied("artifact grant rejected")
            try:
                payload = await self.artifacts.read(session, reference)
            except ArtifactError:
                raise WorkerAccessDenied("artifact unavailable") from None
            if (
                await repository.leased_submission(session, binding, self._token_hash(token))
                is None
            ):
                raise WorkerAccessDenied("worker grant rejected")
            return payload
