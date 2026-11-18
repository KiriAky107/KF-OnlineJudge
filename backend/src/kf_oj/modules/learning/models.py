"""Personal training snapshots; progress derives from real submissions."""

from uuid import UUID

from sqlalchemy import ForeignKey, Integer, String, Text, UniqueConstraint
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
