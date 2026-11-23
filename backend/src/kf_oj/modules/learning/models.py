"""Personal training snapshots; progress derives from real submissions."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from kf_oj.infra.db.base import Base
from kf_oj.infra.db.columns import CreatedAt


class LearningPlan(CreatedAt, Base):
    __tablename__ = "learning_plans"
    __table_args__ = (UniqueConstraint("owner_id", "idempotency_key"),)
    id: Mapped[UUID] = mapped_column(primary_key=True)
    owner_id: Mapped[UUID] = mapped_column(ForeignKey("accounts.id"), index=True)
    idempotency_key: Mapped[UUID]
    request_digest: Mapped[str] = mapped_column(String(64))
    title: Mapped[str] = mapped_column(String(120))
    rule_version: Mapped[str] = mapped_column(String(32))
    parameters: Mapped[dict] = mapped_column(JSONB)


class LearningPlanItem(Base):
    __tablename__ = "learning_plan_items"
    __table_args__ = (
        UniqueConstraint("plan_id", "position", name="uq_learning_items_position"),
        UniqueConstraint("plan_id", "problem_id", name="uq_learning_items_problem"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True)
    plan_id: Mapped[UUID] = mapped_column(ForeignKey("learning_plans.id"), index=True)
    position: Mapped[int] = mapped_column(Integer)
    problem_id: Mapped[UUID] = mapped_column(ForeignKey("problems.id"))
    version_id: Mapped[UUID] = mapped_column(ForeignKey("problem_versions.id"))
    snapshot: Mapped[dict] = mapped_column(JSONB)
    note: Mapped[str] = mapped_column(Text, default="", server_default="")


class TrainingFeedbackRow(Base):
    __tablename__ = "learning_training_feedback"
    __table_args__ = (
        CheckConstraint("rating IN ('too_easy','appropriate','too_hard')", name="rating"),
        CheckConstraint("difficulty IN ('easy','medium','hard')", name="difficulty"),
        CheckConstraint("jsonb_typeof(tags) = 'array'", name="tags"),
        Index("ix_learning_training_feedback_owner_recent", "owner_id", "rated_at"),
    )

    item_id: Mapped[UUID] = mapped_column(ForeignKey("learning_plan_items.id"), primary_key=True)
    owner_id: Mapped[UUID] = mapped_column(ForeignKey("accounts.id"))
    plan_id: Mapped[UUID] = mapped_column(ForeignKey("learning_plans.id"))
    version_id: Mapped[UUID] = mapped_column(ForeignKey("problem_versions.id"))
    difficulty: Mapped[str | None] = mapped_column(String(6))
    tags: Mapped[list[str]] = mapped_column(JSONB)
    rating: Mapped[str] = mapped_column(String(12))
    rated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
