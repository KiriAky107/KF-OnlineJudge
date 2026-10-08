"""Versioned judge wire models. Authorization and persistence belong to services."""

import json
from enum import StrEnum
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StrictBool,
    model_validator,
)

MIB = 1024 * 1024
I64_MAX = (1 << 63) - 1
MAX_SOURCE_BYTES = 256 * 1024
MAX_PACKAGE_BYTES = 64 * MIB
MAX_CASES = 256
MAX_JOB_WALL_US = 3_600_000_000
MAX_WIRE_BYTES = 256 * 1024


def integer_version(value: object) -> object:
    if type(value) is not int:
        raise ValueError("schema version must be an integer")
    return value


def canonical_id(value: object) -> UUID:
    try:
        parsed = UUID(str(value))
    except (ValueError, TypeError, AttributeError) as error:
        raise ValueError("invalid identifier") from error
    if parsed.int == 0 or str(value) != str(parsed):
        raise ValueError("identifier must be non-nil and canonical")
    return parsed


Version = Annotated[Literal[1], BeforeValidator(integer_version)]
Identifier = Annotated[UUID, BeforeValidator(canonical_id)]
Digest = Annotated[str, Field(strict=True, min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")]
Label = Annotated[str, Field(strict=True, pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")]
Counter = Annotated[int, Field(strict=True, ge=1, le=I64_MAX)]
Measurement = Annotated[int, Field(strict=True, ge=0, le=I64_MAX)]


class WireModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Language(StrEnum):
    C17 = "c17"
    CPP20 = "cpp20"


class Verdict(StrEnum):
    ACCEPTED = "accepted"
    WRONG_ANSWER = "wrong_answer"
    COMPILATION_ERROR = "compilation_error"
    RUNTIME_ERROR = "runtime_error"
    TIME_LIMIT = "time_limit_exceeded"
    MEMORY_LIMIT = "memory_limit_exceeded"
    OUTPUT_LIMIT = "output_limit_exceeded"
    CANCELLED = "cancelled"
    SYSTEM_ERROR = "system_error"


class CompilationStatus(StrEnum):
    SUCCEEDED = "succeeded"
    COMPILATION_ERROR = "compilation_error"
    COMPILATION_LIMIT = "compilation_limit"
    CANCELLED = "cancelled"
    SYSTEM_ERROR = "system_error"


class FailureCode(StrEnum):
    ARTIFACT_UNAVAILABLE = "artifact_unavailable"
    SANDBOX_UNAVAILABLE = "sandbox_unavailable"
    CLEANUP_FAILED = "cleanup_failed"
    LEASE_LOST = "lease_lost"
    JOB_DEADLINE = "job_deadline"


class ArtifactReference(WireModel):
    digest: Digest
    bytes: Annotated[int, Field(strict=True, ge=0, le=MAX_PACKAGE_BYTES)]


class Toolchain(WireModel):
    toolchain_id: Label
    image_kind: Literal["linux_gnu_cxx_x86_64_v1"]
    image_revision: Annotated[int, Field(strict=True, ge=1, le=(1 << 32) - 1)]


class RunLimits(WireModel):
    cpu_time_us: Annotated[int, Field(strict=True, ge=1000, le=60_000_000)]
    wall_time_us: Annotated[int, Field(strict=True, ge=1000, le=120_000_000)]
    memory_bytes: Annotated[int, Field(strict=True, ge=1, le=2048 * MIB)]
    stack_bytes: Annotated[int, Field(strict=True, ge=1, le=256 * MIB)]
    processes: Annotated[int, Field(strict=True, ge=1, le=128)]
    open_files: Annotated[int, Field(strict=True, ge=3, le=1024)]
    stdout_bytes: Annotated[int, Field(strict=True, ge=1, le=16 * MIB)]
    stderr_bytes: Annotated[int, Field(strict=True, ge=1, le=16 * MIB)]
    file_bytes: Annotated[int, Field(strict=True, ge=1, le=64 * MIB)]
    workdir_bytes: Annotated[int, Field(strict=True, ge=1, le=256 * MIB)]

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if self.stack_bytes > self.memory_bytes or self.file_bytes > self.workdir_bytes:
            raise ValueError("inconsistent resource limits")
        return self

    def fits(self, ceiling: "RunLimits") -> bool:
        return all(
            getattr(self, name) <= getattr(ceiling, name) for name in type(self).model_fields
        )


class TestCase(WireModel):
    case_id: Identifier
    input: ArtifactReference
    expected: ArtifactReference


class JobManifest(WireModel):
    schema_version: Version
    kind: Literal["judge.manifest"]
    evaluation_id: Identifier
    submission_id: Identifier
    problem_version_id: Identifier
    language: Language
    toolchain: Toolchain
    source: ArtifactReference
    cases: Annotated[tuple[TestCase, ...], Field(min_length=1, max_length=MAX_CASES)]
    comparison: Literal["exact_bytes"]
    compile_limits: RunLimits
    run_limits: RunLimits
    job_wall_time_us: Annotated[int, Field(strict=True, ge=1000, le=MAX_JOB_WALL_US)]

    def package_bytes(self) -> int:
        # Count references, including duplicates: this bounds actual staged/read bytes.
        return self.source.bytes + sum(c.input.bytes + c.expected.bytes for c in self.cases)

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if not 1 <= self.source.bytes <= MAX_SOURCE_BYTES:
            raise ValueError("source size outside policy")
        if len({c.case_id for c in self.cases}) != len(self.cases):
            raise ValueError("duplicate testcase identifiers")
        if self.package_bytes() > MAX_PACKAGE_BYTES:
            raise ValueError("test package outside policy")
        if any(c.expected.bytes > self.run_limits.stdout_bytes for c in self.cases):
            raise ValueError("expected output exceeds output budget")
        required_wall = (
            self.compile_limits.wall_time_us + len(self.cases) * self.run_limits.wall_time_us
        )
        if required_wall > self.job_wall_time_us:
            raise ValueError("job deadline cannot cover execution budgets")
        return self


def manifest_bytes(manifest: JobManifest) -> bytes:
    """Stable UTF-8 bytes for persisted snapshots and their SHA-256 identity."""
    return json.dumps(
        manifest.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


class LeaseBinding(WireModel):
    evaluation_id: Identifier
    attempt_id: Identifier
    worker_id: Identifier
    fencing_version: Counter


class LeaseGrant(WireModel):
    schema_version: Version
    kind: Literal["judge.lease"]
    binding: LeaseBinding
    lease_token: Digest = Field(repr=False)
    attempt_number: Annotated[int, Field(strict=True, ge=1, le=5)]
    expires_at_unix_ms: Counter
    manifest_digest: Digest


class StageReport(WireModel):
    schema_version: Version
    kind: Literal["judge.stage"]
    binding: LeaseBinding
    sequence: Counter
    stage: Literal["compiling", "running"]
    case_index: Annotated[int, Field(strict=True, ge=0, lt=MAX_CASES)] | None

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if (self.stage == "running") != (self.case_index is not None):
            raise ValueError("testcase index only belongs to running phase")
        return self


class CompilationReport(WireModel):
    status: CompilationStatus
    program: ArtifactReference | None
    diagnostics: Annotated[str, Field(strict=True, max_length=65536)] = Field(repr=False)
    diagnostics_truncated: StrictBool
    cpu_time_us: Measurement | None
    memory_peak_bytes: Measurement | None

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if len(self.diagnostics.encode("utf-8")) > 65536:
            raise ValueError("compiler diagnostics exceed byte budget")
        if (self.status == CompilationStatus.SUCCEEDED) != (self.program is not None):
            raise ValueError("compiled artifact only belongs to successful compilation")
        if self.program is not None and self.program.bytes == 0:
            raise ValueError("compiled artifact cannot be empty")
        return self


class CaseReport(WireModel):
    case_id: Identifier
    verdict: Verdict
    cpu_time_us: Measurement | None
    memory_peak_bytes: Measurement | None

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if self.verdict == Verdict.COMPILATION_ERROR:
            raise ValueError("compilation failure is not a testcase outcome")
        return self


class JudgementReport(WireModel):
    schema_version: Version
    kind: Literal["judge.result"]
    binding: LeaseBinding
    manifest_digest: Digest
    compilation: CompilationReport
    cases: Annotated[tuple[CaseReport, ...], Field(max_length=MAX_CASES)]
    verdict: Verdict
    cleanup_complete: StrictBool
    failure_code: FailureCode | None
    run_cpu_time_us: Measurement | None
    run_memory_peak_bytes: Measurement | None

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if len({c.case_id for c in self.cases}) != len(self.cases):
            raise ValueError("duplicate testcase results")
        if not self.cleanup_complete and self.failure_code != FailureCode.CLEANUP_FAILED:
            raise ValueError("incomplete cleanup requires its own failure evidence")
        if self.cleanup_complete and self.failure_code == FailureCode.CLEANUP_FAILED:
            raise ValueError("cleanup failure cannot claim completed cleanup")
        if (self.verdict == Verdict.SYSTEM_ERROR) != (self.failure_code is not None):
            raise ValueError("infrastructure failure and verdict must agree")
        terminal = next(
            (
                i
                for i, c in enumerate(self.cases)
                if c.verdict in (Verdict.SYSTEM_ERROR, Verdict.CANCELLED)
            ),
            None,
        )
        if terminal is not None and terminal != len(self.cases) - 1:
            raise ValueError("results after fatal testcase outcome")
        if self.compilation.status != CompilationStatus.SUCCEEDED:
            mapped = {
                CompilationStatus.COMPILATION_ERROR: Verdict.COMPILATION_ERROR,
                CompilationStatus.COMPILATION_LIMIT: Verdict.COMPILATION_ERROR,
                CompilationStatus.CANCELLED: Verdict.CANCELLED,
                CompilationStatus.SYSTEM_ERROR: Verdict.SYSTEM_ERROR,
            }[self.compilation.status]
            if self.cases or self.verdict != mapped:
                raise ValueError("failed compilation cannot have runtime results")
        elif self.failure_code is None:
            if any(c.verdict == Verdict.SYSTEM_ERROR for c in self.cases):
                raise ValueError("infrastructure failure requires failure code")
            if not self.cases and self.verdict != Verdict.CANCELLED:
                raise ValueError("missing testcase results cannot be a completed judgement")
            if self.cases and self.verdict != Verdict.CANCELLED:
                failures = (Verdict.SYSTEM_ERROR, Verdict.CANCELLED)
                summary = next(
                    (c.verdict for c in self.cases if c.verdict in failures),
                    next(
                        (c.verdict for c in self.cases if c.verdict != Verdict.ACCEPTED),
                        Verdict.ACCEPTED,
                    ),
                )
                if self.verdict != summary:
                    raise ValueError("testcase summary disagrees")
        cpu = (
            sum(c.cpu_time_us for c in self.cases if c.cpu_time_us is not None)
            if self.cases and all(c.cpu_time_us is not None for c in self.cases)
            else None
        )
        memory = (
            max(c.memory_peak_bytes for c in self.cases if c.memory_peak_bytes is not None)
            if self.cases and all(c.memory_peak_bytes is not None for c in self.cases)
            else None
        )
        if self.run_cpu_time_us != cpu or self.run_memory_peak_bytes != memory:
            raise ValueError("runtime measurement summary disagrees")
        return self

    def validate_for_manifest(self, manifest: JobManifest, digest: str) -> None:
        if self.binding.evaluation_id != manifest.evaluation_id or self.manifest_digest != digest:
            raise ValueError("report does not belong to authorized manifest")
        if [c.case_id for c in self.cases] != [
            c.case_id for c in manifest.cases[: len(self.cases)]
        ]:
            raise ValueError("testcase results differ from authorized order")
        if self.verdict not in (Verdict.COMPILATION_ERROR, Verdict.CANCELLED, Verdict.SYSTEM_ERROR):
            if len(self.cases) != len(manifest.cases):
                raise ValueError("incomplete ordinary judgement")


class AdmissionPolicy(WireModel):
    """Trusted node policy. A manifest never supplies or replaces this policy."""

    max_concurrent_jobs: Annotated[int, Field(strict=True, ge=1, le=64)] = 1
    prefetch: Annotated[int, Field(strict=True, ge=1, le=64)] = 1
    max_retries: Annotated[int, Field(strict=True, ge=0, le=4)] = 2
    max_job_wall_time_us: Annotated[int, Field(strict=True, ge=1000, le=MAX_JOB_WALL_US)]
    max_package_bytes: Annotated[int, Field(strict=True, ge=1, le=MAX_PACKAGE_BYTES)]
    toolchain: Toolchain
    compile_ceiling: RunLimits
    run_ceiling: RunLimits

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if self.prefetch > self.max_concurrent_jobs:
            raise ValueError("prefetch exceeds available job slots")
        return self

    def admit(self, manifest: JobManifest, attempt_number: int) -> None:
        if type(attempt_number) is not int or not 1 <= attempt_number <= self.max_retries + 1:
            raise ValueError("attempt retry budget exhausted")
        if (
            manifest.toolchain != self.toolchain
            or not manifest.compile_limits.fits(self.compile_ceiling)
            or not manifest.run_limits.fits(self.run_ceiling)
            or manifest.job_wall_time_us > self.max_job_wall_time_us
            or manifest.package_bytes() > self.max_package_bytes
        ):
            raise ValueError("job exceeds trusted node budget")


class StoppedReport(WireModel):
    schema_version: Version
    kind: Literal["judge.stopped"]
    binding: LeaseBinding
    cleanup_complete: StrictBool


WIRE_MODELS = {
    "judge-manifest": JobManifest,
    "judge-lease": LeaseGrant,
    "judge-stage": StageReport,
    "judge-result": JudgementReport,
    "judge-admission": AdmissionPolicy,
    "judge-stopped": StoppedReport,
}


def decode_wire[T: WireModel](model: type[T], payload: bytes) -> T:
    """UTF-8, bounded size and no ambiguous duplicate members before model validation."""
    if len(payload) > MAX_WIRE_BYTES:
        raise ValueError("wire payload exceeds byte budget")

    def unique_members(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON member")
            result[key] = value
        return result

    def invalid_constant(_: str) -> None:
        raise ValueError("invalid JSON constant")

    try:
        value = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=unique_members,
            parse_constant=invalid_constant,
        )
        return model.model_validate(value)
    except (ValueError, TypeError):
        # Errors at the transport boundary must not echo credentials or hidden inputs.
        raise ValueError("invalid wire payload") from None
