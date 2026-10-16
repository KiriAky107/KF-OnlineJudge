"""Fixed local authority operations and serialized last-administrator protection."""

import sqlalchemy as sa
from alembic import op

revision = "0006_authority_operations"
down_revision = "0005_identity_authority"
branch_labels = None
depends_on = None

OWNER = "kf_oj_identity_authority_owner"


def upgrade() -> None:
    op.execute(f"""
        DO $$ BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='{OWNER}') THEN
                CREATE ROLE {OWNER} NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
                    NOREPLICATION NOBYPASSRLS;
            ELSIF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='{OWNER}' AND
                (rolcanlogin OR rolsuper OR rolcreatedb OR rolcreaterole
                    OR rolreplication OR rolbypassrls))
                OR EXISTS (SELECT 1 FROM pg_auth_members m JOIN pg_roles r ON r.oid=m.member
                    WHERE r.rolname='{OWNER}') THEN
                RAISE EXCEPTION 'unsafe existing authority function owner';
            END IF;
        END $$
    """)
    op.create_table(
        "identity_authority_guard",
        sa.Column("id", sa.SmallInteger(), primary_key=True),
        sa.Column("generation", sa.BigInteger(), nullable=False, server_default="0"),
        sa.CheckConstraint("id = 1", name="singleton"),
        sa.CheckConstraint("generation >= 0", name="generation"),
    )
    op.execute("INSERT INTO public.identity_authority_guard(id) VALUES(1)")
    for role in (OWNER, "kf_oj_identity_operator"):
        op.execute(f"REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {role}")
        op.execute(f"""
            REVOKE SELECT(id,username,password_hash,enabled,created_at,
                is_administrator,authorization_version),
                UPDATE(id,username,password_hash,enabled,created_at,
                is_administrator,authorization_version) ON public.accounts FROM {role}
        """)
        op.execute(f"""
            REVOKE SELECT(token_hash,account_id,csrf_hash,expires_at,revoked_at,created_at),
                UPDATE(token_hash,account_id,csrf_hash,expires_at,revoked_at,created_at)
                ON public.sessions FROM {role}
        """)
    op.execute(f"GRANT USAGE ON SCHEMA public TO {OWNER}")
    op.execute(f"""
        GRANT SELECT(id,username,enabled,is_administrator,authorization_version),
            UPDATE(enabled,is_administrator) ON public.accounts TO {OWNER}
    """)
    op.execute(f"GRANT SELECT(account_id,revoked_at),UPDATE(revoked_at) ON sessions TO {OWNER}")
    op.execute(f"GRANT SELECT,UPDATE(generation) ON identity_authority_guard TO {OWNER}")
    # Every account write obtains the guard BEFORE it can lock an account row.
    # Updating (rather than merely locking) the singleton also makes an old
    # REPEATABLE READ/SERIALIZABLE snapshot fail safely with a serialization error.
    op.execute("""
        CREATE FUNCTION public.kf_serialize_account_authority() RETURNS trigger
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
        BEGIN
            UPDATE public.identity_authority_guard SET generation=generation+1 WHERE id=1;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'authority guard unavailable' USING ERRCODE='23514';
            END IF;
            RETURN NULL;
        END $$
    """)
    op.execute("""
        CREATE FUNCTION public.kf_protect_last_administrator() RETURNS trigger
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
        BEGIN
            IF OLD.is_administrator AND OLD.enabled AND
                (TG_OP='DELETE' OR NOT (NEW.is_administrator AND NEW.enabled)) AND
                (SELECT count(*) FROM public.accounts WHERE is_administrator AND enabled) <= 1
            THEN
                RAISE EXCEPTION 'last enabled administrator is protected' USING ERRCODE='23514';
            END IF;
            IF TG_OP='DELETE' THEN RETURN OLD; END IF;
            RETURN NEW;
        END $$
    """)
    op.execute("""
        CREATE OR REPLACE FUNCTION public.kf_account_authority_version() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog, public AS $$
        BEGIN
            IF NEW.authorization_version IS DISTINCT FROM OLD.authorization_version THEN
                RAISE EXCEPTION 'authorization version is server derived' USING ERRCODE='23514';
            END IF;
            IF OLD.is_administrator AND NEW.enabled IS DISTINCT FROM OLD.enabled
                AND current_user != 'kf_oj_identity_authority_owner'
                AND NOT pg_has_role(current_user,'kf_oj_identity_operator','USAGE') THEN
                RAISE EXCEPTION 'administrator enablement requires the local operator'
                    USING ERRCODE='42501';
            END IF;
            IF NEW.is_administrator IS DISTINCT FROM OLD.is_administrator
                OR NEW.enabled IS DISTINCT FROM OLD.enabled THEN
                NEW.authorization_version := OLD.authorization_version + 1;
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute("""
        CREATE FUNCTION public.kf_manage_account_authority(p_username text,p_operation text)
        RETURNS TABLE(account_id uuid,username text,enabled boolean,is_administrator boolean,
            authorization_version bigint,changed boolean)
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
        DECLARE target record; desired_admin boolean; desired_enabled boolean; active bigint;
        BEGIN
            IF p_username IS NULL OR p_username !~ '^[a-z0-9_]{3,24}$' OR
                p_operation IS NULL OR
                p_operation NOT IN ('bootstrap','grant','revoke','enable','disable') THEN
                RAISE EXCEPTION 'invalid authority operation' USING ERRCODE='22023';
            END IF;
            UPDATE public.identity_authority_guard g SET generation=g.generation+1 WHERE g.id=1;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'authority guard unavailable' USING ERRCODE='23514';
            END IF;
            SELECT a.id,a.username,a.enabled,a.is_administrator,a.authorization_version
                INTO target FROM public.accounts a WHERE a.username=p_username FOR UPDATE;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'account not found' USING ERRCODE='P0002';
            END IF;
            desired_admin:=target.is_administrator; desired_enabled:=target.enabled;
            SELECT count(*) INTO active FROM public.accounts a
                WHERE a.is_administrator AND a.enabled;
            IF p_operation='bootstrap' THEN
                IF active>0 AND NOT (target.is_administrator AND target.enabled) THEN
                    RAISE EXCEPTION 'bootstrap requires no enabled administrator'
                        USING ERRCODE='23514';
                END IF;
                desired_admin:=true; desired_enabled:=true;
            ELSIF p_operation='grant' THEN
                IF active=0 THEN
                    RAISE EXCEPTION 'first administrator requires explicit bootstrap'
                        USING ERRCODE='23514';
                END IF;
                IF NOT target.enabled THEN
                    RAISE EXCEPTION 'enable the account before granting authority'
                        USING ERRCODE='23514';
                END IF;
                desired_admin:=true;
            ELSIF p_operation='revoke' THEN desired_admin:=false;
            ELSIF p_operation='enable' THEN desired_enabled:=true;
            ELSE desired_enabled:=false;
            END IF;
            changed:=desired_admin IS DISTINCT FROM target.is_administrator OR
                desired_enabled IS DISTINCT FROM target.enabled;
            IF changed THEN
                UPDATE public.accounts a SET enabled=desired_enabled,is_administrator=desired_admin
                    WHERE a.id=target.id;
            END IF;
            RETURN QUERY SELECT a.id,a.username::text,a.enabled,a.is_administrator,
                a.authorization_version,changed FROM public.accounts a WHERE a.id=target.id;
        END $$
    """)
    # A narrow NOLOGIN owner holds only these column privileges. The operator
    # receives EXECUTE, never table writes or access to password/session digests.
    op.execute(f"GRANT CREATE ON SCHEMA public TO {OWNER}")
    for signature in (
        "kf_serialize_account_authority()",
        "kf_protect_last_administrator()",
        "kf_manage_account_authority(text,text)",
    ):
        op.execute(f"ALTER FUNCTION public.{signature} OWNER TO {OWNER}")
        op.execute(f"REVOKE ALL ON FUNCTION public.{signature} FROM PUBLIC")
    op.execute(f"REVOKE CREATE ON SCHEMA public FROM {OWNER}")
    op.execute("""
        GRANT EXECUTE ON FUNCTION public.kf_manage_account_authority(text,text)
            TO kf_oj_identity_operator
    """)
    op.execute("""
        CREATE TRIGGER kf_serialize_account_authority BEFORE INSERT OR UPDATE OR DELETE
        ON public.accounts FOR EACH STATEMENT
        EXECUTE FUNCTION public.kf_serialize_account_authority()
    """)
    op.execute("""
        CREATE TRIGGER kf_protect_last_administrator BEFORE UPDATE OR DELETE
        ON public.accounts FOR EACH ROW EXECUTE FUNCTION public.kf_protect_last_administrator()
    """)


def downgrade() -> None:
    op.execute("""
        DO $$ BEGIN
            IF EXISTS(SELECT 1 FROM public.accounts WHERE is_administrator) THEN
                RAISE EXCEPTION 'cannot remove active authority safeguards' USING ERRCODE='23514';
            END IF;
        END $$
    """)
    for name in ("kf_protect_last_administrator", "kf_serialize_account_authority"):
        op.execute(f"DROP TRIGGER {name} ON public.accounts")
        op.execute(f"DROP FUNCTION public.{name}()")
    op.execute("DROP FUNCTION public.kf_manage_account_authority(text,text)")
    op.drop_table("identity_authority_guard")
    op.execute(f"REVOKE ALL ON accounts,sessions FROM {OWNER}")
    op.execute(f"""
        REVOKE SELECT(id,username,enabled,is_administrator,authorization_version),
            UPDATE(enabled,is_administrator) ON accounts FROM {OWNER}
    """)
    op.execute(f"REVOKE SELECT(account_id,revoked_at),UPDATE(revoked_at) ON sessions FROM {OWNER}")
    op.execute("GRANT SELECT ON accounts,sessions TO kf_oj_identity_operator")
    op.execute("GRANT UPDATE(is_administrator,enabled) ON accounts TO kf_oj_identity_operator")
    op.execute("GRANT UPDATE(revoked_at) ON sessions TO kf_oj_identity_operator")
    # Preserve the v0005 trigger policy without referring to the function owner.
    op.execute("""
        CREATE OR REPLACE FUNCTION public.kf_account_authority_version() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog, public AS $$
        BEGIN
            IF NEW.authorization_version IS DISTINCT FROM OLD.authorization_version THEN
                RAISE EXCEPTION 'authorization version is server derived' USING ERRCODE='23514';
            END IF;
            IF OLD.is_administrator AND NEW.enabled IS DISTINCT FROM OLD.enabled
                AND NOT pg_has_role(current_user,'kf_oj_identity_operator','USAGE') THEN
                RAISE EXCEPTION 'administrator enablement requires the local operator'
                    USING ERRCODE='42501';
            END IF;
            IF NEW.is_administrator IS DISTINCT FROM OLD.is_administrator
                OR NEW.enabled IS DISTINCT FROM OLD.enabled THEN
                NEW.authorization_version := OLD.authorization_version + 1;
            END IF;
            RETURN NEW;
        END $$
    """)
