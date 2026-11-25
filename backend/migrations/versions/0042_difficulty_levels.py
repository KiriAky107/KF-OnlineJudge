"""Store reusable problem difficulty labels, scores and colors."""

import sqlalchemy as sa
from alembic import op

revision = "0042_difficulty_levels"
down_revision = "0041_contest_ratings"
branch_labels = depends_on = None


def upgrade():
    table = op.create_table(
        "problem_difficulty_levels",
        sa.Column("key", sa.String(32), primary_key=True),
        sa.Column("label", sa.String(24), nullable=False),
        sa.Column("score", sa.Integer(), nullable=False),
        sa.Column("color", sa.String(7), nullable=False),
        sa.Column("tier", sa.String(8), nullable=False),
        sa.CheckConstraint("score BETWEEN 1 AND 10000", name="score_range"),
        sa.CheckConstraint("color ~ '^#[0-9a-fA-F]{6}$'", name="hex_color"),
        sa.CheckConstraint("tier IN ('easy','medium','hard')", name="known_tier"),
    )
    op.bulk_insert(
        table,
        [
            {"key": "easy", "label": "简单", "score": 800, "color": "#19be6b", "tier": "easy"},
            {"key": "medium", "label": "中等", "score": 1400, "color": "#2d8cf0", "tier": "medium"},
            {"key": "hard", "label": "困难", "score": 2000, "color": "#ed3f14", "tier": "hard"},
        ],
    )
    op.add_column("problem_learning_metadata", sa.Column("difficulty_key", sa.String(32)))
    op.create_foreign_key(
        "fk_problem_learning_difficulty_key",
        "problem_learning_metadata",
        "problem_difficulty_levels",
        ["difficulty_key"],
        ["key"],
    )
    op.execute(
        "GRANT SELECT, INSERT, UPDATE (label, score, color) "
        "ON problem_difficulty_levels TO kf_oj_platform"
    )


def downgrade():
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM problem_learning_metadata WHERE difficulty_key IS NOT NULL)
               OR EXISTS (SELECT 1 FROM problem_difficulty_levels
                          WHERE key NOT IN ('easy','medium','hard'))
            THEN RAISE EXCEPTION 'custom problem difficulties exist'; END IF;
        END $$;
    """)
    op.drop_constraint(
        "fk_problem_learning_difficulty_key", "problem_learning_metadata", type_="foreignkey"
    )
    op.drop_column("problem_learning_metadata", "difficulty_key")
    op.drop_table("problem_difficulty_levels")
