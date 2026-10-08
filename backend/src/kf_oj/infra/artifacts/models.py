"""Metadata ownership and lifecycle for immutable, digest-addressed bytes."""

from sqlalchemy import BigInteger, CheckConstraint, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from kf_oj.infra.db.base import Base
from kf_oj.infra.db.columns import CreatedAt


class Artifact(CreatedAt, Base):
    __tablename__ = "artifacts"
    __table_args__ = (
        CheckConstraint("digest ~ '^[0-9a-f]{64}$'", name="digest"),
        CheckConstraint("bytes BETWEEN 0 AND 67108864", name="bytes"),
        CheckConstraint("state IN ('ready', 'deleting')", name="state"),
        UniqueConstraint("digest", "state"),
    )

    digest: Mapped[str] = mapped_column(String(64), primary_key=True)
    bytes: Mapped[int] = mapped_column(BigInteger)
    state: Mapped[str] = mapped_column(String(8), server_default="ready")
