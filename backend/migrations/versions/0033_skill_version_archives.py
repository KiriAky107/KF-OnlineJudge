"""Keep author archives separate from immutable publications and operator holds."""

import sqlalchemy as sa
from alembic import op

revision = "0033_skill_version_archives"
down_revision = "0032_learning_prerequisites"
branch_labels = depends_on = None

TABLE = "skills_version_archives"


def upgrade():
    op.create_table(
        TABLE,
        sa.Column(
            "version_id",
            sa.Uuid(),
            sa.ForeignKey("skills_publications.id"),
            primary_key=True,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.execute(
        f"REVOKE ALL ON {TABLE} FROM PUBLIC, "
        "kf_oj_coordinator, kf_oj_learning_worker, kf_oj_platform"
    )
    op.execute(f"GRANT SELECT, INSERT ON {TABLE} TO kf_oj_platform")


def downgrade():
    op.execute(f"""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM {TABLE}) THEN
                RAISE EXCEPTION 'cannot downgrade while skill version archives exist';
            END IF;
        END $$;
    """)
    op.drop_table(TABLE)
