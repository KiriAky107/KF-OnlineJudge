"""Give published training snapshots an official or team audience."""

import sqlalchemy as sa
from alembic import op

revision = "0062_team_training"
down_revision = "0061_teams"
branch_labels = depends_on = None

_OLD_TRAININGS = "learning_official_trainings"
_OLD_EXERCISES = "learning_official_training_exercises"
_TRAININGS = "learning_training_publications"
_EXERCISES = "learning_training_publication_items"
_OFFICIAL_PLAN = "uq_training_publications_official_plan"
_TEAM_PLAN = "uq_training_publications_team_plan"
_CATALOG_ORDER = "ix_training_publications_scope_created"
_PRACTICES = "ix_learning_plans_publication_owner"


def upgrade():
    op.drop_constraint(op.f(f"uq_{_OLD_TRAININGS}_source_plan_id"), _OLD_TRAININGS, type_="unique")
    op.drop_index(f"ix_{_OLD_TRAININGS}_created_at", table_name=_OLD_TRAININGS)
    op.rename_table(_OLD_TRAININGS, _TRAININGS)
    op.rename_table(_OLD_EXERCISES, _EXERCISES)
    op.add_column(
        _TRAININGS, sa.Column("team_id", sa.Uuid(), sa.ForeignKey("teams.id"), nullable=True)
    )
    op.create_index(_CATALOG_ORDER, _TRAININGS, ["team_id", "created_at", "id"])
    op.create_index(
        _OFFICIAL_PLAN,
        _TRAININGS,
        ["source_plan_id"],
        unique=True,
        postgresql_where=sa.text("team_id IS NULL"),
    )
    op.create_unique_constraint(_TEAM_PLAN, _TRAININGS, ["source_plan_id", "team_id"])
    op.add_column(
        "learning_plans",
        sa.Column("publication_id", sa.Uuid(), sa.ForeignKey(f"{_TRAININGS}.id"), nullable=True),
    )
    op.create_index(
        _PRACTICES,
        "learning_plans",
        ["publication_id", "owner_id"],
        postgresql_where=sa.text("publication_id IS NOT NULL"),
    )


def downgrade():
    op.execute(f"""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM {_TRAININGS} WHERE team_id IS NOT NULL) THEN
                RAISE EXCEPTION 'cannot downgrade while team training publications exist';
            END IF;
        END $$;
    """)
    op.drop_index(_PRACTICES, table_name="learning_plans")
    op.drop_column("learning_plans", "publication_id")
    op.drop_constraint(_TEAM_PLAN, _TRAININGS, type_="unique")
    op.drop_index(_OFFICIAL_PLAN, table_name=_TRAININGS)
    op.drop_index(_CATALOG_ORDER, table_name=_TRAININGS)
    op.drop_column(_TRAININGS, "team_id")
    op.rename_table(_EXERCISES, _OLD_EXERCISES)
    op.rename_table(_TRAININGS, _OLD_TRAININGS)
    op.create_unique_constraint(
        op.f(f"uq_{_OLD_TRAININGS}_source_plan_id"), _OLD_TRAININGS, ["source_plan_id"]
    )
    op.create_index(f"ix_{_OLD_TRAININGS}_created_at", _OLD_TRAININGS, ["created_at", "id"])
