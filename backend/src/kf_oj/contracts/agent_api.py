"""Authenticated learning routes and their public projections."""

from typing import Annotated, Literal

from pydantic import (
    AwareDatetime,
    Field,
    JsonValue,
    StrictBool,
    StrictInt,
    StringConstraints,
    field_validator,
)

from kf_oj.contracts.agent_learning import (
    AgentLearningInput,
    AgentLearningResult,
    PeriodicReviewRequest,
    RunFeedback,
    TrainingSetDraft,
)
from kf_oj.contracts.agent_runtime import AgentPurpose, RunState
from kf_oj.contracts.diagnostic import (
    DiagnosticMode,
    DiagnosticObservation,
    UbsanFinding,
    UbsanObservation,
)
from kf_oj.contracts.judging import Counter, Identifier, Label, Measurement, WireModel
from kf_oj.contracts.learning import DailyTrainingMinutes, EstimatedExerciseMinutes, Tag
from kf_oj.contracts.learning_model import (
    ChatReasoning,
    LearningChatHistory,
    LearningChatRequest,
    LearningQuestion,
    ModelAnswer,
    ModelReasoningKind,
    ModelTokenUsage,
    PartialChatAnswer,
)


class SkillToolView(WireModel):
    name: Label
    version: Annotated[int, Field(ge=1, le=65535)]


class ConfigureSkill(WireModel):
    enabled: StrictBool
    parameters: dict[str, JsonValue] = Field(default_factory=dict, max_length=16)
    authorized_tools: Annotated[tuple[SkillToolView, ...], Field(max_length=8)] = ()


class SkillBindingView(ConfigureSkill):
    skill_version_id: Identifier


class SkillVersionView(WireModel):
    version_id: Identifier
    origin: Literal["builtin", "published", "trial"]
    slug: Label
    release: int
    title: str
    description: str
    purpose: AgentPurpose
    default_parameters: dict[str, JsonValue]
    available_tools: tuple[SkillToolView, ...]
    binding: SkillBindingView | None = None
    archived_at: AwareDatetime | None = Field(default=None, exclude_if=lambda value: value is None)


class SkillCatalogView(WireModel):
    versions: tuple[SkillVersionView, ...]


class CreateLearningSession(WireModel):
    purpose: AgentPurpose
    conversation_session_id: Identifier | None = Field(
        default=None, exclude_if=lambda value: value is None
    )


class LearningSessionView(CreateLearningSession):
    session_id: Identifier
    created_at: AwareDatetime


class StartAgentRun(AgentLearningInput):
    request_id: Identifier
    skill_version_id: Identifier | None = None


class ConfirmPublicSampleDiagnostic(WireModel):
    submission_id: Identifier
    problem_version_id: Identifier
    sample_ordinal: Annotated[StrictInt, Field(ge=0, le=65535)]
    source_execution_confirmed: Literal[True]
    mode: DiagnosticMode = "optimization"
    review_run_id: Identifier | None = None

    @field_validator("source_execution_confirmed", mode="before")
    @classmethod
    def explicit_confirmation(cls, value: object) -> Literal[True]:
        if value is not True:
            raise ValueError("source execution requires explicit confirmation")
        return True


class PublicDiagnosticView(WireModel):
    request_id: Identifier
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
    mode: DiagnosticMode
    sample_ordinal: Annotated[StrictInt, Field(ge=0, le=65535)]
    observation: DiagnosticObservation | None = None
    ubsan: UbsanObservation | None = Field(default=None, exclude_if=lambda value: value is None)


class PublicDiagnosticAvailability(WireModel):
    available: StrictBool
    ubsan: StrictBool = Field(default=False, exclude_if=lambda value: not value)
    registered_batches: StrictBool = Field(default=False, exclude_if=lambda value: not value)


class UpdatePersonalMistake(WireModel):
    note: Annotated[
        str, StringConstraints(strip_whitespace=True, max_length=600, pattern=r"^[^\x00]*$")
    ]


class ConfirmPersonalMistake(UpdatePersonalMistake):
    confirmed: Literal[True]

    @field_validator("confirmed", mode="before")
    @classmethod
    def explicit_confirmation(cls, value: object) -> Literal[True]:
        if value is not True:
            raise ValueError("saving a personal mistake requires explicit confirmation")
        return True


class DiagnosticMistakeEvidence(WireModel):
    request_id: Identifier
    submission_id: Identifier
    problem_version_id: Identifier
    sample_ordinal: Annotated[StrictInt, Field(ge=0, le=65535)]
    finding: UbsanFinding


class PersonalMistake(DiagnosticMistakeEvidence, UpdatePersonalMistake):
    created_at: AwareDatetime
    updated_at: AwareDatetime


class PersonalMistakePage(WireModel):
    items: tuple[PersonalMistake, ...]
    next_before: Identifier | None


class MistakePracticeRequest(WireModel):
    skill_version_id: Identifier
    request_id: Identifier | None = None
    submission_id: Identifier | None = None
    available_minutes: DailyTrainingMinutes | None = None


class MistakePracticeItem(WireModel):
    problem_version_id: Identifier
    title: str
    shared_tags: tuple[Tag, ...]
    estimated_minutes: EstimatedExerciseMinutes | None = None


class MistakePractice(WireModel):
    evidence: DiagnosticMistakeEvidence
    source_title: str | None
    source_available: StrictBool
    focus: str
    items: tuple[MistakePracticeItem, ...]
    gaps: tuple[str, ...]
    available_minutes: DailyTrainingMinutes | None = None
    review_minutes: EstimatedExerciseMinutes | None = None
    verification_minutes: EstimatedExerciseMinutes | None = None


class AgentRunView(WireModel):
    run_id: Identifier
    session_id: Identifier
    skill_version_id: Identifier
    state: RunState
    created_at: AwareDatetime
    result: AgentLearningResult | None = None
    training_request: TrainingSetDraft | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    chat: LearningChatRequest | None = Field(default=None, exclude_if=lambda value: value is None)
    review_question: LearningQuestion | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    periodic_review: PeriodicReviewRequest | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    chat_history: LearningChatHistory = Field(default=(), exclude_if=lambda value: not value)
    partial_answer: PartialChatAnswer | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    partial_reasoning: ChatReasoning | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    failure_code: Label | None = None
    feedback: RunFeedback | None = Field(default=None, exclude_if=lambda value: value is None)
    training_id: Identifier | None = Field(default=None, exclude_if=lambda value: value is None)


class AgentHistoryItem(WireModel):
    run_id: Identifier
    session_id: Identifier
    skill_version_id: Identifier
    purpose: AgentPurpose
    state: RunState
    created_at: AwareDatetime
    conversation_title: Annotated[str, Field(max_length=120)] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )

    training_id: Identifier | None = Field(default=None, exclude_if=lambda value: value is None)
    conversation_id: Identifier | None = Field(default=None, exclude_if=lambda value: value is None)
    has_training_request: StrictBool = Field(default=False, exclude_if=lambda value: not value)


class AgentHistoryPage(WireModel):
    items: tuple[AgentHistoryItem, ...]
    truncated: bool


class ConversationMessage(WireModel):
    run_id: Identifier
    question: LearningQuestion | None
    answer: ModelAnswer | None
    state: RunState
    training_request: TrainingSetDraft | None = Field(
        default=None, exclude_if=lambda value: value is None
    )


class ConversationPage(WireModel):
    messages: tuple[ConversationMessage, ...]
    next_before: Identifier | None


class AgentConversationRun(ConversationMessage):
    session_id: Identifier
    purpose: AgentPurpose
    created_at: AwareDatetime
    training_id: Identifier | None = Field(default=None, exclude_if=lambda value: value is None)
    reasoning: ChatReasoning | None = Field(default=None, exclude_if=lambda value: value is None)
    reasoning_kind: ModelReasoningKind | None = Field(
        default=None, exclude_if=lambda value: value is None
    )


class AgentConversationPage(WireModel):
    conversation_id: Identifier
    runs: tuple[AgentConversationRun, ...]
    next_before: Identifier | None


class AgentTraceStep(WireModel):
    sequence: Counter
    kind: Literal["model", "tool"]
    invocation_version: Label
    state: Literal["running", "succeeded", "failed", "cancelled"]
    error_code: Label | None


class AgentRunTrace(WireModel):
    run_id: Identifier
    attempts: Measurement
    queue_ms: Measurement | None
    duration_ms: Measurement | None
    tool_calls: Measurement
    finished_tool_calls: Measurement
    tool_elapsed_ms: Measurement
    model_elapsed_ms: Measurement | None = Field(
        default_factory=lambda: None, exclude_if=lambda value: value is None
    )
    model_usage: ModelTokenUsage | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    steps: tuple[AgentTraceStep, ...]
    truncated: bool
