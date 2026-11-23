"""Store owners' private feedback separately from completed learning results."""

import sqlalchemy as sa
from alembic import op

revision = "0034_learning_agent_feedback"
down_revision = "0033_skill_version_archives"
branch_labels = depends_on = None

TABLE = "learning_agent_feedback"


def upgrade():
    op.create_table(
        TABLE,
        sa.Column(
            "run_id",
            sa.Uuid(),
            sa.ForeignKey("learning_agent_runs.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("rating", sa.String(16), nullable=False),
        sa.Column("reason", sa.String(24), nullable=True),
        sa.Column("rated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("rating IN ('helpful','partly_helpful','not_helpful')", name="rating"),
        sa.CheckConstraint(
            "reason IN ('unclear_next_step','unrelated_hint','insufficient_evidence')",
            name="reason",
        ),
    )
    op.execute(
        f"REVOKE ALL ON {TABLE} FROM PUBLIC, "
        "kf_oj_coordinator, kf_oj_learning_worker, kf_oj_platform"
    )
    op.execute(f"GRANT SELECT, INSERT, DELETE ON {TABLE} TO kf_oj_platform")
    op.execute(f"GRANT UPDATE (rating, reason, rated_at) ON {TABLE} TO kf_oj_platform")


def downgrade():
    op.execute(f"""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM {TABLE}) THEN
                RAISE EXCEPTION 'cannot downgrade while private learning feedback exists';
            END IF;
        END $$;
    """)
    op.drop_table(TABLE)
