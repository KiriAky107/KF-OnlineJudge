"""Declarations for the first-party learning skills."""

from typing import Annotated, Literal, Self

from pydantic import Field, TypeAdapter, model_validator

from kf_oj.contracts.agent_learning import (
    FocusedTrainingParameters,
    GuidedHelpParameters,
    PeriodicReviewParameters,
    SubmissionReviewParameters,
)
from kf_oj.contracts.agent_runtime import AgentPurpose
from kf_oj.contracts.judging import Label, Version, WireModel


class RequestedTool(WireModel):
    name: Label
    version: Annotated[int, Field(ge=1, le=65535)]


class SkillDeclaration(WireModel):
    schema_version: Version
    runtime_version: Version
    slug: Label
    release: Annotated[int, Field(ge=1, le=65535)]
    author: Literal["KF-OnlineJudge"]
    license: Literal["MIT"]
    title: Annotated[str, Field(min_length=1, max_length=80)]
    description: Annotated[str, Field(min_length=1, max_length=500)]
    method: Annotated[str, Field(min_length=1, max_length=4000)]
    requested_tools: Annotated[tuple[RequestedTool, ...], Field(max_length=8)]

    @model_validator(mode="after")
    def distinct_tools(self) -> Self:
        keys = {(tool.name, tool.version) for tool in self.requested_tools}
        if len(keys) != len(self.requested_tools):
            raise ValueError("duplicate skill tool declaration")
        return self


class SubmissionReview(SkillDeclaration):
    purpose: Literal[AgentPurpose.SUBMISSION_REVIEW]
    parameters: SubmissionReviewParameters


class GuidedHelp(SkillDeclaration):
    purpose: Literal[AgentPurpose.GUIDED_HELP]
    parameters: GuidedHelpParameters


class FocusedTraining(SkillDeclaration):
    purpose: Literal[AgentPurpose.FOCUSED_TRAINING]
    parameters: FocusedTrainingParameters


class PeriodicReview(SkillDeclaration):
    purpose: Literal[AgentPurpose.PERIODIC_REVIEW]
    parameters: PeriodicReviewParameters


BuiltinDeclaration = Annotated[
    SubmissionReview | GuidedHelp | FocusedTraining | PeriodicReview,
    Field(discriminator="purpose"),
]

BUILTIN_DECLARATION = TypeAdapter(BuiltinDeclaration)

PARAMETERS_BY_PURPOSE = {
    AgentPurpose.SUBMISSION_REVIEW: SubmissionReviewParameters,
    AgentPurpose.GUIDED_HELP: GuidedHelpParameters,
    AgentPurpose.FOCUSED_TRAINING: FocusedTrainingParameters,
    AgentPurpose.PERIODIC_REVIEW: PeriodicReviewParameters,
}
