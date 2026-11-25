"""Per-training hint cap and read-only policy checks for the Learning Worker."""

import sqlalchemy as sa
from alembic import op

revision = "0049_learning_hint_ceiling"
down_revision = "0048_learning_problem_notes"
branch_labels = depends_on = None

_ROLE_PROBE = "public.kf_agent_session_administrator(uuid,bigint,text)"
_TRAINING_CAP = "public.kf_training_hint_ceiling(uuid,uuid,uuid)"


def upgrade():
    op.add_column(
        "learning_plans",
        sa.Column("max_hint_level", sa.String(2), nullable=False, server_default="L3"),
    )
    op.create_check_constraint(
        "hint_ceiling", "learning_plans", "max_hint_level IN ('L0','L1','L2','L3')"
    )
    op.execute("GRANT UPDATE (max_hint_level) ON learning_plans TO kf_oj_platform")
    op.execute("""
        CREATE FUNCTION public.kf_agent_session_administrator(actor uuid, epoch bigint, proof text)
        RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
        BEGIN
            IF NOT public.kf_lock_agent_session(actor, epoch, proof) THEN RETURN NULL; END IF;
            RETURN (SELECT a.is_administrator FROM public.accounts AS a WHERE a.id=actor);
        END
        $$
    """)
    op.execute("""
        CREATE FUNCTION public.kf_training_hint_ceiling(actor uuid, plan uuid, version uuid)
        RETURNS text LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
            SELECT p.max_hint_level::text FROM public.learning_plans AS p
            JOIN public.learning_plan_items AS i ON i.plan_id=p.id
            WHERE p.id=plan AND p.owner_id=actor AND i.version_id=version
            FOR SHARE OF p, i
        $$
    """)
    for signature, roles in (
        (_ROLE_PROBE, "kf_oj_learning_worker"),
        (_TRAINING_CAP, "kf_oj_platform,kf_oj_learning_worker"),
    ):
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO {roles}")


def downgrade():
    op.execute(f"DROP FUNCTION {_TRAINING_CAP}")
    op.execute(f"DROP FUNCTION {_ROLE_PROBE}")
    op.execute("REVOKE UPDATE (max_hint_level) ON learning_plans FROM kf_oj_platform")
    op.drop_constraint("hint_ceiling", "learning_plans", type_="check")
    op.drop_column("learning_plans", "max_hint_level")
