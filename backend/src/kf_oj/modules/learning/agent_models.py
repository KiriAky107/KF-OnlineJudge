"""Private learning execution records owned by the learning domain."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    LargeBinary,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from kf_oj.contracts.submission_evidence import MAX_SOURCE_EXCERPT_BYTES
from kf_oj.infra.db.base import Base
from kf_oj.infra.db.columns import CreatedAt


class AgentSessionRow(CreatedAt, Base):
    __tablename__ = "learning_agent_sessions"
    __table_args__ = (
        CheckConstraint(
            "purpose IN ('submission_review','guided_help','focused_training','periodic_review')",
            name="purpose",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    owner_id: Mapped[UUID] = mapped_column(ForeignKey("accounts.id"), index=True)
    purpose: Mapped[str] = mapped_column(String(24))


class AgentRunRow(CreatedAt, Base):
    __tablename__ = "learning_agent_runs"
    __table_args__ = (
        UniqueConstraint("session_id", "request_id", name="uq_learning_agent_run_request"),
        CheckConstraint("revision >= 0", name="revision"),
        CheckConstraint(
            "state IN ('queued','running','retry_wait','cancel_requested','cancelled',"
            "'succeeded','failed')",
            name="state",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    session_id: Mapped[UUID] = mapped_column(ForeignKey("learning_agent_sessions.id"))
    request_id: Mapped[UUID]
    skill_version_id: Mapped[UUID]
    configuration_digest: Mapped[str] = mapped_column(String(64))
    input_digest: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(20), server_default="queued")
    revision: Mapped[int] = mapped_column(BigInteger, server_default="0")


class AgentSourceExcerptRow(Base):
    __tablename__ = "learning_agent_source_excerpts"
    __table_args__ = (
        CheckConstraint(
            f"octet_length(source_utf8) BETWEEN 1 AND {MAX_SOURCE_EXCERPT_BYTES}", name="bytes"
        ),
    )

    run_id: Mapped[UUID] = mapped_column(
        ForeignKey("learning_agent_runs.id", ondelete="CASCADE"), primary_key=True
    )
    source_utf8: Mapped[bytes] = mapped_column(LargeBinary)


class AgentStepRow(Base):
    __tablename__ = "learning_agent_steps"
    __table_args__ = (
        UniqueConstraint("run_id", "sequence", name="uq_learning_agent_step_sequence"),
        CheckConstraint("sequence > 0", name="sequence"),
        CheckConstraint("kind IN ('model','tool')", name="kind"),
        CheckConstraint("state IN ('running','succeeded','failed','cancelled')", name="state"),
        CheckConstraint("(result_digest IS NOT NULL) = (state = 'succeeded')", name="result"),
        CheckConstraint("(error_code IS NOT NULL) = (state = 'failed')", name="error"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    run_id: Mapped[UUID] = mapped_column(ForeignKey("learning_agent_runs.id"))
    sequence: Mapped[int] = mapped_column(BigInteger)
    kind: Mapped[str] = mapped_column(String(5))
    invocation_version: Mapped[str] = mapped_column(String(64))
    arguments_digest: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(10))
    result_digest: Mapped[str | None] = mapped_column(String(64))
    error_code: Mapped[str | None] = mapped_column(String(64))


class AgentWorkflowRow(Base):
    __tablename__ = "learning_agent_workflow_data"
    __table_args__ = (
        CheckConstraint("jsonb_typeof(input) = 'object'", name="input"),
        CheckConstraint("result IS NULL OR jsonb_typeof(result) = 'object'", name="result"),
        CheckConstraint("result IS NULL OR failure_code IS NULL", name="outcome"),
    )

    run_id: Mapped[UUID] = mapped_column(ForeignKey("learning_agent_runs.id"), primary_key=True)
    input: Mapped[dict] = mapped_column(JSONB)
    result: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    failure_code: Mapped[str | None] = mapped_column(String(64))


class AgentFeedbackRow(Base):
    __tablename__ = "learning_agent_feedback"
    __table_args__ = (
        CheckConstraint("rating IN ('helpful','partly_helpful','not_helpful')", name="rating"),
        CheckConstraint(
            "reason IN ('unclear_next_step','unrelated_hint','insufficient_evidence')",
            name="reason",
        ),
    )

    run_id: Mapped[UUID] = mapped_column(
        ForeignKey("learning_agent_runs.id", ondelete="CASCADE"), primary_key=True
    )
    rating: Mapped[str] = mapped_column(String(16))
    reason: Mapped[str | None] = mapped_column(String(24))
    rated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AgentTaskRow(Base):
    __tablename__ = "learning_agent_tasks"
    __table_args__ = (
        CheckConstraint("authorization_version > 0", name="authority"),
        CheckConstraint("session_proof ~ '^[0-9a-f]{64}$'", name="session_proof"),
        CheckConstraint("attempts BETWEEN 0 AND 3", name="attempts"),
    )

    run_id: Mapped[UUID] = mapped_column(
        ForeignKey("learning_agent_workflow_data.run_id"), primary_key=True
    )
    authorization_version: Mapped[int] = mapped_column(BigInteger)
    session_proof: Mapped[str] = mapped_column(String(64))
    attempts: Mapped[int] = mapped_column(server_default="0")
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AgentCheckpointRow(Base):
    __tablename__ = "learning_agent_checkpoints"
    __table_args__ = (
        CheckConstraint("ordinal > 0", name="ordinal"),
        CheckConstraint("jsonb_typeof(output) = 'object'", name="output"),
    )

    run_id: Mapped[UUID] = mapped_column(ForeignKey("learning_agent_runs.id"), primary_key=True)
    ordinal: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    step_id: Mapped[UUID] = mapped_column(ForeignKey("learning_agent_steps.id"), unique=True)
    output: Mapped[dict] = mapped_column(JSONB)


class AgentPlanLinkRow(CreatedAt, Base):
    __tablename__ = "learning_agent_plan_links"

    run_id: Mapped[UUID] = mapped_column(ForeignKey("learning_agent_runs.id"), primary_key=True)
    plan_id: Mapped[UUID] = mapped_column(ForeignKey("learning_plans.id"), unique=True)


class AgentTimingRow(Base):
    __tablename__ = "learning_agent_timings"
    __table_args__ = (
        CheckConstraint(
            "first_claimed_at IS NOT NULL OR finished_at IS NOT NULL", name="has_event"
        ),
    )

    run_id: Mapped[UUID] = mapped_column(
        ForeignKey("learning_agent_tasks.run_id"), primary_key=True
    )
    first_claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AgentModelAttemptRow(Base):
    __tablename__ = "learning_agent_model_attempts"
    __table_args__ = (
        CheckConstraint("expires_at > started_at", name="expires"),
        CheckConstraint("finished_at IS NULL OR finished_at >= started_at", name="finished"),
    )

    run_id: Mapped[UUID] = mapped_column(ForeignKey("learning_agent_runs.id"), primary_key=True)
    owner_id: Mapped[UUID] = mapped_column(ForeignKey("accounts.id"))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reported_usage: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
