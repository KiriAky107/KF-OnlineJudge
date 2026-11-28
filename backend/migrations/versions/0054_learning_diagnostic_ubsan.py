"""Allow a single-sample UBSan observation alongside the existing optimization mode."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0054_learning_diagnostic_ubsan"
down_revision = "0053_learning_review_mcp"
branch_labels = depends_on = None

_TABLE = "learning_diagnostic_requests"
_OUTCOME = "ck_learning_diagnostic_requests_outcome"
_FAILURE = "(state = 'failed') = (failure_code IS NOT NULL)"
_OPTIMIZATION = (
    "(state = 'observed') = (o2 IS NOT NULL) "
    "AND (o2 IS NOT NULL AND o2 IN ('wrong_answer', 'runtime_error')) = (o0 IS NOT NULL)"
)


def upgrade():
    op.add_column(
        _TABLE, sa.Column("mode", sa.String(12), nullable=False, server_default="optimization")
    )
    op.add_column(_TABLE, sa.Column("ubsan", JSONB(none_as_null=True)))
    op.drop_constraint(op.f(_OUTCOME), _TABLE, type_="check")
    op.create_check_constraint(
        op.f(_OUTCOME),
        _TABLE,
        f"((mode = 'optimization' AND ubsan IS NULL AND {_OPTIMIZATION}) "
        "OR (mode = 'ubsan' AND o2 IS NULL AND o0 IS NULL "
        "AND (state = 'observed') = (ubsan IS NOT NULL) "
        "AND (ubsan IS NULL OR jsonb_typeof(ubsan) = 'object'))) "
        f"AND {_FAILURE}",
    )
    op.execute(f"GRANT UPDATE (ubsan) ON {_TABLE} TO kf_oj_platform")


def downgrade():
    op.execute(f"""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM {_TABLE} WHERE mode = 'ubsan') THEN
                RAISE EXCEPTION 'cannot downgrade while UBSan diagnostics exist';
            END IF;
        END $$
    """)
    op.drop_constraint(op.f(_OUTCOME), _TABLE, type_="check")
    op.create_check_constraint(op.f(_OUTCOME), _TABLE, f"{_OPTIMIZATION} AND {_FAILURE}")
    op.drop_column(_TABLE, "ubsan")
    op.drop_column(_TABLE, "mode")
