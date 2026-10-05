from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class JudgeTaskNotification(BaseModel):
    """A wake-up notification, not execution authority or the source of task state."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1]
    kind: Literal["judge.evaluation"]
    evaluation_id: UUID
