"""Hold durable claims for the independent learning queue."""

from alembic import op

from kf_oj.modules.learning.agent_models import AgentTaskRow

revision = "0016_learning_agent_tasks"
down_revision = "0015_agent_workflow_data"
branch_labels = depends_on = None

TABLE = "learning_agent_tasks"


def upgrade():
    AgentTaskRow.__table__.create(op.get_bind())
    op.execute(f"REVOKE ALL ON {TABLE} FROM PUBLIC, kf_oj_coordinator, kf_oj_platform")
    op.execute(f"GRANT SELECT, INSERT ON {TABLE} TO kf_oj_platform")
    op.execute(
        f"GRANT UPDATE (attempts, available_at, lease_expires_at) ON {TABLE} TO kf_oj_platform"
    )


def downgrade():
    op.execute(f"""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM {TABLE}) THEN
                RAISE EXCEPTION 'cannot downgrade while learning tasks exist';
            END IF;
        END $$
    """)
    AgentTaskRow.__table__.drop(op.get_bind())
