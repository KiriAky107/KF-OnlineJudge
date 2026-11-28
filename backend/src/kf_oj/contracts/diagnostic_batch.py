"""Private operator manifest for independently prepared diagnostic data."""

from typing import Annotated, Literal, Self

from pydantic import AwareDatetime, Field, StringConstraints, field_validator, model_validator

from kf_oj.contracts import judging as v1

MAX_DIAGNOSTIC_BATCH_CASES = 4
MAX_BATCH_MANIFEST_BYTES = 32 * 1024
BatchPurpose = Literal["differential", "boundary"]
BatchDiagnosticOutcome = Literal[
    "reference_pending",
    "input_not_verified",
    "reduction_verified",
    "reduction_unverified",
    "accepted",
    "completed",
    "compilation_error",
    "wrong_answer",
    "runtime_error",
    "resource_limit",
]


class ConfirmBatchDiagnostic(v1.WireModel):
    submission_id: v1.Identifier
    problem_version_id: v1.Identifier
    manifest_sha256: v1.Digest
    source_execution_confirmed: Literal[True]
    review_run_id: v1.Identifier | None = None

    @field_validator("source_execution_confirmed", mode="before")
    @classmethod
    def explicit_confirmation(cls, value: object) -> Literal[True]:
        if value is not True:
            raise ValueError("source execution requires explicit confirmation")
        return True


class BatchDiagnosticSummary(v1.WireModel):
    schema_version: v1.Version
    submission_id: v1.Identifier
    problem_version_id: v1.Identifier
    purpose: BatchPurpose
    outcome: BatchDiagnosticOutcome

    @model_validator(mode="after")
    def scope(self) -> Self:
        if (
            self.purpose == "boundary"
            and self.outcome
            not in ("completed", "resource_limit", "runtime_error", "input_not_verified")
        ) or (self.purpose == "differential" and self.outcome == "completed"):
            raise ValueError("diagnostic outcome differs from the batch purpose")
        return self


class BatchDiagnosticOption(v1.WireModel):
    manifest_sha256: v1.Digest
    title: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)]
    purpose: BatchPurpose


class RegisteredBatchOffer(BatchDiagnosticOption):
    problem_version_id: v1.Identifier
    language: Literal["c17", "cpp20"]


class BatchDiagnosticOptions(v1.WireModel):
    options: tuple[BatchDiagnosticOption, ...]


class BatchDiagnosticView(v1.WireModel):
    request_id: v1.Identifier
    state: Literal[
        "queued",
        "running",
        "cancel_pending",
        "cancelled",
        "expired",
        "unavailable",
        "observed",
        "failed",
    ]
    queue_deadline: AwareDatetime
    summary: BatchDiagnosticSummary | None = Field(
        default=None, exclude_if=lambda value: value is None
    )


class BatchFile(v1.WireModel):
    name: Annotated[str, Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$")]
    artifact: v1.ArtifactReference


class GeneratedCase(v1.WireModel):
    input: BatchFile
    independent_expected: BatchFile | None = None


class GenerationRecord(v1.WireModel):
    recipe: v1.Label
    revision: BatchFile
    seed: v1.Measurement


class IndependentOracle(v1.WireModel):
    method: v1.Label
    revision: BatchFile


class InputChecker(v1.WireModel):
    constraints_revision: BatchFile
    source: BatchFile


class ReductionProposal(v1.WireModel):
    original_input_sha256: v1.Digest
    smaller_input_sha256: v1.Digest | None = None


class ReviewedReference(v1.WireModel):
    origin: Literal["operator_reviewed"]
    submission_id: v1.Identifier
    evaluation_id: v1.Identifier
    accepted_report_sha256: v1.Digest
    source: v1.ArtifactReference


class ModelReferenceCandidate(v1.WireModel):
    origin: Literal["model_candidate"]
    source: v1.ArtifactReference


Reference = Annotated[ReviewedReference | ModelReferenceCandidate, Field(discriminator="origin")]


class DiagnosticBatchManifest(v1.WireModel):
    schema_version: v1.Version
    kind: Literal["diagnostic.batch-inputs"]
    problem_version_id: v1.Identifier
    language: v1.Language
    purpose: BatchPurpose
    generation: GenerationRecord
    oracle: IndependentOracle | None = None
    reference: Reference | None = None
    input_checker: InputChecker | None = None
    reduction: ReductionProposal | None = None
    cases: Annotated[
        tuple[GeneratedCase, ...], Field(min_length=2, max_length=MAX_DIAGNOSTIC_BATCH_CASES)
    ]

    @property
    def searches_reduction(self) -> bool:
        return self.reduction is not None and self.reduction.smaller_input_sha256 is None

    @model_validator(mode="after")
    def scope(self) -> Self:
        expected = tuple(case.independent_expected for case in self.cases)
        if self.purpose == "differential":
            if self.reference is None:
                raise ValueError("differential cases require a reference")
            if self.oracle is None:
                if isinstance(self.reference, ReviewedReference) or any(expected):
                    raise ValueError("differential cases require independent expected results")
            elif (
                not all(expected)
                or self.oracle.revision.artifact.digest == self.reference.source.digest
                or self.oracle.revision.artifact.digest == self.generation.revision.artifact.digest
            ):
                raise ValueError("differential cases require independent expected results")
        elif self.reference is not None or self.oracle is not None or any(expected):
            raise ValueError("boundary cases only carry generated inputs")
        if len({case.input.artifact.digest for case in self.cases}) != len(self.cases):
            raise ValueError("diagnostic inputs must be distinct")
        if (
            self.reference is not None
            and not 1 <= self.reference.source.bytes <= v1.MAX_SOURCE_BYTES
        ):
            raise ValueError("reference source size outside diagnostic limits")
        if self.input_checker is not None:
            source = self.input_checker.source.artifact
            constraint = self.input_checker.constraints_revision.artifact
            independent = {source.digest, constraint.digest}
            evidence = {self.generation.revision.artifact.digest}
            if self.reference is not None:
                evidence.add(self.reference.source.digest)
            if self.oracle is not None:
                evidence.add(self.oracle.revision.artifact.digest)
            if (
                len(independent) != 2
                or independent & evidence
                or not 1 <= source.bytes <= v1.MAX_SOURCE_BYTES
                or constraint.bytes == 0
            ):
                raise ValueError(
                    "input checker requires independent reviewed constraints and source"
                )
        if self.reduction is not None:
            if (
                self.purpose != "differential"
                or not isinstance(self.reference, ReviewedReference)
                or self.input_checker is None
            ):
                raise ValueError("reduction requires a reviewed differential batch with a checker")
            original, *smaller = (case.input.artifact for case in self.cases)
            if (
                original.digest != self.reduction.original_input_sha256
                or any(item.bytes >= original.bytes for item in smaller)
                or (
                    not self.searches_reduction
                    and (
                        len(smaller) != 1
                        or smaller[0].digest != self.reduction.smaller_input_sha256
                    )
                )
            ):
                raise ValueError("reduction must bind the original and only smaller inputs")
        return self


class BoundaryMeasurement(v1.WireModel):
    case_id: v1.Identifier
    outcome: Literal[
        "completed", "runtime_error", "time_limit", "memory_limit", "output_limit", "other_limit"
    ]
    cpu_time_us: v1.Measurement
    memory_peak_bytes: v1.Measurement


class DiagnosticBoundaryReport(v1.WireModel):
    schema_version: v1.Version
    kind: Literal["diagnostic.boundary-measurements"]
    binding: v1.LeaseBinding
    manifest_digest: v1.Digest
    cases: Annotated[
        tuple[BoundaryMeasurement, ...], Field(min_length=2, max_length=MAX_DIAGNOSTIC_BATCH_CASES)
    ]

    @model_validator(mode="after")
    def distinct_cases(self) -> Self:
        if len({case.case_id for case in self.cases}) != len(self.cases):
            raise ValueError("boundary measurements must bind distinct cases")
        return self
