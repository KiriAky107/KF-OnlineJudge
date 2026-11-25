"""Record scheduled contests and confirmed participants."""

import sqlalchemy as sa
from alembic import op

revision = "0043_contest_agent_access"
down_revision = "0042_difficulty_levels"
branch_labels = depends_on = None


def upgrade():
    op.create_table(
        "scheduled_contests",
        sa.Column("key", sa.String(80), primary_key=True),
        sa.Column("title", sa.String(120), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("starts_at < ends_at", name="scheduled_contest_ordered_window"),
    )
    op.create_table(
        "contest_entrants",
        sa.Column("account_id", sa.Uuid(), sa.ForeignKey("accounts.id"), primary_key=True),
        sa.Column(
            "contest_key", sa.String(80), sa.ForeignKey("scheduled_contests.key"), primary_key=True
        ),
    )
    op.execute("GRANT SELECT, INSERT ON scheduled_contests, contest_entrants TO kf_oj_platform")
    op.execute("GRANT SELECT ON contest_entrants TO kf_oj_learning_worker")
    op.execute(
        "GRANT SELECT (key, starts_at, ends_at) ON scheduled_contests TO kf_oj_learning_worker"
    )


def downgrade():
    op.drop_table("contest_entrants")
    op.drop_table("scheduled_contests")
