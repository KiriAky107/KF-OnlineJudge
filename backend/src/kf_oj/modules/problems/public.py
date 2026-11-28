"""Authorized immutable version projection; hidden references stay out of public DTOs."""

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from kf_oj.contracts import judging as v1
from kf_oj.contracts import judging_v2 as v2
from kf_oj.contracts.judging_versions import Manifest


class ProblemUnavailable(ValueError):
    pass


@dataclass(frozen=True)
class JudgingVersion:
    version_id: UUID
    problem_id: UUID
    languages: tuple[v1.Language | v2.Language, ...]
    toolchain: v1.Toolchain | None
    compile_limits: v1.RunLimits | None
    run_limits: v1.RunLimits | None
    job_wall_time_us: int | None
    cases: tuple[v1.TestCase, ...]
    judging_schema_version: int = 1
    language_configurations: tuple[v2.LanguageConfiguration, ...] = ()

    def manifest(
        self,
        evaluation_id: UUID,
        submission_id: UUID,
        language: v1.Language | v2.Language,
        source: v1.ArtifactReference,
        *,
        cases: tuple[v1.TestCase, ...] | None = None,
        self_test: bool = False,
    ) -> Manifest:
        """Build only from the immutable authorized version, preserving v1 wire bytes."""
        if language not in self.languages:
            raise ProblemUnavailable("language unavailable")
        fields = dict(
            schema_version=self.judging_schema_version,
            kind="judge.manifest",
            evaluation_id=evaluation_id,
            submission_id=submission_id,
            problem_version_id=self.version_id,
            language=language,
            source=source,
            cases=self.cases if cases is None else cases,
            self_test=self_test,
            comparison="exact_bytes",
        )
        if self.judging_schema_version == 1:
            if self.language_configurations:
                raise ProblemUnavailable("inconsistent legacy version")
            return v1.JobManifest(
                **fields,
                toolchain=self.toolchain,
                compile_limits=self.compile_limits,
                run_limits=self.run_limits,
                job_wall_time_us=self.job_wall_time_us,
            )
        if self.judging_schema_version != 2 or any(
            field is not None
            for field in (
                self.toolchain,
                self.compile_limits,
                self.run_limits,
                self.job_wall_time_us,
            )
        ):
            raise ProblemUnavailable("unsupported judging configuration")
        if tuple(c.language for c in self.language_configurations) != self.languages:
            raise ProblemUnavailable("inconsistent language configurations")
        configuration = next(c for c in self.language_configurations if c.language == language)
        return v2.JobManifest(
            **fields,
            **configuration.model_dump(exclude={"language"}),
        )


class ProblemPort(Protocol):
    async def for_self_test(
        self, session, version_id: UUID, *, accepted=False
    ) -> JudgingVersion: ...

    async def for_submission(self, session, version_id: UUID) -> JudgingVersion: ...

    async def for_accepted_execution(self, session, version_id: UUID) -> JudgingVersion:
        """Require an already-validated live execution lease; allow a disabled published version."""
        ...


@dataclass(frozen=True)
class LearningCandidate:
    problem_id: UUID
    version_id: UUID
    title: str
    tags: tuple[str, ...]
    difficulty: str | None
    metadata_revision: int
    available: bool
    slug: str
    is_current_version: bool
    prerequisite_tags: tuple[str, ...] = ()
    difficulty_score: int | None = None


async def learning_candidates(
    session,
    *,
    tags=(),
    difficulty=None,
    allowed_difficulties=(),
    match_any_tag=False,
    score_range: tuple[int | None, int | None] | None = None,
    limit=500,
) -> tuple[LearningCandidate, ...]:
    from .learning_metadata import candidates

    return await candidates(
        session,
        tags=tags,
        difficulty=difficulty,
        allowed_difficulties=allowed_difficulties,
        match_any_tag=match_any_tag,
        score_range=score_range,
        limit=limit,
    )


async def learning_versions(session, version_ids: set[UUID]) -> dict[UUID, LearningCandidate]:
    from .learning_metadata import versions

    return await versions(session, version_ids)


async def learning_selected(
    session, version_ids: set[UUID], *, current_only: bool = True
) -> dict[UUID, LearningCandidate]:
    from .learning_metadata import selected

    return await selected(session, version_ids, current_only=current_only)


@dataclass(frozen=True, slots=True)
class LearningDifficulty:
    label: str
    score: int


async def learning_difficulties(session, version_ids: set[UUID]) -> dict[UUID, LearningDifficulty]:
    from sqlalchemy import func, select

    from .learning_models import ProblemDifficultyLevel, ProblemLearningMetadata

    if not version_ids:
        return {}
    rows = await session.execute(
        select(
            ProblemLearningMetadata.problem_version_id,
            ProblemDifficultyLevel.label,
            ProblemDifficultyLevel.score,
        )
        .join(
            ProblemDifficultyLevel,
            ProblemDifficultyLevel.key
            == func.coalesce(
                ProblemLearningMetadata.difficulty_key, ProblemLearningMetadata.difficulty
            ),
        )
        .where(ProblemLearningMetadata.problem_version_id.in_(version_ids))
    )
    return {version_id: LearningDifficulty(label, score) for version_id, label, score in rows}


@dataclass(frozen=True)
class PublicDiagnosticSample:
    version_id: UUID
    ordinal: int
    input_bytes: bytes
    expected_bytes: bytes


async def diagnostic_version_available(session, version_id: UUID) -> bool:
    """Check that a currently enabled, published version can still run a diagnostic."""
    from .repository import published_version

    return await published_version(session, version_id, admission=True) is not None


async def public_diagnostic_sample(
    session, version_id: UUID, ordinal: int
) -> PublicDiagnosticSample:
    """Resolve an advertised sample by zero-based ordinal while holding the admission lock."""
    from base64 import b64decode
    from binascii import Error

    from kf_oj.contracts.problems import PublicSample

    from .repository import public_version

    if ordinal < 0:
        raise ProblemUnavailable("public sample unavailable")
    row = await public_version(session, version_id, admission=True)
    if row is None or ordinal >= len(row[0].public_samples):
        raise ProblemUnavailable("public sample unavailable")
    try:
        sample = PublicSample.model_validate(row[0].public_samples[ordinal])
        content = b64decode(sample.input, validate=True)
        expected = b64decode(sample.output, validate=True)
    except (Error, ValueError):
        raise ProblemUnavailable("public sample unavailable") from None
    if max(len(content), len(expected)) > 64 * 1024:
        raise ProblemUnavailable("public sample unavailable")
    return PublicDiagnosticSample(version_id, ordinal, content, expected)


async def learning_statement(session, version_id: UUID) -> tuple[str, str, int] | None:
    from .repository import public_version

    row = await public_version(session, version_id)
    if row is None:
        return None
    version, _ = row
    return version.title, version.statement, len(version.public_samples)


async def learning_review_problem(session, version_id: UUID) -> dict | None:
    """Project bounded public context for the restricted learning Worker."""
    from sqlalchemy import func, select

    return await session.scalar(select(func.kf_learning_review_problem(version_id)))
