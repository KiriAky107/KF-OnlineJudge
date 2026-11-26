"""Store semantic completion policy and immutable contest problem versions."""

import sqlalchemy as sa
from alembic import op

revision = "0052_contest_editor_policy"
down_revision = "0051_learning_chat_full_output"


def upgrade():
    op.add_column(
        "scheduled_contests",
        sa.Column(
            "semantic_completion_enabled", sa.Boolean(), server_default=sa.true(), nullable=False
        ),
    )
    op.create_table(
        "contest_editor_problems",
        sa.Column(
            "contest_key",
            sa.String(80),
            sa.ForeignKey("scheduled_contests.key", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("version_id", sa.Uuid(), sa.ForeignKey("problem_versions.id"), primary_key=True),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.UniqueConstraint("contest_key", "position"),
    )
    op.create_index(
        "ix_contest_editor_problems_version_id", "contest_editor_problems", ["version_id"]
    )
    op.execute("GRANT SELECT, INSERT, DELETE ON contest_editor_problems TO kf_oj_platform")
    op.execute("GRANT UPDATE (semantic_completion_enabled) ON scheduled_contests TO kf_oj_platform")


def downgrade():
    op.drop_table("contest_editor_problems")
    op.drop_column("scheduled_contests", "semantic_completion_enabled")
