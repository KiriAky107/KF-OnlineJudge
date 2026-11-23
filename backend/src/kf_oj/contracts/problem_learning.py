from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, field_validator

from kf_oj.contracts.judging import WireModel

type LearningLabel = Annotated[str, Field(min_length=1, max_length=40)]
type PrerequisiteTags = Annotated[tuple[LearningLabel, ...], Field(max_length=8)]


class LearningMetadataUpdate(WireModel):
    tags: Annotated[tuple[LearningLabel, ...], Field(max_length=16)]
    difficulty: Literal["easy", "medium", "hard"] | None
    prerequisite_tags: PrerequisiteTags | None = None

    @field_validator("tags", "prerequisite_tags")
    @classmethod
    def valid_tags(cls, tags):
        if tags is None:
            return None
        if any(t != t.strip() or any(ord(c) < 32 for c in t) for t in tags) or len(
            set(tags)
        ) != len(tags):
            raise ValueError("tags must be unique nonempty plain labels")
        return tags


class LearningMetadataView(LearningMetadataUpdate):
    prerequisite_tags: PrerequisiteTags = ()
    problem_version_id: UUID
    revision: Annotated[int, Field(ge=0)]
