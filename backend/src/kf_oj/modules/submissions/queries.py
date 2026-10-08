from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from kf_oj.contracts.judging import MAX_SOURCE_BYTES, ArtifactReference
from kf_oj.infra.artifacts.models import Artifact
from kf_oj.infra.artifacts.repository import ArtifactRepository
from kf_oj.infra.artifacts.store import ArtifactError
from kf_oj.modules.identity.public import Principal
from kf_oj.modules.submissions.models import Submission
from kf_oj.modules.submissions.public import SubmissionUnavailable


class SubmissionQueries:
    def __init__(self, sessions: async_sessionmaker, artifacts: ArtifactRepository):
        self.sessions, self.artifacts = sessions, artifacts

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
