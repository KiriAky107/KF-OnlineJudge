"""Private skill preferences, grants and pinned runtime configurations."""

from uuid import UUID

from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from kf_oj.infra.db.base import Base
from kf_oj.infra.db.columns import CreatedAt


class SkillBindingRow(Base):
    __tablename__ = "skills_bindings"
    __table_args__ = (
        CheckConstraint("authorization_epoch > 0", name="authorization_epoch"),
        CheckConstraint("jsonb_typeof(preferences) = 'object'", name="preferences"),
        CheckConstraint("jsonb_typeof(authorized_tools) = 'array'", name="authorized_tools"),
    )

    owner_id: Mapped[UUID] = mapped_column(ForeignKey("accounts.id"), primary_key=True)
    slug: Mapped[str] = mapped_column(String(64), primary_key=True)
    version_id: Mapped[UUID]
    preferences: Mapped[dict] = mapped_column(JSONB)
    authorized_tools: Mapped[list] = mapped_column(JSONB)
    enabled: Mapped[bool]
    authorization_epoch: Mapped[int] = mapped_column(BigInteger)


class SkillConfigurationRow(CreatedAt, Base):
    __tablename__ = "skills_run_configurations"
    __table_args__ = (
        CheckConstraint("digest ~ '^[0-9a-f]{64}$'", name="digest"),
        CheckConstraint("authorization_epoch > 0", name="authorization_epoch"),
        CheckConstraint("jsonb_typeof(preferences) = 'object'", name="preferences"),
        CheckConstraint("jsonb_typeof(authorized_tools) = 'array'", name="authorized_tools"),
    )

    owner_id: Mapped[UUID] = mapped_column(ForeignKey("accounts.id"), primary_key=True)
    version_id: Mapped[UUID] = mapped_column(primary_key=True)
    digest: Mapped[str] = mapped_column(String(64), primary_key=True)
    preferences: Mapped[dict] = mapped_column(JSONB)
    authorized_tools: Mapped[list] = mapped_column(JSONB)
    authorization_epoch: Mapped[int] = mapped_column(BigInteger)
