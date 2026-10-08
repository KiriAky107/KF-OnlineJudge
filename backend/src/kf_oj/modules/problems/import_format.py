"""Local operator format. Filenames never cross into the judge execution protocol."""

from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from kf_oj.contracts.judging import (
    MAX_CASES,
    MAX_JOB_WALL_US,
    Label,
    Language,
    RunLimits,
    Toolchain,
    Version,
    WireModel,
)

FileName = Annotated[str, Field(strict=True, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$")]


class ImportCase(WireModel):
    key: Label
    input_file: FileName
    expected_file: FileName


class ImportSample(WireModel):
    input_file: FileName
    output_file: FileName


class ProblemImport(WireModel):
    schema_version: Version
    slug: Label
    title: Annotated[str, Field(strict=True, min_length=1, max_length=160)]
    statement_file: FileName
    languages: Annotated[tuple[Language, ...], Field(min_length=1, max_length=2)]
    toolchain: Toolchain
    comparison: Literal["exact_bytes"]
    compile_limits: RunLimits
    run_limits: RunLimits
    job_wall_time_us: Annotated[int, Field(strict=True, ge=1000, le=MAX_JOB_WALL_US)]
    cases: Annotated[tuple[ImportCase, ...], Field(min_length=1, max_length=MAX_CASES)]
    samples: Annotated[tuple[ImportSample, ...], Field(max_length=10)]

    @model_validator(mode="after")
    def unique(self) -> Self:
        if len(set(self.languages)) != len(self.languages):
            raise ValueError("duplicate language")
        if len({case.key for case in self.cases}) != len(self.cases):
            raise ValueError("duplicate testcase key")
        if len({(c.input_file, c.expected_file) for c in self.cases}) != len(self.cases):
            raise ValueError("duplicate testcase reference")
        return self
