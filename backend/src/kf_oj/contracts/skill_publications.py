"""Version publication requests, public discovery, and review projections."""

from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, StrictBool, StringConstraints

from kf_oj.contracts.judging import Counter, Digest, Identifier, Measurement, WireModel
from kf_oj.contracts.skill_drafts import SkillDraftDocument

PublicationState = Literal["pending", "published", "archived", "rejected", "withdrawn", "disabled"]
SkillScore = Annotated[int, Field(strict=True, ge=1, le=5)]


class SubmitSkillVersion(WireModel):
    revision: Measurement
    confirmed_public: StrictBool


class ReviewSkillVersion(WireModel):
    action: Literal["publish", "reject"]
    note: Annotated[str, StringConstraints(strip_whitespace=True, max_length=500)] = ""


class DisableSkillVersion(WireModel):
    reason: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]


class PublishedSkill(WireModel):
    skill_id: Identifier
    version_id: Identifier
    version: Counter
    based_on_version_id: Identifier | None
    author: str
    content_digest: Digest
    document: SkillDraftDocument
    published_at: AwareDatetime
    favorited: bool


class PublishedSkillPage(WireModel):
    items: Annotated[tuple[PublishedSkill, ...], Field(max_length=50)]
    truncated: bool


class ChangeSkillFavorite(WireModel):
    favorited: StrictBool


class ChangeSkillRating(WireModel):
    score: SkillScore


class SkillVersionRating(WireModel):
    version_id: Identifier
    count: Measurement
    average: Annotated[float, Field(ge=1, le=5)] | None
    own_score: SkillScore | None


class SkillVersionReview(WireModel):
    draft_id: Identifier
    draft_revision: Measurement
    version_id: Identifier
    version: Counter
    based_on_version_id: Identifier | None
    author: str
    content_digest: Digest
    document: SkillDraftDocument
    state: PublicationState
    submitted_at: AwareDatetime
    decided_at: AwareDatetime | None
    review_note: str | None
    archived_at: AwareDatetime | None = Field(default=None, exclude_if=lambda value: value is None)
    disabled_at: AwareDatetime | None = None
    disable_reason: str | None = None


class SkillVersionReviewPage(WireModel):
    items: Annotated[tuple[SkillVersionReview, ...], Field(max_length=50)]
    truncated: bool
