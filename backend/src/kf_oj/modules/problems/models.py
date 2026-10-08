"""Immutable published versions, ordered cases, and mutable public availability."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Computed,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from kf_oj.infra.db.base import Base
from kf_oj.infra.db.columns import CreatedAt


class Problem(CreatedAt, Base):
    __tablename__ = "problems"
    __table_args__ = (
        CheckConstraint("slug ~ '^[a-z0-9][a-z0-9_-]{0,63}$'", name="slug"),
        ForeignKeyConstraint(
            ["active_version_id", "id"],
            ["problem_versions.id", "problem_versions.problem_id"],
            name="fk_problems_active_version",
            use_alter=True,
            deferrable=True,
            initially="DEFERRED",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(64), unique=True)
    active_version_id: Mapped[UUID | None]
    enabled: Mapped[bool] = mapped_column(Boolean, server_default="true")


class ProblemVersion(CreatedAt, Base):
    __tablename__ = "problem_versions"
    __table_args__ = (
        UniqueConstraint("id", "problem_id"),
        UniqueConstraint("problem_id", "revision"),
        CheckConstraint("revision > 0", name="revision"),
        CheckConstraint("octet_length(statement) BETWEEN 1 AND 262144", name="statement_bytes"),
        CheckConstraint("char_length(title) BETWEEN 1 AND 160", name="title_size"),
        CheckConstraint("comparison = 'exact_bytes'", name="comparison"),
        CheckConstraint("job_wall_time_us BETWEEN 1000 AND 3600000000", name="job_wall"),
        Index("ix_problem_versions_public", "problem_id", "published_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    problem_id: Mapped[UUID] = mapped_column(ForeignKey("problems.id"))
    revision: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(160))
    statement: Mapped[str] = mapped_column(Text)
    languages: Mapped[list[str]] = mapped_column(JSONB)
    toolchain: Mapped[dict] = mapped_column(JSONB)
    compile_limits: Mapped[dict] = mapped_column(JSONB)
    run_limits: Mapped[dict] = mapped_column(JSONB)
    job_wall_time_us: Mapped[int] = mapped_column(BigInteger)
    comparison: Mapped[str] = mapped_column(String(16), server_default="exact_bytes")
    public_samples: Mapped[list[dict]] = mapped_column(JSONB)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ProblemCase(Base):
    __tablename__ = "problem_cases"
    __table_args__ = (
        UniqueConstraint("id", "version_id"),
        UniqueConstraint("version_id", "position"),
        CheckConstraint("position BETWEEN 0 AND 255", name="position"),
        ForeignKeyConstraint(
            ["input_digest", "artifact_state"], ["artifacts.digest", "artifacts.state"]
        ),
        ForeignKeyConstraint(
            ["expected_digest", "artifact_state"],
            ["artifacts.digest", "artifacts.state"],
            name="fk_problem_cases_expected_digest_artifacts",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    version_id: Mapped[UUID] = mapped_column(ForeignKey("problem_versions.id"))
    position: Mapped[int]
    input_digest: Mapped[str] = mapped_column(String(64))
    expected_digest: Mapped[str] = mapped_column(String(64))
    artifact_state: Mapped[str] = mapped_column(String(8), Computed("'ready'", persisted=True))
