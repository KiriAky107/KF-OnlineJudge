from typing import Annotated, Literal

from pydantic import Field

from kf_oj.contracts.judging import Identifier, Label, RunLimits, WireModel
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
    statement: str
    samples: tuple[PublicSample, ...]
    run_limits: RunLimits | None
    language_configurations: tuple[PublicLanguageConfiguration, ...] = ()


class ProblemPage(WireModel):
    items: tuple[ProblemSummary, ...]
    next_after: Label | None
