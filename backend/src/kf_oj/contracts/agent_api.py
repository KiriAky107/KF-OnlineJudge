"""Authenticated learning routes and their public projections."""

from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, JsonValue, StrictBool

from kf_oj.contracts.agent_learning import AgentLearningInput, AgentLearningResult, RunFeedback
from kf_oj.contracts.agent_runtime import AgentPurpose, RunState
from kf_oj.contracts.judging import Counter, Identifier, Label, Measurement, WireModel
from kf_oj.contracts.learning_model import ModelTokenUsage


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


class LearningSessionView(CreateLearningSession):
    session_id: Identifier
    created_at: AwareDatetime


class StartAgentRun(AgentLearningInput):
    request_id: Identifier
    skill_version_id: Identifier


class AgentRunView(WireModel):
    run_id: Identifier
    session_id: Identifier
    skill_version_id: Identifier
    state: RunState
    created_at: AwareDatetime
    result: AgentLearningResult | None = None
    failure_code: Label | None = None
    feedback: RunFeedback | None = Field(default=None, exclude_if=lambda value: value is None)


class AgentHistoryItem(WireModel):
    run_id: Identifier
    session_id: Identifier
    skill_version_id: Identifier
    purpose: AgentPurpose
    state: RunState
    created_at: AwareDatetime


class AgentHistoryPage(WireModel):
    items: tuple[AgentHistoryItem, ...]
    truncated: bool


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
