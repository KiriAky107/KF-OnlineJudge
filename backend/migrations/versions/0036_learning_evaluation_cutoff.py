"""Filter owned learning evidence by the recorded evaluation update time."""

from alembic import op

revision = "0036_learning_evaluation_cutoff"
down_revision = "0035_learning_model_timing_read"
branch_labels = depends_on = None


def _replace_prerequisite_projection(*, apply_cutoff: bool) -> None:
    cutoff = "AND e.updated_at<=p_as_of" if apply_cutoff else ""
    op.execute(f"""
        CREATE OR REPLACE FUNCTION public.kf_learning_prerequisite_evidence(
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
              {cutoff}
              AND p.enabled AND v.published_at IS NOT NULL
              AND labels.tag=ANY(p_tags)
            ORDER BY labels.tag, s.created_at DESC, s.id DESC
        $$
    """)


def upgrade():
    op.execute("GRANT SELECT (updated_at) ON public.evaluations TO kf_oj_learning_worker")
    _replace_prerequisite_projection(apply_cutoff=True)


def downgrade():
    _replace_prerequisite_projection(apply_cutoff=False)
    op.execute("REVOKE SELECT (updated_at) ON public.evaluations FROM kf_oj_learning_worker")
