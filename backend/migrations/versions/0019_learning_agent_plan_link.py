"""Tie one approved plan to its originating Agent run."""

from alembic import op

from kf_oj.modules.learning.agent_models import AgentPlanLinkRow

revision = "0019_learning_agent_plan_link"
down_revision = "0018_learning_worker_role"
branch_labels = depends_on = None

TABLE = "learning_agent_plan_links"


def upgrade():
    AgentPlanLinkRow.__table__.create(op.get_bind())
    op.execute(f"REVOKE ALL ON {TABLE} FROM PUBLIC, kf_oj_coordinator, kf_oj_platform")
    op.execute(f"GRANT SELECT, INSERT ON {TABLE} TO kf_oj_platform")


def downgrade():
    op.execute(f"""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM {TABLE}) THEN
                RAISE EXCEPTION 'cannot downgrade while saved Agent plans exist';
            END IF;
        END $$
    """)
    AgentPlanLinkRow.__table__.drop(op.get_bind())
