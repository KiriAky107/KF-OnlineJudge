import base64
from pathlib import Path
from uuid import UUID, uuid4, uuid5

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from kf_oj.contracts import judging_v2 as v2
from kf_oj.contracts.judging import (
    MAX_PACKAGE_BYTES,
    MAX_SOURCE_BYTES,
    ArtifactReference,
    Language,
    RunLimits,
    TestCase,
    Toolchain,
    decode_wire,
)
from kf_oj.contracts.problems import (
    ProblemDetail,
    ProblemPage,
    ProblemSummary,
    PublicLanguageConfiguration,
    PublicSample,
)
from kf_oj.infra.artifacts.models import Artifact
from kf_oj.infra.artifacts.repository import ArtifactRepository
from kf_oj.modules.problems import repository
from kf_oj.modules.problems.import_format import ImportEnvelope, ProblemImport
from kf_oj.modules.problems.local_files import LocalImportFiles
from kf_oj.modules.problems.models import Problem, ProblemCase, ProblemVersion
from kf_oj.modules.problems.public import JudgingVersion, ProblemUnavailable


class ProblemService:
    def __init__(
        self,
        sessions: async_sessionmaker,
        artifacts: ArtifactRepository | None = None,
        policy: v2.NodeAdmission | None = None,
    ):
        self.sessions, self.artifacts, self.policy = sessions, artifacts, policy

    async def import_directory(self, root: Path, manifest_file: str = "problem.json") -> UUID:
        if self.artifacts is None or self.policy is None:
            raise ValueError("artifact store and trusted node policy required for import")
        files = LocalImportFiles(root)
        try:
            manifest = decode_wire(ImportEnvelope, files.read(manifest_file, 256 * 1024)).root
            stdout_limit = (
                manifest.run_limits.stdout_bytes
                if isinstance(manifest, ProblemImport)
                else min(c.run_limits.stdout_bytes for c in manifest.language_configurations)
            )
            statement = files.read(manifest.statement_file, 256 * 1024).decode("utf-8")
            if not statement or "\x00" in statement or "\x00" in manifest.title:
                raise ValueError("invalid public text")
            # Read the complete bounded package before writing anything. Public samples are
            # separately declared, at most 64 KiB each, and never inferred from hidden tests.
            remaining = MAX_PACKAGE_BYTES - MAX_SOURCE_BYTES
            payloads = []
            for case in manifest.cases:
                data = files.read(case.input_file, remaining)
                remaining -= len(data)
                answer = files.read(case.expected_file, min(remaining, stdout_limit))
                remaining -= len(answer)
                payloads.append((data, answer))
            samples = tuple(
                PublicSample(
                    input=base64.b64encode(files.read(s.input_file, 65536)).decode("ascii"),
                    output=base64.b64encode(files.read(s.output_file, 65536)).decode("ascii"),
                )
                for s in manifest.samples
            )
        finally:
            files.close()
        version_id = uuid4()
        cases = tuple(
            TestCase(
                case_id=uuid5(version_id, case.key),
                input=self.artifacts.store.reference(data),
                expected=self.artifacts.store.reference(answer),
            )
            for case, (data, answer) in zip(manifest.cases, payloads, strict=True)
        )
        legacy = isinstance(manifest, ProblemImport)
        projection = JudgingVersion(
            version_id,
            uuid4(),
            manifest.languages,
            manifest.toolchain if legacy else None,
            manifest.compile_limits if legacy else None,
            manifest.run_limits if legacy else None,
            manifest.job_wall_time_us if legacy else None,
            cases,
            manifest.schema_version,
            () if legacy else manifest.language_configurations,
        )
        for language in manifest.languages:
            check = projection.manifest(
                uuid4(),
                uuid4(),
                language,
                ArtifactReference(digest="0" * 64, bytes=MAX_SOURCE_BYTES),
            )
            v2.admit_manifest(self.policy, check, 1)
        async with self.sessions.begin() as session:
            # Lock existing and not-yet-existing logical slugs before assigning revisions.
            await repository.lock_slug(session, manifest.slug)
            problem = await repository.lock_problem(session, manifest.slug)
            if problem is None:
                problem = Problem(id=uuid4(), slug=manifest.slug)
                session.add(problem)
                await session.flush()
            if not problem.enabled:
                raise ProblemUnavailable("problem disabled")
            revision = await repository.next_revision(session, problem.id)
            session.add(
                ProblemVersion(
                    id=version_id,
                    problem_id=problem.id,
                    revision=revision,
                    title=manifest.title,
                    statement=statement,
                    languages=[lang.value for lang in manifest.languages],
                    judging_schema_version=manifest.schema_version,
                    language_configurations=(
                        None
                        if legacy
                        else [c.model_dump(mode="json") for c in manifest.language_configurations]
                    ),
                    toolchain=manifest.toolchain.model_dump(mode="json") if legacy else None,
                    compile_limits=manifest.compile_limits.model_dump(mode="json")
                    if legacy
                    else None,
                    run_limits=manifest.run_limits.model_dump(mode="json") if legacy else None,
                    job_wall_time_us=manifest.job_wall_time_us if legacy else None,
                    comparison=manifest.comparison,
                    public_samples=[s.model_dump() for s in samples],
                )
            )
            await session.flush()
            # Consistent digest lock order avoids deadlocks between overlapping imports/jobs.
            unique = {
                ref.digest: (data, ref)
                for case, (input_bytes, answer_bytes) in zip(cases, payloads, strict=True)
                for data, ref in [(input_bytes, case.input), (answer_bytes, case.expected)]
            }
            for digest in sorted(unique):
                data, reference = unique[digest]
                await self.artifacts.put(session, data, reference)
            for position, case in enumerate(cases):
                session.add(
                    ProblemCase(
                        id=case.case_id,
                        version_id=version_id,
                        position=position,
                        input_digest=case.input.digest,
                        expected_digest=case.expected.digest,
                    )
                )
            await session.flush()
            version = await session.get(ProblemVersion, version_id)
            version.published_at = await session.scalar(select(func.now()))
            problem.active_version_id = version_id
        return version_id

    @staticmethod
    def _summary(problem, version) -> ProblemSummary:
        return ProblemSummary(
            id=problem.id,
            slug=problem.slug,
            version_id=version.id,
            revision=version.revision,
            title=version.title,
            languages=tuple(version.languages),
        )

    async def list(self, *, after: str | None = None, limit: int = 20) -> ProblemPage:
        if not 1 <= limit <= 100:
            raise ValueError("invalid page limit")
        async with self.sessions() as session:
            rows = await repository.list_public(session, after, limit + 1)
            visible = rows[:limit]
            return ProblemPage(
                items=tuple(self._summary(*row) for row in visible),
                next_after=visible[-1][0].slug if len(rows) > limit else None,
            )

    async def detail(self, version_id: UUID) -> ProblemDetail:
        async with self.sessions() as session:
            row = await repository.public_version(session, version_id)
            if row is None:
                raise ProblemUnavailable("problem version unavailable")
            version, problem = row
            return ProblemDetail(
                **self._summary(problem, version).model_dump(),
                statement=version.statement,
                samples=tuple(PublicSample.model_validate(s) for s in version.public_samples),
                run_limits=(
                    RunLimits.model_validate(version.run_limits)
                    if version.judging_schema_version == 1
                    else None
                ),
                language_configurations=tuple(
                    PublicLanguageConfiguration.from_configuration(
                        v2.LanguageConfiguration.model_validate(c)
                    )
                    for c in version.language_configurations or ()
                ),
            )

    async def for_submission(self, session, version_id: UUID) -> JudgingVersion:
        if self.artifacts is None:
            raise ProblemUnavailable("artifact service unavailable")
        row = await repository.public_version(session, version_id)
        if row is None:
            raise ProblemUnavailable("problem version unavailable")
        version, problem = row
        cases = await repository.ordered_cases(session, version.id)
        refs = []
        digests = {d for case in cases for d in (case.input_digest, case.expected_digest)}
        records = {
            a.digest: a
            for a in await session.scalars(select(Artifact).where(Artifact.digest.in_(digests)))
        }
        for case in cases:
            input_record = records.get(case.input_digest)
            answer_record = records.get(case.expected_digest)
            if (
                not input_record
                or not answer_record
                or input_record.state != "ready"
                or answer_record.state != "ready"
            ):
                raise ProblemUnavailable("problem artifacts unavailable")
            refs.append(
                TestCase(
                    case_id=case.id,
                    input=ArtifactReference(digest=case.input_digest, bytes=input_record.bytes),
                    expected=ArtifactReference(
                        digest=case.expected_digest, bytes=answer_record.bytes
                    ),
                )
            )
        return JudgingVersion(
            version.id,
            problem.id,
            tuple(
                (Language if version.judging_schema_version == 1 else v2.Language)(v)
                for v in version.languages
            ),
            Toolchain.model_validate(version.toolchain)
            if version.judging_schema_version == 1
            else None,
            RunLimits.model_validate(version.compile_limits)
            if version.judging_schema_version == 1
            else None,
            RunLimits.model_validate(version.run_limits)
            if version.judging_schema_version == 1
            else None,
            version.job_wall_time_us,
            tuple(refs),
            version.judging_schema_version,
            tuple(
                v2.LanguageConfiguration.model_validate(c)
                for c in version.language_configurations or ()
            ),
        )
