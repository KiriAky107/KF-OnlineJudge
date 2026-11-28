"""Store the verified public comparison baseline separately from self-test execution."""

import sqlalchemy as sa
from alembic import op

revision = "0065_self_test_comparisons"
down_revision = "0064_diagnostic_observation"
branch_labels = depends_on = None

_TABLE = "self_test_public_comparisons"


def upgrade():
    op.create_table(
        _TABLE,
        sa.Column(
            "self_test_id",
            sa.Uuid(),
            sa.ForeignKey("self_tests.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("expected_digest", sa.String(64), nullable=False),
        sa.Column("diagnostic_request_id", sa.Uuid()),
        sa.CheckConstraint("ordinal BETWEEN 0 AND 65535", name="ordinal"),
        sa.CheckConstraint("expected_digest ~ '^[0-9a-f]{64}$'", name="expected_digest"),
    )
    op.execute(f"REVOKE ALL ON {_TABLE} FROM PUBLIC, kf_oj_coordinator, kf_oj_learning_worker")
    op.execute(f"GRANT SELECT, INSERT ON {_TABLE} TO kf_oj_platform")


def downgrade():
    op.drop_table(_TABLE)
