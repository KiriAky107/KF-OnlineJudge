from typing import Annotated
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from kf_oj.contracts.judging import WireModel


class Standing(WireModel):
    username: Annotated[str, Field(pattern=r"^[a-z0-9_]{3,24}$")]
    rank: Annotated[int, Field(ge=1, le=512)]


class SettleContest(WireModel):
    key: Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,79}$")]
    title: Annotated[str, Field(min_length=1, max_length=120)]
    ended_at: AwareDatetime
    standings: Annotated[tuple[Standing, ...], Field(min_length=2, max_length=512)]

    @model_validator(mode="after")
    def valid_standings(self):
        if not self.title.strip() or len({row.username for row in self.standings}) != len(
            self.standings
        ):
            raise ValueError("contest requires a title and distinct entrants")
        ordered = sorted(row.rank for row in self.standings)
        for place, rank in enumerate(ordered, 1):
            if (place == 1 or rank != ordered[place - 2]) and rank != place:
                raise ValueError("use competition ranks, for example 1, 2, 2, 4")
        return self


class ContestSettlement(WireModel):
    key: str
    title: str
    ended_at: AwareDatetime
    settled_at: AwareDatetime
    participants: int


class RatingChange(WireModel):
    contest_key: str
    title: str
    ended_at: AwareDatetime
    rank: int
    old_rating: int
    new_rating: int


class ContestRating(WireModel):
    rating: int | None
    highest_rating: int | None
    rated_contests: int
    history: tuple[RatingChange, ...]


class ContestSchedule(WireModel):
    key: Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,79}$")]
    title: Annotated[str, Field(min_length=1, max_length=120)]
    starts_at: AwareDatetime
    ends_at: AwareDatetime
    semantic_completion_enabled: bool = True

    @model_validator(mode="after")
    def ordered_window(self):
        if not self.title.strip() or self.starts_at >= self.ends_at:
            raise ValueError("contest end must follow its start")
        return self


class ContestRegistration(WireModel):
    confirmed: bool


class ContestEditorPolicy(WireModel):
    semantic_completion_enabled: bool
    version_ids: Annotated[tuple[UUID, ...], Field(max_length=24)]

    @model_validator(mode="after")
    def distinct_versions(self):
        if len(set(self.version_ids)) != len(self.version_ids):
            raise ValueError("contest problems must be distinct")
        return self


class ContestEditorProblem(WireModel):
    version_id: UUID
    title: str


class ContestAgentAccess(WireModel):
    blocked_until: AwareDatetime | None
    next_start_at: AwareDatetime | None
    checked_at: AwareDatetime
