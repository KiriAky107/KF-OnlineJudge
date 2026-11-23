"""Record actual first claim and terminal times for learning runs."""

from alembic import op

from kf_oj.modules.learning.agent_models import AgentTimingRow

revision = "0021_learning_agent_timings"
down_revision = "0020_learning_training_feedback"
branch_labels = depends_on = None

TABLE = "learning_agent_timings"
ROLES = "kf_oj_platform,kf_oj_learning_worker"


def upgrade():
    AgentTimingRow.__table__.create(op.get_bind())
    op.execute(f"REVOKE ALL ON {TABLE} FROM PUBLIC, kf_oj_coordinator, {ROLES}")
    op.execute(f"GRANT SELECT, INSERT, UPDATE (finished_at) ON {TABLE} TO {ROLES}")


def downgrade():
    op.execute(f"""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM {TABLE}) THEN
                RAISE EXCEPTION 'cannot downgrade while learning execution timing exists';
            END IF;
        END $$
    """)
    AgentTimingRow.__table__.drop(op.get_bind())
