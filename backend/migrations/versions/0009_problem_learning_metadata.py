"""Learning metadata separate from immutable problem versions."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0009_problem_learning_metadata"
down_revision = "0008_problem_maintenance"
branch_labels = depends_on = None
ACTIONS = (
    "'upload_created','upload_received','import_ready','import_failed','import_discarded','import_expired',"
    "'published','disabled','restored',"
    "'authority_bootstrap','authority_grant','authority_revoke','authority_enable','authority_disable'"
)


def upgrade():
    op.alter_column("problem_maintenance_events", "action", type_=sa.String(32))
    op.create_table(
        "problem_learning_metadata",
        sa.Column("problem_version_id", sa.Uuid(), nullable=False),
        sa.Column("tags", postgresql.JSONB(), nullable=False),
        sa.Column("difficulty", sa.String(8), nullable=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["problem_version_id"],
            ["problem_versions.id"],
            name=op.f("fk_problem_learning_metadata_problem_version_id_problem_versions"),
        ),
        sa.PrimaryKeyConstraint("problem_version_id", name=op.f("pk_problem_learning_metadata")),
        sa.CheckConstraint("revision > 0", name=op.f("ck_problem_learning_metadata_revision")),
        sa.CheckConstraint(
            "difficulty IS NULL OR difficulty IN ('easy','medium','hard')",
            name=op.f("ck_problem_learning_metadata_difficulty"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(tags) = 'array' AND jsonb_array_length(tags) <= 16",
            name=op.f("ck_problem_learning_metadata_tags"),
        ),
    )
    op.execute("GRANT SELECT,INSERT,UPDATE ON problem_learning_metadata TO kf_oj_platform")
    op.drop_constraint(op.f("ck_problem_maintenance_events_action"), "problem_maintenance_events")
    op.create_check_constraint(
        "action",
        "problem_maintenance_events",
        "action IN (" + ACTIONS + ",'learning_metadata_updated')",
    )


def downgrade():
    op.drop_table("problem_learning_metadata")
    # Keep the additive audit action to preserve append-only historical events.
