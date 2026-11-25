"""Persist finalized ratings and immutable settlement inputs."""

import sqlalchemy as sa
from alembic import op

revision = "0041_contest_ratings"
down_revision = "0040_submission_catalog_index"
branch_labels = depends_on = None


def upgrade():
    op.create_table(
        "rated_contests",
        sa.Column("key", sa.String(80), primary_key=True),
        sa.Column("title", sa.String(120), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("settled_by", sa.Uuid(), sa.ForeignKey("accounts.id"), nullable=False),
        sa.Column("payload_digest", sa.String(64), nullable=False),
        sa.Column("participants", sa.Integer(), nullable=False),
    )
    op.create_table(
        "contest_rating_changes",
        sa.Column(
            "contest_key", sa.String(80), sa.ForeignKey("rated_contests.key"), primary_key=True
        ),
        sa.Column("account_id", sa.Uuid(), sa.ForeignKey("accounts.id"), primary_key=True),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("round_number", sa.Integer(), nullable=False),
        sa.Column("old_rating", sa.Integer(), nullable=False),
        sa.Column("new_rating", sa.Integer(), nullable=False),
        sa.Column("internal_rating", sa.Integer(), nullable=False),
        sa.UniqueConstraint("account_id", "round_number"),
    )
    op.execute("GRANT SELECT, INSERT ON rated_contests, contest_rating_changes TO kf_oj_platform")


def downgrade():
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM rated_contests) THEN
                RAISE EXCEPTION 'rated contest history exists';
            END IF;
        END $$;
    """)
    op.drop_table("contest_rating_changes")
    op.drop_table("rated_contests")
