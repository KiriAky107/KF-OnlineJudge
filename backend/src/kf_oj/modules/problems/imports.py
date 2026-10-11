"""Authenticated durable import lifecycle; filesystem work remains behind private adapters."""

import asyncio
import hashlib
import os
from contextlib import asynccontextmanager
from datetime import timedelta
from uuid import UUID

from sqlalchemy import func, select, text

from kf_oj.contracts.admin_problems import (
    IMPORT_LIFETIME_SECONDS,
    MAX_OWNER_CANDIDATES,
    MAX_TOTAL_CANDIDATES,
    MAX_UPLOAD_CHUNK_BYTES,
    MAX_UPLOAD_CHUNKS,
    STAGING_MAX_BYTES,
    STAGING_RESERVATION_BYTES,
    UPLOAD_DEADLINE_SECONDS,
    CandidatePreview,
    CreateProblemImport,
    ProblemImportCapabilities,
    ProblemImportDetail,
    ProblemImportPage,
    ProblemImportSummary,
)
from kf_oj.infra.artifacts.staging import ImportStaging
from kf_oj.infra.artifacts.store import ArtifactError
from kf_oj.modules.identity.public import MaintenanceDenied, ProblemMaintenancePort
from kf_oj.modules.operations.public import MaintenanceAuditPort
from kf_oj.modules.problems.import_models import ImportPool, ProblemCandidate
from kf_oj.modules.problems.import_models import ProblemImportOperation as Operation

ACTIVE = ("uploading", "uploaded", "validating", "ready")


class ImportConflict(ValueError):
    pass


class ImportBadDigest(ImportConflict):
    pass


class ImportMissing(ValueError):
    pass


class ImportUnavailable(ValueError):
    pass


def summary(operation) -> ProblemImportSummary:
    return ProblemImportSummary(
        operation_id=operation.id,
        state=operation.state,
        expected_archive_digest=operation.archive_digest,
        archive_bytes=operation.archive_bytes,
        created_at=operation.created_at,
        expires_at=operation.expires_at,
        updated_at=operation.updated_at,
        failure_code=operation.failure_code,
        candidate_id=operation.candidate_id,
        published_version_id=operation.published_version_id,
    )


def policy_digest(policy):
    return hashlib.sha256(policy.model_dump_json().encode()).hexdigest()


class ProblemImports:
    def __init__(
        self,
        sessions,
        staging: ImportStaging,
        identity: ProblemMaintenancePort,
        audit: MaintenanceAuditPort,
        policy,
    ):
        self.sessions, self.staging, self.identity, self.audit, self.policy = (
            sessions,
            staging,
            identity,
            audit,
            policy,
        )

    @asynccontextmanager
    async def write(self, principal):
        async with self.sessions.begin() as session:
            await session.execute(text("SET LOCAL statement_timeout='5s'"))
            await session.execute(text("SET LOCAL lock_timeout='2s'"))
            await self.identity.require_problem_maintenance(session, principal)
            yield session

    @staticmethod
    async def pool(session):
        pool = await session.get(ImportPool, 1, with_for_update=True)
        if pool is None:
            raise ImportUnavailable("import capacity unavailable")
        actual = await session.scalar(
            select(func.coalesce(func.sum(Operation.reserved_bytes), 0)).where(
                Operation.reserved_bytes > 0
            )
        )
        if pool.reserved_bytes != actual:
            raise ImportUnavailable("import capacity inconsistent")
        return pool

    async def capabilities(self):
        async with self.sessions() as session:
            pool = await session.get(ImportPool, 1)
            now = await session.scalar(select(func.clock_timestamp()))
            available = bool(
                pool
                and pool.worker_ready_until
                and pool.worker_ready_until > now
                and pool.worker_policy_digest == policy_digest(self.policy)
            )
        return ProblemImportCapabilities(
            uploads_available=available, validation_available=available
        )

    async def create(self, principal, request: CreateProblemImport):
        # Immutable receipt retries need no filesystem slot, including while a parser/GC
        # owns the root lock. Missing receipts still enter the locked creation path below.
        async with self.write(principal) as session:
            existing = await session.get(Operation, request.operation_id)
            if existing:
                if (
                    existing.owner_id != principal.account_id
                    or existing.archive_digest != request.archive_digest
                    or existing.archive_bytes != request.archive_bytes
                ):
                    raise ImportConflict("operation key already bound")
                return summary(existing)
        try:
            with self.staging.exclusive():
                await asyncio.to_thread(self.staging.usage)
                async with self.write(principal) as session:
                    pool = await self.pool(session)
                    existing = await session.get(
                        Operation, request.operation_id, with_for_update=True
                    )
                    if existing:
                        if (
                            existing.owner_id != principal.account_id
                            or existing.archive_digest != request.archive_digest
                            or existing.archive_bytes != request.archive_bytes
                        ):
                            raise ImportConflict("operation key already bound")
                        return summary(existing)
                    now = await session.scalar(select(func.clock_timestamp()))
                    if (
                        not pool.worker_ready_until
                        or pool.worker_ready_until <= now
                        or pool.worker_policy_digest != policy_digest(self.policy)
                    ):
                        raise ImportUnavailable("import validation unavailable")
                    global_count = await session.scalar(
                        select(func.count())
                        .select_from(Operation)
                        .where(Operation.state.in_(ACTIVE))
                    )
                    owner_count = await session.scalar(
                        select(func.count())
                        .select_from(Operation)
                        .where(
                            Operation.state.in_(ACTIVE), Operation.owner_id == principal.account_id
                        )
                    )
                    busy = await session.scalar(
                        select(func.count())
                        .select_from(Operation)
                        .where(
                            Operation.state.in_(("uploading", "validating")),
                            Operation.lease_until > now,
                        )
                    )
                    if (
                        global_count >= MAX_TOTAL_CANDIDATES
                        or owner_count >= MAX_OWNER_CANDIDATES
                        or busy
                        or pool.reserved_bytes + STAGING_RESERVATION_BYTES > STAGING_MAX_BYTES
                    ):
                        raise ImportUnavailable("import capacity exhausted")
                    operation = Operation(
                        id=request.operation_id,
                        owner_id=principal.account_id,
                        authorization_version=principal.authorization_version,
                        archive_digest=request.archive_digest,
                        archive_bytes=request.archive_bytes,
                        state="uploading",
                        created_at=now,
                        updated_at=now,
                        expires_at=now + timedelta(seconds=IMPORT_LIFETIME_SECONDS),
                        lease_until=now + timedelta(seconds=UPLOAD_DEADLINE_SECONDS + 15),
                        attempts=0,
                        fence=0,
                        reserved_bytes=STAGING_RESERVATION_BYTES,
                    )
                    session.add(operation)
                    pool.reserved_bytes += STAGING_RESERVATION_BYTES
                    await self.audit.record(
                        session,
                        principal.account_id,
                        principal.authorization_version,
                        operation.id,
                        "upload_created",
                    )
                    await session.flush()
                    return summary(operation)
        except ArtifactError:
            raise ImportUnavailable("import storage unavailable") from None

    async def detail(self, principal, operation_id):
        # Recheck live authority even when a principal was injected by an internal caller.
        async with self.write(principal) as session:
            op = await session.get(Operation, operation_id)
            if op is None or op.owner_id != principal.account_id:
                raise ImportMissing("import unavailable")
            candidate = (
                await session.get(ProblemCandidate, op.candidate_id) if op.candidate_id else None
            )
            return ProblemImportDetail(
                **summary(op).model_dump(),
                candidate=CandidatePreview.model_validate(candidate.preview) if candidate else None,
            )

    async def list(self, principal, after=None, limit=20):
        if not 1 <= limit <= 100:
            raise ValueError("invalid page limit")
        async with self.write(principal) as session:
            query = select(Operation).where(Operation.owner_id == principal.account_id)
            if after:
                query = query.where(Operation.id > after)
            rows = list(await session.scalars(query.order_by(Operation.id).limit(limit + 1)))
            return ProblemImportPage(
                items=tuple(summary(o) for o in rows[:limit]),
                next_after=rows[limit - 1].id if len(rows) > limit else None,
            )

    async def upload(self, principal, operation_id: UUID, chunks, content_length: int | None):
        # Reject missing/foreign receipts before consulting shared filesystem capacity.
        # The locked transaction below still rechecks authority and ownership before writes.
        async with self.write(principal) as session:
            op = await session.get(Operation, operation_id)
            if op is None or op.owner_id != principal.account_id:
                raise ImportMissing("import unavailable")
            if content_length is not None and content_length != op.archive_bytes:
                raise ImportConflict("archive length mismatch")
        descriptor = operation_fd = None
        fence = None
        received = 0
        chunk_count = 0
        digest = hashlib.sha256()
        try:
            with self.staging.exclusive():
                async with self.write(principal) as session:
                    await self.pool(session)
                    op = await session.get(Operation, operation_id, with_for_update=True)
                    if not op or op.owner_id != principal.account_id:
                        raise ImportMissing("import unavailable")
                    accepted = op.state in ("uploaded", "validating", "ready", "published")
                    now = await session.scalar(select(func.clock_timestamp()))
                    if (
                        not accepted
                        and (
                            op.state != "uploading"
                            or op.lease_until is None
                            or op.lease_until <= now
                        )
                    ) or op.expires_at <= now:
                        raise ImportConflict("upload no longer available")
                    if content_length is not None and content_length != op.archive_bytes:
                        raise ImportConflict("archive length mismatch")
                    if not accepted:
                        op.fence += 1
                        fence = op.fence
                        op.lease_until = now + timedelta(seconds=UPLOAD_DEADLINE_SECONDS + 15)
                    expected_bytes, expected_digest = op.archive_bytes, op.archive_digest
                if not accepted:
                    operation_fd = self.staging.operation(operation_id, create=True)
                    descriptor = os.open(
                        "archive",
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                        0o600,
                        dir_fd=operation_fd,
                    )
                async with asyncio.timeout(UPLOAD_DEADLINE_SECONDS):
                    async for chunk in chunks:
                        chunk_count += 1
                        received += len(chunk)
                        if (
                            type(chunk) is not bytes
                            or len(chunk) > MAX_UPLOAD_CHUNK_BYTES
                            or chunk_count > MAX_UPLOAD_CHUNKS
                            or received > expected_bytes
                        ):
                            raise ImportConflict("archive bytes outside policy")
                        digest.update(chunk)
                        if accepted:
                            continue
                        # Cancellation waits for the finite writer outside the event loop.
                        task = asyncio.create_task(asyncio.to_thread(write_all, descriptor, chunk))
                        try:
                            await asyncio.shield(task)
                        except BaseException:
                            await task
                            raise
                if received != expected_bytes or digest.hexdigest() != expected_digest:
                    raise ImportBadDigest("archive digest or length mismatch")
                if accepted:
                    async with self.write(principal) as session:
                        op = await session.get(Operation, operation_id)
                        if not op or op.state not in (
                            "uploaded",
                            "validating",
                            "ready",
                            "published",
                        ):
                            raise ImportConflict("import receipt no longer available")
                        return summary(op)
                await asyncio.to_thread(os.fsync, descriptor)
                os.close(descriptor)
                descriptor = None
                os.fsync(operation_fd)
                async with self.write(principal) as session:
                    await self.pool(session)
                    op = await session.get(Operation, operation_id, with_for_update=True)
                    now = await session.scalar(select(func.clock_timestamp()))
                    if op.state != "uploading" or op.fence != fence or op.lease_until <= now:
                        raise ImportConflict("upload receipt expired")
                    op.state = "uploaded"
                    op.lease_until = None
                    op.updated_at = now
                    await self.audit.record(
                        session, op.owner_id, op.authorization_version, op.id, "upload_received"
                    )
                    result = summary(op)
                return result
        except BaseException as error:
            # Failure bookkeeping cannot authorize a new business write. It only fences this
            # accepted upload and releases capacity after physical bytes have been removed.
            if descriptor is not None:
                os.close(descriptor)
                descriptor = None
            if operation_fd is not None:
                os.close(operation_fd)
                operation_fd = None
            if fence is not None:
                try:
                    with self.staging.exclusive():
                        await asyncio.to_thread(self.staging.remove, operation_id)
                        async with self.sessions.begin() as session:
                            pool = await self.pool(session)
                            op = await session.get(Operation, operation_id, with_for_update=True)
                            if op and op.state == "uploading" and op.fence == fence:
                                op.state = "failed"
                                op.failure_code = (
                                    "upload_deadline"
                                    if isinstance(error, TimeoutError)
                                    else "digest_mismatch"
                                    if isinstance(error, ImportBadDigest)
                                    else "authority_revoked"
                                    if isinstance(error, MaintenanceDenied)
                                    else "storage_unavailable"
                                    if isinstance(error, OSError)
                                    else "upload_interrupted"
                                )
                                op.updated_at = await session.scalar(select(func.clock_timestamp()))
                                op.lease_until = None
                                pool.reserved_bytes -= op.reserved_bytes
                                op.reserved_bytes = 0
                                await self.audit.record(
                                    session,
                                    op.owner_id,
                                    op.authorization_version,
                                    op.id,
                                    "import_failed",
                                )
                except (ArtifactError, OSError):
                    pass  # Durable reservation retained for fenced GC.
            raise
        finally:
            if descriptor is not None:
                os.close(descriptor)
            if operation_fd is not None:
                os.close(operation_fd)

    async def discard(self, principal, operation_id):
        async with self.write(principal) as session:
            op = await session.get(Operation, operation_id)
            if op is None or op.owner_id != principal.account_id:
                raise ImportMissing("import unavailable")
            if op.state == "discarded":
                return summary(op)
        try:
            with self.staging.exclusive():
                async with self.write(principal) as session:
                    pool = await self.pool(session)
                    op = await session.get(Operation, operation_id, with_for_update=True)
                    if not op or op.owner_id != principal.account_id:
                        raise ImportMissing("import unavailable")
                    if op.state == "published":
                        raise ImportConflict("published import cannot be discarded")
                    if op.state == "discarded":
                        return summary(op)
                    await asyncio.to_thread(self.staging.remove, operation_id)
                    op.state = "discarded"
                    op.candidate_id = None
                    op.failure_code = None
                    op.lease_until = None
                    op.fence += 1
                    op.updated_at = await session.scalar(select(func.clock_timestamp()))
                    pool.reserved_bytes -= op.reserved_bytes
                    op.reserved_bytes = 0
                    await self.audit.record(
                        session,
                        principal.account_id,
                        principal.authorization_version,
                        op.id,
                        "import_discarded",
                    )
                    return summary(op)
        except ArtifactError:
            raise ImportUnavailable("import storage unavailable") from None


def write_all(descriptor, data):
    remaining = memoryview(data)
    while remaining:
        size = os.write(descriptor, remaining)
        if size <= 0:
            raise OSError("short staging write")
        remaining = remaining[size:]
