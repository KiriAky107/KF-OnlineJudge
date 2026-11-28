"""Allow the learning worker to read the public difficulty label."""

from alembic import op

revision = "0069_learning_difficulty_label"
down_revision = "0068_original_reasoning_stream"
branch_labels = depends_on = None


def upgrade():
    op.execute("GRANT SELECT (label) ON public.problem_difficulty_levels TO kf_oj_learning_worker")


def downgrade():
    op.execute(
        "REVOKE SELECT (label) ON public.problem_difficulty_levels FROM kf_oj_learning_worker"
    )
