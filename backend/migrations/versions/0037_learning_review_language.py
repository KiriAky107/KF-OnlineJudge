"""Let the learning worker scope a selected review to its submission language."""

from alembic import op

revision = "0037_learning_review_language"
down_revision = "0036_learning_evaluation_cutoff"
branch_labels = depends_on = None


def upgrade():
    op.execute("GRANT SELECT (language) ON public.submissions TO kf_oj_learning_worker")


def downgrade():
    op.execute("REVOKE SELECT (language) ON public.submissions FROM kf_oj_learning_worker")
