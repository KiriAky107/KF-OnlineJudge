"""Persist private workflow inputs and the outcome of a learning run."""

from alembic import op

from kf_oj.modules.learning.agent_models import AgentWorkflowRow

revision = "0015_agent_workflow_data"
down_revision = "0014_skills_bindings"
branch_labels = depends_on = None

TABLE = "learning_agent_workflow_data"


def upgrade():
    AgentWorkflowRow.__table__.create(op.get_bind())
    op.execute(f"REVOKE ALL ON {TABLE} FROM PUBLIC, kf_oj_coordinator, kf_oj_platform")
    op.execute(f"GRANT SELECT, INSERT, UPDATE (result, failure_code) ON {TABLE} TO kf_oj_platform")


def downgrade():
    op.execute(f"""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM {TABLE}) THEN
                RAISE EXCEPTION 'cannot downgrade while learning workflow data exists';
            END IF;
        END $$
    """)
    AgentWorkflowRow.__table__.drop(op.get_bind())
