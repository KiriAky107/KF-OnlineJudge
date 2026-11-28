"""Private contract from the diagnostics broker to the isolated execution Worker."""

from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from kf_oj.contracts import judging as v1
from kf_oj.contracts.judging_v2 import LanguageConfiguration

CLAIM_SECONDS = 60
MAX_DIAGNOSTIC_WALL_US = 55_000_000
MAX_PREPARATION_WALL_US = 20_000_000
MAX_SAMPLE_WALL_US = 5_000_000
MAX_SAMPLE_BYTES = 64 * 1024
MAX_CONTROL_BYTES = 16 * 1024
DiagnosticMode = Literal["optimization", "ubsan"]
SourceCoordinate = Annotated[int, Field(strict=True, ge=1, le=v1.MAX_SOURCE_BYTES)]


class DiagnosticBinding(v1.WireModel):
    request_id: v1.Identifier
    execution_id: v1.Identifier
    worker_id: v1.Identifier


class DiagnosticRun(v1.WireModel):
    schema_version: v1.Version
    kind: Literal["diagnostic.run"]
    binding: DiagnosticBinding
    problem_version_id: v1.Identifier
    configuration: LanguageConfiguration
    claim_expires_at_unix_ms: v1.Counter
    source: v1.ArtifactReference
    sample: v1.TestCase
    mode: DiagnosticMode = Field(
        default="optimization", exclude_if=lambda value: value == "optimization"
    )

    @model_validator(mode="after")
    def consistent(self) -> Self:
        config = self.configuration
        compile_us = config.compile_limits.wall_time_us
        run_us = config.run_limits.wall_time_us
        passes = 1 if self.mode == "ubsan" else 2
        if (
            config.language not in ("c17", "cpp20")
            or compile_us > MAX_PREPARATION_WALL_US
            or run_us > MAX_SAMPLE_WALL_US
            or not passes * (compile_us + run_us)
            <= config.job_wall_time_us
            <= MAX_DIAGNOSTIC_WALL_US
            or not 1 <= self.source.bytes <= v1.MAX_SOURCE_BYTES
            or max(self.sample.input.bytes, self.sample.expected.bytes) > MAX_SAMPLE_BYTES
            or self.sample.expected.bytes > config.run_limits.stdout_bytes
            or self.sample.case_id != self.binding.execution_id
        ):
            raise ValueError("diagnostic input or budget is outside policy")
        return self


class DiagnosticObservation(v1.WireModel):
    o2: v1.Verdict
    o0: v1.Verdict | None

    @model_validator(mode="after")
    def consistent(self) -> Self:
        unavailable = (v1.Verdict.CANCELLED, v1.Verdict.SYSTEM_ERROR)
        contrast = (v1.Verdict.WRONG_ANSWER, v1.Verdict.RUNTIME_ERROR)
        if (
            self.o2 in unavailable
            or self.o0 in unavailable
            or (self.o2 in contrast) != (self.o0 is not None)
        ):
            raise ValueError("invalid diagnostic observation")
        return self


class UbsanFinding(v1.WireModel):
    category: Literal[
        "signed_integer_overflow", "division_by_zero", "index_out_of_bounds", "invalid_shift"
    ]
    line: SourceCoordinate
    column: SourceCoordinate


class UbsanObservation(v1.WireModel):
    verdict: v1.Verdict
    finding: UbsanFinding | None

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if self.verdict in (v1.Verdict.CANCELLED, v1.Verdict.SYSTEM_ERROR) or (
            self.finding is not None and self.verdict is not v1.Verdict.RUNTIME_ERROR
        ):
            raise ValueError("invalid UBSan observation")
        return self


class DiagnosticResult(v1.WireModel):
    schema_version: v1.Version
    kind: Literal["diagnostic.result"]
    binding: DiagnosticBinding
    state: Literal["observed", "failed", "cancelled", "cleanup_unconfirmed"]
    observation: DiagnosticObservation | None
    failure_code: v1.FailureCode | None
    ubsan: UbsanObservation | None = Field(default=None, exclude_if=lambda value: value is None)

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if (
            (self.state == "observed") != (self.observation is not None or self.ubsan is not None)
            or (self.observation is not None and self.ubsan is not None)
            or (self.state == "failed") != (self.failure_code is not None)
            or self.failure_code == v1.FailureCode.CLEANUP_FAILED
        ):
            raise ValueError("invalid diagnostic result")
        return self
