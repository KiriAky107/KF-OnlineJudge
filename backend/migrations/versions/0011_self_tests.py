"""Separate owned self-tests sharing only execution and outbox machinery."""

import sqlalchemy as sa
from alembic import op

from kf_oj.modules.submissions.models import SelfTest

revision = "0011_self_tests"
down_revision = "0010_learning_plans"
branch_labels = depends_on = None


def upgrade():
    SelfTest.__table__.create(op.get_bind())
    op.alter_column("evaluations", "submission_id", nullable=True)
    op.add_column("evaluations", sa.Column("self_test_id", sa.Uuid(), nullable=True))
    op.create_unique_constraint("uq_evaluations_self_test_id", "evaluations", ["self_test_id"])
    op.create_foreign_key(
        "fk_evaluations_self_test",
        "evaluations",
        "self_tests",
        ["self_test_id", "problem_version_id"],
        ["id", "problem_version_id"],
    )
    op.create_check_constraint(
        "execution_owner", "evaluations", "(submission_id IS NULL) <> (self_test_id IS NULL)"
    )
    op.execute("GRANT SELECT,INSERT ON self_tests TO kf_oj_platform")
    op.execute("GRANT SELECT ON self_tests TO kf_oj_coordinator")
    op.execute("GRANT INSERT(self_test_id) ON evaluations TO kf_oj_platform")


def downgrade():
    op.drop_constraint(op.f("ck_evaluations_execution_owner"), "evaluations")
    op.drop_constraint("fk_evaluations_self_test", "evaluations")
    op.drop_constraint("uq_evaluations_self_test_id", "evaluations")
    op.drop_column("evaluations", "self_test_id")
    op.alter_column("evaluations", "submission_id", nullable=False)
    SelfTest.__table__.drop(op.get_bind())
