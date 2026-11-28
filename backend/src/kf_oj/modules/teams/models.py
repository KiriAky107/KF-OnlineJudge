from datetime import datetime
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, text
from sqlalchemy.orm import Mapped, mapped_column

from kf_oj.infra.db.base import Base
from kf_oj.infra.db.columns import CreatedAt


class Team(CreatedAt, Base):
    __tablename__ = "teams"
    __table_args__ = (Index("ix_teams_created_at", "created_at", "id"),)

    id: Mapped[UUID] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(30))
    description: Mapped[str] = mapped_column(String(500))


class TeamMembership(CreatedAt, Base):
    __tablename__ = "team_memberships"
    __table_args__ = (
        CheckConstraint("role IN ('captain', 'member', 'pending')", name="role"),
        CheckConstraint("(role = 'pending') = (approved_at IS NULL)", name="approval_matches_role"),
        Index("ix_team_memberships_role_time", "team_id", "role", "created_at", "account_id"),
        Index("ix_team_memberships_account", "account_id", "team_id"),
        Index(
            "uq_team_memberships_captain",
            "team_id",
            unique=True,
            postgresql_where=text("role = 'captain'"),
        ),
    )

    team_id: Mapped[UUID] = mapped_column(ForeignKey("teams.id"), primary_key=True)
    account_id: Mapped[UUID] = mapped_column(ForeignKey("accounts.id"), primary_key=True)
    role: Mapped[str] = mapped_column(String(8))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
