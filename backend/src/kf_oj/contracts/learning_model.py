"""Bounded input and output for optional learning model advice."""

from collections.abc import Awaitable, Callable
from typing import Annotated, Literal, Self

from pydantic import Field, StrictBool, StringConstraints, model_validator

from kf_oj.contracts.judging import Counter, Identifier, Measurement, Verdict, WireModel
from kf_oj.contracts.judging_v2 import Language
from kf_oj.contracts.submission_evidence import (
    MAX_SOURCE_EXCERPT_BYTES,
    CodeControlFlowDiff,
    ComparisonCase,
    CompilerCorrectionKind,
    SourceLineRange,
)

MODEL_REQUEST_SECONDS = 15
CHAT_MODEL_REQUEST_SECONDS = 120
ModelTopic = Literal["binary_search", "graph_traversal", "dynamic_programming"]


ModelStatus = Literal[
    "generated",
    "unavailable",
    "uncertain",
    "budget_exhausted",
    "output_limit",
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
ModelOutputChannel = Literal["answer", "reasoning"]
ModelOutput = Callable[[ModelOutputChannel, str], Awaitable[None]]
MAX_MODEL_HINT_CHARS = 6000
PartialChatAnswer = Annotated[str, StringConstraints(min_length=1)]
ChatReasoning = Annotated[str, StringConstraints(min_length=1)]
ModelAnswer = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
ModelHintText = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_MODEL_HINT_CHARS)
]
LearningQuestion = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=1200, pattern=r"^[^\x00]+$"),
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
    question: LearningQuestion | None = Field(default=None, exclude_if=lambda value: value is None)
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


CHAT_HISTORY_LIMIT = 3
CHAT_STATEMENT_LIMIT = 4000
ProgressiveHintLevel = Literal["L0", "L1", "L2", "L3"]
PROGRESSIVE_HINT_LEVELS: tuple[ProgressiveHintLevel, ...] = ("L0", "L1", "L2", "L3")


class LearningChatRequest(WireModel):
    question: LearningQuestion
    problem_version_id: Identifier | None = None
    previous_run_id: Identifier | None = None
    training_id: Identifier | None = Field(default=None, exclude_if=lambda value: value is None)
    include_submission_details: StrictBool = False
    hint_level: ProgressiveHintLevel | None = Field(
        default=None, exclude_if=lambda value: value is None
    )

    def assisted_version(self) -> Identifier | None:
        return self.problem_version_id if self.hint_level in {"L1", "L2", "L3"} else None

    def bounded_hint_level(self) -> ProgressiveHintLevel | None:
        return (self.hint_level or "L0") if self.problem_version_id is not None else None


class LearningHintCeiling(WireModel):
    max_hint_level: ProgressiveHintLevel


class LearningChatTurn(WireModel):
    question: LearningQuestion
    answer: ModelHintText


LearningChatHistory = Annotated[tuple[LearningChatTurn, ...], Field(max_length=CHAT_HISTORY_LIMIT)]


class RecentPracticeFact(WireModel):
    title: str
    difficulty: str | None
    difficulty_score: int | None
    accepted: StrictBool


class SourceExcerptModelContext(SourceLineRange):
    language: Language
    question: LearningQuestion | None = Field(default=None, exclude_if=lambda value: value is None)


class SourceExcerptModelFacts(SourceExcerptModelContext):
    text: Annotated[str, Field(min_length=1, max_length=MAX_SOURCE_EXCERPT_BYTES, repr=False)]

    @model_validator(mode="after")
    def bounded_bytes(self) -> Self:
        if len(self.text.encode("utf-8")) > MAX_SOURCE_EXCERPT_BYTES:
            raise ValueError("source excerpt exceeds the byte limit")
        return self


class ChatSubmissionDetails(WireModel):
    verdict: Verdict | None
    language: Language
    compilation_status: str | None
    cpu_time_us: Measurement | None
    memory_peak_bytes: Measurement | None
    accepted_cases: Measurement | None
    executed_cases: Measurement | None


class GuidedChatModelFacts(WireModel):
    question: LearningQuestion
    hint_level: ProgressiveHintLevel | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    history: LearningChatHistory = ()
    problem_title: str | None = None
    problem_statement: Annotated[str, Field(max_length=CHAT_STATEMENT_LIMIT)] | None = None
    public_sample_count: Measurement | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    recent_practice: Annotated[tuple[RecentPracticeFact, ...], Field(max_length=8)] = ()
    submission: ChatSubmissionDetails | None = None
    source_excerpt: SourceExcerptModelFacts | None = None
    hint_choices: Annotated[tuple[str, ...], Field(max_length=6)] = Field(
        default=(), exclude_if=lambda value: not value
    )


class GuidedHintChoice(WireModel):
    choice: Annotated[int, Field(strict=True, ge=1, le=6)]


GuidedDiagnosticCheck = Literal["values", "bounds", "branch", "update", "numeric_range", "input"]


class GuidedDiagnosticChoice(WireModel):
    line: Counter
    check: GuidedDiagnosticCheck


type ModelHintInput = (
    ModelTopic
    | GuidedHelpModelFacts
    | GuidedChatModelFacts
    | ReviewModelFacts
    | PlanModelFacts
    | SourceExcerptModelFacts
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
    text: ModelAnswer
    reasoning: ChatReasoning | None = Field(default=None, exclude_if=lambda value: value is None)
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
