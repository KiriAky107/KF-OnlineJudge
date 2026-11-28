from typing import Annotated, Literal
from uuid import UUID

from pydantic import AwareDatetime, StringConstraints

from kf_oj.contracts.judging import WireModel

TeamRole = Literal["captain", "member", "pending"]


class CreateTeam(WireModel):
    idempotency_key: UUID
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=2, max_length=30)]
    description: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)
    ]


class TeamView(WireModel):
    id: UUID
    name: str
    description: str
    member_count: int
    created_at: AwareDatetime


class TeamPage(WireModel):
    items: tuple[TeamView, ...]
    truncated: bool = False


class TeamMembershipView(WireModel):
    role: TeamRole | None


class TeamMemberView(WireModel):
    account_id: UUID
    username: str
    role: TeamRole


class TeamMembersPage(WireModel):
    items: tuple[TeamMemberView, ...]
    next_offset: int | None


class UpdateTeamMember(WireModel):
    decision: Literal["approve", "remove"]
