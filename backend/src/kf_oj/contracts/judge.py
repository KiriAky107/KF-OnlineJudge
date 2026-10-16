from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, BeforeValidator, ConfigDict

from kf_oj.contracts.judging import integer_version


class JudgeTaskNotification(BaseModel):
    """A wake-up notification, not execution authority or the source of task state."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Annotated[Literal[1], BeforeValidator(integer_version)]
    kind: Literal["judge.evaluation"]
    evaluation_id: UUID
