"""Agent tool attempts used for the shared database budget."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index
from sqlalchemy.orm import Mapped, mapped_column

from kf_oj.infra.db.base import Base


class AgentToolAttemptRow(Base):
    __tablename__ = "learning_agent_tool_attempts"
    __table_args__ = (
        Index("ix_learning_agent_tool_attempts_owner_started", "owner_id", "started_at"),
        CheckConstraint("expires_at > started_at", name="expiry"),
        CheckConstraint("finished_at >= started_at", name="finish"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    run_id: Mapped[UUID] = mapped_column(ForeignKey("learning_agent_runs.id"), index=True)
    owner_id: Mapped[UUID] = mapped_column(ForeignKey("accounts.id"))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ReviewEvaluationBudgetRow(Base):
    __tablename__ = "learning_review_evaluation_budgets"
    __table_args__ = (CheckConstraint("reads > 0", name="positive_reads"),)

    owner_id: Mapped[UUID] = mapped_column(ForeignKey("accounts.id"), primary_key=True)
    problem_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("problem_versions.id"), primary_key=True
    )
    window_started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    reads: Mapped[int]
