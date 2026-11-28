from datetime import datetime
from uuid import UUID

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, Index, String, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from kf_oj.infra.db.base import Base


class DiagnosticRequestRow(Base):
    __tablename__ = "learning_diagnostic_requests"
    __table_args__ = (
        Index("ix_learning_diagnostic_requests_owner_time", "owner_id", "created_at"),
        Index("ix_learning_diagnostic_requests_state_time", "state", "created_at"),
        Index(
            "ix_learning_diagnostic_observed_at",
            "finished_at",
            postgresql_where=text("state = 'observed'"),
        ),
        Index(
            "uq_learning_diagnostic_requests_running",
            text("(true)"),
            unique=True,
            postgresql_where=text("state IN ('running', 'cancel_pending')"),
        ),
        CheckConstraint("language IN ('c17', 'cpp20')", name="language"),
        CheckConstraint("authorization_version > 0", name="authority"),
        CheckConstraint(
            "session_proof ~ '^[0-9a-f]{64}$' "
            "AND source_digest ~ '^[0-9a-f]{64}$' "
            "AND input_digest ~ '^[0-9a-f]{64}$' "
            "AND expected_digest ~ '^[0-9a-f]{64}$' "
            "AND batch_manifest_digest ~ '^[0-9a-f]{64}$'",
            name="digests",
        ),
        CheckConstraint(
            "source_bytes BETWEEN 1 AND 262144 "
            "AND input_bytes BETWEEN 0 AND 65536 "
            "AND expected_bytes BETWEEN 0 AND 65536",
            name="artifact_sizes",
        ),
        CheckConstraint(
            "(mode IN ('optimization', 'ubsan') AND sample_ordinal IS NOT NULL "
            "AND sample_ordinal >= 0 AND input_digest IS NOT NULL AND input_bytes IS NOT NULL "
            "AND expected_digest IS NOT NULL AND expected_bytes IS NOT NULL "
            "AND batch_manifest_digest IS NULL AND batch_purpose IS NULL) "
            "OR (mode = 'batch' AND sample_ordinal IS NULL "
            "AND input_digest IS NULL AND input_bytes IS NULL "
            "AND expected_digest IS NULL AND expected_bytes IS NULL "
            "AND batch_manifest_digest IS NOT NULL "
            "AND batch_purpose IN ('differential', 'boundary') AND batch_purpose IS NOT NULL)",
            name="input_kind",
        ),
        CheckConstraint(
            "(execution_id IS NULL AND execution_deadline IS NULL) "
            "OR (execution_id IS NOT NULL AND execution_deadline IS NOT NULL)",
            name="execution",
        ),
        CheckConstraint(
            "(state = 'queued' AND execution_id IS NULL AND finished_at IS NULL) "
            "OR (state IN ('running', 'cancel_pending') "
            "AND execution_id IS NOT NULL AND finished_at IS NULL) "
            "OR (state IN ('expired', 'unavailable', 'cancelled') AND finished_at IS NOT NULL) "
            "OR (state IN ('observed', 'failed') "
            "AND execution_id IS NOT NULL AND finished_at IS NOT NULL)",
            name="state",
        ),
        CheckConstraint(
            "((mode = 'optimization' AND ubsan IS NULL AND batch_outcome IS NULL "
            "AND (state = 'observed') = (o2 IS NOT NULL) "
            "AND (o2 IS NOT NULL AND o2 IN ('wrong_answer', 'runtime_error')) = (o0 IS NOT NULL)) "
            "OR (mode = 'ubsan' AND o2 IS NULL AND o0 IS NULL AND batch_outcome IS NULL "
            "AND (state = 'observed') = (ubsan IS NOT NULL) "
            "AND (ubsan IS NULL OR jsonb_typeof(ubsan) = 'object')) "
            "OR (mode = 'batch' AND o2 IS NULL AND o0 IS NULL AND ubsan IS NULL "
            "AND (state = 'observed') = (batch_outcome IS NOT NULL) "
            "AND (batch_outcome IS NULL OR batch_outcome IN "
            "('input_not_verified', 'runtime_error', 'resource_limit') "
            "OR (batch_purpose = 'boundary' AND batch_outcome = 'completed') "
            "OR (batch_purpose = 'differential' AND batch_outcome IN "
            "('reference_pending', 'reduction_verified', 'reduction_unverified', "
            "'accepted', 'compilation_error', 'wrong_answer'))))) "
            "AND (state = 'failed') = (failure_code IS NOT NULL)",
            name="outcome",
        ),
        CheckConstraint("queue_deadline > created_at", name="queue_deadline"),
        CheckConstraint(
            "execution_started_at IS NULL OR execution_id IS NOT NULL", name="execution_timing"
        ),
        CheckConstraint(
            "execution_profile IS NULL OR "
            "(execution_started_at IS NOT NULL AND jsonb_typeof(execution_profile) = 'object')",
            name="execution_profile",
        ),
        CheckConstraint(
            "review_context IS NULL OR jsonb_typeof(review_context) = 'object'",
            name="review_context",
        ),
    )

    owner_id: Mapped[UUID] = mapped_column(ForeignKey("accounts.id"), primary_key=True)
    id: Mapped[UUID] = mapped_column(primary_key=True)
    submission_id: Mapped[UUID] = mapped_column(ForeignKey("submissions.id"))
    problem_version_id: Mapped[UUID] = mapped_column(ForeignKey("problem_versions.id"))
    sample_ordinal: Mapped[int | None]
    language: Mapped[str] = mapped_column(String(5))
    source_digest: Mapped[str] = mapped_column(String(64), ForeignKey("artifacts.digest"))
    source_bytes: Mapped[int]
    input_digest: Mapped[str | None] = mapped_column(String(64))
    input_bytes: Mapped[int | None]
    expected_digest: Mapped[str | None] = mapped_column(String(64))
    expected_bytes: Mapped[int | None]
    batch_manifest_digest: Mapped[str | None] = mapped_column(String(64))
    batch_purpose: Mapped[str | None] = mapped_column(String(12))
    batch_outcome: Mapped[str | None] = mapped_column(String(24))
    authorization_version: Mapped[int] = mapped_column(BigInteger)
    session_proof: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    queue_deadline: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    execution_id: Mapped[UUID | None]
    execution_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    execution_profile: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    review_context: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    execution_deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    mode: Mapped[str] = mapped_column(String(12), server_default="optimization")
    ubsan: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    o2: Mapped[str | None] = mapped_column(String(24))
    o0: Mapped[str | None] = mapped_column(String(24))
    failure_code: Mapped[str | None] = mapped_column(String(32))
