from typing import Annotated, Literal

from pydantic import Field

from kf_oj.contracts.difficulty import DifficultyLevel
from kf_oj.contracts.judging import Identifier, Label, RunLimits, Verdict, WireModel
from kf_oj.contracts.judging_v2 import Language, PreparationKind, runtime_contract


class PublicSample(WireModel):
    encoding: Literal["base64"] = "base64"
    input: Annotated[str, Field(max_length=87384)]
    output: Annotated[str, Field(max_length=87384)]


class ProblemSummary(WireModel):
    id: Identifier
    slug: Label
    version_id: Identifier
    revision: int
    title: str
    languages: tuple[Language, ...]


ProblemDifficulty = Literal["easy", "medium", "hard"]


class CatalogProblem(ProblemSummary):
    difficulty_level: DifficultyLevel | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    difficulty: ProblemDifficulty | None = None
    tags: tuple[str, ...] = ()
    accepted_submissions: Annotated[int, Field(ge=0)] = 0
    judged_submissions: Annotated[int, Field(ge=0)] = 0
    total_submissions: Annotated[int, Field(ge=0)] = 0
    verdict_counts: dict[Verdict, Annotated[int, Field(ge=0)]] = Field(default_factory=dict)


class PublicLanguageConfiguration(WireModel):
    language: Language
    runtime_version: str
    source_name: str
    entrypoint: str
    preparation_kind: PreparationKind
    compile_limits: RunLimits
    run_limits: RunLimits
    job_wall_time_us: int

    @classmethod
    def from_configuration(cls, configuration):
        source, _, _, _, preparation = runtime_contract(configuration.language)
        return cls(
            language=configuration.language,
            runtime_version=configuration.toolchain.runtime_version,
            source_name=source,
            entrypoint="Main" if configuration.language == Language.JAVA21 else source,
            preparation_kind=preparation,
            compile_limits=configuration.compile_limits,
            run_limits=configuration.run_limits,
            job_wall_time_us=configuration.job_wall_time_us,
        )


class ProblemDetail(ProblemSummary):
    difficulty_level: DifficultyLevel | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    statement: str
    samples: tuple[PublicSample, ...]
    run_limits: RunLimits | None
    language_configurations: tuple[PublicLanguageConfiguration, ...] = ()
    execution_available: bool | None = None


class ProblemPage(WireModel):
    items: tuple[CatalogProblem, ...]
    next_after: Label | None
