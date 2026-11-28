"""Keep official training snapshots separate from personal training state."""

import sqlalchemy as sa
from alembic import op

revision = "0060_official_trainings"
down_revision = "0059_learning_diagnostic_batches"
branch_labels = depends_on = None

_CATALOG = "learning_official_trainings"
_EXERCISES = "learning_official_training_exercises"


def upgrade():
    op.create_table(
        _CATALOG,
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "source_plan_id",
            sa.Uuid(),
            sa.ForeignKey("learning_plans.id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("title", sa.String(120), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index(f"ix_{_CATALOG}_created_at", _CATALOG, ["created_at", "id"])
    op.create_table(
        _EXERCISES,
        sa.Column("training_id", sa.Uuid(), sa.ForeignKey(f"{_CATALOG}.id"), primary_key=True),
        sa.Column("position", sa.Integer(), primary_key=True),
        sa.Column("version_id", sa.Uuid(), sa.ForeignKey("problem_versions.id"), nullable=False),
        sa.CheckConstraint("position >= 0", name="position"),
        sa.UniqueConstraint("training_id", "version_id"),
    )
    op.execute(
        f"REVOKE ALL ON {_CATALOG}, {_EXERCISES} "
        "FROM PUBLIC, kf_oj_coordinator, kf_oj_learning_worker"
    )
    op.execute(f"GRANT SELECT, INSERT ON {_CATALOG}, {_EXERCISES} TO kf_oj_platform")


def downgrade():
    op.execute(f"""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM {_CATALOG}) THEN
                RAISE EXCEPTION 'cannot downgrade while official trainings exist';
            END IF;
        END $$
    """)
    op.drop_table(_EXERCISES)
    op.drop_table(_CATALOG)
