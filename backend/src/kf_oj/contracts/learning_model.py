"""Bounded input and output for optional learning model advice."""

from typing import Annotated, Literal, Self

from pydantic import Field, StrictBool, StringConstraints, model_validator

from kf_oj.contracts.judging import Counter, Measurement, Verdict, WireModel
from kf_oj.contracts.judging_v2 import Language
from kf_oj.contracts.submission_evidence import (
    MAX_SOURCE_EXCERPT_BYTES,
    CodeControlFlowDiff,
    ComparisonCase,
    CompilerCorrectionKind,
    SourceLineRange,
)

MODEL_REQUEST_SECONDS = 15
ModelTopic = Literal["binary_search", "graph_traversal", "dynamic_programming"]


ModelStatus = Literal[
    "generated",
    "unavailable",
    "uncertain",
    "budget_exhausted",
    "insufficient_evidence",
    "source_model_unavailable",
]
EvidenceCount = Annotated[int, Field(strict=True, ge=0, le=50)]
ComparedLineCount = Annotated[int, Field(strict=True, ge=0, le=512)]
ModelReviewBasis = Literal[
    "evaluations",
    "evaluations_and_code_counts",
    "evaluations_and_control_flow",
    "evaluations_and_comparison_cases",
    "source_excerpt",
]
ReviewFocusCategory = Literal["wrong_answer", "compilation_error", "later_accepted"]
ModelHintText = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1800)
]


class ReviewChangeCounts(WireModel):
    regions: Annotated[int, Field(strict=True, ge=0, le=16)]
    inserted_lines: ComparedLineCount
    deleted_lines: ComparedLineCount
    replaced_before_lines: ComparedLineCount
    replaced_after_lines: ComparedLineCount
    truncated: StrictBool


ComparisonCaseCount = Annotated[int, Field(strict=True, ge=0, le=8)]


class ReviewComparisonCases(WireModel):
    less: ComparisonCaseCount
    equal: ComparisonCaseCount
    greater: ComparisonCaseCount


class ReviewCodeFacts(WireModel):
    changes: ReviewChangeCounts | None = None
    compiler_issue: CompilerCorrectionKind | None = None
    control_flow: CodeControlFlowDiff | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    comparison_cases: ReviewComparisonCases | None = Field(
        default=None, exclude_if=lambda value: value is None
    )


class ReviewModelFacts(WireModel):
    finished: Annotated[int, Field(strict=True, ge=1, le=50)]
    accepted: EvidenceCount
    wrong_answers: EvidenceCount
    compile_errors: EvidenceCount
    runtime_errors: EvidenceCount
    resource_limits: EvidenceCount
    other_verdicts: EvidenceCount
    same_version_pairs: EvidenceCount
    later_accepted_pairs: EvidenceCount
    truncated: StrictBool
    code: ReviewCodeFacts | None = Field(default=None, exclude_if=lambda value: value is None)
    focus_options: Annotated[tuple[ReviewFocusCategory, ...], Field(max_length=3)] = Field(
        default=(), exclude_if=lambda value: not value
    )


PlanExerciseCount = Annotated[int, Field(strict=True, ge=0, le=7)]
PlanGoalCount = Annotated[int, Field(strict=True, ge=0, le=10)]


class PlanModelFacts(WireModel):
    easy: PlanExerciseCount
    medium: PlanExerciseCount
    hard: PlanExerciseCount
    unclassified: PlanExerciseCount
    daily_minutes: Annotated[int, Field(strict=True, ge=5, le=180)] | None = None
    cycle_days: Annotated[int, Field(strict=True, ge=7, le=90)] | None = None
    sessions_per_week: Annotated[int, Field(strict=True, ge=1, le=7)] | None = None
    covered_goals: PlanGoalCount | None = None
    uncovered_goals: PlanGoalCount | None = None


class GuidedHelpModelFacts(WireModel):
    topic: ModelTopic
    verdict: Verdict


class SourceExcerptModelContext(SourceLineRange):
    language: Language


class SourceExcerptModelFacts(SourceExcerptModelContext):
    text: Annotated[str, Field(min_length=1, max_length=MAX_SOURCE_EXCERPT_BYTES, repr=False)]

    @model_validator(mode="after")
    def bounded_bytes(self) -> Self:
        if len(self.text.encode("utf-8")) > MAX_SOURCE_EXCERPT_BYTES:
            raise ValueError("source excerpt exceeds the byte limit")
        return self


type ModelHintInput = (
    ModelTopic | GuidedHelpModelFacts | ReviewModelFacts | PlanModelFacts | SourceExcerptModelFacts
)


ModelFailureCode = Literal[
    "model_unavailable",
    "model_timeout",
    "model_rate_limited",
    "model_provider_rejected",
    "model_invalid_response",
    "model_output_limit",
]


class ModelHintUnavailable(RuntimeError):
    """A model could not return a usable result."""

    def __init__(
        self,
        message: str,
        *,
        code: ModelFailureCode = "model_unavailable",
        usage: "ModelTokenUsage | None" = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.usage = usage


class ModelTokenUsage(WireModel):
    input_tokens: Measurement
    output_tokens: Measurement
    total_tokens: Measurement


class ReviewFocusSelection(WireModel):
    text: ModelHintText
    focus: ReviewFocusCategory | None


class ReviewComparisonSelection(ReviewFocusSelection):
    comparison_case: ComparisonCase


class SourceExcerptReview(WireModel):
    text: ModelHintText
    line: Counter


class ModelHint(WireModel):
    text: ModelHintText
    model: Annotated[str, Field(min_length=1, max_length=96, pattern=r"^[A-Za-z0-9_.:-]+$")]
    response_id: Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")]
    latency_ms: Measurement
    usage: ModelTokenUsage | None = None
    review_focus: ReviewFocusCategory | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    review_comparison_case: ComparisonCase | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    source_line: Counter | None = Field(default=None, exclude_if=lambda value: value is None)
