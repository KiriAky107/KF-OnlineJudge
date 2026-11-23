"""Private learning workflow contracts shared by the planner and its tools."""

from datetime import datetime
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, Field, StrictBool, TypeAdapter, model_validator

from kf_oj.contracts.agent_runtime import AgentPurpose, AgentRun
from kf_oj.contracts.judging import Identifier, Label, WireModel
from kf_oj.contracts.learning import (
    DailyTrainingMinutes,
    Difficulty,
    EstimatedExerciseMinutes,
    LearningEvidence,
    LearningParameters,
    PrerequisiteGap,
    ReviewCycleDays,
    Tag,
    TrainingFeedbackEvidence,
    TrainingGoal,
    TrainingGoalCoverage,
    TrainingPrerequisite,
    TrainingRankingProfile,
    TrainingTiming,
    WeeklyTrainingSessions,
)
from kf_oj.contracts.learning_model import (
    ModelHint,
    ModelReviewBasis,
    ModelStatus,
    ModelTopic,
    ReviewFocusCategory,
)
from kf_oj.contracts.submission_evidence import SourceLineRange, SubmissionCodeEvidence


class SubmissionReviewParameters(WireModel):
    days: Annotated[int, Field(ge=1, le=90)] = 30
    problem_ids: Annotated[tuple[Identifier, ...], Field(max_length=20)] = ()
    detail: Literal["brief", "normal", "detailed"] = "normal"


class GuidedHelpParameters(WireModel):
    hint_level: Literal["concept", "location", "local_change"] = "concept"
    allow_code_examples: StrictBool = False


class FocusedTrainingParameters(WireModel):
    tags: Annotated[tuple[Tag, ...], Field(max_length=10)] = ()
    goal: TrainingGoal = ""
    daily_minutes: DailyTrainingMinutes = 30
    minimum_difficulty: Difficulty = "easy"
    maximum_difficulty: Difficulty = "hard"
    cover_each_goal: StrictBool = Field(default=False, exclude_if=lambda value: not value)
    ranking_profile: TrainingRankingProfile | None = Field(
        default=None, exclude_if=lambda value: value is None
    )

    @model_validator(mode="after")
    def ordered_difficulty(self) -> Self:
        levels = ("easy", "medium", "hard")
        if levels.index(self.minimum_difficulty) > levels.index(self.maximum_difficulty):
            raise ValueError("minimum difficulty exceeds maximum difficulty")
        return self


class PeriodicReviewParameters(WireModel):
    cycle_days: ReviewCycleDays = 14
    sessions_per_week: WeeklyTrainingSessions = 3
    stage_goal: TrainingGoal = ""
    ranking_profile: TrainingRankingProfile | None = Field(
        default=None, exclude_if=lambda value: value is None
    )


class SubmissionHistoryArguments(WireModel):
    days: Annotated[int, Field(ge=1, le=90)] = 30
    limit: Annotated[int, Field(ge=1, le=50)] = 20
    problem_ids: Annotated[tuple[Identifier, ...], Field(max_length=20)] = ()
    submission_id: Identifier | None = None
    anchor_submission_id: Identifier | None = Field(
        default=None, exclude_if=lambda value: value is None
    )


class SubmissionHistoryResult(WireModel):
    since: datetime
    truncated: bool
    records: tuple[LearningEvidence, ...]


class TrainingCandidate(WireModel):
    problem_id: UUID
    problem_version_id: UUID
    title: str
    tags: tuple[Tag, ...]
    difficulty: Difficulty | None
    previously_accepted: bool
    feedback_evidence: TrainingFeedbackEvidence | None = None
    timing: TrainingTiming | None = None
    prerequisites: Annotated[tuple[TrainingPrerequisite, ...], Field(max_length=8)] = ()


class TrainingCandidatesArguments(LearningParameters):
    allowed_difficulties: Annotated[tuple[Difficulty, ...], Field(max_length=3)] = ()


class TrainingCandidatesResult(WireModel):
    items: tuple[TrainingCandidate, ...]
    candidate_limit: int
    candidates_truncated: bool
    prerequisite_gap: PrerequisiteGap | None = None
    goal_coverage: TrainingGoalCoverage | None = None


class AgentLearningInput(WireModel):
    submission_id: Identifier | None = None
    include_source_evidence: StrictBool = False
    model_topic: ModelTopic | None = None
    include_model_verdict: StrictBool = Field(
        default_factory=lambda: False, exclude_if=lambda value: not value
    )
    include_model_review: StrictBool = False
    include_model_code_context: StrictBool = False
    include_model_control_flow: StrictBool = Field(
        default_factory=lambda: False, exclude_if=lambda value: not value
    )
    include_model_comparison_cases: StrictBool = Field(
        default_factory=lambda: False, exclude_if=lambda value: not value
    )
    include_model_evidence_focus: StrictBool = Field(
        default_factory=lambda: False, exclude_if=lambda value: not value
    )
    include_model_plan_explanation: StrictBool = Field(
        default=False, exclude_if=lambda value: not value
    )
    model_source_excerpt: SourceLineRange | None = Field(
        default=None, exclude_if=lambda value: value is None
    )


class StoredAgentLearningInput(AgentLearningInput):
    source_evidence: SubmissionCodeEvidence | None = None
    evidence_as_of: AwareDatetime | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    review_anchor: Identifier | None = Field(default=None, exclude_if=lambda value: value is None)


class SourceEvidenceArguments(WireModel):
    pass


class SourceEvidenceResult(WireModel):
    evidence: SubmissionCodeEvidence | None


class ReviewComparison(WireModel):
    earlier_submission_id: UUID
    later_submission_id: UUID
    outcome: Literal["later_accepted", "later_non_accepted", "other"]


class ReviewEvidenceFocus(WireModel):
    category: ReviewFocusCategory
    evidence: LearningEvidence


class LearningResult(WireModel):
    evidence_as_of: AwareDatetime | None = Field(
        default=None, exclude_if=lambda value: value is None
    )


class SubmissionReviewResult(LearningResult):
    purpose: Literal[AgentPurpose.SUBMISSION_REVIEW]
    since: datetime
    truncated: bool
    records: tuple[LearningEvidence, ...]
    comparisons: tuple[ReviewComparison, ...]
    anchor_submission_id: Identifier | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    code: SubmissionCodeEvidence | None = None
    model_hint: ModelHint | None = None
    model_status: ModelStatus | None = None
    model_review_basis: ModelReviewBasis | None = None
    model_focus: ReviewEvidenceFocus | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    gaps: tuple[str, ...]


class GuidedHelpResult(LearningResult):
    purpose: Literal[AgentPurpose.GUIDED_HELP]
    requested_level: Literal["concept", "location", "local_change"]
    delivered_level: Literal["concept", "location", "local_change"] = "concept"
    hint: str
    hint_example: Annotated[str, Field(max_length=120)] | None = None
    evidence: LearningEvidence | None
    code: SubmissionCodeEvidence | None = None
    model_hint: ModelHint | None = None
    model_status: ModelStatus | None = None
    model_hint_basis: Literal["topic_and_verdict"] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    gaps: tuple[str, ...]


class ProposedExercise(WireModel):
    problem_id: UUID
    problem_version_id: UUID
    title: str
    difficulty: Difficulty | None
    tags: tuple[Tag, ...]
    position: int
    objective: str
    estimated_minutes: EstimatedExerciseMinutes | None = None
    feedback_evidence: TrainingFeedbackEvidence | None = None
    timing: TrainingTiming | None = None
    prerequisites: Annotated[tuple[TrainingPrerequisite, ...], Field(max_length=8)] = ()


class FocusedTrainingResult(LearningResult):
    purpose: Literal[AgentPurpose.FOCUSED_TRAINING]
    goal: str
    daily_minutes: int
    items: tuple[ProposedExercise, ...]
    gaps: tuple[str, ...]
    goal_coverage: TrainingGoalCoverage | None = None
    model_hint: ModelHint | None = None
    model_status: ModelStatus | None = None
    ranking_profile: TrainingRankingProfile | None = Field(
        default=None, exclude_if=lambda value: value is None
    )


class PeriodicReviewResult(LearningResult):
    purpose: Literal[AgentPurpose.PERIODIC_REVIEW]
    goal: str
    cycle_days: int
    sessions_per_week: int
    evidence: tuple[LearningEvidence, ...]
    items: tuple[ProposedExercise, ...]
    gaps: tuple[str, ...]
    model_hint: ModelHint | None = None
    model_status: ModelStatus | None = None
    ranking_profile: TrainingRankingProfile | None = Field(
        default=None, exclude_if=lambda value: value is None
    )


AgentLearningResult = Annotated[
    SubmissionReviewResult | GuidedHelpResult | FocusedTrainingResult | PeriodicReviewResult,
    Field(discriminator="purpose"),
]

LEARNING_RESULT = TypeAdapter(AgentLearningResult)


RunFeedbackRating = Literal["helpful", "partly_helpful", "not_helpful"]
RunFeedbackReason = Literal["unclear_next_step", "unrelated_hint", "insufficient_evidence"]


class UpdateRunFeedback(WireModel):
    rating: RunFeedbackRating | None
    reason: RunFeedbackReason | None = None

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if self.rating is None and self.reason is not None:
            raise ValueError("withdrawing feedback cannot set a reason")
        return self


class RunFeedback(WireModel):
    rating: RunFeedbackRating
    reason: RunFeedbackReason | None
    rated_at: AwareDatetime


class AgentExecution(WireModel):
    run: AgentRun
    result: AgentLearningResult | None = None
    failure_code: Label | None = None
    feedback: RunFeedback | None = Field(default=None, exclude_if=lambda value: value is None)
