"""Private durable imports and immutable candidates; no grading-state write authority."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Computed,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    SmallInteger,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from kf_oj.infra.db.base import Base
from kf_oj.infra.db.columns import CreatedAt
from kf_oj.modules.problems.models import ProblemVersion


class ImportPool(Base):
    __tablename__ = "problem_import_pool"
    __table_args__ = (
        CheckConstraint("id = 1", name="singleton"),
        CheckConstraint("reserved_bytes BETWEEN 0 AND 268435456", name="capacity"),
    )
    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    reserved_bytes: Mapped[int] = mapped_column(BigInteger, server_default="0")
    worker_ready_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    worker_policy_digest: Mapped[str | None] = mapped_column(String(64))


class ProblemImportOperation(CreatedAt, Base):
    __tablename__ = "problem_import_operations"
    __table_args__ = (
        CheckConstraint(
            "state IN ('uploading','uploaded','validating','ready','failed',"
            "'published','discarded','expired')",
            name="state",
        ),
        CheckConstraint("archive_digest ~ '^[0-9a-f]{64}$'", name="digest"),
        CheckConstraint("archive_bytes BETWEEN 1 AND 67108864", name="bytes"),
        CheckConstraint("authorization_version > 0", name="authority"),
        CheckConstraint("attempts BETWEEN 0 AND 2 AND fence >= 0", name="attempts"),
        CheckConstraint("reserved_bytes IN (0,134217728)", name="reservation"),
        CheckConstraint("expires_at > created_at AND updated_at >= created_at", name="times"),
        Index("ix_problem_import_owner", "owner_id", "id"),
        Index("ix_problem_import_queue", "state", "created_at"),
        Index("ix_problem_import_expiry", "expires_at"),
        Index("ix_problem_import_reservations", "id", postgresql_where=text("reserved_bytes>0")),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True)
    owner_id: Mapped[UUID] = mapped_column(ForeignKey("accounts.id"))
    authorization_version: Mapped[int] = mapped_column(BigInteger)
    archive_digest: Mapped[str] = mapped_column(String(64))
    archive_bytes: Mapped[int] = mapped_column(BigInteger)
    state: Mapped[str] = mapped_column(String(16), server_default="uploading")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, server_default="0")
    fence: Mapped[int] = mapped_column(BigInteger, server_default="0")
    reserved_bytes: Mapped[int] = mapped_column(BigInteger, server_default="134217728")
    failure_code: Mapped[str | None] = mapped_column(String(32))
    candidate_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("problem_candidates.id", use_alter=True, name="fk_problem_import_candidate")
    )
    published_version_id: Mapped[UUID | None] = mapped_column(ForeignKey(ProblemVersion.id))


class ProblemCandidate(CreatedAt, Base):
    __tablename__ = "problem_candidates"
    __table_args__ = (
        UniqueConstraint("operation_id"),
        CheckConstraint("manifest_digest ~ '^[0-9a-f]{64}$'", name="digest"),
        CheckConstraint("case_count BETWEEN 1 AND 256", name="cases"),
        CheckConstraint("octet_length(preview::text) <= 4194304", name="preview"),
        CheckConstraint("octet_length(manifest::text) <= 262144", name="manifest"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True)
    operation_id: Mapped[UUID] = mapped_column(ForeignKey("problem_import_operations.id"))
    manifest_digest: Mapped[str] = mapped_column(String(64))
    manifest: Mapped[dict] = mapped_column(JSONB)
    preview: Mapped[dict] = mapped_column(JSONB)
    case_count: Mapped[int] = mapped_column(Integer)


class CandidateCase(Base):
    __tablename__ = "problem_candidate_cases"
    __table_args__ = (
        CheckConstraint("position BETWEEN 0 AND 255", name="position"),
        CheckConstraint(
            "input_bytes BETWEEN 0 AND 67108864 AND expected_bytes BETWEEN 0 AND 67108864",
            name="bytes",
        ),
        ForeignKeyConstraint(
            ["input_digest", "artifact_state"], ["artifacts.digest", "artifacts.state"]
        ),
        ForeignKeyConstraint(
            ["expected_digest", "artifact_state"],
            ["artifacts.digest", "artifacts.state"],
            name="fk_candidate_cases_expected_artifacts",
        ),
    )
    candidate_id: Mapped[UUID] = mapped_column(
        ForeignKey("problem_candidates.id"), primary_key=True
    )
    position: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String(64))
    input_digest: Mapped[str] = mapped_column(String(64))
    input_bytes: Mapped[int] = mapped_column(BigInteger)
    expected_digest: Mapped[str] = mapped_column(String(64))
    expected_bytes: Mapped[int] = mapped_column(BigInteger)
    artifact_state: Mapped[str] = mapped_column(String(8), Computed("'ready'", persisted=True))
