"""Mutable learning annotations, separate from immutable judging versions."""

from uuid import UUID

from sqlalchemy import CheckConstraint, ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from kf_oj.infra.db.base import Base


class ProblemLearningMetadata(Base):
    __tablename__ = "problem_learning_metadata"
    __table_args__ = (
        CheckConstraint("revision > 0", name="revision"),
        CheckConstraint(
            "difficulty IS NULL OR difficulty IN ('easy','medium','hard')", name="difficulty"
        ),
        CheckConstraint(
            "jsonb_typeof(tags) = 'array' AND jsonb_array_length(tags) <= 16", name="tags"
        ),
    )
    problem_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("problem_versions.id"), primary_key=True
    )
    tags: Mapped[list[str]] = mapped_column(JSONB)
    difficulty: Mapped[str | None] = mapped_column(String(8))
    revision: Mapped[int] = mapped_column(Integer)
