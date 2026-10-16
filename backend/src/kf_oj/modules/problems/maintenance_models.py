"""Append-only actor-bound receipts; replay never re-applies a published operation."""

from uuid import UUID

from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from kf_oj.infra.db.base import Base
from kf_oj.infra.db.columns import CreatedAt


class ProblemMaintenanceOperation(CreatedAt, Base):
    __tablename__ = "problem_maintenance_operations"
    __table_args__ = (
        CheckConstraint("action IN ('publish','disable','restore')", name="action"),
        CheckConstraint("request_digest ~ '^[0-9a-f]{64}$'", name="digest"),
        CheckConstraint("authorization_version > 0", name="authority"),
        CheckConstraint("octet_length(receipt::text) <= 4096", name="receipt"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True)
    owner_id: Mapped[UUID] = mapped_column(ForeignKey("accounts.id"))
    authorization_version: Mapped[int] = mapped_column(BigInteger)
    action: Mapped[str] = mapped_column(String(16))
    request_digest: Mapped[str] = mapped_column(String(64))
    receipt: Mapped[dict] = mapped_column(JSONB)
