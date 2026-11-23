"""Publication can change availability while its reviewed document stays fixed."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    SmallInteger,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from kf_oj.infra.db.base import Base
from kf_oj.infra.db.columns import CreatedAt


class SkillPublicationRow(CreatedAt, Base):
    __tablename__ = "skills_publications"
    __table_args__ = (
        CheckConstraint("draft_revision >= 0 AND version > 0", name="version"),
        CheckConstraint("jsonb_typeof(document) = 'object'", name="document"),
        CheckConstraint("content_digest ~ '^[0-9a-f]{64}$'", name="digest"),
        CheckConstraint("state IN ('pending','published','rejected','withdrawn')", name="state"),
        CheckConstraint("(state = 'pending') = (decided_at IS NULL)", name="decision"),
        CheckConstraint(
            "(state IN ('published','rejected')) = (reviewer_id IS NOT NULL)", name="review"
        ),
        UniqueConstraint("draft_id", "draft_revision", name="uq_skill_review_draft_revision"),
        UniqueConstraint("draft_id", "version", name="uq_skill_publication_version"),
        Index("ix_skills_publications_state_date", "state", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    draft_id: Mapped[UUID] = mapped_column(ForeignKey("skills_drafts.id"))
    owner_id: Mapped[UUID] = mapped_column(ForeignKey("accounts.id"), index=True)
    author_name: Mapped[str] = mapped_column(String(24))
    draft_revision: Mapped[int] = mapped_column(BigInteger)
    version: Mapped[int] = mapped_column(BigInteger)
    based_on_version_id: Mapped[UUID | None]
    content_digest: Mapped[str] = mapped_column(String(64))
    document: Mapped[dict] = mapped_column(JSONB)
    state: Mapped[str] = mapped_column(String(12), server_default="pending")
    reviewer_id: Mapped[UUID | None] = mapped_column(ForeignKey("accounts.id"))
    review_note: Mapped[str | None] = mapped_column(String(500))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SkillFavoriteRow(CreatedAt, Base):
    __tablename__ = "skills_favorites"

    owner_id: Mapped[UUID] = mapped_column(ForeignKey("accounts.id"), primary_key=True)
    skill_id: Mapped[UUID] = mapped_column(ForeignKey("skills_drafts.id"), primary_key=True)


class SkillRatingRow(Base):
    __tablename__ = "skills_ratings"
    __table_args__ = (CheckConstraint("score BETWEEN 1 AND 5", name="score"),)

    version_id: Mapped[UUID] = mapped_column(ForeignKey("skills_publications.id"), primary_key=True)
    owner_id: Mapped[UUID] = mapped_column(ForeignKey("accounts.id"), primary_key=True)
    score: Mapped[int] = mapped_column(SmallInteger)


class SkillVersionHoldRow(CreatedAt, Base):
    __tablename__ = "skills_version_holds"

    version_id: Mapped[UUID] = mapped_column(ForeignKey("skills_publications.id"), primary_key=True)
    operator_id: Mapped[UUID] = mapped_column(ForeignKey("accounts.id"))
    reason: Mapped[str] = mapped_column(String(500))


class SkillVersionArchiveRow(CreatedAt, Base):
    __tablename__ = "skills_version_archives"

    version_id: Mapped[UUID] = mapped_column(ForeignKey("skills_publications.id"), primary_key=True)
