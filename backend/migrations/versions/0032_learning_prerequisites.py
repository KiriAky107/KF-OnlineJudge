"""Configure version-scoped prerequisites and project their owned AC evidence."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0032_learning_prerequisites"
down_revision = "0031_training_recency"
branch_labels = depends_on = None

TABLE = "problem_learning_metadata"
CONSTRAINT = "ck_problem_learning_metadata_prerequisite_tags"
SIGNATURE = "public.kf_learning_prerequisite_evidence(uuid,text[],timestamptz)"


def upgrade():
    op.add_column(
        TABLE,
        sa.Column(
            "prerequisite_tags",
            postgresql.JSONB(),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )
    op.create_check_constraint(
        op.f(CONSTRAINT),
        TABLE,
        "jsonb_typeof(prerequisite_tags) = 'array' AND jsonb_array_length(prerequisite_tags) <= 8",
    )
    op.execute(f"GRANT SELECT (prerequisite_tags) ON {TABLE} TO kf_oj_learning_worker")
    op.execute("""
        CREATE FUNCTION public.kf_learning_prerequisite_evidence(
            p_owner uuid, p_tags text[], p_as_of timestamptz
        ) RETURNS TABLE (
            tag text, submission_id uuid, evaluation_id uuid, problem_id uuid,
            problem_version_id uuid, submitted_at timestamptz, metadata_revision integer
        )
        LANGUAGE sql STABLE SET search_path=pg_catalog,pg_temp AS $$
            SELECT DISTINCT ON (labels.tag)
                labels.tag, s.id, e.id, s.problem_id, s.problem_version_id, s.created_at, m.revision
            FROM public.submissions AS s
            JOIN public.evaluations AS e ON e.submission_id=s.id
            JOIN public.problems AS p ON p.id=s.problem_id
            JOIN public.problem_versions AS v ON v.id=s.problem_version_id
            JOIN public.problem_learning_metadata AS m ON m.problem_version_id=v.id
            CROSS JOIN LATERAL jsonb_array_elements_text(m.tags) AS labels(tag)
            WHERE s.account_id=p_owner AND s.created_at<=p_as_of
              AND e.state='finished' AND e.verdict='accepted' AND e.failure_code IS NULL
              AND p.enabled AND v.published_at IS NOT NULL
              AND labels.tag=ANY(p_tags)
            ORDER BY labels.tag, s.created_at DESC, s.id DESC
        $$
    """)
    op.execute(f"REVOKE ALL ON FUNCTION {SIGNATURE} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {SIGNATURE} TO kf_oj_platform,kf_oj_learning_worker")


def downgrade():
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (
                SELECT 1 FROM public.problem_learning_metadata
                WHERE prerequisite_tags <> '[]'::jsonb
            ) THEN
                RAISE EXCEPTION 'cannot downgrade while authored learning prerequisites exist';
            END IF;
        END $$
    """)
    op.execute(f"DROP FUNCTION {SIGNATURE}")
    op.drop_constraint(op.f(CONSTRAINT), TABLE, type_="check")
    op.drop_column(TABLE, "prerequisite_tags")
