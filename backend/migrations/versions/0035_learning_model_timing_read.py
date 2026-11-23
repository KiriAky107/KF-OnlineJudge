"""Allow the authenticated platform trace to read finished model timings."""

from alembic import op

revision = "0035_learning_model_timing_read"
down_revision = "0034_learning_agent_feedback"
branch_labels = depends_on = None

TABLE = "learning_agent_model_attempts"
COLUMNS = "run_id, started_at, finished_at"


def upgrade():
    op.execute(f"GRANT SELECT ({COLUMNS}) ON {TABLE} TO kf_oj_platform")


def downgrade():
    op.execute(f"REVOKE SELECT ({COLUMNS}) ON {TABLE} FROM kf_oj_platform")
