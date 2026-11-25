"""Store the current chat answer preview while a worker holds its run."""

import sqlalchemy as sa
from alembic import op

revision = "0044_learning_partial_answer"
down_revision = "0043_contest_agent_access"
branch_labels = depends_on = None


def upgrade():
    op.create_table(
        "learning_agent_partial_answers",
        sa.Column(
            "run_id",
            sa.Uuid(),
            sa.ForeignKey("learning_agent_runs.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("text", sa.String(1800), nullable=False),
    )
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON learning_agent_partial_answers "
        "TO kf_oj_learning_worker"
    )
    op.execute("GRANT SELECT, DELETE ON learning_agent_partial_answers TO kf_oj_platform")


def downgrade():
    op.drop_table("learning_agent_partial_answers")
