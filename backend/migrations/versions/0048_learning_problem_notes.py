"""Store private Markdown notes for immutable problem versions."""

import sqlalchemy as sa
from alembic import op

revision = "0048_learning_problem_notes"
down_revision = "0047_learning_search_scores"
branch_labels = depends_on = None


def upgrade():
    op.create_table(
        "learning_problem_notes",
        sa.Column(
            "owner_id",
            sa.Uuid(),
            sa.ForeignKey("accounts.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("version_id", sa.Uuid(), sa.ForeignKey("problem_versions.id"), primary_key=True),
        sa.Column("note", sa.String(1000), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON learning_problem_notes TO kf_oj_platform")


def downgrade():
    op.drop_table("learning_problem_notes")
