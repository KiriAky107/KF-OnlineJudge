"""Track model reasoning separately from the answer while a chat is running."""

import sqlalchemy as sa
from alembic import op

revision = "0046_learning_chat_reasoning"
down_revision = "0045_learning_conversation_root"
branch_labels = depends_on = None

TABLE = "learning_agent_partial_answers"


def upgrade():
    op.alter_column(
        TABLE, "text", existing_type=sa.String(1800), type_=sa.String(6000), nullable=True
    )
    op.add_column(TABLE, sa.Column("reasoning", sa.String(8192)))
    op.create_check_constraint("content", TABLE, "text IS NOT NULL OR reasoning IS NOT NULL")


def downgrade():
    op.drop_constraint("content", TABLE, type_="check")
    op.execute("DELETE FROM learning_agent_partial_answers WHERE text IS NULL")
    op.drop_column(TABLE, "reasoning")
    op.alter_column(
        TABLE,
        "text",
        existing_type=sa.String(6000),
        type_=sa.String(1800),
        nullable=False,
        postgresql_using="left(text, 1800)",
    )
