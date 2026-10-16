"""Byte durability precedes metadata. Digest locks span caller reference transactions."""

import asyncio
from datetime import timedelta

from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kf_oj.contracts.judging import ArtifactReference
from kf_oj.infra.artifacts.models import Artifact
from kf_oj.infra.artifacts.store import ArtifactError, ArtifactStore


async def lock_digest(session: AsyncSession, digest: str) -> None:
    await session.execute(
        text("SELECT pg_advisory_xact_lock(:key)"),
        {"key": int(digest[:16], 16) & ((1 << 63) - 1)},
    )


class ArtifactRepository:
    def __init__(self, store: ArtifactStore):
        self.store = store

    async def put(
        self, session: AsyncSession, data: bytes, expected: ArtifactReference | None = None
    ) -> ArtifactReference:
        reference = self.store.reference(data)
        if expected is not None and reference != expected:
            raise ArtifactError("artifact digest or length mismatch")
        await lock_digest(session, reference.digest)
        existing = await session.get(Artifact, reference.digest)
        if existing is not None and (
            existing.state != "ready" or existing.bytes != reference.bytes
        ):
            raise ArtifactError("artifact metadata unavailable")
        await asyncio.to_thread(self.store.put, data, reference)
        await session.execute(
            insert(Artifact)
            .values(digest=reference.digest, bytes=reference.bytes)
            .on_conflict_do_nothing(index_elements=[Artifact.digest])
        )
        return reference

    async def read(self, session: AsyncSession, reference: ArtifactReference) -> bytes:
        metadata = await session.get(Artifact, reference.digest)
        if metadata is None or metadata.state != "ready" or metadata.bytes != reference.bytes:
            raise ArtifactError("artifact metadata unavailable")
        return await asyncio.to_thread(self.store.read, reference)

    async def put_file(
        self, session: AsyncSession, descriptor: int, reference: ArtifactReference
    ) -> ArtifactReference:
        await lock_digest(session, reference.digest)
        existing = await session.get(Artifact, reference.digest)
        if existing is not None and (
            existing.state != "ready" or existing.bytes != reference.bytes
        ):
            raise ArtifactError("artifact metadata unavailable")
        task = asyncio.create_task(asyncio.to_thread(self.store.put_file, descriptor, reference))
        try:
            await asyncio.shield(task)
        except BaseException:
            await task
            raise
        await session.execute(
            insert(Artifact)
            .values(digest=reference.digest, bytes=reference.bytes)
            .on_conflict_do_nothing(index_elements=[Artifact.digest])
        )
        return reference

    async def check_metadata(self, session: AsyncSession, reference: ArtifactReference) -> None:
        metadata = await session.get(Artifact, reference.digest)
        if metadata is None or metadata.state != "ready" or metadata.bytes != reference.bytes:
            raise ArtifactError("artifact metadata unavailable")

    async def collect_metadata(
        self,
        sessions: async_sessionmaker,
        *,
        min_age_seconds: int = 86400,
        limit: int = 100,
        after: str | None = None,
    ) -> tuple[int, str | None]:
        if min_age_seconds < 3600 or not 1 <= limit <= 100:
            raise ArtifactError("invalid collection budget")
        if after is not None:
            ArtifactReference(digest=after, bytes=0)
        async with sessions() as session:
            query = select(Artifact.digest).where(
                Artifact.state == "ready",
                Artifact.created_at < func.now() - timedelta(seconds=min_age_seconds),
            )
            if after is not None:
                query = query.where(Artifact.digest > after)
            names = list(await session.scalars(query.order_by(Artifact.digest).limit(limit)))
        marked = 0
        for digest in names:
            async with sessions.begin() as session:
                try:
                    async with session.begin_nested():
                        changed = await self.mark_deleting(session, digest)
                    marked += int(changed)
                except IntegrityError:
                    # Referenced rows are protected by their generated ready-state foreign key.
                    # No domain ORM imports or reverse dependencies are needed for reclamation.
                    pass
        return marked, names[-1] if len(names) == limit else None

    async def collect_orphans(
        self, sessions: async_sessionmaker, *, min_age_seconds: int = 86400, limit: int = 100
    ) -> int:
        # Only filesystem orphans are collected here. Referenced metadata is never deleted.
        # A durable row cannot become an orphan while another transaction publishes it:
        # every writer and collector holds the same transaction-scoped digest lock.
        names = await asyncio.to_thread(
            self.store.old_digests, min_age_seconds=min_age_seconds, limit=limit
        )
        collected = 0
        for digest in names:
            async with sessions.begin() as session:
                await lock_digest(session, digest)
                if await session.get(Artifact, digest) is None:
                    await asyncio.to_thread(self.store.remove, digest)
                    collected += 1
        await asyncio.to_thread(
            self.store.clean_temporary, min_age_seconds=min_age_seconds, limit=limit
        )
        return collected

    async def mark_deleting(self, session: AsyncSession, digest: str) -> bool:
        # FK(digest, 'ready') on every business reference rejects this transition if referenced.
        await lock_digest(session, digest)
        result = await session.scalar(
            update(Artifact)
            .where(Artifact.digest == digest, Artifact.state == "ready")
            .values(state="deleting")
            .returning(Artifact.digest)
        )
        return result is not None

    async def finish_deleting(self, sessions: async_sessionmaker, *, limit: int = 100) -> int:
        if not 1 <= limit <= 100:
            raise ArtifactError("invalid collection budget")
        async with sessions() as session:
            names = list(
                await session.scalars(
                    select(Artifact.digest)
                    .where(Artifact.state == "deleting")
                    .order_by(Artifact.digest)
                    .limit(limit)
                )
            )
        count = 0
        for digest in names:
            async with sessions.begin() as session:
                await lock_digest(session, digest)
                metadata = await session.get(Artifact, digest)
                if metadata is not None and metadata.state == "deleting":
                    await asyncio.to_thread(self.store.remove, digest)
                    await session.delete(metadata)
                    count += 1
        return count
