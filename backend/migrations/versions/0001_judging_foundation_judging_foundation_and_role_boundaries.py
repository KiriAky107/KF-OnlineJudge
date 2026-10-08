"""judging foundation and role boundaries

Revision ID: 0001_judging_foundation
Revises:
Create Date: 2026-10-09 00:57:34.004943
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_judging_foundation"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def install_guards_and_permissions() -> None:
    # Serialize case insertion against the one-way publication transition.
    op.execute("""
        CREATE FUNCTION public.kf_immutable_case() RETURNS trigger LANGUAGE plpgsql
        SET search_path = pg_catalog, public AS $$
        DECLARE published timestamptz;
        BEGIN
            IF TG_OP = 'UPDATE' THEN
                RAISE EXCEPTION 'test cases are immutable' USING ERRCODE = '23514';
            END IF;
            SELECT published_at INTO published FROM public.problem_versions
                WHERE id = CASE WHEN TG_OP = 'DELETE' THEN OLD.version_id ELSE NEW.version_id END
                FOR UPDATE;
            IF published IS NOT NULL THEN
                RAISE EXCEPTION 'published test cases are immutable' USING ERRCODE = '23514';
            END IF;
            IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
            RETURN NEW;
        END $$
    """)
    op.execute("""
        CREATE TRIGGER kf_immutable_case BEFORE INSERT OR UPDATE OR DELETE ON public.problem_cases
        FOR EACH ROW EXECUTE FUNCTION public.kf_immutable_case()
    """)
    op.execute("""
        CREATE FUNCTION public.kf_immutable_version() RETURNS trigger LANGUAGE plpgsql
        SET search_path = pg_catalog, public AS $$
        DECLARE count_cases integer; last_position integer;
        BEGIN
            IF TG_OP = 'INSERT' THEN
                IF NEW.published_at IS NOT NULL THEN
                    RAISE EXCEPTION 'publish after inserting cases' USING ERRCODE = '23514';
                END IF;
                RETURN NEW;
            END IF;
            IF TG_OP = 'DELETE' THEN
                IF OLD.published_at IS NOT NULL THEN
                    RAISE EXCEPTION 'published version is immutable' USING ERRCODE = '23514';
                END IF;
                RETURN OLD;
            END IF;
            IF OLD.published_at IS NOT NULL OR NEW.published_at IS NULL
                OR (to_jsonb(NEW) - 'published_at') != (to_jsonb(OLD) - 'published_at') THEN
                RAISE EXCEPTION 'version may only be published once' USING ERRCODE = '23514';
            END IF;
            SELECT count(*), max(position) INTO count_cases, last_position
                FROM public.problem_cases WHERE version_id = OLD.id;
            IF count_cases NOT BETWEEN 1 AND 256 OR last_position != count_cases - 1 THEN
                RAISE EXCEPTION 'publication requires contiguous ordered cases'
                    USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute("""
        CREATE TRIGGER kf_immutable_version BEFORE INSERT OR UPDATE OR DELETE
        ON public.problem_versions
        FOR EACH ROW EXECUTE FUNCTION public.kf_immutable_version()
    """)
    op.execute("REVOKE ALL ON FUNCTION public.kf_immutable_case() FROM PUBLIC")
    op.execute("REVOKE ALL ON FUNCTION public.kf_immutable_version() FROM PUBLIC")
    # Migration owner needs CREATEROLE; runtime logins are separate, unprivileged group members.
    for role in ("kf_oj_platform", "kf_oj_coordinator"):
        op.execute(f"""
            DO $$ BEGIN
                IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN
                    CREATE ROLE {role} NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
                ELSIF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}' AND
                    (rolcanlogin OR rolsuper OR rolcreatedb OR rolcreaterole OR rolbypassrls))
                    OR EXISTS (SELECT 1 FROM pg_auth_members m JOIN pg_roles r ON r.oid=m.member
                        WHERE r.rolname='{role}') THEN
                    RAISE EXCEPTION 'unsafe existing runtime role';
                END IF;
            END $$
        """)
    tables = (
        "accounts,sessions,login_throttles,artifacts,problems,problem_versions,problem_cases,"
        "submissions,evaluations,judge_attempts,case_results,outbox"
    )
    op.execute(f"REVOKE ALL ON {tables} FROM PUBLIC, kf_oj_platform, kf_oj_coordinator")
    op.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
    op.execute("GRANT USAGE ON SCHEMA public TO kf_oj_platform, kf_oj_coordinator")
    op.execute(f"GRANT SELECT ON {tables} TO kf_oj_platform")
    op.execute("GRANT INSERT,UPDATE,DELETE ON accounts,sessions,login_throttles TO kf_oj_platform")
    op.execute("GRANT INSERT,SELECT,DELETE ON artifacts TO kf_oj_platform")
    op.execute("GRANT UPDATE(state) ON artifacts TO kf_oj_platform")
    op.execute(
        "GRANT INSERT ON problems,problem_versions,problem_cases,submissions,outbox "
        "TO kf_oj_platform"
    )
    op.execute("GRANT UPDATE(active_version_id,enabled) ON problems TO kf_oj_platform")
    op.execute("GRANT UPDATE(published_at) ON problem_versions TO kf_oj_platform")
    op.execute("GRANT INSERT(id,submission_id,problem_version_id) ON evaluations TO kf_oj_platform")
    op.execute(
        "GRANT UPDATE(failures,next_attempt_at,locked_until,lock_owner,delivered_at,"
        "last_error_code) ON outbox TO kf_oj_platform"
    )
    op.execute(
        "GRANT SELECT ON problems,problem_versions,problem_cases,artifacts,submissions,"
        "evaluations,judge_attempts,case_results,outbox TO kf_oj_coordinator"
    )
    op.execute(
        "GRANT UPDATE(state,fencing_version,attempts_count,current_attempt_id,not_before,"
        "cancel_requested_at,updated_at,verdict,failure_code,final_report,final_report_digest) "
        "ON evaluations TO kf_oj_coordinator"
    )
    op.execute("GRANT INSERT ON judge_attempts,case_results TO kf_oj_coordinator")
    op.execute(
        "GRANT UPDATE(expires_at,revoked_at,sequence,stage,case_index,report,report_digest,"
        "cleanup_complete) ON judge_attempts TO kf_oj_coordinator"
    )
    # Recovery may enqueue the same immutable notification, but cannot change identity fields.
    op.execute("GRANT INSERT ON outbox TO kf_oj_coordinator")
    op.execute("GRANT UPDATE(next_attempt_at,delivered_at) ON outbox TO kf_oj_coordinator")


def upgrade() -> None:
    op.create_table(
        "accounts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("username", sa.String(length=24), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default="true", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "username ~ '^[a-z0-9_]{3,24}$'", name=op.f("ck_accounts_canonical_username")
        ),
        sa.CheckConstraint(
            "char_length(password_hash) BETWEEN 40 AND 256",
            name=op.f("ck_accounts_password_hash_size"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_accounts")),
        sa.UniqueConstraint("username", name=op.f("uq_accounts_username")),
    )
    op.create_table(
        "artifacts",
        sa.Column("digest", sa.String(length=64), nullable=False),
        sa.Column("bytes", sa.BigInteger(), nullable=False),
        sa.Column("state", sa.String(length=8), server_default="ready", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("digest ~ '^[0-9a-f]{64}$'", name=op.f("ck_artifacts_digest")),
        sa.CheckConstraint("state IN ('ready', 'deleting')", name=op.f("ck_artifacts_state")),
        sa.CheckConstraint("bytes BETWEEN 0 AND 67108864", name=op.f("ck_artifacts_bytes")),
        sa.PrimaryKeyConstraint("digest", name=op.f("pk_artifacts")),
        sa.UniqueConstraint("digest", "state", name=op.f("uq_artifacts_digest")),
    )
    op.create_table(
        "login_throttles",
        sa.Column("bucket_hash", sa.String(length=64), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("reset_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "bucket_hash ~ '^[0-9a-f]{64}$'", name=op.f("ck_login_throttles_bucket_digest")
        ),
        sa.CheckConstraint(
            "attempts BETWEEN 1 AND 10", name=op.f("ck_login_throttles_attempt_budget")
        ),
        sa.PrimaryKeyConstraint("bucket_hash", name=op.f("pk_login_throttles")),
    )
    op.create_index("ix_login_throttles_reset", "login_throttles", ["reset_at"], unique=False)
    op.create_table(
        "problems",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("slug", sa.String(length=64), nullable=False),
        sa.Column("active_version_id", sa.Uuid(), nullable=True),
        sa.Column("enabled", sa.Boolean(), server_default="true", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("slug ~ '^[a-z0-9][a-z0-9_-]{0,63}$'", name=op.f("ck_problems_slug")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_problems")),
        sa.UniqueConstraint("slug", name=op.f("uq_problems_slug")),
    )
    op.create_table(
        "problem_versions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("problem_id", sa.Uuid(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=160), nullable=False),
        sa.Column("statement", sa.Text(), nullable=False),
        sa.Column("languages", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("toolchain", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("compile_limits", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("run_limits", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("job_wall_time_us", sa.BigInteger(), nullable=False),
        sa.Column("comparison", sa.String(length=16), server_default="exact_bytes", nullable=False),
        sa.Column("public_samples", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "comparison = 'exact_bytes'", name=op.f("ck_problem_versions_comparison")
        ),
        sa.CheckConstraint(
            "char_length(title) BETWEEN 1 AND 160", name=op.f("ck_problem_versions_title_size")
        ),
        sa.CheckConstraint(
            "job_wall_time_us BETWEEN 1000 AND 3600000000",
            name=op.f("ck_problem_versions_job_wall"),
        ),
        sa.CheckConstraint(
            "octet_length(statement) BETWEEN 1 AND 262144",
            name=op.f("ck_problem_versions_statement_bytes"),
        ),
        sa.CheckConstraint("revision > 0", name=op.f("ck_problem_versions_revision")),
        sa.ForeignKeyConstraint(
            ["problem_id"], ["problems.id"], name=op.f("fk_problem_versions_problem_id_problems")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_problem_versions")),
        sa.UniqueConstraint("id", "problem_id", name=op.f("uq_problem_versions_id")),
        sa.UniqueConstraint("problem_id", "revision", name=op.f("uq_problem_versions_problem_id")),
    )
    op.create_index(
        "ix_problem_versions_public",
        "problem_versions",
        ["problem_id", "published_at"],
        unique=False,
    )
    op.create_table(
        "sessions",
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("csrf_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("csrf_hash ~ '^[0-9a-f]{64}$'", name=op.f("ck_sessions_csrf_digest")),
        sa.CheckConstraint("token_hash ~ '^[0-9a-f]{64}$'", name=op.f("ck_sessions_token_digest")),
        sa.CheckConstraint("expires_at > created_at", name=op.f("ck_sessions_expiry")),
        sa.ForeignKeyConstraint(
            ["account_id"], ["accounts.id"], name=op.f("fk_sessions_account_id_accounts")
        ),
        sa.PrimaryKeyConstraint("token_hash", name=op.f("pk_sessions")),
    )
    op.create_index(
        "ix_sessions_account_expiry", "sessions", ["account_id", "expires_at"], unique=False
    )
    op.create_table(
        "problem_cases",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("version_id", sa.Uuid(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("input_digest", sa.String(length=64), nullable=False),
        sa.Column("expected_digest", sa.String(length=64), nullable=False),
        sa.Column(
            "artifact_state",
            sa.String(length=8),
            sa.Computed("'ready'", persisted=True),
            nullable=False,
        ),
        sa.CheckConstraint("position BETWEEN 0 AND 255", name=op.f("ck_problem_cases_position")),
        sa.ForeignKeyConstraint(
            ["expected_digest", "artifact_state"],
            ["artifacts.digest", "artifacts.state"],
            name="fk_problem_cases_expected_digest_artifacts",
        ),
        sa.ForeignKeyConstraint(
            ["input_digest", "artifact_state"],
            ["artifacts.digest", "artifacts.state"],
            name=op.f("fk_problem_cases_input_digest_artifacts"),
        ),
        sa.ForeignKeyConstraint(
            ["version_id"],
            ["problem_versions.id"],
            name=op.f("fk_problem_cases_version_id_problem_versions"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_problem_cases")),
        sa.UniqueConstraint("id", "version_id", name=op.f("uq_problem_cases_id")),
        sa.UniqueConstraint("version_id", "position", name=op.f("uq_problem_cases_version_id")),
    )
    op.create_table(
        "submissions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("problem_id", sa.Uuid(), nullable=False),
        sa.Column("problem_version_id", sa.Uuid(), nullable=False),
        sa.Column("language", sa.String(length=8), nullable=False),
        sa.Column("source_digest", sa.String(length=64), nullable=False),
        sa.Column(
            "artifact_state",
            sa.String(length=8),
            sa.Computed("'ready'", persisted=True),
            nullable=False,
        ),
        sa.Column("idempotency_key", sa.Uuid(), nullable=False),
        sa.Column("request_digest", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("language IN ('c17', 'cpp20')", name=op.f("ck_submissions_language")),
        sa.CheckConstraint(
            "request_digest ~ '^[0-9a-f]{64}$'", name=op.f("ck_submissions_request_digest")
        ),
        sa.ForeignKeyConstraint(
            ["account_id"], ["accounts.id"], name=op.f("fk_submissions_account_id_accounts")
        ),
        sa.ForeignKeyConstraint(
            ["problem_version_id", "problem_id"],
            ["problem_versions.id", "problem_versions.problem_id"],
            name=op.f("fk_submissions_problem_version_id_problem_versions"),
        ),
        sa.ForeignKeyConstraint(
            ["source_digest", "artifact_state"],
            ["artifacts.digest", "artifacts.state"],
            name=op.f("fk_submissions_source_digest_artifacts"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_submissions")),
        sa.UniqueConstraint(
            "account_id", "idempotency_key", name=op.f("uq_submissions_account_id")
        ),
        sa.UniqueConstraint("id", "problem_version_id", name=op.f("uq_submissions_id")),
    )
    op.create_index(
        "ix_submissions_account_history",
        "submissions",
        ["account_id", "created_at", "id"],
        unique=False,
    )
    op.create_table(
        "evaluations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("submission_id", sa.Uuid(), nullable=False),
        sa.Column("problem_version_id", sa.Uuid(), nullable=False),
        sa.Column("state", sa.String(length=16), server_default="queued", nullable=False),
        sa.Column("fencing_version", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("attempts_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("max_attempts", sa.Integer(), server_default="3", nullable=False),
        sa.Column("current_attempt_id", sa.Uuid(), nullable=True),
        sa.Column(
            "not_before",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("cancel_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("verdict", sa.String(length=32), nullable=True),
        sa.Column("failure_code", sa.String(length=32), nullable=True),
        sa.Column("final_report", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("final_report_digest", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "state IN ('queued','claimed','compiling','running','cancelling',"
            "'finished','cancelled','failed')",
            name=op.f("ck_evaluations_state"),
        ),
        sa.CheckConstraint(
            "state NOT IN ('claimed','compiling','running','cancelling') "
            "OR current_attempt_id IS NOT NULL",
            name=op.f("ck_evaluations_active_attempt"),
        ),
        sa.CheckConstraint(
            "attempts_count BETWEEN 0 AND max_attempts", name=op.f("ck_evaluations_attempts_count")
        ),
        sa.CheckConstraint("fencing_version >= 0", name=op.f("ck_evaluations_fence")),
        sa.CheckConstraint(
            "max_attempts BETWEEN 1 AND 5", name=op.f("ck_evaluations_max_attempts")
        ),
        sa.ForeignKeyConstraint(
            ["submission_id", "problem_version_id"],
            ["submissions.id", "submissions.problem_version_id"],
            name=op.f("fk_evaluations_submission_id_submissions"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_evaluations")),
        sa.UniqueConstraint("id", "problem_version_id", name=op.f("uq_evaluations_id")),
        sa.UniqueConstraint("submission_id", name=op.f("uq_evaluations_submission_id")),
    )
    op.create_index(
        "ix_evaluations_recovery", "evaluations", ["state", "not_before", "id"], unique=False
    )
    op.create_table(
        "judge_attempts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("evaluation_id", sa.Uuid(), nullable=False),
        sa.Column("worker_id", sa.Uuid(), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("fencing_version", sa.BigInteger(), nullable=False),
        sa.Column("lease_token_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("manifest", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("manifest_digest", sa.String(length=64), nullable=False),
        sa.Column("sequence", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("stage", sa.String(length=16), nullable=True),
        sa.Column("case_index", sa.Integer(), nullable=True),
        sa.Column("report", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("report_digest", sa.String(length=64), nullable=True),
        sa.Column("cleanup_complete", sa.Boolean(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "lease_token_hash ~ '^[0-9a-f]{64}$'", name=op.f("ck_judge_attempts_token_digest")
        ),
        sa.CheckConstraint(
            "manifest_digest ~ '^[0-9a-f]{64}$'", name=op.f("ck_judge_attempts_manifest_digest")
        ),
        sa.CheckConstraint("fencing_version > 0", name=op.f("ck_judge_attempts_fence")),
        sa.CheckConstraint("number BETWEEN 1 AND 5", name=op.f("ck_judge_attempts_number")),
        sa.CheckConstraint("sequence >= 0", name=op.f("ck_judge_attempts_sequence")),
        sa.ForeignKeyConstraint(
            ["evaluation_id"],
            ["evaluations.id"],
            name=op.f("fk_judge_attempts_evaluation_id_evaluations"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_judge_attempts")),
        sa.UniqueConstraint(
            "evaluation_id", "fencing_version", name="uq_judge_attempts_evaluation_fence"
        ),
        sa.UniqueConstraint("evaluation_id", "number", name="uq_judge_attempts_evaluation_number"),
        sa.UniqueConstraint("id", "evaluation_id", name=op.f("uq_judge_attempts_id")),
    )
    op.create_index(
        "ix_judge_attempts_expiry", "judge_attempts", ["expires_at", "evaluation_id"], unique=False
    )
    op.create_table(
        "outbox",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("evaluation_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=32), server_default="judge.evaluation", nullable=False),
        sa.Column("failures", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lock_owner", sa.Uuid(), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(length=32), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("kind = 'judge.evaluation'", name=op.f("ck_outbox_kind")),
        sa.CheckConstraint("failures BETWEEN 0 AND 100", name=op.f("ck_outbox_failure_budget")),
        sa.ForeignKeyConstraint(
            ["evaluation_id"], ["evaluations.id"], name=op.f("fk_outbox_evaluation_id_evaluations")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_outbox")),
        sa.UniqueConstraint("evaluation_id", name=op.f("uq_outbox_evaluation_id")),
    )
    op.create_index(
        "ix_outbox_delivery", "outbox", ["delivered_at", "next_attempt_at", "id"], unique=False
    )
    op.create_table(
        "case_results",
        sa.Column("attempt_id", sa.Uuid(), nullable=False),
        sa.Column("case_id", sa.Uuid(), nullable=False),
        sa.Column("evaluation_id", sa.Uuid(), nullable=False),
        sa.Column("problem_version_id", sa.Uuid(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("verdict", sa.String(length=32), nullable=False),
        sa.Column("cpu_time_us", sa.BigInteger(), nullable=True),
        sa.Column("memory_peak_bytes", sa.BigInteger(), nullable=True),
        sa.CheckConstraint(
            "cpu_time_us IS NULL OR cpu_time_us >= 0", name=op.f("ck_case_results_cpu")
        ),
        sa.CheckConstraint(
            "memory_peak_bytes IS NULL OR memory_peak_bytes >= 0",
            name=op.f("ck_case_results_memory"),
        ),
        sa.CheckConstraint("position BETWEEN 0 AND 255", name=op.f("ck_case_results_position")),
        sa.ForeignKeyConstraint(
            ["attempt_id", "evaluation_id"],
            ["judge_attempts.id", "judge_attempts.evaluation_id"],
            name=op.f("fk_case_results_attempt_id_judge_attempts"),
        ),
        sa.ForeignKeyConstraint(
            ["case_id", "problem_version_id"],
            ["problem_cases.id", "problem_cases.version_id"],
            name=op.f("fk_case_results_case_id_problem_cases"),
        ),
        sa.ForeignKeyConstraint(
            ["evaluation_id", "problem_version_id"],
            ["evaluations.id", "evaluations.problem_version_id"],
            name=op.f("fk_case_results_evaluation_id_evaluations"),
        ),
        sa.PrimaryKeyConstraint("attempt_id", "case_id", name=op.f("pk_case_results")),
        sa.UniqueConstraint("attempt_id", "position", name=op.f("uq_case_results_attempt_id")),
    )

    op.create_foreign_key(
        "fk_problems_active_version",
        "problems",
        "problem_versions",
        ["active_version_id", "id"],
        ["id", "problem_id"],
        deferrable=True,
        initially="DEFERRED",
    )
    op.create_foreign_key(
        "fk_evaluations_current_attempt",
        "evaluations",
        "judge_attempts",
        ["current_attempt_id", "id"],
        ["id", "evaluation_id"],
        deferrable=True,
        initially="DEFERRED",
    )
    install_guards_and_permissions()


def downgrade() -> None:
    op.drop_constraint("fk_evaluations_current_attempt", "evaluations", type_="foreignkey")
    op.drop_constraint("fk_problems_active_version", "problems", type_="foreignkey")
    op.drop_table("case_results")
    op.drop_index("ix_outbox_delivery", table_name="outbox")
    op.drop_table("outbox")
    op.drop_index("ix_judge_attempts_expiry", table_name="judge_attempts")
    op.drop_table("judge_attempts")
    op.drop_index("ix_evaluations_recovery", table_name="evaluations")
    op.drop_table("evaluations")
    op.drop_index("ix_submissions_account_history", table_name="submissions")
    op.drop_table("submissions")
    op.drop_table("problem_cases")
    op.drop_index("ix_sessions_account_expiry", table_name="sessions")
    op.drop_table("sessions")
    op.drop_index("ix_problem_versions_public", table_name="problem_versions")
    op.drop_table("problem_versions")
    op.drop_table("problems")
    op.drop_index("ix_login_throttles_reset", table_name="login_throttles")
    op.drop_table("login_throttles")
    op.drop_table("artifacts")
    op.drop_table("accounts")
    op.execute("DROP FUNCTION public.kf_immutable_version()")
    op.execute("DROP FUNCTION public.kf_immutable_case()")
    # NOLOGIN groups are intentionally retained: a downgrade never deletes operator roles.
