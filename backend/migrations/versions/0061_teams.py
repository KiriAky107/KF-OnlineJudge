"""Create real team membership and captain authority."""

import sqlalchemy as sa
from alembic import op

revision = "0061_teams"
down_revision = "0060_official_trainings"
branch_labels = depends_on = None


def upgrade():
    op.create_table(
        "teams",
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column("name", sa.String(30), nullable=False),
        sa.Column("description", sa.String(500), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("ix_teams_created_at", "teams", ["created_at", "id"])
    op.create_table(
        "team_memberships",
        sa.Column("team_id", sa.Uuid, sa.ForeignKey("teams.id"), primary_key=True),
        sa.Column("account_id", sa.Uuid, sa.ForeignKey("accounts.id"), primary_key=True),
        sa.Column("role", sa.String(8), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("role IN ('captain', 'member', 'pending')", name="role"),
    )
    op.create_index(
        "ix_team_memberships_role_time",
        "team_memberships",
        ["team_id", "role", "created_at", "account_id"],
    )
    op.create_index("ix_team_memberships_account", "team_memberships", ["account_id", "team_id"])
    op.create_index(
        "uq_team_memberships_captain",
        "team_memberships",
        ["team_id"],
        unique=True,
        postgresql_where=sa.text("role = 'captain'"),
    )
    op.execute(
        "REVOKE ALL ON teams, team_memberships "
        "FROM PUBLIC, kf_oj_coordinator, kf_oj_learning_worker"
    )
    op.execute("GRANT SELECT, INSERT ON teams TO kf_oj_platform")
    op.execute("GRANT SELECT, INSERT, DELETE ON team_memberships TO kf_oj_platform")
    op.execute("GRANT UPDATE (role) ON team_memberships TO kf_oj_platform")


def downgrade():
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM teams) THEN
                RAISE EXCEPTION 'cannot downgrade while teams exist';
            END IF;
        END $$
    """)
    op.drop_table("team_memberships")
    op.drop_table("teams")
