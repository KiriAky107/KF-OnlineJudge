"""Personal learning plans and recommendation snapshots."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0010_learning_plans"
down_revision = "0009_problem_learning_metadata"
branch_labels = depends_on = None


def upgrade():
    op.create_table(
        "learning_plans",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("owner_id", sa.Uuid(), sa.ForeignKey("accounts.id"), nullable=False),
        sa.Column("idempotency_key", sa.Uuid(), nullable=False),
        sa.Column("request_digest", sa.String(64), nullable=False),
        sa.Column("title", sa.String(120), nullable=False),
        sa.Column("rule_version", sa.String(32), nullable=False),
        sa.Column("parameters", JSONB(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("owner_id", "idempotency_key"),
    )
    op.create_index("ix_learning_plans_owner_id", "learning_plans", ["owner_id"])
    op.create_table(
        "learning_plan_items",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("plan_id", sa.Uuid(), sa.ForeignKey("learning_plans.id"), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("problem_id", sa.Uuid(), sa.ForeignKey("problems.id"), nullable=False),
        sa.Column("version_id", sa.Uuid(), sa.ForeignKey("problem_versions.id"), nullable=False),
        sa.Column("snapshot", JSONB(), nullable=False),
        sa.Column("note", sa.Text(), server_default="", nullable=False),
        sa.UniqueConstraint("plan_id", "position", name="uq_learning_items_position"),
        sa.UniqueConstraint("plan_id", "problem_id", name="uq_learning_items_problem"),
    )
    op.create_index("ix_learning_plan_items_plan_id", "learning_plan_items", ["plan_id"])
    op.execute("GRANT SELECT,INSERT ON learning_plans TO kf_oj_platform")
    op.execute("GRANT SELECT,INSERT,UPDATE ON learning_plan_items TO kf_oj_platform")


def downgrade():
    op.drop_table("learning_plan_items")
    op.drop_table("learning_plans")
