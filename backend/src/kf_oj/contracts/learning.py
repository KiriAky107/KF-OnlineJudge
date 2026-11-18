"""Deterministic learning evidence and personal plans; no inferred mastery scores."""

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field

from kf_oj.contracts.judging import WireModel

Difficulty = Literal["easy", "medium", "hard"]
Tag = Annotated[str, Field(min_length=1, max_length=64)]


class LearningParameters(WireModel):
    tags: Annotated[tuple[Tag, ...], Field(max_length=10)] = ()
    difficulty: Difficulty | None = None
    count: Annotated[int, Field(ge=1, le=10)] = 5


class LearningEvidence(WireModel):
    submission_id: UUID
    evaluation_id: UUID
    problem_id: UUID
    problem_version_id: UUID
    submitted_at: datetime
    verdict: str | None
    state: str


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


class LearningRecommendations(WireModel):
    candidate_limit: int = 500
    candidates_truncated: bool = False
    generated_at: datetime
    rule_version: str
    parameters: LearningParameters
    items: tuple[RecommendedProblem, ...]
    gaps: tuple[str, ...]


class CreateTraining(LearningParameters):
    idempotency_key: UUID
    title: Annotated[str, Field(min_length=1, max_length=120)] = "个人训练计划"


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


class TrainingDetail(TrainingSummary):
    parameters: LearningParameters
    items: tuple[TrainingItem, ...]


class TrainingPage(WireModel):
    limit: int = 50
    truncated: bool = False
    items: tuple[TrainingSummary, ...]


class UpdateTrainingNote(WireModel):
    note: Annotated[str, Field(max_length=1000)]
