"""Save completed tool outcomes atomically with their steps."""

from alembic import op

from kf_oj.modules.learning.agent_models import AgentCheckpointRow

revision = "0017_learning_checkpoints"
down_revision = "0016_learning_agent_tasks"
branch_labels = depends_on = None

TABLE = "learning_agent_checkpoints"


def upgrade():
    AgentCheckpointRow.__table__.create(op.get_bind())
    op.execute(f"REVOKE ALL ON {TABLE} FROM PUBLIC, kf_oj_coordinator, kf_oj_platform")
    op.execute(f"GRANT SELECT, INSERT ON {TABLE} TO kf_oj_platform")


def downgrade():
    op.execute(f"""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM {TABLE}) THEN
                RAISE EXCEPTION 'cannot downgrade while learning checkpoints exist';
            END IF;
        END $$
    """)
    AgentCheckpointRow.__table__.drop(op.get_bind())
