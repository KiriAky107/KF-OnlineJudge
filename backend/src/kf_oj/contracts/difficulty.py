from typing import Annotated, Literal

from pydantic import Field, StringConstraints

from kf_oj.contracts.judging import WireModel

type DifficultyKey = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,31}$")]
type DifficultyTier = Literal["easy", "medium", "hard"]
type DifficultyScore = Annotated[int, Field(ge=1, le=10000, strict=True)]


class DifficultySettings(WireModel):
    label: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=24)]
    score: Annotated[int, Field(ge=1, le=10000)]
    color: Annotated[str, Field(pattern=r"^#[0-9a-fA-F]{6}$")]
    tier: DifficultyTier


class DifficultyLevel(DifficultySettings):
    key: DifficultyKey
