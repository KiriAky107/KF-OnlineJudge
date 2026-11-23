"""Project prior training dates without exposing the contents of saved plans."""

from alembic import op

revision = "0031_training_recency"
down_revision = "0030_skill_trials"
branch_labels = depends_on = None

SIGNATURE = "public.kf_recent_training_problems(uuid,uuid[],timestamptz,timestamptz)"


def upgrade():
    op.execute("""
        CREATE FUNCTION public.kf_recent_training_problems(
            p_owner uuid, p_problems uuid[], p_since timestamptz, p_as_of timestamptz
        ) RETURNS TABLE (problem_id uuid, last_planned_at timestamptz)
        LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
            SELECT i.problem_id, max(p.created_at)
            FROM public.learning_plans AS p
            JOIN public.learning_plan_items AS i ON i.plan_id=p.id
            WHERE p.owner_id=p_owner AND i.problem_id=ANY(p_problems)
              AND p.created_at BETWEEN p_since AND p_as_of
            GROUP BY i.problem_id
        $$
    """)
    op.execute(f"REVOKE ALL ON FUNCTION {SIGNATURE} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {SIGNATURE} TO kf_oj_platform,kf_oj_learning_worker")


def downgrade():
    op.execute(f"DROP FUNCTION {SIGNATURE}")
