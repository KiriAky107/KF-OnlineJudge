"""Deterministic learning evidence and personal plans; no inferred mastery scores."""

from collections.abc import Iterable
from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, StrictBool, field_validator

from kf_oj.contracts.judging import WireModel
from kf_oj.contracts.learning_model import ProgressiveHintLevel

Difficulty = Literal["easy", "medium", "hard"]
TrainingRankingProfile = Literal["feedback_first", "unresolved_first"]
TrainingRating = Literal["too_easy", "appropriate", "too_hard"]
Tag = Annotated[str, Field(min_length=1, max_length=64)]
TrainingGoal = Annotated[str, Field(max_length=200)]
TrainingTitle = Annotated[str, Field(min_length=1, max_length=120)]
DailyTrainingMinutes = Annotated[int, Field(ge=5, le=180)]
ReviewCycleDays = Annotated[int, Field(ge=7, le=90)]
WeeklyTrainingSessions = Annotated[int, Field(ge=1, le=7)]
EstimatedExerciseMinutes = Annotated[int, Field(ge=1, le=180)]


def focused_exercise_count(daily_minutes: int) -> int:
    return min(5, max(1, daily_minutes // 20))


class LearningParameters(WireModel):
    tags: Annotated[tuple[Tag, ...], Field(max_length=10)] = ()
    difficulty: Difficulty | None = None
    count: Annotated[int, Field(ge=1, le=10)] = 5
    # Keep absent/default mode out of the existing idempotency and tool argument bytes.
    cover_each_goal: StrictBool = Field(default=False, exclude_if=lambda value: not value)
    ranking_profile: TrainingRankingProfile | None = Field(
        default=None, exclude_if=lambda value: value is None
    )


class FocusedTrainingSchedule(WireModel):
    purpose: Literal["focused_training"] = "focused_training"
    goal: TrainingGoal
    daily_minutes: DailyTrainingMinutes


class PeriodicReviewSchedule(WireModel):
    purpose: Literal["periodic_review"] = "periodic_review"
    goal: TrainingGoal
    cycle_days: ReviewCycleDays
    sessions_per_week: WeeklyTrainingSessions
    available_minutes: DailyTrainingMinutes | None = Field(
        default=None, exclude_if=lambda value: value is None
    )


TrainingSchedule = Annotated[
    FocusedTrainingSchedule | PeriodicReviewSchedule, Field(discriminator="purpose")
]


class StoredTrainingParameters(LearningParameters):
    schedule: TrainingSchedule | None = Field(default=None, exclude_if=lambda value: value is None)
    previous_plan_id: UUID | None = Field(default=None, exclude_if=lambda value: value is None)
    allowed_difficulties: Annotated[tuple[Difficulty, ...], Field(max_length=3)] = Field(
        default=(), exclude_if=lambda value: not value
    )

    def selection(self) -> LearningParameters:
        return LearningParameters.model_validate(
            self.model_dump(include=set(LearningParameters.model_fields))
        )


class LearningEvidence(WireModel):
    submission_id: UUID
    evaluation_id: UUID
    problem_id: UUID
    problem_version_id: UUID
    submitted_at: datetime
    verdict: str | None
    state: str


class TrainingPrerequisite(WireModel):
    tag: Tag
    evidence: LearningEvidence
    metadata_revision: int


class PrerequisiteGap(WireModel):
    affected_candidates: Annotated[int, Field(ge=1, le=500)]
    missing_tags: Annotated[tuple[Tag, ...], Field(min_length=1, max_length=20)]
    remaining_tags: Annotated[int, Field(ge=0)]

    def message(self) -> str:
        labels = "、".join(self.missing_tags)
        other = f"及另外 {self.remaining_tags} 个知识点" if self.remaining_tags else ""
        return f"{self.affected_candidates} 道题需要先取得 {labels}{other}的通过记录。"


class TrainingGoalCoverage(WireModel):
    covered: Annotated[tuple[Tag, ...], Field(max_length=10)]
    missing: Annotated[tuple[Tag, ...], Field(max_length=10)]

    @classmethod
    def from_tags(
        cls, goals: tuple[str, ...], selected_tags: Iterable[tuple[str, ...]]
    ) -> "TrainingGoalCoverage":
        covered = {tag for tags in selected_tags for tag in tags}
        targets = dict.fromkeys(goals)
        return cls(
            covered=tuple(tag for tag in targets if tag in covered),
            missing=tuple(tag for tag in targets if tag not in covered),
        )


class TagStatistic(WireModel):
    tag: str
    sample_count: int
    accepted_count: int
    non_accepted_count: int
    evidence: tuple[LearningEvidence, ...]


class LearningOverview(WireModel):
    generated_at: datetime
    rule_version: str
    since: datetime
    limit: int
    truncated: bool
    earliest_record_at: datetime | None
    submission_count: int
    ordinary_sample_count: int
    distinct_problem_count: int
    accepted_problem_count: int
    accepted_version_ids: tuple[UUID, ...]
    verdict_counts: dict[str, int]
    tags: tuple[TagStatistic, ...]
    recent: tuple[LearningEvidence, ...]
    missing_metadata_count: int
    metadata_revisions: dict[UUID, int | None]


class TrainingFeedback(WireModel):
    rating: TrainingRating
    rated_at: AwareDatetime


class TrainingFeedbackEvidence(TrainingFeedback):
    training_id: UUID
    training_item_id: UUID
    problem_version_id: UUID
    preferred_difficulty: Difficulty


PracticeStatus = Literal["review_due", "practice", "review_later", "recent_attempt", "recent_plan"]
FirstAcceptanceKind = Literal["independent", "assisted"]


class TrainingTiming(WireModel):
    status: PracticeStatus
    last_attempt_at: AwareDatetime | None = None
    last_accepted_at: AwareDatetime | None = None
    last_planned_at: AwareDatetime | None = None
    review_due_at: AwareDatetime | None = None
    first_acceptance_kind: FirstAcceptanceKind | None = Field(
        default=None, exclude_if=lambda value: value is None
    )


PRACTICE_PRIORITY: dict[PracticeStatus, int] = {
    "review_due": 0,
    "practice": 1,
    "review_later": 2,
    "recent_attempt": 3,
    "recent_plan": 3,
}
PRACTICE_REASONS: dict[PracticeStatus, str] = {
    "review_due": "距上次通过已满 14 天，适合复习",
    "practice": "距上次提交已超过 7 天，可继续练习",
    "review_later": "距上次通过未满 14 天，可稍后复习",
    "recent_attempt": "最近 7 天已提交此版本",
    "recent_plan": "最近 7 天已纳入训练计划",
}


def practice_reason(timing: TrainingTiming) -> str:
    if timing.status == "review_due" and timing.first_acceptance_kind == "assisted":
        return "首个 AC 前已获得学习助手提示，距上次通过已满 7 天，适合复习"
    return PRACTICE_REASONS[timing.status]


def practice_priority(previously_accepted: bool, timing: TrainingTiming | None) -> int:
    if timing is not None:
        return PRACTICE_PRIORITY[timing.status]
    return PRACTICE_PRIORITY["review_later" if previously_accepted else "practice"]


def feedback_priority(
    difficulty: Difficulty | None, source: TrainingFeedbackEvidence | None
) -> int:
    if difficulty is None or source is None:
        return 0
    return -1 if difficulty == source.preferred_difficulty else 1


class RecommendedProblem(WireModel):
    problem_id: UUID
    problem_version_id: UUID
    slug: str
    title: str
    tags: tuple[str, ...]
    difficulty: Difficulty | None
    metadata_revision: int | None
    previously_accepted: bool
    reasons: tuple[str, ...]
    evidence: tuple[LearningEvidence, ...]
    feedback_evidence: TrainingFeedbackEvidence | None = None
    timing: TrainingTiming | None = None
    prerequisites: Annotated[tuple[TrainingPrerequisite, ...], Field(max_length=8)] = ()
    estimated_minutes: EstimatedExerciseMinutes | None = Field(
        default=None, exclude_if=lambda value: value is None
    )


class LearningRecommendations(WireModel):
    candidate_limit: int = 500
    candidates_truncated: bool = False
    generated_at: datetime
    rule_version: str
    parameters: LearningParameters
    items: tuple[RecommendedProblem, ...]
    gaps: tuple[str, ...]
    prerequisite_gap: PrerequisiteGap | None = None
    goal_coverage: TrainingGoalCoverage | None = None


class CreateTraining(LearningParameters):
    idempotency_key: UUID
    title: TrainingTitle = "个人训练计划"


class CreateTrainingFollowUp(WireModel):
    idempotency_key: UUID
    daily_minutes: DailyTrainingMinutes | None = Field(
        default_factory=lambda: None, exclude_if=lambda value: value is None
    )
    cycle_days: ReviewCycleDays | None = Field(
        default_factory=lambda: None, exclude_if=lambda value: value is None
    )
    sessions_per_week: WeeklyTrainingSessions | None = Field(
        default_factory=lambda: None, exclude_if=lambda value: value is None
    )


class TrainingSummary(WireModel):
    id: UUID
    title: str
    created_at: datetime
    rule_version: str
    item_count: int
    completed_count: int


class TrainingItem(RecommendedProblem):
    id: UUID
    position: int
    note: str
    progress: Literal["not_started", "attempted", "completed"]
    available: bool
    is_current_version: bool
    progress_evidence: tuple[LearningEvidence, ...]
    feedback: TrainingFeedback | None = None


class TrainingDetail(TrainingSummary):
    parameters: LearningParameters
    items: tuple[TrainingItem, ...]
    goal_coverage: TrainingGoalCoverage | None = None
    schedule: TrainingSchedule | None = Field(default=None, exclude_if=lambda value: value is None)
    previous_plan_id: UUID | None = Field(default=None, exclude_if=lambda value: value is None)
    official_publication_id: UUID | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    max_hint_level: ProgressiveHintLevel = "L3"
    publication_max_hint_level: ProgressiveHintLevel | None = Field(
        default=None, exclude_if=lambda value: value is None
    )


class TrainingPage(WireModel):
    limit: int = 50
    truncated: bool = False
    items: tuple[TrainingSummary, ...]


class ConfirmTrainingPublication(WireModel):
    confirmed: Literal[True]

    @field_validator("confirmed", mode="before")
    @classmethod
    def explicit_confirmation(cls, value: object) -> Literal[True]:
        if value is not True:
            raise ValueError("official publication requires explicit confirmation")
        return True


class TrainingPublicationSummary(WireModel):
    id: UUID
    title: str
    created_at: AwareDatetime
    item_count: int


class TrainingPublicationItem(WireModel):
    position: int
    problem_version_id: UUID | None = None
    title: str | None = None
    difficulty: Difficulty | None = None


class TrainingPublicationDetail(TrainingPublicationSummary):
    max_hint_level: ProgressiveHintLevel = "L3"
    items: tuple[TrainingPublicationItem, ...]


class TrainingPublicationPage(WireModel):
    items: tuple[TrainingPublicationSummary, ...]
    truncated: bool = False


TeachingTopic = Literal["correctness", "resource_use", "runtime", "compilation", "mixed"]


class TeamTrainingReviewItem(WireModel):
    position: int
    title: str
    topic: TeachingTopic


class TeamTrainingReview(WireModel):
    state: Literal["insufficient_data", "ready"]
    items: tuple[TeamTrainingReviewItem, ...] = ()


class UpdateTrainingNote(WireModel):
    note: Annotated[str, Field(max_length=1000)]


class ProblemNoteView(UpdateTrainingNote):
    problem_version_id: UUID
    updated_at: AwareDatetime | None


class UpdateTrainingFeedback(WireModel):
    rating: TrainingRating | None
