"""Preserve when a learner became a team member."""

import sqlalchemy as sa
from alembic import op

revision = "0063_team_membership_approval"
down_revision = "0062_team_training"
branch_labels = depends_on = None

_TABLE = "team_memberships"


def upgrade():
    op.add_column(_TABLE, sa.Column("approved_at", sa.DateTime(timezone=True)))
    op.execute("UPDATE team_memberships SET approved_at=created_at WHERE role <> 'pending'")
    op.create_check_constraint(
        "approval_matches_role", _TABLE, "(role = 'pending') = (approved_at IS NULL)"
    )
    op.execute("GRANT UPDATE (approved_at) ON team_memberships TO kf_oj_platform")


def downgrade():
    op.drop_constraint("approval_matches_role", _TABLE, type_="check")
    op.drop_column(_TABLE, "approved_at")
