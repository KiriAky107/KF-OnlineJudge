"""Explicit multi-runtime protocol; existing v1 snapshots remain byte-for-byte stable."""

import re
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import BeforeValidator, Field, model_validator

from kf_oj.contracts import judging as v1

Version = Annotated[Literal[2], BeforeValidator(v1.integer_version)]
MAX_CLASS_FILES = 256
JVM_NATIVE_RESERVE_BYTES = 160 * v1.MIB
JVM_THREAD_STACK_BYTES = 256 * 1024


class Language(StrEnum):
    C17 = "c17"
    CPP20 = "cpp20"
    PYTHON313 = "python313"
    JAVA21 = "java21"


class PreparationKind(StrEnum):
    COMPILE = "compile"
    SYNTAX_CHECK = "syntax_check"


class ProgramKind(StrEnum):
    NATIVE = "native_executable"
    PYTHON = "python_source"
    JAVA = "java_classes"


class RuntimeIdentity(v1.WireModel):
    """An exact identity matched against trusted configuration, never a host path."""

    toolchain_id: v1.Label
    image_kind: Literal[
        "linux_gnu_cxx_x86_64_v1", "linux_cpython313_x86_64_v1", "linux_openjdk21_x86_64_v1"
    ]
    image_revision: Annotated[int, Field(strict=True, ge=1, le=(1 << 32) - 1)]
    image_digest: v1.Digest
    runtime_version: Annotated[
        str, Field(strict=True, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.+-]{0,63}$")
    ]


def runtime_contract(language: Language) -> tuple[str, str, str, ProgramKind, PreparationKind]:
    """Fixed source/profile names. Runtime callers cannot introduce argv or paths."""
    return {
        Language.C17: (
            "main.c",
            "compile_c",
            "native",
            ProgramKind.NATIVE,
            PreparationKind.COMPILE,
        ),
        Language.CPP20: (
            "main.cpp",
            "compile_cpp",
            "native",
            ProgramKind.NATIVE,
            PreparationKind.COMPILE,
        ),
        Language.PYTHON313: (
            "main.py",
            "python_check",
            "python_run",
            ProgramKind.PYTHON,
            PreparationKind.SYNTAX_CHECK,
        ),
        Language.JAVA21: (
            "Main.java",
            "java_compile",
            "java_run",
            ProgramKind.JAVA,
            PreparationKind.COMPILE,
        ),
    }[language]


def validate_runtime(language: Language, identity: RuntimeIdentity) -> None:
    expected = {
        Language.C17: ("gnu-cxx-v1", "linux_gnu_cxx_x86_64_v1"),
        Language.CPP20: ("gnu-cxx-v1", "linux_gnu_cxx_x86_64_v1"),
        Language.PYTHON313: ("cpython313-v1", "linux_cpython313_x86_64_v1"),
        Language.JAVA21: ("openjdk21-v1", "linux_openjdk21_x86_64_v1"),
    }[language]
    if (identity.toolchain_id, identity.image_kind) != expected:
        raise ValueError("runtime family differs from language contract")
    if language == Language.PYTHON313 and identity.runtime_version != "3.13.16":
        raise ValueError("unsupported CPython version")
    if language == Language.JAVA21 and not re.fullmatch(
        r"21\.[0-9]+\.[0-9]+(?:\.[0-9]+)?\+[1-9][0-9]*(?:-LTS)?", identity.runtime_version
    ):
        raise ValueError("JDK version must identify a fixed Java 21 build")


def validate_startup_budget(language: Language, limits: v1.RunLimits, *, preparing: bool) -> None:
    """Admission floors, not multipliers on problem limits; real execution still required."""
    if language == Language.PYTHON313:
        floor = (10_000, 50_000, 32 * v1.MIB, 1, 16)
    elif language == Language.JAVA21:
        floor = (
            (1_000_000, 3_000_000, 384 * v1.MIB, 16, 32)
            if preparing
            else (200_000, 1_000_000, 256 * v1.MIB, 16, 32)
        )
    else:
        return
    actual = (
        limits.cpu_time_us,
        limits.wall_time_us,
        limits.memory_bytes,
        limits.processes,
        limits.open_files,
    )
    if any(value < minimum for value, minimum in zip(actual, floor, strict=True)):
        raise ValueError("budget below runtime startup policy")
    if language == Language.JAVA21:
        jvm_heap_bytes(limits)


def jvm_heap_bytes(limits: v1.RunLimits) -> int:
    """Reserve capped metaspace/code/native and every allowed thread stack inside cgroup."""
    reserve = JVM_NATIVE_RESERVE_BYTES + limits.processes * JVM_THREAD_STACK_BYTES
    heap = (limits.memory_bytes - reserve) // v1.MIB * v1.MIB
    if heap < 32 * v1.MIB or limits.stack_bytes < JVM_THREAD_STACK_BYTES:
        raise ValueError("budget cannot cover JVM heap and explicit non-heap reserve")
    return heap


class LanguageConfiguration(v1.WireModel):
    language: Language
    toolchain: RuntimeIdentity
    compile_limits: v1.RunLimits
    run_limits: v1.RunLimits
    job_wall_time_us: Annotated[int, Field(strict=True, ge=1000, le=v1.MAX_JOB_WALL_US)]

    @model_validator(mode="after")
    def runtime_and_budget(self) -> Self:
        validate_runtime(self.language, self.toolchain)
        validate_startup_budget(self.language, self.compile_limits, preparing=True)
        validate_startup_budget(self.language, self.run_limits, preparing=False)
        if self.compile_limits.wall_time_us + self.run_limits.wall_time_us > self.job_wall_time_us:
            raise ValueError("job deadline cannot cover preparation and execution")
        return self


class JobManifest(v1.JobManifest):
    schema_version: Version
    language: Language
    toolchain: RuntimeIdentity

    @model_validator(mode="after")
    def runtime_and_budget(self) -> Self:
        validate_runtime(self.language, self.toolchain)
        validate_startup_budget(self.language, self.compile_limits, preparing=True)
        validate_startup_budget(self.language, self.run_limits, preparing=False)
        return self


class ProgramFile(v1.WireModel):
    path: Annotated[str, Field(strict=True, min_length=1, max_length=128)]
    artifact: v1.ArtifactReference


class CompiledProgram(v1.WireModel):
    """Sealed file identities bound to a particular source, runtime and lease attempt."""

    kind: ProgramKind
    binding: v1.LeaseBinding
    source: v1.ArtifactReference
    toolchain: RuntimeIdentity
    files: Annotated[tuple[ProgramFile, ...], Field(min_length=1, max_length=MAX_CLASS_FILES)]

    @property
    def bytes(self) -> int:
        return sum(file.artifact.bytes for file in self.files)

    @model_validator(mode="after")
    def bounded_files(self) -> Self:
        names = [file.path for file in self.files]
        if names != sorted(set(names)):
            raise ValueError("program files must have unique canonical order")
        if not 1 <= self.source.bytes <= v1.MAX_SOURCE_BYTES:
            raise ValueError("program source outside policy")
        if self.bytes > v1.MAX_PACKAGE_BYTES or any(
            file.artifact.bytes == 0 for file in self.files
        ):
            raise ValueError("program files empty or outside byte policy")
        if self.kind == ProgramKind.NATIVE:
            validate_runtime(Language.C17, self.toolchain)
            if names != ["main"] or self.toolchain.image_kind != "linux_gnu_cxx_x86_64_v1":
                raise ValueError("native program requires its fixed executable and runtime")
        elif self.kind == ProgramKind.PYTHON:
            validate_runtime(Language.PYTHON313, self.toolchain)
            if names != ["main.py"] or self.files[0].artifact != self.source:
                raise ValueError("Python program must be the exact checked source")
        else:
            validate_runtime(Language.JAVA21, self.toolchain)
            if "Main.class" not in names or any(
                not re.fullmatch(r"[A-Za-z_$][A-Za-z0-9_$]*\.class", name) for name in names
            ):
                raise ValueError("Java requires a bounded default-package Main class set")
        return self


class CompilationReport(v1.CompilationReport):
    program: CompiledProgram | None


class JudgementReport(v1.JudgementReport):
    schema_version: Version
    compilation: CompilationReport

    @model_validator(mode="after")
    def program_attempt(self) -> Self:
        if (
            self.compilation.program is not None
            and self.compilation.program.binding != self.binding
        ):
            raise ValueError("program belongs to another attempt")
        return self

    def validate_for_manifest(self, manifest: JobManifest, digest: str) -> None:
        super().validate_for_manifest(manifest, digest)
        program = self.compilation.program
        if program is not None and (
            program.source != manifest.source
            or program.toolchain != manifest.toolchain
            or program.kind != runtime_contract(manifest.language)[3]
        ):
            raise ValueError("program differs from authorized source or runtime")


class LeaseGrant(v1.LeaseGrant):
    schema_version: Version


class StageReport(v1.StageReport):
    schema_version: Version
    preparation_kind: PreparationKind

    def validate_for_manifest(self, manifest: JobManifest) -> None:
        if self.binding.evaluation_id != manifest.evaluation_id or (
            self.preparation_kind != runtime_contract(manifest.language)[4]
        ):
            raise ValueError("stage differs from authorized language preparation")


class StoppedReport(v1.StoppedReport):
    schema_version: Version


class RuntimeCapability(v1.WireModel):
    language: Language
    toolchain: RuntimeIdentity
    compile_ceiling: v1.RunLimits
    run_ceiling: v1.RunLimits

    @model_validator(mode="after")
    def runtime_and_budget(self) -> Self:
        validate_runtime(self.language, self.toolchain)
        validate_startup_budget(self.language, self.compile_ceiling, preparing=True)
        validate_startup_budget(self.language, self.run_ceiling, preparing=False)
        return self


class AdmissionPolicy(v1.WireModel):
    schema_version: Version
    max_concurrent_jobs: Annotated[int, Field(strict=True, ge=1, le=64)] = 1
    prefetch: Annotated[int, Field(strict=True, ge=1, le=64)] = 1
    max_retries: Annotated[int, Field(strict=True, ge=0, le=4)] = 2
    max_job_wall_time_us: Annotated[int, Field(strict=True, ge=1000, le=v1.MAX_JOB_WALL_US)]
    max_package_bytes: Annotated[int, Field(strict=True, ge=1, le=v1.MAX_PACKAGE_BYTES)]
    capabilities: Annotated[tuple[RuntimeCapability, ...], Field(min_length=1, max_length=4)]

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if self.prefetch > self.max_concurrent_jobs:
            raise ValueError("prefetch exceeds available job slots")
        if len({cap.language for cap in self.capabilities}) != len(self.capabilities):
            raise ValueError("duplicate language capability")
        return self

    def admit(self, manifest: JobManifest, attempt_number: int) -> None:
        # Revalidate constructed/copied values as well as transport-decoded models.
        manifest = JobManifest.model_validate(manifest.model_dump(mode="json"))
        AdmissionPolicy.model_validate(self.model_dump(mode="json"))
        if type(attempt_number) is not int or not 1 <= attempt_number <= self.max_retries + 1:
            raise ValueError("attempt retry budget exhausted")
        cap = next((cap for cap in self.capabilities if cap.language == manifest.language), None)
        if cap is None or (
            cap.toolchain != manifest.toolchain
            or not manifest.compile_limits.fits(cap.compile_ceiling)
            or not manifest.run_limits.fits(cap.run_ceiling)
            or manifest.job_wall_time_us > self.max_job_wall_time_us
            or manifest.package_bytes() > self.max_package_bytes
        ):
            raise ValueError("job exceeds trusted runtime capability or budget")


class AdmissionRegistry(v1.WireModel):
    """Explicit operator opt-in for both protocol generations; no silent conversion."""

    schema_version: Version
    legacy: v1.AdmissionPolicy | None
    multi_runtime: AdmissionPolicy | None

    @model_validator(mode="after")
    def at_least_one_protocol(self) -> Self:
        if self.legacy is None and self.multi_runtime is None:
            raise ValueError("node must advertise at least one explicit protocol capability")
        if self.legacy is not None and self.multi_runtime is not None:
            for field in ("max_concurrent_jobs", "prefetch", "max_retries"):
                if getattr(self.legacy, field) != getattr(self.multi_runtime, field):
                    raise ValueError("protocol policies disagree on node capacity or retries")
        return self

    def admit(self, manifest: v1.JobManifest | JobManifest, attempt_number: int) -> None:
        AdmissionRegistry.model_validate(self.model_dump(mode="json"))
        policy = self.legacy if manifest.schema_version == 1 else self.multi_runtime
        admit_manifest(policy, manifest, attempt_number)


NodeAdmission = v1.AdmissionPolicy | AdmissionPolicy | AdmissionRegistry


def admit_manifest(
    policy: NodeAdmission | None, manifest: v1.JobManifest | JobManifest, attempt_number: int
) -> None:
    if isinstance(policy, AdmissionRegistry):
        policy.admit(manifest, attempt_number)
    elif manifest.schema_version == 1 and type(policy) is v1.AdmissionPolicy:
        policy.admit(manifest, attempt_number)
    elif manifest.schema_version == 2 and type(policy) is AdmissionPolicy:
        policy.admit(manifest, attempt_number)
    else:
        raise ValueError("node does not advertise this manifest protocol")


WIRE_MODELS = {
    "judge-manifest": JobManifest,
    "judge-lease": LeaseGrant,
    "judge-stage": StageReport,
    "judge-result": JudgementReport,
    "judge-admission": AdmissionPolicy,
    "judge-stopped": StoppedReport,
    "judge-registry": AdmissionRegistry,
}
