"""Administrator transactions; no evaluation writes or private data in projections."""

import asyncio
import hashlib
import json
from contextlib import asynccontextmanager
from uuid import uuid4, uuid5

from sqlalchemy import func, select, text

from kf_oj.contracts.admin_problems import CandidatePreview
from kf_oj.contracts.judging import ArtifactReference, TestCase
from kf_oj.contracts.problem_maintenance import (
    MaintainedProblem,
    MaintainedProblemPage,
    MaintainedVersion,
    MaintainedVersionPage,
    MaintenanceReceipt,
)
from kf_oj.modules.identity.public import ProblemMaintenancePort
from kf_oj.modules.operations.public import MaintenanceAuditPort
from kf_oj.modules.problems import repository
from kf_oj.modules.problems.candidate_validation import validate_candidate
from kf_oj.modules.problems.import_format import ImportEnvelope
from kf_oj.modules.problems.import_models import (
    CandidateCase,
    ProblemCandidate,
    ProblemImportOperation,
)
from kf_oj.modules.problems.imports import ImportConflict, ImportMissing, ImportUnavailable
from kf_oj.modules.problems.maintenance_models import ProblemMaintenanceOperation
from kf_oj.modules.problems.models import Problem, ProblemCase, ProblemVersion
from kf_oj.modules.problems.publication import admit_version, publish_version
from kf_oj.modules.problems.service import ProblemService


def project_problem(problem, version):
    return MaintainedProblem(
        id=problem.id,
        slug=problem.slug,
        enabled=problem.enabled,
        active_version_id=problem.active_version_id,
        revision=version.revision if version else 0,
        title=version.title if version else None,
        created_at=problem.created_at,
    )


class ProblemMaintenance:
    def __init__(
        self,
        sessions,
        identity: ProblemMaintenancePort,
        audit: MaintenanceAuditPort,
        artifacts=None,
        policy=None,
    ):
        self.sessions, self.identity, self.audit = sessions, identity, audit
        self.artifacts, self.policy = artifacts, policy
        self._publishing = asyncio.Semaphore(2)

    @asynccontextmanager
    async def transaction(self, principal):
        async with self.sessions.begin() as session:
            await session.execute(text("SET LOCAL statement_timeout='5s'"))
            await session.execute(text("SET LOCAL lock_timeout='2s'"))
            await self.identity.require_problem_maintenance(session, principal)
            yield session

    async def page(self, principal, *, after=None, limit=20, enabled=None):
        if not 1 <= limit <= 100:
            raise ValueError("invalid page limit")
        async with self.transaction(principal) as session:
            q = select(Problem, ProblemVersion).outerjoin(
                ProblemVersion, Problem.active_version_id == ProblemVersion.id
            )
            if after is not None:
                q = q.where(Problem.slug > after)
            if enabled is not None:
                q = q.where(Problem.enabled == enabled)
            rows = list(await session.execute(q.order_by(Problem.slug).limit(limit + 1)))
            return MaintainedProblemPage(
                items=tuple(project_problem(*r) for r in rows[:limit]),
                next_after=rows[limit - 1][0].slug if len(rows) > limit else None,
            )

    async def by_slug(self, principal, slug):
        async with self.transaction(principal) as session:
            row = (
                await session.execute(
                    select(Problem, ProblemVersion)
                    .outerjoin(ProblemVersion, Problem.active_version_id == ProblemVersion.id)
                    .where(Problem.slug == slug)
                )
            ).one_or_none()
            if row is None:
                raise ImportMissing("problem unavailable")
            return project_problem(*row)

    async def detail(self, principal, problem_id):
        async with self.transaction(principal) as session:
            problem = await session.get(Problem, problem_id)
            if problem is None:
                raise ImportMissing("problem unavailable")
            version = (
                await session.get(ProblemVersion, problem.active_version_id)
                if problem.active_version_id
                else None
            )
            return project_problem(problem, version)

    async def versions(self, principal, problem_id, *, before=None, limit=20):
        if not 1 <= limit <= 100 or (before is not None and before < 1):
            raise ValueError("invalid page limit")
        async with self.transaction(principal) as session:
            problem = await session.get(Problem, problem_id)
            if problem is None:
                raise ImportMissing("problem unavailable")
            count = (
                select(func.count())
                .where(ProblemCase.version_id == ProblemVersion.id)
                .scalar_subquery()
            )
            q = select(ProblemVersion, count).where(
                ProblemVersion.problem_id == problem_id, ProblemVersion.published_at.is_not(None)
            )
            if before is not None:
                q = q.where(ProblemVersion.revision < before)
            rows = list(
                await session.execute(q.order_by(ProblemVersion.revision.desc()).limit(limit + 1))
            )
            return MaintainedVersionPage(
                items=tuple(
                    MaintainedVersion(
                        **ProblemService._summary(problem, v).model_dump(),
                        published_at=v.published_at,
                        case_count=n,
                    )
                    for v, n in rows[:limit]
                ),
                next_before=rows[limit - 1][0].revision if len(rows) > limit else None,
            )

    async def receipt(self, principal, operation_id):
        async with self.transaction(principal) as session:
            row = await session.get(ProblemMaintenanceOperation, operation_id)
            if row is None or row.owner_id != principal.account_id:
                raise ImportMissing("operation unavailable")
            return MaintenanceReceipt.model_validate(row.receipt)

    async def _replay(self, session, principal, action, body, target):
        digest = hashlib.sha256(
            json.dumps(
                {"action": action, "target": str(target), "request": body.model_dump(mode="json")},
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        key = int.from_bytes(hashlib.sha256(body.operation_id.bytes).digest()[:4], signed=True)
        await session.execute(text("SELECT pg_advisory_xact_lock(1262907470,:key)"), {"key": key})
        row = await session.get(ProblemMaintenanceOperation, body.operation_id)
        if row is not None:
            if row.owner_id != principal.account_id:
                raise ImportMissing("operation unavailable")
            if row.action != action or row.request_digest != digest:
                raise ImportConflict("operation UUID already bound")
            return digest, MaintenanceReceipt.model_validate(row.receipt)
        return digest, None

    async def _record(
        self, session, principal, request, action, digest, problem, version, published=None
    ):
        receipt = MaintenanceReceipt(
            operation_id=request.operation_id,
            action=action,
            completed_at=await session.scalar(select(func.clock_timestamp())),
            problem=project_problem(problem, version),
            published_version_id=published,
        )
        session.add(
            ProblemMaintenanceOperation(
                id=request.operation_id,
                owner_id=principal.account_id,
                authorization_version=principal.authorization_version,
                action=action,
                request_digest=digest,
                receipt=receipt.model_dump(mode="json"),
            )
        )
        await self.audit.record(
            session,
            principal.account_id,
            principal.authorization_version,
            request.operation_id,
            {"publish": "published", "disable": "disabled", "restore": "restored"}[action],
            published if action == "publish" else problem.id,
        )
        await session.flush()
        return receipt

    async def publish(self, principal, request):
        if self.artifacts is None or self.policy is None or self._publishing.locked():
            raise ImportUnavailable("publication unavailable")
        async with self._publishing:
            async with self.transaction(principal) as session:
                digest, replay = await self._replay(
                    session, principal, "publish", request, request.import_operation_id
                )
                if replay:
                    return replay
                op = await session.get(
                    ProblemImportOperation, request.import_operation_id, with_for_update=True
                )
                if op is None or op.owner_id != principal.account_id:
                    raise ImportMissing("candidate unavailable")
                if (
                    op.state != "ready"
                    or op.candidate_id != request.candidate_id
                    or op.authorization_version != principal.authorization_version
                    or op.expires_at <= await session.scalar(select(func.clock_timestamp()))
                ):
                    raise ImportConflict("candidate no longer publishable")
                candidate = await session.get(ProblemCandidate, op.candidate_id)
                if candidate is None or candidate.operation_id != op.id:
                    raise ImportUnavailable("candidate unavailable")
                manifest = ImportEnvelope.model_validate(candidate.manifest).root
                preview = CandidatePreview.model_validate(candidate.preview)
                rows = list(
                    await session.scalars(
                        select(CandidateCase)
                        .where(CandidateCase.candidate_id == candidate.id)
                        .order_by(CandidateCase.position)
                    )
                )
                if len(rows) != len(manifest.cases) or len(rows) != candidate.case_count:
                    raise ImportUnavailable("candidate references unavailable")
                refs = {}
                for position, (row, c) in enumerate(zip(rows, manifest.cases, strict=True)):
                    if row.position != position or row.key != c.key:
                        raise ImportUnavailable("candidate references unavailable")
                    for name, ref in (
                        (
                            c.input_file,
                            ArtifactReference(digest=row.input_digest, bytes=row.input_bytes),
                        ),
                        (
                            c.expected_file,
                            ArtifactReference(digest=row.expected_digest, bytes=row.expected_bytes),
                        ),
                    ):
                        if name in refs and refs[name] != ref:
                            raise ImportUnavailable("candidate references unavailable")
                        refs[name] = ref
                validated = validate_candidate(
                    candidate.manifest, candidate.preview, refs, self.policy
                )
                if (
                    validated.digest != candidate.manifest_digest
                    or preview.candidate_id != candidate.id
                ):
                    raise ImportUnavailable("candidate integrity unavailable")
                await repository.lock_slug(session, manifest.slug)
                problem = await repository.lock_problem(session, manifest.slug)
                actual = problem.active_version_id if problem else None
                if actual != request.expected_active_version_id or (
                    problem and not problem.enabled
                ):
                    raise ImportConflict("active version or availability changed")
                if problem is None:
                    problem = Problem(id=uuid4(), slug=manifest.slug)
                    session.add(problem)
                    await session.flush()
                version_id = uuid4()
                cases = tuple(
                    TestCase(
                        case_id=uuid5(version_id, c.key),
                        input=refs[c.input_file],
                        expected=refs[c.expected_file],
                    )
                    for c in manifest.cases
                )
                admit_version(self.policy, manifest, version_id, problem.id, cases)
                # Candidate FKs protect all referenced ready metadata from GC. The
                # private adapter additionally re-hashes disk bytes without buffering them.
                for ref in sorted(
                    {r.digest: r for r in refs.values()}.values(), key=lambda r: r.digest
                ):
                    await self.artifacts.check_metadata(session, ref)
                    task = asyncio.create_task(asyncio.to_thread(self.artifacts.store.verify, ref))
                    try:
                        await asyncio.shield(task)
                    except BaseException:
                        await task
                        raise
                version = await publish_version(
                    session, problem, manifest, preview, version_id, cases
                )
                op.state, op.published_version_id = "published", version.id
                op.updated_at = await session.scalar(select(func.clock_timestamp()))
                return await self._record(
                    session, principal, request, "publish", digest, problem, version, version.id
                )

    async def availability(self, principal, problem_id, request, *, enabled):
        action = "restore" if enabled else "disable"
        async with self.transaction(principal) as session:
            digest, replay = await self._replay(session, principal, action, request, problem_id)
            if replay:
                return replay
            problem = await session.get(Problem, problem_id, with_for_update=True)
            if problem is None:
                raise ImportMissing("problem unavailable")
            if (
                problem.active_version_id != request.expected_active_version_id
                or problem.enabled != request.expected_enabled
                or problem.enabled == enabled
            ):
                raise ImportConflict("active version or availability changed")
            version = await session.get(ProblemVersion, problem.active_version_id)
            if version is None or version.published_at is None:
                raise ImportUnavailable("published version unavailable")
            problem.enabled = enabled
            await session.flush()
            return await self._record(session, principal, request, action, digest, problem, version)
