from datetime import datetime
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, String, UniqueConstraint, true
from sqlalchemy.orm import Mapped, mapped_column

from kf_oj.infra.db.base import Base


class RatedContest(Base):
    __tablename__ = "rated_contests"

    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    title: Mapped[str] = mapped_column(String(120))
    ended_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    settled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    settled_by: Mapped[UUID] = mapped_column(ForeignKey("accounts.id"))
    payload_digest: Mapped[str] = mapped_column(String(64))
    participants: Mapped[int]


class ContestRatingChange(Base):
    __tablename__ = "contest_rating_changes"
    __table_args__ = (UniqueConstraint("account_id", "round_number"),)

    contest_key: Mapped[str] = mapped_column(ForeignKey("rated_contests.key"), primary_key=True)
    account_id: Mapped[UUID] = mapped_column(ForeignKey("accounts.id"), primary_key=True)
    rank: Mapped[int]
    round_number: Mapped[int]
    old_rating: Mapped[int]
    new_rating: Mapped[int]
    internal_rating: Mapped[int]


class ScheduledContest(Base):
    __tablename__ = "scheduled_contests"
    __table_args__ = (
        CheckConstraint("starts_at < ends_at", name="scheduled_contest_ordered_window"),
    )

    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    title: Mapped[str] = mapped_column(String(120))
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    semantic_completion_enabled: Mapped[bool] = mapped_column(server_default=true())


class ContestEditorProblem(Base):
    __tablename__ = "contest_editor_problems"
    __table_args__ = (UniqueConstraint("contest_key", "position"),)

    contest_key: Mapped[str] = mapped_column(
        ForeignKey("scheduled_contests.key", ondelete="CASCADE"), primary_key=True
    )
    version_id: Mapped[UUID] = mapped_column(
        ForeignKey("problem_versions.id"), primary_key=True, index=True
    )
    position: Mapped[int]


class ContestEntrant(Base):
    __tablename__ = "contest_entrants"

    account_id: Mapped[UUID] = mapped_column(ForeignKey("accounts.id"), primary_key=True)
    contest_key: Mapped[str] = mapped_column(ForeignKey("scheduled_contests.key"), primary_key=True)
