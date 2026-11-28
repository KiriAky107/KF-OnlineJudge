"""Account for the optional second provider request in a review turn."""

import sqlalchemy as sa
from alembic import op

revision = "0053_learning_review_mcp"
down_revision = "0052_learning_diagnostic_queue"
branch_labels = depends_on = None

_TABLE = "learning_agent_model_attempts"
_PUBLIC_CONTEXT = "public.kf_learning_review_problem(uuid)"


def upgrade():
    # Revision 0029 uses the current table model on a clean installation.
    op.add_column(
        _TABLE,
        sa.Column(
            "provider_requests",
            sa.Integer(),
            sa.CheckConstraint("provider_requests BETWEEN 1 AND 2", name="provider_requests"),
            nullable=False,
            server_default="1",
        ),
        if_not_exists=True,
    )
    op.execute(f"GRANT UPDATE (provider_requests) ON {_TABLE} TO kf_oj_learning_worker")
    op.execute("""
        CREATE FUNCTION public.kf_learning_review_problem(version_id uuid) RETURNS jsonb
        LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
            SELECT jsonb_build_object(
                'title', v.title,
                'statement', left(v.statement, 3200),
                'statement_truncated', char_length(v.statement) > 3200,
                'public_sample_count', jsonb_array_length(v.public_samples)
            )
            FROM public.problem_versions AS v
            JOIN public.problems AS p ON p.id = v.problem_id
            WHERE v.id = version_id AND v.published_at IS NOT NULL AND p.enabled
        $$
    """)
    op.execute(f"REVOKE ALL ON FUNCTION {_PUBLIC_CONTEXT} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {_PUBLIC_CONTEXT} TO kf_oj_learning_worker")


def downgrade():
    op.execute(f"DROP FUNCTION {_PUBLIC_CONTEXT}")
    op.drop_column(_TABLE, "provider_requests")
