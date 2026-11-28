"""Persist personally confirmed diagnostic findings."""

import sqlalchemy as sa
from alembic import op

revision = "0055_learning_personal_mistakes"
down_revision = "0054_learning_diagnostic_ubsan"
branch_labels = depends_on = None

_TABLE = "learning_personal_mistakes"


def upgrade():
    op.create_table(
        _TABLE,
        sa.Column("owner_id", sa.Uuid(), primary_key=True),
        sa.Column("request_id", sa.Uuid(), primary_key=True),
        sa.Column("submission_id", sa.Uuid(), nullable=False),
        sa.Column("problem_version_id", sa.Uuid(), nullable=False),
        sa.Column("sample_ordinal", sa.Integer(), nullable=False),
        sa.Column("category", sa.String(32), nullable=False),
        sa.Column("source_line", sa.Integer(), nullable=False),
        sa.Column("source_column", sa.Integer(), nullable=False),
        sa.Column("note", sa.String(600), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ("owner_id", "request_id"),
            ("learning_diagnostic_requests.owner_id", "learning_diagnostic_requests.id"),
        ),
        sa.CheckConstraint(
            "category IN ('signed_integer_overflow', 'division_by_zero', "
            "'index_out_of_bounds', 'invalid_shift')",
            name="category",
        ),
        sa.CheckConstraint(
            "sample_ordinal BETWEEN 0 AND 65535 AND source_line BETWEEN 1 AND 262144 "
            "AND source_column BETWEEN 1 AND 262144",
            name="location",
        ),
        sa.CheckConstraint("updated_at >= created_at", name="timestamps"),
    )
    op.create_index(
        "ix_learning_personal_mistakes_owner_created",
        _TABLE,
        ["owner_id", "created_at", "request_id"],
    )
    op.execute(f"REVOKE ALL ON {_TABLE} FROM PUBLIC, kf_oj_coordinator, kf_oj_learning_worker")
    op.execute(f"GRANT SELECT, INSERT, DELETE ON {_TABLE} TO kf_oj_platform")
    op.execute(f"GRANT UPDATE (note, updated_at) ON {_TABLE} TO kf_oj_platform")


def downgrade():
    op.execute(f"""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM {_TABLE}) THEN
                RAISE EXCEPTION 'cannot downgrade while personal mistake records exist';
            END IF;
        END $$
    """)
    op.drop_table(_TABLE)
