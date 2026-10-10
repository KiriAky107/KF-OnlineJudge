"""Platform creates initial rows; Coordinator exclusively advances execution."""

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
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from kf_oj.infra.db.base import Base
from kf_oj.infra.db.columns import CreatedAt


class Submission(CreatedAt, Base):
    __tablename__ = "submissions"
    __table_args__ = (
        UniqueConstraint("account_id", "idempotency_key"),
        UniqueConstraint("id", "problem_version_id"),
        CheckConstraint("language IN ('c17','cpp20','python313','java21')", name="language"),
        CheckConstraint("request_digest ~ '^[0-9a-f]{64}$'", name="request_digest"),
        ForeignKeyConstraint(
            ["problem_version_id", "problem_id"],
            ["problem_versions.id", "problem_versions.problem_id"],
        ),
        ForeignKeyConstraint(
            ["source_digest", "artifact_state"], ["artifacts.digest", "artifacts.state"]
        ),
        Index("ix_submissions_account_history", "account_id", "created_at", "id"),
        Index(
            "ix_submissions_owned_problem_history",
            "account_id",
            "problem_version_id",
            text("created_at DESC"),
            text("id DESC"),
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    account_id: Mapped[UUID] = mapped_column(ForeignKey("accounts.id"))
    problem_id: Mapped[UUID]
    problem_version_id: Mapped[UUID]
    language: Mapped[str] = mapped_column(String(16))
    source_digest: Mapped[str] = mapped_column(String(64))
    artifact_state: Mapped[str] = mapped_column(String(8), Computed("'ready'", persisted=True))
    idempotency_key: Mapped[UUID]
    request_digest: Mapped[str] = mapped_column(String(64))


class Evaluation(CreatedAt, Base):
    __tablename__ = "evaluations"
    __table_args__ = (
        UniqueConstraint("id", "problem_version_id"),
        ForeignKeyConstraint(
            ["submission_id", "problem_version_id"],
            ["submissions.id", "submissions.problem_version_id"],
        ),
        ForeignKeyConstraint(
            ["current_attempt_id", "id"],
            ["judge_attempts.id", "judge_attempts.evaluation_id"],
            name="fk_evaluations_current_attempt",
            use_alter=True,
            deferrable=True,
            initially="DEFERRED",
        ),
        CheckConstraint(
            "state IN ('queued','claimed','compiling','running','cancelling',"
            "'finished','cancelled','failed')",
            name="state",
        ),
        CheckConstraint("fencing_version >= 0", name="fence"),
        CheckConstraint("max_attempts BETWEEN 1 AND 5", name="max_attempts"),
        CheckConstraint("attempts_count BETWEEN 0 AND max_attempts", name="attempts_count"),
        CheckConstraint(
            "state NOT IN ('claimed','compiling','running','cancelling') "
            "OR current_attempt_id IS NOT NULL",
            name="active_attempt",
        ),
        Index("ix_evaluations_recovery", "state", "not_before", "id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    submission_id: Mapped[UUID] = mapped_column(unique=True)
    problem_version_id: Mapped[UUID]
    state: Mapped[str] = mapped_column(String(16), server_default="queued")
    fencing_version: Mapped[int] = mapped_column(BigInteger, server_default="0")
    attempts_count: Mapped[int] = mapped_column(server_default="0")
    max_attempts: Mapped[int] = mapped_column(server_default="3")
    current_attempt_id: Mapped[UUID | None]
    not_before: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    cancel_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    verdict: Mapped[str | None] = mapped_column(String(32))
    failure_code: Mapped[str | None] = mapped_column(String(32))
    final_report: Mapped[dict | None] = mapped_column(JSONB)
    final_report_digest: Mapped[str | None] = mapped_column(String(64))


class JudgeAttempt(CreatedAt, Base):
    __tablename__ = "judge_attempts"
    __table_args__ = (
        UniqueConstraint("id", "evaluation_id"),
        UniqueConstraint("evaluation_id", "number", name="uq_judge_attempts_evaluation_number"),
        UniqueConstraint(
            "evaluation_id", "fencing_version", name="uq_judge_attempts_evaluation_fence"
        ),
        CheckConstraint("number BETWEEN 1 AND 5", name="number"),
        CheckConstraint("fencing_version > 0", name="fence"),
        CheckConstraint("lease_token_hash ~ '^[0-9a-f]{64}$'", name="token_digest"),
        CheckConstraint("manifest_digest ~ '^[0-9a-f]{64}$'", name="manifest_digest"),
        CheckConstraint(
            "measurement_epoch IS NULL OR measurement_epoch ~ '^[0-9a-f]{64}$'",
            name="measurement_epoch",
        ),
        CheckConstraint("sequence >= 0", name="sequence"),
        Index("ix_judge_attempts_expiry", "expires_at", "evaluation_id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    evaluation_id: Mapped[UUID] = mapped_column(ForeignKey("evaluations.id"))
    worker_id: Mapped[UUID]
    number: Mapped[int]
    fencing_version: Mapped[int] = mapped_column(BigInteger)
    lease_token_hash: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    manifest: Mapped[dict] = mapped_column(JSONB)
    manifest_digest: Mapped[str] = mapped_column(String(64))
    sequence: Mapped[int] = mapped_column(BigInteger, server_default="0")
    stage: Mapped[str | None] = mapped_column(String(16))
    case_index: Mapped[int | None]
    report: Mapped[dict | None] = mapped_column(JSONB)
    report_digest: Mapped[str | None] = mapped_column(String(64))
    cleanup_complete: Mapped[bool | None] = mapped_column(Boolean)
    measurement_epoch: Mapped[str | None] = mapped_column(String(64))


class EvaluationPerformance(Base):
    """Coordinator-only immutable final projection; the platform only reads it."""

    __tablename__ = "evaluation_performance"
    __table_args__ = (
        ForeignKeyConstraint(
            ["evaluation_id", "problem_version_id"],
            ["evaluations.id", "evaluations.problem_version_id"],
        ),
        ForeignKeyConstraint(
            ["submission_id", "problem_version_id"],
            ["submissions.id", "submissions.problem_version_id"],
        ),
        ForeignKeyConstraint(
            ["attempt_id", "evaluation_id"], ["judge_attempts.id", "judge_attempts.evaluation_id"]
        ),
        CheckConstraint("language IN ('c17','cpp20','python313','java21')", name="language"),
        CheckConstraint("comparison_group ~ '^[0-9a-f]{64}$'", name="comparison_group"),
        CheckConstraint("measurement_epoch ~ '^[0-9a-f]{64}$'", name="measurement_epoch"),
        CheckConstraint("report_digest ~ '^[0-9a-f]{64}$'", name="report_digest"),
        CheckConstraint("cpu_time_us IS NULL OR cpu_time_us >= 0", name="cpu"),
        CheckConstraint("memory_peak_bytes IS NULL OR memory_peak_bytes >= 0", name="memory"),
        Index(
            "ix_performance_group_watermark",
            "comparison_group",
            text("finished_at DESC"),
            text("evaluation_id DESC"),
        ),
        Index(
            "ix_performance_cpu_time_us_cohort",
            "comparison_group",
            text("finished_at DESC"),
            text("evaluation_id DESC"),
            postgresql_where=text("cpu_time_us IS NOT NULL"),
        ),
        Index(
            "ix_performance_memory_peak_bytes_cohort",
            "comparison_group",
            text("finished_at DESC"),
            text("evaluation_id DESC"),
            postgresql_where=text("memory_peak_bytes IS NOT NULL"),
        ),
    )

    evaluation_id: Mapped[UUID] = mapped_column(primary_key=True)
    submission_id: Mapped[UUID] = mapped_column(unique=True)
    attempt_id: Mapped[UUID] = mapped_column(unique=True)
    problem_version_id: Mapped[UUID]
    language: Mapped[str] = mapped_column(String(16))
    comparison_group: Mapped[str] = mapped_column(String(64))
    measurement_epoch: Mapped[str] = mapped_column(String(64))
    report_digest: Mapped[str] = mapped_column(String(64))
    finished_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    cpu_time_us: Mapped[int | None] = mapped_column(BigInteger)
    memory_peak_bytes: Mapped[int | None] = mapped_column(BigInteger)


class CaseResult(Base):
    __tablename__ = "case_results"
    __table_args__ = (
        UniqueConstraint("attempt_id", "position"),
        ForeignKeyConstraint(
            ["case_id", "problem_version_id"], ["problem_cases.id", "problem_cases.version_id"]
        ),
        ForeignKeyConstraint(
            ["evaluation_id", "problem_version_id"],
            ["evaluations.id", "evaluations.problem_version_id"],
        ),
        ForeignKeyConstraint(
            ["attempt_id", "evaluation_id"], ["judge_attempts.id", "judge_attempts.evaluation_id"]
        ),
        CheckConstraint("position BETWEEN 0 AND 255", name="position"),
        CheckConstraint("cpu_time_us IS NULL OR cpu_time_us >= 0", name="cpu"),
        CheckConstraint("memory_peak_bytes IS NULL OR memory_peak_bytes >= 0", name="memory"),
    )

    attempt_id: Mapped[UUID] = mapped_column(primary_key=True)
    case_id: Mapped[UUID] = mapped_column(primary_key=True)
    evaluation_id: Mapped[UUID]
    problem_version_id: Mapped[UUID]
    position: Mapped[int]
    verdict: Mapped[str] = mapped_column(String(32))
    cpu_time_us: Mapped[int | None] = mapped_column(BigInteger)
    memory_peak_bytes: Mapped[int | None] = mapped_column(BigInteger)
