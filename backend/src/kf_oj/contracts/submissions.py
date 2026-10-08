from datetime import datetime
from typing import Annotated, Literal

from pydantic import Field, model_validator

from kf_oj.contracts.judging import MAX_SOURCE_BYTES, Identifier, Language, WireModel

EvaluationState = Literal[
    "queued", "claimed", "compiling", "running", "cancelling", "finished", "cancelled", "failed"
]


class CreateSubmission(WireModel):
    problem_version_id: Identifier
    language: Language
    idempotency_key: Identifier
    source: Annotated[
        str, Field(strict=True, min_length=1, max_length=MAX_SOURCE_BYTES, repr=False)
    ]

    @model_validator(mode="after")
    def bounded_source(self):
        try:
            if len(self.source.encode("utf-8")) > MAX_SOURCE_BYTES:
                raise ValueError("source exceeds byte policy")
        except UnicodeError:
            raise ValueError("source must be UTF-8") from None
        return self


class SubmissionCreated(WireModel):
    submission_id: Identifier
    evaluation_id: Identifier
    problem_version_id: Identifier
    language: Language
    state: EvaluationState
    created_at: datetime
