"""Private, declarative skill authoring documents."""

from typing import Annotated, Self

from pydantic import AwareDatetime, Field, JsonValue, StringConstraints, model_validator

from kf_oj.contracts.agent_api import SkillToolView
from kf_oj.contracts.agent_runtime import AgentPurpose
from kf_oj.contracts.judging import Digest, Identifier, Measurement, Version, WireModel


class SkillReference(WireModel):
    path: Annotated[
        str,
        StringConstraints(pattern=r"^references/[a-z0-9][a-z0-9_-]{0,59}\.(md|txt)$"),
    ]
    content: Annotated[str, StringConstraints(min_length=1, max_length=3000)]
    sha256: Digest | None = None


class SkillDraftDocument(WireModel):
    schema_version: Version = 1
    runtime_version: Version = 1
    purpose: AgentPurpose
    title: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]
    description: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)
    ]
    method: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)]
    license: Annotated[str, StringConstraints(strip_whitespace=True, max_length=80)] | None = None
    attribution: Annotated[str, StringConstraints(strip_whitespace=True, max_length=256)] | None = (
        None
    )
    parameters: dict[str, JsonValue] = Field(default_factory=dict, max_length=16)
    requested_tools: Annotated[tuple[SkillToolView, ...], Field(max_length=8)] = ()
    references: Annotated[tuple[SkillReference, ...], Field(max_length=4)] = Field(
        default_factory=tuple, exclude_if=lambda value: not value
    )

    @model_validator(mode="after")
    def unique_reference_paths(self) -> Self:
        if len({item.path for item in self.references}) != len(self.references):
            raise ValueError("duplicate reference path")
        return self


class CreateSkillDraft(WireModel):
    request_id: Identifier
    source_version_id: Identifier | None = None
    document: SkillDraftDocument | None = None

    @model_validator(mode="after")
    def require_one_source(self) -> Self:
        if (self.source_version_id is None) == (self.document is None):
            raise ValueError("provide either a source version or a declaration")
        return self


class UpdateSkillDraft(WireModel):
    revision: Measurement
    document: SkillDraftDocument


class CreateSkillTrial(WireModel):
    revision: Measurement


class SkillTrialView(WireModel):
    version_id: Identifier
    revision: Measurement
    content_digest: Digest


class SkillDraftSummary(WireModel):
    draft_id: Identifier
    title: str
    purpose: AgentPurpose
    revision: Measurement
    updated_at: AwareDatetime


class SkillDraftView(SkillDraftSummary):
    source_version_id: Identifier | None
    content_digest: Digest
    document: SkillDraftDocument


class SkillDraftPage(WireModel):
    items: tuple[SkillDraftSummary, ...]
    truncated: bool
