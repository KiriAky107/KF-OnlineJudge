"""Identify live previews sourced from provider reasoning text."""

import sqlalchemy as sa
from alembic import op

revision = "0068_original_reasoning_stream"
down_revision = "0067_review_batch_mcp"
branch_labels = depends_on = None

_TABLE = "learning_agent_partial_answers"


def upgrade():
    op.add_column(
        _TABLE,
        sa.Column("reasoning_is_original", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade():
    op.drop_column(_TABLE, "reasoning_is_original")
