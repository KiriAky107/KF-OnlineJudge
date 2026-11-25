"""Store streaming model output without display-length limits."""

import sqlalchemy as sa
from alembic import op

revision = "0051_learning_chat_full_output"
down_revision = "0050_learning_assisted_practice"
branch_labels = depends_on = None

TABLE = "learning_agent_partial_answers"


def upgrade():
    op.alter_column(TABLE, "text", existing_type=sa.String(6000), type_=sa.Text())
    op.alter_column(TABLE, "reasoning", existing_type=sa.String(8192), type_=sa.Text())


def downgrade():
    # Only active previews need to fit the previous schema. Final replies are JSONB.
    op.alter_column(
        TABLE,
        "text",
        existing_type=sa.Text(),
        type_=sa.String(6000),
        postgresql_using="left(text, 6000)",
    )
    op.alter_column(
        TABLE,
        "reasoning",
        existing_type=sa.Text(),
        type_=sa.String(8192),
        postgresql_using="left(reasoning, 8192)",
    )
