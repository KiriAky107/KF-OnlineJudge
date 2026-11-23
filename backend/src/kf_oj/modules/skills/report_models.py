"""Reported skill version state and decision history."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from kf_oj.infra.db.base import Base
from kf_oj.infra.db.columns import CreatedAt


class SkillReportRow(CreatedAt, Base):
    __tablename__ = "skills_reports"
    __table_args__ = (
        CheckConstraint(
            "reason IN ('incorrect_content','privacy','licensing','other')", name="reason"
        ),
        CheckConstraint("state IN ('pending','confirmed','dismissed')", name="state"),
        CheckConstraint(
            "(state = 'pending') = (decided_at IS NULL AND reviewer_id IS NULL AND note IS NULL)"
            " AND (state = 'pending' OR "
            "(decided_at IS NOT NULL AND reviewer_id IS NOT NULL AND note IS NOT NULL))",
            name="decision",
        ),
        UniqueConstraint("reporter_id", "request_id", name="uq_skill_report_request"),
        Index(
            "uq_skill_report_pending",
            "reporter_id",
            "version_id",
            unique=True,
            postgresql_where=text("state = 'pending'"),
        ),
        Index("ix_skills_reports_state_date", "state", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    version_id: Mapped[UUID] = mapped_column(ForeignKey("skills_publications.id"))
    reporter_id: Mapped[UUID] = mapped_column(ForeignKey("accounts.id"))
    request_id: Mapped[UUID]
    reason: Mapped[str] = mapped_column(String(24))
    details: Mapped[str] = mapped_column(String(500))
    state: Mapped[str] = mapped_column(String(12), server_default="pending")
    reviewer_id: Mapped[UUID | None] = mapped_column(ForeignKey("accounts.id"))
    note: Mapped[str | None] = mapped_column(String(500))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
