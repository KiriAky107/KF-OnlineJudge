"""Authored declarations are private and separate from runtime bindings."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from kf_oj.infra.db.base import Base


class SkillDraftRow(Base):
    __tablename__ = "skills_drafts"
    __table_args__ = (
        CheckConstraint("revision >= 0", name="revision"),
        CheckConstraint("jsonb_typeof(document) = 'object'", name="document"),
        CheckConstraint("content_digest ~ '^[0-9a-f]{64}$'", name="digest"),
        CheckConstraint("request_digest ~ '^[0-9a-f]{64}$'", name="request"),
        UniqueConstraint("owner_id", "request_id", name="uq_skills_draft_request"),
        Index("ix_skills_drafts_owner_updated", "owner_id", "updated_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    owner_id: Mapped[UUID] = mapped_column(ForeignKey("accounts.id"))
    request_id: Mapped[UUID]
    request_digest: Mapped[str] = mapped_column(String(64))
    source_version_id: Mapped[UUID | None]
    document: Mapped[dict] = mapped_column(JSONB)
    content_digest: Mapped[str] = mapped_column(String(64))
    revision: Mapped[int] = mapped_column(BigInteger, server_default="0")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SkillTrialRow(Base):
    __tablename__ = "skills_trials"
    __table_args__ = (
        CheckConstraint("revision >= 0", name="revision"),
        CheckConstraint("jsonb_typeof(declaration) = 'object'", name="declaration"),
        CheckConstraint("content_digest ~ '^[0-9a-f]{64}$'", name="digest"),
        UniqueConstraint("draft_id", "revision", name="uq_skills_trial_revision"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    draft_id: Mapped[UUID] = mapped_column(ForeignKey("skills_drafts.id"))
    revision: Mapped[int] = mapped_column(BigInteger)
    content_digest: Mapped[str] = mapped_column(String(64))
    declaration: Mapped[dict] = mapped_column(JSONB)
