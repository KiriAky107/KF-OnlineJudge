"""Index formal submissions by logical problem for catalog totals."""

from alembic import op

revision = "0040_submission_catalog_index"
down_revision = "0039_learning_source_excerpt"
branch_labels = depends_on = None


def upgrade():
    op.create_index("ix_submissions_problem_id", "submissions", ["problem_id"])


def downgrade():
    op.drop_index("ix_submissions_problem_id", table_name="submissions")
