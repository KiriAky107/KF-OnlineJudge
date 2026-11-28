from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    String,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from kf_oj.infra.db.base import Base


class PersonalMistakeRow(Base):
    __tablename__ = "learning_personal_mistakes"
    __table_args__ = (
        ForeignKeyConstraint(
            ("owner_id", "request_id"),
            ("learning_diagnostic_requests.owner_id", "learning_diagnostic_requests.id"),
        ),
        Index(
            "ix_learning_personal_mistakes_owner_created", "owner_id", "created_at", "request_id"
        ),
        CheckConstraint(
            "category IN ('signed_integer_overflow', 'division_by_zero', "
            "'index_out_of_bounds', 'invalid_shift')",
            name="category",
        ),
        CheckConstraint(
            "sample_ordinal BETWEEN 0 AND 65535 AND source_line BETWEEN 1 AND 262144 "
            "AND source_column BETWEEN 1 AND 262144",
            name="location",
        ),
        CheckConstraint("updated_at >= created_at", name="timestamps"),
    )

    owner_id: Mapped[UUID] = mapped_column(primary_key=True)
    request_id: Mapped[UUID] = mapped_column(primary_key=True)
    submission_id: Mapped[UUID]
    problem_version_id: Mapped[UUID]
    sample_ordinal: Mapped[int]
    category: Mapped[str] = mapped_column(String(32))
    source_line: Mapped[int]
    source_column: Mapped[int]
    note: Mapped[str] = mapped_column(String(600))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
