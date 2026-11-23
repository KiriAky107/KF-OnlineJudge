"""Internal learning runtime records shared by its workflow and persistence ports."""

from enum import StrEnum
from typing import Literal, Self

from pydantic import AwareDatetime, model_validator

from kf_oj.contracts.judging import (
    Counter,
    Digest,
    Identifier,
    Label,
    Measurement,
    Version,
    WireModel,
)


class AgentPurpose(StrEnum):
    SUBMISSION_REVIEW = "submission_review"
    GUIDED_HELP = "guided_help"
    FOCUSED_TRAINING = "focused_training"
    PERIODIC_REVIEW = "periodic_review"


class RunState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    RETRY_WAIT = "retry_wait"
    CANCEL_REQUESTED = "cancel_requested"
    CANCELLED = "cancelled"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class AgentSession(WireModel):
    schema_version: Version = 1
    session_id: Identifier
    owner_id: Identifier
    purpose: AgentPurpose
    created_at: AwareDatetime


class AgentRun(WireModel):
    schema_version: Version = 1
    run_id: Identifier
    session_id: Identifier
    request_id: Identifier
    skill_version_id: Identifier
    configuration_digest: Digest
    input_digest: Digest
    state: RunState = RunState.QUEUED
    revision: Measurement = 0
    created_at: AwareDatetime


class AgentStep(WireModel):
    schema_version: Version = 1
    step_id: Identifier
    run_id: Identifier
    sequence: Counter
    kind: Literal["model", "tool"]
    invocation_version: Label
    arguments_digest: Digest
    state: Literal["running", "succeeded", "failed", "cancelled"]
    result_digest: Digest | None = None
    error_code: Label | None = None

    @model_validator(mode="after")
    def outcome_matches_state(self) -> Self:
        if (self.result_digest is not None) != (self.state == "succeeded"):
            raise ValueError("only a successful step has a result")
        if (self.error_code is not None) != (self.state == "failed"):
            raise ValueError("only a failed step has an error code")
        return self
