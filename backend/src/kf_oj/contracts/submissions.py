from datetime import datetime
from typing import Annotated, Literal

from pydantic import Field, model_validator

from kf_oj.contracts.judging import (
    MAX_SOURCE_BYTES,
    CompilationStatus,
    FailureCode,
    Identifier,
    Language,
    Measurement,
    Verdict,
    WireModel,
)

EvaluationState = Literal[
    "queued", "claimed", "compiling", "running", "cancelling", "finished", "cancelled", "failed"
]


class CreateSubmission(WireModel):
    problem_version_id: Identifier
    language: Language
    idempotency_key: Identifier
    source: Annotated[
        str, Field(strict=True, min_length=1, max_length=MAX_SOURCE_BYTES, repr=False)
    ]

    @model_validator(mode="after")
    def bounded_source(self):
        try:
            if len(self.source.encode("utf-8")) > MAX_SOURCE_BYTES:
                raise ValueError("source exceeds byte policy")
        except UnicodeError:
            raise ValueError("source must be UTF-8") from None
        return self


class SubmissionCreated(WireModel):
    submission_id: Identifier
    evaluation_id: Identifier
    problem_version_id: Identifier
    language: Language
    state: EvaluationState
    created_at: datetime


class SubmissionSummary(SubmissionCreated):
    updated_at: datetime
    attempts: int
    verdict: Verdict | None
    failure_code: FailureCode | None


class PublicCaseResult(WireModel):
    position: int
    verdict: Verdict
    cpu_time_us: Measurement | None
    memory_peak_bytes: Measurement | None


class PublicJudgement(WireModel):
    compilation_status: CompilationStatus
    diagnostics: Annotated[str, Field(max_length=65536)]
    diagnostics_truncated: bool
    cpu_time_us: Measurement | None
    memory_peak_bytes: Measurement | None
    cleanup_complete: bool
    cases: tuple[PublicCaseResult, ...]


class SubmissionDetail(SubmissionSummary):
    result: PublicJudgement | None


class SubmissionPage(WireModel):
    items: tuple[SubmissionSummary, ...]
    next_after: str | None


class SubmissionSource(WireModel):
    submission_id: Identifier
    language: Language
    source: Annotated[str, Field(max_length=MAX_SOURCE_BYTES, repr=False)]


class CancellationAccepted(WireModel):
    submission_id: Identifier
    evaluation_id: Identifier
    # Acceptance means Coordinator durably accepted intent, not that processes have stopped.
    accepted: Literal[True] = True
