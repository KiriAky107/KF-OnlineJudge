"""Immutable maintenance receipts, explicit operator audit and read-only history."""

import sqlalchemy as sa
from alembic import op

from kf_oj.modules.problems.maintenance_models import ProblemMaintenanceOperation

revision = "0008_problem_maintenance"
down_revision = "0007_problem_imports"
branch_labels = depends_on = None
OWNER = "kf_oj_identity_authority_owner"


def upgrade():
    ProblemMaintenanceOperation.__table__.create(op.get_bind())
    op.execute("GRANT SELECT,INSERT ON problem_maintenance_operations TO kf_oj_platform")
    op.add_column(
        "problem_maintenance_events",
        sa.Column("actor_kind", sa.String(8), nullable=False, server_default=sa.text("'account'")),
    )
    op.add_column("problem_maintenance_events", sa.Column("operator_name", sa.String(63)))
    op.alter_column("problem_maintenance_events", "actor_id", nullable=True)
    op.drop_constraint(op.f("ck_problem_maintenance_events_action"), "problem_maintenance_events")
    op.create_check_constraint(
        "action",
        "problem_maintenance_events",
        "action IN ('upload_created','upload_received','import_ready','import_failed',"
        "'import_discarded','import_expired','published','disabled','restored',"
        "'authority_bootstrap','authority_grant','authority_revoke','authority_enable','authority_disable')",
    )
    op.create_check_constraint(
        "actor",
        "problem_maintenance_events",
        "(actor_kind='account' AND actor_id IS NOT NULL AND operator_name IS NULL) OR "
        "(actor_kind='operator' AND actor_id IS NULL AND operator_name IS NOT NULL)",
    )
    op.execute("""CREATE FUNCTION public.kf_append_only_maintenance() RETURNS trigger
        LANGUAGE plpgsql SET search_path=pg_catalog,pg_temp AS $$ BEGIN
        RAISE EXCEPTION 'append-only maintenance history' USING ERRCODE='23514'; END $$""")
    for table in ("problem_maintenance_operations", "problem_maintenance_events"):
        op.execute(f"""CREATE TRIGGER append_only_maintenance BEFORE UPDATE OR DELETE ON {table}
                       FOR EACH ROW EXECUTE FUNCTION public.kf_append_only_maintenance()""")
    # Preserve the already-verified authority implementation; its only entry
    # point becomes a fixed audited wrapper owned by the same narrow NOLOGIN role.
    op.execute(
        "ALTER FUNCTION public.kf_manage_account_authority(text,text) "
        "RENAME TO kf_manage_account_authority_unlogged"
    )
    op.execute(
        "REVOKE ALL ON FUNCTION public.kf_manage_account_authority_unlogged(text,text) "
        "FROM PUBLIC,kf_oj_identity_operator"
    )
    op.execute(f"GRANT INSERT ON problem_maintenance_events TO {OWNER}")
    op.execute("""
        CREATE FUNCTION public.kf_manage_account_authority(p_username text,p_operation text)
        RETURNS TABLE(account_id uuid,username text,enabled boolean,is_administrator boolean,
            authorization_version bigint,changed boolean)
        LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
        DECLARE result record; BEGIN
            SELECT * INTO STRICT result FROM
                public.kf_manage_account_authority_unlogged(p_username,p_operation);
            IF result.changed THEN
                INSERT INTO public.problem_maintenance_events
                    (id,created_at,actor_id,actor_kind,operator_name,authorization_version,
                     operation_id,action,target_id)
                VALUES (gen_random_uuid(),clock_timestamp(),NULL,'operator',session_user,
                        result.authorization_version,gen_random_uuid(),'authority_' || p_operation,
                        result.account_id);
            END IF;
            RETURN QUERY SELECT result.account_id,result.username,result.enabled,
                result.is_administrator,result.authorization_version,result.changed;
        END $$""")
    op.execute(f"GRANT CREATE ON SCHEMA public TO {OWNER}")
    op.execute(f"ALTER FUNCTION public.kf_manage_account_authority(text,text) OWNER TO {OWNER}")
    op.execute(f"REVOKE CREATE ON SCHEMA public FROM {OWNER}")
    op.execute("REVOKE ALL ON FUNCTION public.kf_manage_account_authority(text,text) FROM PUBLIC")
    op.execute(
        "GRANT EXECUTE ON FUNCTION public.kf_manage_account_authority(text,text) "
        "TO kf_oj_identity_operator"
    )


def downgrade():
    if (
        op.get_bind()
        .exec_driver_sql(
            "SELECT EXISTS(SELECT 1 FROM problem_maintenance_operations) OR "
            "EXISTS(SELECT 1 FROM problem_maintenance_events WHERE actor_kind='operator')"
        )
        .scalar()
    ):
        raise RuntimeError("cannot discard committed maintenance history")
    op.execute("DROP FUNCTION public.kf_manage_account_authority(text,text)")
    op.execute(
        "ALTER FUNCTION public.kf_manage_account_authority_unlogged(text,text) "
        "RENAME TO kf_manage_account_authority"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION public.kf_manage_account_authority(text,text) "
        "TO kf_oj_identity_operator"
    )
    op.execute(f"REVOKE INSERT ON problem_maintenance_events FROM {OWNER}")
    for table in ("problem_maintenance_operations", "problem_maintenance_events"):
        op.execute(f"DROP TRIGGER append_only_maintenance ON {table}")
    op.execute("DROP FUNCTION public.kf_append_only_maintenance()")
    ProblemMaintenanceOperation.__table__.drop(op.get_bind())
    op.drop_constraint(op.f("ck_problem_maintenance_events_actor"), "problem_maintenance_events")
    op.drop_constraint(op.f("ck_problem_maintenance_events_action"), "problem_maintenance_events")
    op.create_check_constraint(
        "action",
        "problem_maintenance_events",
        "action IN ('upload_created','upload_received','import_ready','import_failed',"
        "'import_discarded','import_expired','published','disabled','restored')",
    )
    op.alter_column("problem_maintenance_events", "actor_id", nullable=False)
    op.drop_column("problem_maintenance_events", "operator_name")
    op.drop_column("problem_maintenance_events", "actor_kind")
