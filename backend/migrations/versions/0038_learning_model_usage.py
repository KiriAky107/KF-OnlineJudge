"""Keep provider-reported usage with the existing durable model attempt."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0038_learning_model_usage"
down_revision = "0037_learning_review_language"
branch_labels = depends_on = None

TABLE = "learning_agent_model_attempts"


def upgrade():
    # Revision 0029 creates this table from the current model on fresh databases.
    op.add_column(TABLE, sa.Column("reported_usage", postgresql.JSONB()), if_not_exists=True)
    op.execute(f"GRANT SELECT (reported_usage) ON {TABLE} TO kf_oj_platform")
    op.execute(f"GRANT UPDATE (reported_usage) ON {TABLE} TO kf_oj_learning_worker")


def downgrade():
    op.drop_column(TABLE, "reported_usage")
