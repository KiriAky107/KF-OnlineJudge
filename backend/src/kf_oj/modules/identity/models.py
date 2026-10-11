"""Identity-owned persistent account and revocable session records."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from kf_oj.infra.db.base import Base
from kf_oj.infra.db.columns import CreatedAt


class Account(CreatedAt, Base):
    __tablename__ = "accounts"
    __table_args__ = (
        CheckConstraint("username ~ '^[a-z0-9_]{3,24}$'", name="canonical_username"),
        CheckConstraint("char_length(password_hash) BETWEEN 40 AND 256", name="password_hash_size"),
        CheckConstraint("authorization_version >= 1", name="authorization_version"),
        Index(
            "ix_accounts_active_administrators",
            "id",
            postgresql_where=text("is_administrator AND enabled"),
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(24), unique=True)
    password_hash: Mapped[str] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, server_default="true")
    is_administrator: Mapped[bool] = mapped_column(Boolean, server_default="false")
    authorization_version: Mapped[int] = mapped_column(BigInteger, server_default="1")


class Session(CreatedAt, Base):
    __tablename__ = "sessions"
    __table_args__ = (
        CheckConstraint("token_hash ~ '^[0-9a-f]{64}$'", name="token_digest"),
        CheckConstraint("csrf_hash ~ '^[0-9a-f]{64}$'", name="csrf_digest"),
        CheckConstraint("expires_at > created_at", name="expiry"),
        Index("ix_sessions_account_expiry", "account_id", "expires_at"),
    )

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    account_id: Mapped[UUID] = mapped_column(ForeignKey("accounts.id"))
    csrf_hash: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class LoginThrottle(Base):
    __tablename__ = "login_throttles"
    __table_args__ = (
        CheckConstraint("bucket_hash ~ '^[0-9a-f]{64}$'", name="bucket_digest"),
        CheckConstraint("attempts BETWEEN 1 AND 10", name="attempt_budget"),
        Index("ix_login_throttles_reset", "reset_at"),
    )

    bucket_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    attempts: Mapped[int]
    reset_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
