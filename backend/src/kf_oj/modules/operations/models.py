"""Append-only bounded maintenance events, independent of evaluation state."""

from uuid import UUID

from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, Index, String, text
from sqlalchemy.orm import Mapped, mapped_column

from kf_oj.infra.db.base import Base
from kf_oj.infra.db.columns import CreatedAt


class MaintenanceEvent(CreatedAt, Base):
    __tablename__ = "problem_maintenance_events"
    __table_args__ = (
        CheckConstraint(
            "action IN ('upload_created','upload_received','import_ready',"
            "'import_failed','import_discarded','import_expired','published',"
            "'disabled','restored','authority_bootstrap','authority_grant',"
            "'authority_revoke','authority_enable','authority_disable')",
            name="action",
        ),
        CheckConstraint("authorization_version > 0", name="authority"),
        CheckConstraint(
            "(actor_kind='account' AND actor_id IS NOT NULL AND operator_name IS NULL) "
            "OR (actor_kind='operator' AND actor_id IS NULL AND operator_name IS NOT NULL)",
            name="actor",
        ),
        Index("ix_problem_maintenance_events_page", "created_at", "id"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True)
    actor_id: Mapped[UUID | None] = mapped_column(ForeignKey("accounts.id"))
    actor_kind: Mapped[str] = mapped_column(
        String(8), default="account", server_default=text("'account'")
    )
    operator_name: Mapped[str | None] = mapped_column(String(63))
    authorization_version: Mapped[int] = mapped_column(BigInteger)
    operation_id: Mapped[UUID] = mapped_column()
    action: Mapped[str] = mapped_column(String(24))
    target_id: Mapped[UUID | None] = mapped_column()
