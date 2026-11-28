"""Persist diagnostic claim timing and the configuration actually handed to the broker."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0064_diagnostic_observation"
down_revision = "0063_team_membership_approval"
branch_labels = depends_on = None

_TABLE = "learning_diagnostic_requests"
_INDEX = "ix_learning_diagnostic_observed_at"


def upgrade():
    op.add_column(_TABLE, sa.Column("execution_started_at", sa.DateTime(timezone=True)))
    op.add_column(_TABLE, sa.Column("execution_profile", postgresql.JSONB()))
    op.add_column(_TABLE, sa.Column("review_context", postgresql.JSONB()))
    op.create_check_constraint(
        "execution_timing", _TABLE, "execution_started_at IS NULL OR execution_id IS NOT NULL"
    )
    op.create_check_constraint(
        "execution_profile",
        _TABLE,
        "execution_profile IS NULL OR "
        "(execution_started_at IS NOT NULL AND jsonb_typeof(execution_profile) = 'object')",
    )
    op.create_check_constraint(
        "review_context",
        _TABLE,
        "review_context IS NULL OR jsonb_typeof(review_context) = 'object'",
    )
    op.create_index(_INDEX, _TABLE, ["finished_at"], postgresql_where=sa.text("state = 'observed'"))
    op.execute(
        f"GRANT UPDATE (execution_started_at, execution_profile) ON {_TABLE} TO kf_oj_platform"
    )


def downgrade():
    op.drop_index(_INDEX, table_name=_TABLE)
    op.drop_constraint("review_context", _TABLE, type_="check")
    op.drop_constraint("execution_profile", _TABLE, type_="check")
    op.drop_constraint("execution_timing", _TABLE, type_="check")
    op.drop_column(_TABLE, "execution_profile")
    op.drop_column(_TABLE, "review_context")
    op.drop_column(_TABLE, "execution_started_at")
