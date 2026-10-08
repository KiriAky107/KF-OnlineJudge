"""Transactional notifications; contains IDs, never source or hidden artifacts."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, func
from sqlalchemy.orm import Mapped, mapped_column

from kf_oj.infra.db.base import Base
from kf_oj.infra.db.columns import CreatedAt


class Outbox(CreatedAt, Base):
    __tablename__ = "outbox"
    __table_args__ = (
        CheckConstraint("failures BETWEEN 0 AND 100", name="failure_budget"),
        CheckConstraint("kind = 'judge.evaluation'", name="kind"),
        Index("ix_outbox_delivery", "delivered_at", "next_attempt_at", "id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    evaluation_id: Mapped[UUID] = mapped_column(ForeignKey("evaluations.id"), unique=True)
    kind: Mapped[str] = mapped_column(String(32), server_default="judge.evaluation")
    failures: Mapped[int] = mapped_column(server_default="0")
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lock_owner: Mapped[UUID | None]
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(32))
