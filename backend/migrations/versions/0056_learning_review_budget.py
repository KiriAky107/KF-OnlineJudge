"""Budget model access to coarse evaluation summaries per owner and problem version."""

import sqlalchemy as sa
from alembic import op

revision = "0056_learning_review_budget"
down_revision = "0055_learning_personal_mistakes"
branch_labels = depends_on = None

_TABLE = "learning_review_evaluation_budgets"


def upgrade():
    op.create_table(
        _TABLE,
        sa.Column("owner_id", sa.Uuid(), sa.ForeignKey("accounts.id"), primary_key=True),
        sa.Column(
            "problem_version_id", sa.Uuid(), sa.ForeignKey("problem_versions.id"), primary_key=True
        ),
        sa.Column("window_started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reads", sa.Integer(), nullable=False),
        sa.CheckConstraint("reads > 0", name="positive_reads"),
    )
    op.execute(f"REVOKE ALL ON {_TABLE} FROM PUBLIC, kf_oj_coordinator, kf_oj_platform")
    op.execute(f"GRANT SELECT, INSERT ON {_TABLE} TO kf_oj_learning_worker")
    op.execute(f"GRANT UPDATE (window_started_at, reads) ON {_TABLE} TO kf_oj_learning_worker")


def downgrade():
    op.drop_table(_TABLE)
