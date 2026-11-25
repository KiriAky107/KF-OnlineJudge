"""Associate purpose-specific learning sessions with an owned conversation."""

import sqlalchemy as sa
from alembic import op

revision = "0045_learning_conversation_root"
down_revision = "0044_learning_partial_answer"
branch_labels = depends_on = None


def upgrade():
    op.create_table(
        "learning_agent_conversation_sessions",
        sa.Column(
            "session_id", sa.Uuid(), sa.ForeignKey("learning_agent_sessions.id"), primary_key=True
        ),
        sa.Column(
            "root_session_id",
            sa.Uuid(),
            sa.ForeignKey("learning_agent_sessions.id"),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_learning_agent_conversation_sessions_root_session_id",
        "learning_agent_conversation_sessions",
        ["root_session_id"],
    )
    op.execute("GRANT SELECT, INSERT ON learning_agent_conversation_sessions TO kf_oj_platform")


def downgrade():
    op.drop_table("learning_agent_conversation_sessions")
