"""Allow learning searches to read the configured difficulty score."""

from alembic import op

revision = "0047_learning_search_scores"
down_revision = "0046_learning_chat_reasoning"
branch_labels = depends_on = None
WORKER = "kf_oj_learning_worker"


def upgrade():
    op.execute(f"GRANT SELECT (key, score) ON problem_difficulty_levels TO {WORKER}")
    op.execute(f"GRANT SELECT (difficulty_key) ON problem_learning_metadata TO {WORKER}")


def downgrade():
    op.execute(f"REVOKE SELECT (difficulty_key) ON problem_learning_metadata FROM {WORKER}")
    op.execute(f"REVOKE SELECT (key, score) ON problem_difficulty_levels FROM {WORKER}")
