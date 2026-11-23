"""Store private learner self-assessments independently of grading."""

from alembic import op

from kf_oj.modules.learning.models import TrainingFeedbackRow

revision = "0020_learning_training_feedback"
down_revision = "0019_learning_agent_plan_link"
branch_labels = depends_on = None

TABLE = "learning_training_feedback"


def upgrade():
    TrainingFeedbackRow.__table__.create(op.get_bind())
    op.execute(f"REVOKE ALL ON {TABLE} FROM PUBLIC, kf_oj_coordinator, kf_oj_platform")
    op.execute(f"GRANT SELECT, INSERT, DELETE ON {TABLE} TO kf_oj_platform")
    op.execute(f"GRANT UPDATE (rating, rated_at) ON {TABLE} TO kf_oj_platform")
    op.execute(f"GRANT SELECT ON {TABLE} TO kf_oj_learning_worker")


def downgrade():
    op.execute(f"""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM {TABLE}) THEN
                RAISE EXCEPTION 'cannot downgrade while training feedback exists';
            END IF;
        END $$
    """)
    TrainingFeedbackRow.__table__.drop(op.get_bind())
