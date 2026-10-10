"""Indexed group watermark for bounded aggregate cache invalidation."""

import sqlalchemy as sa
from alembic import op

revision = "0004_performance_watermark"
down_revision = "0003_submission_performance"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_performance_group_watermark",
        "evaluation_performance",
        ["comparison_group", sa.text("finished_at DESC"), sa.text("evaluation_id DESC")],
    )


def downgrade() -> None:
    op.drop_index("ix_performance_group_watermark", table_name="evaluation_performance")
