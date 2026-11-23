"""Give all learning processes the same tool budgets."""

from alembic import op

from kf_oj.modules.learning.agent_tool_models import AgentToolAttemptRow

revision = "0013_learning_agent_tools"
down_revision = "0012_learning_agent_records"
branch_labels = depends_on = None

TABLE = "learning_agent_tool_attempts"


def upgrade():
    AgentToolAttemptRow.__table__.create(op.get_bind())
    op.execute(f"REVOKE ALL ON {TABLE} FROM PUBLIC, kf_oj_coordinator, kf_oj_platform")
    op.execute(f"GRANT SELECT, INSERT, UPDATE (finished_at) ON {TABLE} TO kf_oj_platform")


def downgrade():
    op.execute(f"""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM {TABLE}) THEN
                RAISE EXCEPTION 'cannot downgrade while learning Agent tool attempts exist';
            END IF;
        END $$
    """)
    AgentToolAttemptRow.__table__.drop(op.get_bind())
