"""Default-denied administrator authority and separate local operator/import roles."""

import sqlalchemy as sa
from alembic import op

revision = "0005_identity_authority"
down_revision = "0004_performance_watermark"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "accounts",
        sa.Column("is_administrator", sa.Boolean(), server_default="false", nullable=False),
    )
    op.add_column(
        "accounts",
        sa.Column("authorization_version", sa.BigInteger(), server_default="1", nullable=False),
    )
    op.create_check_constraint(
        op.f("ck_accounts_authorization_version"), "accounts", "authorization_version >= 1"
    )
    op.create_index(
        "ix_accounts_active_administrators",
        "accounts",
        ["id"],
        postgresql_where=sa.text("is_administrator AND enabled"),
    )
    # Logins and secrets are provisioned separately by an operator. Neither group
    # may inherit an existing privileged role or quietly reuse a privileged login.
    for role in ("kf_oj_identity_operator", "kf_oj_problem_import"):
        op.execute(f"""
            DO $$ BEGIN
                IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='{role}') THEN
                    CREATE ROLE {role} NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
                        NOREPLICATION NOBYPASSRLS;
                ELSIF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='{role}' AND
                    (rolcanlogin OR rolsuper OR rolcreatedb OR rolcreaterole
                        OR rolreplication OR rolbypassrls))
                    OR EXISTS (SELECT 1 FROM pg_auth_members m JOIN pg_roles r ON r.oid=m.member
                        WHERE r.rolname='{role}') THEN
                    RAISE EXCEPTION 'unsafe existing authority or import role';
                END IF;
            END $$
        """)
    op.execute(
        "REVOKE ALL ON ALL TABLES IN SCHEMA public "
        "FROM kf_oj_identity_operator,kf_oj_problem_import"
    )
    # Table REVOKE does not remove explicit column grants. Remove those too,
    # including grants left by an earlier downgrade or manual role provisioning.
    op.execute("""
        DO $$ DECLARE t record; BEGIN
            FOR t IN SELECT c.relname, string_agg(quote_ident(a.attname),',' ORDER BY a.attnum) cols
                FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
                JOIN pg_attribute a ON a.attrelid=c.oid
                WHERE n.nspname='public' AND c.relkind IN ('r','p','v','f')
                    AND a.attnum>0 AND NOT a.attisdropped
                GROUP BY c.oid,c.relname LOOP
                EXECUTE format('REVOKE SELECT (%s),INSERT (%s),UPDATE (%s),REFERENCES (%s) '
                    'ON TABLE public.%I FROM kf_oj_identity_operator,kf_oj_problem_import',
                    t.cols,t.cols,t.cols,t.cols,t.relname);
            END LOOP;
        END $$
    """)
    op.execute("GRANT USAGE ON SCHEMA public TO kf_oj_identity_operator,kf_oj_problem_import")
    op.execute("REVOKE INSERT,UPDATE,DELETE ON accounts FROM kf_oj_platform")
    op.execute(
        "REVOKE INSERT(id,username,password_hash,enabled,created_at,"
        "is_administrator,authorization_version),"
        "UPDATE(id,username,password_hash,enabled,created_at,"
        "is_administrator,authorization_version) "
        "ON accounts FROM kf_oj_platform"
    )
    op.execute(
        "GRANT INSERT(id,username,password_hash,enabled,created_at) ON accounts TO kf_oj_platform"
    )
    op.execute("GRANT UPDATE(enabled) ON accounts TO kf_oj_platform")
    op.execute("GRANT SELECT ON accounts,sessions TO kf_oj_identity_operator")
    op.execute("GRANT UPDATE(is_administrator,enabled) ON accounts TO kf_oj_identity_operator")
    op.execute("GRANT UPDATE(revoked_at) ON sessions TO kf_oj_identity_operator")
    op.execute("REVOKE ALL ON accounts,sessions FROM kf_oj_coordinator,kf_oj_problem_import")
    # No import queue exists yet. Its role deliberately has no table privileges
    # until the import migration grants only the queue/staging tables it needs.
    op.execute("""
        CREATE FUNCTION public.kf_account_authority_version() RETURNS trigger LANGUAGE plpgsql
        SET search_path = pg_catalog, public AS $$
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
    op.execute("""
        CREATE FUNCTION public.kf_revoke_authority_sessions() RETURNS trigger LANGUAGE plpgsql
        SET search_path = pg_catalog, public AS $$
        BEGIN
            IF NEW.authorization_version != OLD.authorization_version THEN
                UPDATE public.sessions SET revoked_at=coalesce(revoked_at,clock_timestamp())
                    WHERE account_id=NEW.id AND revoked_at IS NULL;
            END IF;
            RETURN NEW;
        END $$
    """)
    for function in ("kf_account_authority_version", "kf_revoke_authority_sessions"):
        op.execute(f"REVOKE ALL ON FUNCTION public.{function}() FROM PUBLIC")
    op.execute("""
        CREATE TRIGGER kf_account_authority_version BEFORE UPDATE ON public.accounts
        FOR EACH ROW EXECUTE FUNCTION public.kf_account_authority_version()
    """)
    op.execute("""
        CREATE TRIGGER kf_revoke_authority_sessions AFTER UPDATE ON public.accounts
        FOR EACH ROW EXECUTE FUNCTION public.kf_revoke_authority_sessions()
    """)


def downgrade() -> None:
    op.execute("""
        DO $$ BEGIN
            IF EXISTS(SELECT 1 FROM accounts
                WHERE is_administrator OR authorization_version != 1) THEN
                RAISE EXCEPTION 'cannot discard authority or revocation history'
                    USING ERRCODE='23514';
            END IF;
        END $$
    """)
    for name in ("kf_revoke_authority_sessions", "kf_account_authority_version"):
        op.execute(f"DROP TRIGGER {name} ON public.accounts")
        op.execute(f"DROP FUNCTION public.{name}()")
    op.execute("REVOKE ALL ON accounts,sessions FROM kf_oj_identity_operator")
    op.execute("REVOKE UPDATE(is_administrator,enabled) ON accounts FROM kf_oj_identity_operator")
    op.execute("REVOKE UPDATE(revoked_at) ON sessions FROM kf_oj_identity_operator")
    op.drop_index("ix_accounts_active_administrators", table_name="accounts")
    op.drop_constraint(op.f("ck_accounts_authorization_version"), "accounts", type_="check")
    op.drop_column("accounts", "authorization_version")
    op.drop_column("accounts", "is_administrator")
    op.execute("GRANT INSERT,UPDATE,DELETE ON accounts TO kf_oj_platform")
    # Shared NOLOGIN groups survive downgrade, as in the foundation migration;
    # removing a cluster role could break another database using the same groups.
