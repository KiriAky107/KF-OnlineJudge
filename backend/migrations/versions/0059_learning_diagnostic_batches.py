"""Store registered batches in the existing diagnostic execution queue."""

import sqlalchemy as sa
from alembic import op

revision = "0059_learning_diagnostic_batches"
down_revision = "0057_guided_evaluation_budget"
branch_labels = depends_on = None

_TABLE = "learning_diagnostic_requests"
_SAMPLE_FIELDS = (
    "sample_ordinal",
    "input_digest",
    "input_bytes",
    "expected_digest",
    "expected_bytes",
)
_DIGESTS = (
    "session_proof ~ '^[0-9a-f]{64}$' AND source_digest ~ '^[0-9a-f]{64}$' "
    "AND input_digest ~ '^[0-9a-f]{64}$' AND expected_digest ~ '^[0-9a-f]{64}$'"
)
_SAMPLE_OUTCOME = (
    "(mode = 'optimization' AND ubsan IS NULL "
    "AND (state = 'observed') = (o2 IS NOT NULL) "
    "AND (o2 IS NOT NULL AND o2 IN ('wrong_answer', 'runtime_error')) = (o0 IS NOT NULL)) "
    "OR (mode = 'ubsan' AND o2 IS NULL AND o0 IS NULL "
    "AND (state = 'observed') = (ubsan IS NOT NULL) "
    "AND (ubsan IS NULL OR jsonb_typeof(ubsan) = 'object'))"
)
_BATCH_OUTCOME = (
    "mode = 'batch' AND o2 IS NULL AND o0 IS NULL AND ubsan IS NULL "
    "AND (state = 'observed') = (batch_outcome IS NOT NULL) "
    "AND (batch_outcome IS NULL OR batch_outcome IN "
    "('input_not_verified', 'runtime_error', 'resource_limit') "
    "OR (batch_purpose = 'boundary' AND batch_outcome = 'completed') "
    "OR (batch_purpose = 'differential' AND batch_outcome IN "
    "('reference_pending', 'reduction_verified', 'reduction_unverified', "
    "'accepted', 'compilation_error', 'wrong_answer')))"
)
_FAILURE = "(state = 'failed') = (failure_code IS NOT NULL)"


def _drop(name: str):
    op.drop_constraint(op.f(f"ck_{_TABLE}_{name}"), _TABLE, type_="check")


def _check(name: str, condition: str):
    op.create_check_constraint(op.f(f"ck_{_TABLE}_{name}"), _TABLE, condition)


def upgrade():
    for name, length in (
        ("batch_manifest_digest", 64),
        ("batch_purpose", 12),
        ("batch_outcome", 24),
    ):
        op.add_column(_TABLE, sa.Column(name, sa.String(length)))
    for column in _SAMPLE_FIELDS:
        op.alter_column(_TABLE, column, nullable=True)
    for name in ("sample", "digests", "outcome"):
        _drop(name)
    _check("digests", f"{_DIGESTS} AND batch_manifest_digest ~ '^[0-9a-f]{{64}}$'")
    _check(
        "input_kind",
        "(mode IN ('optimization', 'ubsan') AND "
        + " AND ".join(f"{column} IS NOT NULL" for column in _SAMPLE_FIELDS)
        + " AND sample_ordinal >= 0 AND batch_manifest_digest IS NULL AND batch_purpose IS NULL) "
        "OR (mode = 'batch' AND "
        + " AND ".join(f"{column} IS NULL" for column in _SAMPLE_FIELDS)
        + " AND batch_manifest_digest IS NOT NULL AND batch_purpose IS NOT NULL "
        "AND batch_purpose IN ('differential', 'boundary'))",
    )
    _check(
        "outcome",
        f"((({_SAMPLE_OUTCOME}) AND batch_outcome IS NULL) OR ({_BATCH_OUTCOME})) AND {_FAILURE}",
    )
    op.execute(f"GRANT UPDATE (batch_outcome) ON {_TABLE} TO kf_oj_platform")


def downgrade():
    op.execute(f"""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM {_TABLE} WHERE mode = 'batch') THEN
                RAISE EXCEPTION 'cannot downgrade while registered batch diagnostics exist';
            END IF;
        END $$
    """)
    for name in ("input_kind", "digests", "outcome"):
        _drop(name)
    _check("sample", "sample_ordinal >= 0")
    _check("digests", _DIGESTS)
    _check("outcome", f"({_SAMPLE_OUTCOME}) AND {_FAILURE}")
    for column in _SAMPLE_FIELDS:
        op.alter_column(_TABLE, column, nullable=False)
    for column in ("batch_outcome", "batch_purpose", "batch_manifest_digest"):
        op.drop_column(_TABLE, column)
