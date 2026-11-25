"""Record the first available directional hint per learner and problem version."""

import sqlalchemy as sa
from alembic import op

revision = "0050_learning_assisted_practice"
down_revision = "0049_learning_hint_ceiling"
branch_labels = depends_on = None


def upgrade():
    op.create_table(
        "learning_hint_usage",
        sa.Column(
            "owner_id",
            sa.Uuid(),
            sa.ForeignKey("accounts.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("version_id", sa.Uuid(), sa.ForeignKey("problem_versions.id"), primary_key=True),
        sa.Column(
            "first_provided_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.clock_timestamp(),
        ),
    )
    op.execute("GRANT SELECT ON learning_hint_usage TO kf_oj_platform")
    op.execute("GRANT SELECT, INSERT ON learning_hint_usage TO kf_oj_learning_worker")


def downgrade():
    op.drop_table("learning_hint_usage")
