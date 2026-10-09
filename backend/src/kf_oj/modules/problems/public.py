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
            cases=self.cases,
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
    async def for_submission(self, session, version_id: UUID) -> JudgingVersion: ...
