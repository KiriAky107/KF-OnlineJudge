"""One admission and immutable publication path for local and web imports."""

from uuid import uuid4

from sqlalchemy import func, select

from kf_oj.contracts import judging_v2 as v2
from kf_oj.contracts.judging import MAX_SOURCE_BYTES, ArtifactReference
from kf_oj.modules.problems import repository
from kf_oj.modules.problems.import_format import ProblemImport
from kf_oj.modules.problems.models import ProblemCase, ProblemVersion
from kf_oj.modules.problems.public import JudgingVersion, ProblemUnavailable


def admit_version(policy, manifest, version_id, problem_id, cases):
    legacy = isinstance(manifest, ProblemImport)
    projection = JudgingVersion(
        version_id,
        problem_id,
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
        v2.admit_manifest(
            policy,
            projection.manifest(
                uuid4(),
                uuid4(),
                language,
                ArtifactReference(digest="0" * 64, bytes=MAX_SOURCE_BYTES),
            ),
            1,
        )
    return projection


async def publish_version(session, problem, manifest, preview, version_id, cases):
    """Caller holds the logical slug/row locks and has made all artifacts durable."""
    if not problem.enabled:
        raise ProblemUnavailable("problem disabled")
    legacy = isinstance(manifest, ProblemImport)
    version = ProblemVersion(
        id=version_id,
        problem_id=problem.id,
        revision=await repository.next_revision(session, problem.id),
        title=manifest.title,
        statement=preview.statement,
        languages=[lang.value for lang in manifest.languages],
        judging_schema_version=manifest.schema_version,
        language_configurations=None
        if legacy
        else [c.model_dump(mode="json") for c in manifest.language_configurations],
        toolchain=manifest.toolchain.model_dump(mode="json") if legacy else None,
        compile_limits=manifest.compile_limits.model_dump(mode="json") if legacy else None,
        run_limits=manifest.run_limits.model_dump(mode="json") if legacy else None,
        job_wall_time_us=manifest.job_wall_time_us if legacy else None,
        comparison=manifest.comparison,
        public_samples=[s.model_dump(mode="json") for s in preview.samples],
    )
    session.add(version)
    await session.flush()
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
    version.published_at = await session.scalar(select(func.now()))
    problem.active_version_id = version_id
    await session.flush()
    return version
