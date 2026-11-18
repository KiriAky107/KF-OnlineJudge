"""Personal learning plans and recommendation snapshots."""

from alembic import op

from kf_oj.modules.learning.models import LearningPlan, LearningPlanItem

revision = "0010_learning_plans"
down_revision = "0009_problem_learning_metadata"
branch_labels = depends_on = None


def upgrade():
    LearningPlan.__table__.create(op.get_bind())
    LearningPlanItem.__table__.create(op.get_bind())
    op.execute("GRANT SELECT,INSERT ON learning_plans TO kf_oj_platform")
    op.execute("GRANT SELECT,INSERT,UPDATE ON learning_plan_items TO kf_oj_platform")


def downgrade():
    LearningPlanItem.__table__.drop(op.get_bind())
    LearningPlan.__table__.drop(op.get_bind())
