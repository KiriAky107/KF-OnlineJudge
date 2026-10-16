"""Fenced ordinary-user import consumption. No student execution or evaluation DB access."""

import asyncio
import json
import os
import resource
import sys
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

from sqlalchemy import delete, func, or_, select, text
from sqlalchemy.exc import DBAPIError

from kf_oj.contracts.admin_problems import MAX_IMPORT_ATTEMPTS, VALIDATION_DEADLINE_SECONDS
from kf_oj.infra.artifacts.store import ArtifactError
from kf_oj.modules.identity.public import MaintenanceDenied
from kf_oj.modules.problems.candidate_validation import validate_candidate
from kf_oj.modules.problems.import_models import (
    CandidateCase,
    ImportPool,
    ProblemCandidate,
)
from kf_oj.modules.problems.import_models import (
    ProblemImportOperation as Operation,
)
from kf_oj.modules.problems.imports import ACTIVE, ImportUnavailable, ProblemImports, policy_digest


def verify_worker_limits(configured_group: Path):
    if os.getuid() == 0:
        raise ImportUnavailable("import worker must be ordinary")
    actual = next(
        line.split(":", 2)[2]
        for line in Path("/proc/self/cgroup").read_text().splitlines()
        if line.startswith("0::")
    )
    group = Path("/sys/fs/cgroup") / actual.lstrip("/")
    if group.resolve() != configured_group.absolute().resolve():
        raise ImportUnavailable("import worker cgroup mismatch")
    caps = {
        n: (group / n).read_text().strip()
        for n in ("memory.max", "memory.swap.max", "pids.max", "cpu.max")
    }
    if (
        caps["memory.max"] == "max"
        or not 0 < int(caps["memory.max"]) <= 134217728
        or caps["memory.swap.max"] != "0"
        or caps["pids.max"] == "max"
        or not 0 < int(caps["pids.max"]) <= 16
    ):
        raise ImportUnavailable("import worker resource limits unavailable")
    quota, period = caps["cpu.max"].split()
    if quota == "max" or int(quota) > int(period):
        raise ImportUnavailable("import worker CPU limit unavailable")
    for name, maximum in (
        (resource.RLIMIT_FSIZE, 67108864),
        (resource.RLIMIT_NOFILE, 64),
        (resource.RLIMIT_CORE, 0),
    ):
        limits = resource.getrlimit(name)
        if any(v == resource.RLIM_INFINITY or v > maximum for v in limits):
            raise ImportUnavailable("import worker process limits unavailable")
    return caps


class ImportWorker:
    def __init__(self, sessions, staging, artifacts, authority, audit, policy, cgroup):
        (
            self.sessions,
            self.staging,
            self.artifacts,
            self.authority,
            self.audit,
            self.policy,
            self.cgroup,
        ) = sessions, staging, artifacts, authority, audit, policy, cgroup

    async def heartbeat(self):
        verify_worker_limits(self.cgroup)
        async with self.sessions.begin() as session:
            safe = await session.scalar(
                text("""
              SELECT rolcanlogin AND NOT(rolsuper OR rolcreatedb OR rolcreaterole
                OR rolreplication OR rolbypassrls)
                AND pg_has_role(current_user,'kf_oj_problem_import','USAGE')
                AND NOT pg_has_role(current_user,'kf_oj_platform','MEMBER')
                AND NOT pg_has_role(current_user,'kf_oj_coordinator','MEMBER')
                AND NOT pg_has_role(current_user,'kf_oj_identity_operator','MEMBER')
                AND NOT pg_has_role(current_user,'kf_oj_identity_authority_owner','MEMBER')
              FROM pg_roles WHERE rolname=current_user
            """)
            )
            if not safe:
                raise ImportUnavailable("dedicated unprivileged import login required")
            pool = await session.get(ImportPool, 1, with_for_update=True)
            if pool is None:
                raise ImportUnavailable("import pool unavailable")
            pool.worker_ready_until = await session.scalar(
                select(func.clock_timestamp())
            ) + timedelta(seconds=90)
            pool.worker_policy_digest = policy_digest(self.policy)

    async def _release(self, operation_id):
        await asyncio.to_thread(self.staging.remove, operation_id)
        async with self.sessions.begin() as session:
            pool = await ProblemImports.pool(session)
            op = await session.get(Operation, operation_id, with_for_update=True)
            pool.reserved_bytes -= op.reserved_bytes
            op.reserved_bytes = 0
            op.lease_until = None

    async def collect(self):
        # Caller owns the root flock, so a live upload/parser cannot be unlinked or
        # have its reservation reused, even if its DB lease expired while suspended.
        async with self.sessions() as session:
            now = await session.scalar(select(func.clock_timestamp()))
            names = list(
                await session.scalars(
                    select(Operation.id)
                    .where(
                        or_(
                            Operation.reserved_bytes > 0,
                            Operation.state.in_(ACTIVE) & (Operation.expires_at <= now),
                        )
                    )
                    .order_by(Operation.updated_at, Operation.id)
                    .limit(32)
                )
            )
            garbage = list(
                await session.scalars(
                    select(ProblemCandidate.operation_id)
                    .join(Operation, Operation.id == ProblemCandidate.operation_id)
                    .where(Operation.state.in_(("discarded", "expired", "failed")))
                    .limit(32)
                )
            )
            names = list(dict.fromkeys(names + garbage))[:32]
        for identifier in names:
            async with self.sessions.begin() as session:
                await ProblemImports.pool(session)
                op = await session.get(Operation, identifier, with_for_update=True)
                now = await session.scalar(select(func.clock_timestamp()))
                if op.state in ACTIVE and op.expires_at <= now:
                    op.state = "expired"
                    op.failure_code = "expired"
                    op.updated_at = now
                    op.candidate_id = None
                    op.fence += 1
                    await self.audit.record(
                        session, op.owner_id, op.authorization_version, op.id, "import_expired"
                    )
                elif op.state == "uploading" and op.lease_until and op.lease_until <= now:
                    op.state = "failed"
                    op.failure_code = "upload_interrupted"
                    op.updated_at = now
                    op.fence += 1
                    await self.audit.record(
                        session, op.owner_id, op.authorization_version, op.id, "import_failed"
                    )
                elif op.state == "validating" and op.lease_until and op.lease_until <= now:
                    if op.attempts < MAX_IMPORT_ATTEMPTS:
                        op.state = "uploaded"
                        op.failure_code = None
                        op.lease_until = None
                        op.fence += 1
                        op.updated_at = now
                        await asyncio.to_thread(self.staging.reset_validation, op.id)
                    else:
                        op.state = "failed"
                        op.failure_code = "validation_unavailable"
                        op.updated_at = now
                        op.fence += 1
                        await self.audit.record(
                            session, op.owner_id, op.authorization_version, op.id, "import_failed"
                        )
                if op.state in ("discarded", "expired", "failed"):
                    op.candidate_id = None
                    await session.flush()
                    candidate = await session.scalar(
                        select(ProblemCandidate).where(ProblemCandidate.operation_id == op.id)
                    )
                    if candidate:
                        await session.execute(
                            delete(CandidateCase).where(CandidateCase.candidate_id == candidate.id)
                        )
                        await session.delete(candidate)
                release = (
                    op.state in ("ready", "published", "discarded", "expired", "failed")
                    and op.reserved_bytes > 0
                )
            if release:
                await self._release(identifier)
        # Uncommitted staging objects after a failed transaction are removed only under
        # the same kernel lock and only from this service's private, bounded namespace.
        for identifier in self.staging.operation_ids():
            async with self.sessions() as session:
                exists = await session.get(Operation, identifier)
            if exists is None:
                await asyncio.to_thread(self.staging.remove, identifier)

    async def once(self):
        await self.heartbeat()
        try:
            with self.staging.exclusive() as lock:
                await self.collect()
                async with self.sessions.begin() as session:
                    await ProblemImports.pool(session)
                    now = await session.scalar(select(func.clock_timestamp()))
                    busy = await session.scalar(
                        select(func.count())
                        .select_from(Operation)
                        .where(
                            Operation.state.in_(("uploading", "validating")),
                            Operation.lease_until > now,
                        )
                    )
                    if busy:
                        return False
                    op = await session.scalar(
                        select(Operation)
                        .where(
                            Operation.state == "uploaded",
                            Operation.attempts < MAX_IMPORT_ATTEMPTS,
                            Operation.expires_at > now,
                        )
                        .order_by(Operation.created_at, Operation.id)
                        .limit(1)
                        .with_for_update()
                    )
                    if op is None:
                        return False
                    op.attempts += 1
                    op.fence += 1
                    op.state = "validating"
                    op.updated_at = now
                    op.lease_until = now + timedelta(seconds=VALIDATION_DEADLINE_SECONDS + 15)
                    identifier, fence, size, digest, actor, epoch = (
                        op.id,
                        op.fence,
                        op.archive_bytes,
                        op.archive_digest,
                        op.owner_id,
                        op.authorization_version,
                    )
                try:
                    async with self.sessions.begin() as session:
                        await self.authority.require_reserved_maintenance(session, actor, epoch)
                    candidate_id = uuid4()
                    result = await self.parse(identifier, size, digest, candidate_id, lock)
                    if "failure_code" in result:
                        await self.fail(identifier, fence, result["failure_code"])
                        return True
                    candidate = validate_candidate(
                        result["manifest"], result["preview"], result["references"], self.policy
                    )
                    if (
                        candidate.digest != result["manifest_digest"]
                        or candidate.preview.candidate_id != candidate_id
                    ):
                        raise ValueError("invalid candidate receipt")
                    directory = self.staging.operation(identifier)
                    members = self.staging.members(directory)
                    try:
                        async with self.sessions.begin() as session:
                            await self.authority.require_reserved_maintenance(session, actor, epoch)
                            await ProblemImports.pool(session)
                            op = await session.get(Operation, identifier, with_for_update=True)
                            now = await session.scalar(select(func.clock_timestamp()))
                            if (
                                op.state != "validating"
                                or op.fence != fence
                                or op.lease_until <= now
                                or op.expires_at <= now
                            ):
                                raise ValueError("stale candidate receipt")
                            for name, reference in sorted(
                                candidate.references.items(), key=lambda item: item[1].digest
                            ):
                                fd = os.open(
                                    name,
                                    os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                                    dir_fd=members,
                                )
                                try:
                                    await self.artifacts.put_file(session, fd, reference)
                                finally:
                                    os.close(fd)
                            session.add(
                                ProblemCandidate(
                                    id=candidate_id,
                                    operation_id=identifier,
                                    manifest_digest=candidate.digest,
                                    manifest=candidate.manifest,
                                    preview=candidate.preview.model_dump(mode="json"),
                                    case_count=candidate.preview.case_count,
                                )
                            )
                            await session.flush()
                            for position, case in enumerate(candidate.manifest["cases"]):
                                input_ref, answer_ref = (
                                    candidate.references[case["input_file"]],
                                    candidate.references[case["expected_file"]],
                                )
                                session.add(
                                    CandidateCase(
                                        candidate_id=candidate_id,
                                        position=position,
                                        key=case["key"],
                                        input_digest=input_ref.digest,
                                        input_bytes=input_ref.bytes,
                                        expected_digest=answer_ref.digest,
                                        expected_bytes=answer_ref.bytes,
                                    )
                                )
                            await session.flush()
                            # Adoption must not turn an expired lease into a ready candidate.
                            completed_at = await session.scalar(select(func.clock_timestamp()))
                            if op.lease_until <= completed_at or op.expires_at <= completed_at:
                                raise ValueError("candidate completion lease expired")
                            op.state = "ready"
                            op.candidate_id = candidate_id
                            op.updated_at = completed_at
                            op.lease_until = None
                            await self.audit.record(
                                session, actor, epoch, identifier, "import_ready", candidate_id
                            )
                    finally:
                        os.close(members)
                        os.close(directory)
                    await self._release(identifier)
                except MaintenanceDenied:
                    await self.fail(identifier, fence, "authority_revoked")
                except (ArtifactError, OSError):
                    await self.fail(identifier, fence, "storage_unavailable")
                except (ValueError, DBAPIError):
                    await self.fail(identifier, fence, "validation_unavailable")
                return True
        except ArtifactError:
            return False  # Another live writer holds the one global slot.

    async def fail(self, identifier, fence, code):
        from kf_oj.contracts.admin_problems import ImportFailure

        code = ImportFailure(code).value
        async with self.sessions.begin() as session:
            await ProblemImports.pool(session)
            op = await session.get(Operation, identifier, with_for_update=True)
            if op.state != "validating" or op.fence != fence:
                return
            op.state = "failed"
            op.failure_code = code
            op.updated_at = await session.scalar(select(func.clock_timestamp()))
            op.lease_until = None
            await self.audit.record(
                session, op.owner_id, op.authorization_version, op.id, "import_failed"
            )
        await self._release(identifier)

    async def parse(self, identifier, size, digest, candidate, lock):
        verify_worker_limits(self.cgroup)
        directory = self.staging.operation(identifier)
        archive = members = None
        try:
            archive = os.open(
                "archive",
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                dir_fd=directory,
            )
            members = self.staging.members(directory, create=True)
            # Preserve only the trusted runtime import roots, without inheriting DB/
            # service credentials. Curated runtimes may keep dependencies outside site-packages.
            env = {
                "PATH": str(Path(sys.executable).parent),
                "PYTHONPATH": os.pathsep.join(
                    dict.fromkeys(
                        [str(Path(__file__).parents[3])]
                        + [p for p in os.environ.get("PYTHONPATH", "").split(os.pathsep) if p]
                    )
                ),
                "PYTHONUNBUFFERED": "1",
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONNOUSERSITE": "1",
                "LC_ALL": "C.UTF-8",
            }
            # Minimal environment may require the trusted dynamic-loader path in a curated VM.
            if "LD_LIBRARY_PATH" in os.environ:
                env["LD_LIBRARY_PATH"] = os.environ["LD_LIBRARY_PATH"]
            child = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "kf_oj.workers._archive_child",
                str(archive),
                str(members),
                str(lock),
                str(size),
                digest,
                str(candidate),
                self.policy.model_dump_json(),
                env=env,
                pass_fds=(archive, members, lock),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                async with asyncio.timeout(VALIDATION_DEADLINE_SECONDS + 5):
                    output, _ = await child.communicate()
            except BaseException:
                child.kill()
                await child.wait()
                raise
            if child.returncode != 0 or not 0 < len(output) <= 4 * 1024**2:
                return {
                    "failure_code": "validation_deadline"
                    if child.returncode == -14
                    else "validation_unavailable"
                }
            result = json.loads(output)
            if not isinstance(result, dict):
                raise ValueError("invalid child result")
            return result
        except TimeoutError:
            return {"failure_code": "validation_deadline"}
        finally:
            if members is not None:
                os.close(members)
            if archive is not None:
                os.close(archive)
            os.close(directory)
