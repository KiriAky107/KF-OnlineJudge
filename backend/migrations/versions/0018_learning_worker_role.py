"""Restrict the learning worker to its queue and public learning projections."""

from alembic import op

revision = "0018_learning_worker_role"
down_revision = "0017_learning_checkpoints"
branch_labels = depends_on = None

ROLE = "kf_oj_learning_worker"
QUEUE_TABLES = (
    "learning_agent_sessions, learning_agent_runs, learning_agent_steps, "
    "learning_agent_tool_attempts, learning_agent_workflow_data, learning_agent_tasks, "
    "learning_agent_checkpoints"
)


def upgrade():
    op.execute(f"""
        DO $$ BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='{ROLE}') THEN
                CREATE ROLE {ROLE} NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
            END IF;
        END $$
    """)
    op.execute(f"GRANT USAGE ON SCHEMA public TO {ROLE}")
    op.execute(
        f"GRANT SELECT ON {QUEUE_TABLES}, skills_bindings, skills_run_configurations TO {ROLE}"
    )
    op.execute(
        f"GRANT INSERT ON learning_agent_steps, learning_agent_checkpoints, "
        f"learning_agent_tool_attempts TO {ROLE}"
    )
    op.execute(f"GRANT UPDATE (state, revision) ON learning_agent_runs TO {ROLE}")
    op.execute(f"GRANT UPDATE (state, result_digest, error_code) ON learning_agent_steps TO {ROLE}")
    op.execute(f"GRANT UPDATE (finished_at) ON learning_agent_tool_attempts TO {ROLE}")
    op.execute(f"GRANT UPDATE (result, failure_code) ON learning_agent_workflow_data TO {ROLE}")
    op.execute(
        f"GRANT UPDATE (attempts, available_at, lease_expires_at) ON learning_agent_tasks TO {ROLE}"
    )

    op.execute(f"GRANT SELECT (id, slug, enabled, active_version_id) ON problems TO {ROLE}")
    op.execute(f"GRANT SELECT (id, problem_id, title, published_at) ON problem_versions TO {ROLE}")
    op.execute(
        f"GRANT SELECT (problem_version_id, tags, difficulty, revision) "
        f"ON problem_learning_metadata TO {ROLE}"
    )
    op.execute(
        f"GRANT SELECT (id, account_id, problem_id, problem_version_id, created_at) "
        f"ON submissions TO {ROLE}"
    )
    op.execute(
        f"GRANT SELECT (id, submission_id, state, verdict, failure_code) ON evaluations TO {ROLE}"
    )

    op.execute("""
        CREATE FUNCTION public.kf_lock_agent_session(owner uuid, epoch bigint, proof text)
        RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
        BEGIN
            PERFORM 1 FROM public.accounts AS a
            WHERE a.id=owner AND a.enabled AND a.authorization_version=epoch FOR SHARE;
            IF NOT FOUND THEN RETURN false; END IF;
            PERFORM 1 FROM public.sessions AS s
            WHERE s.account_id=owner AND s.revoked_at IS NULL AND s.expires_at>clock_timestamp()
                AND encode(sha256(convert_to(s.token_hash, 'UTF8')), 'hex')=proof
            FOR SHARE;
            RETURN FOUND;
        END
        $$
    """)
    op.execute("""
        CREATE FUNCTION public.kf_lock_agent_skill(owner uuid, skill_slug text)
        RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
        BEGIN
            PERFORM 1 FROM public.skills_bindings AS b
            WHERE b.owner_id=owner AND b.slug=skill_slug AND b.enabled FOR SHARE;
            RETURN FOUND;
        END
        $$
    """)
    for signature, allowed in (
        ("kf_lock_agent_session(uuid,bigint,text)", ROLE),
        ("kf_lock_agent_skill(uuid,text)", f"{ROLE},kf_oj_platform"),
    ):
        op.execute(f"REVOKE ALL ON FUNCTION public.{signature} FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION public.{signature} TO {allowed}")


def downgrade():
    op.execute("DROP FUNCTION public.kf_lock_agent_session(uuid,bigint,text)")
    op.execute("DROP FUNCTION public.kf_lock_agent_skill(uuid,text)")
    op.execute(
        f"REVOKE ALL ON {QUEUE_TABLES}, skills_bindings, skills_run_configurations FROM {ROLE}"
    )
    op.execute(
        f"REVOKE ALL PRIVILEGES (id, slug, enabled, active_version_id) ON problems FROM {ROLE}"
    )
    op.execute(
        "REVOKE ALL PRIVILEGES (id, problem_id, title, published_at) "
        f"ON problem_versions FROM {ROLE}"
    )
    op.execute(
        f"REVOKE ALL PRIVILEGES (problem_version_id, tags, difficulty, revision) "
        f"ON problem_learning_metadata FROM {ROLE}"
    )
    op.execute(
        f"REVOKE ALL PRIVILEGES (id, account_id, problem_id, problem_version_id, created_at) "
        f"ON submissions FROM {ROLE}"
    )
    op.execute(
        f"REVOKE ALL PRIVILEGES (id, submission_id, state, verdict, failure_code) "
        f"ON evaluations FROM {ROLE}"
    )
    op.execute(f"REVOKE USAGE ON SCHEMA public FROM {ROLE}")
