"""Reserve model-call budgets durably before external requests."""

from alembic import op

from kf_oj.modules.learning.agent_models import AgentModelAttemptRow

revision = "0029_learning_model_budget"
down_revision = "0028_skill_version_holds"
branch_labels = depends_on = None

TABLE = "learning_agent_model_attempts"
ROLE = "kf_oj_learning_worker"


def upgrade():
    AgentModelAttemptRow.__table__.create(op.get_bind())
    op.execute(f"REVOKE ALL ON {TABLE} FROM PUBLIC, kf_oj_coordinator, kf_oj_platform, {ROLE}")
    op.execute(f"GRANT SELECT, INSERT ON {TABLE} TO {ROLE}")
    op.execute(f"GRANT UPDATE (finished_at) ON {TABLE} TO {ROLE}")


def downgrade():
    op.execute(f"""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM {TABLE}) THEN
                RAISE EXCEPTION 'cannot downgrade while model attempt records exist';
            END IF;
        END $$
    """)
    AgentModelAttemptRow.__table__.drop(op.get_bind())
