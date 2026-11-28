"""Share the evaluation evidence budget between admission and review workers."""

from alembic import op

revision = "0057_guided_evaluation_budget"
down_revision = "0056_learning_review_budget"
branch_labels = depends_on = None

_TABLE = "learning_review_evaluation_budgets"


def upgrade():
    op.execute(f"GRANT SELECT, INSERT ON {_TABLE} TO kf_oj_platform")
    op.execute(f"GRANT UPDATE (window_started_at, reads) ON {_TABLE} TO kf_oj_platform")


def downgrade():
    op.execute(f"REVOKE UPDATE (window_started_at, reads) ON {_TABLE} FROM kf_oj_platform")
    op.execute(f"REVOKE SELECT, INSERT ON {_TABLE} FROM kf_oj_platform")
