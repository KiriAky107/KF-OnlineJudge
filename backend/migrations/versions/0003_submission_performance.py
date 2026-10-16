"""Trusted final performance projection; no re-encoding or promotion of old reports."""

import sqlalchemy as sa
from alembic import op

revision = "0003_submission_performance"
down_revision = "0002_multi_runtime"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("judge_attempts", sa.Column("measurement_epoch", sa.String(64), nullable=True))
    op.create_check_constraint(
        op.f("ck_judge_attempts_measurement_epoch"),
        "judge_attempts",
        "measurement_epoch IS NULL OR measurement_epoch ~ '^[0-9a-f]{64}$'",
    )
    op.create_table(
        "evaluation_performance",
        sa.Column("evaluation_id", sa.Uuid(), primary_key=True),
        sa.Column("submission_id", sa.Uuid(), nullable=False, unique=True),
        sa.Column("attempt_id", sa.Uuid(), nullable=False, unique=True),
        sa.Column("problem_version_id", sa.Uuid(), nullable=False),
        sa.Column("language", sa.String(16), nullable=False),
        sa.Column("comparison_group", sa.String(64), nullable=False),
        sa.Column("measurement_epoch", sa.String(64), nullable=False),
        sa.Column("report_digest", sa.String(64), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("cpu_time_us", sa.BigInteger(), nullable=True),
        sa.Column("memory_peak_bytes", sa.BigInteger(), nullable=True),
        sa.ForeignKeyConstraint(
            ["evaluation_id", "problem_version_id"],
            ["evaluations.id", "evaluations.problem_version_id"],
        ),
        sa.ForeignKeyConstraint(
            ["submission_id", "problem_version_id"],
            ["submissions.id", "submissions.problem_version_id"],
        ),
        sa.ForeignKeyConstraint(
            ["attempt_id", "evaluation_id"], ["judge_attempts.id", "judge_attempts.evaluation_id"]
        ),
        sa.CheckConstraint("language IN ('c17','cpp20','python313','java21')", name="language"),
        sa.CheckConstraint("comparison_group ~ '^[0-9a-f]{64}$'", name="comparison_group"),
        sa.CheckConstraint("measurement_epoch ~ '^[0-9a-f]{64}$'", name="measurement_epoch"),
        sa.CheckConstraint("report_digest ~ '^[0-9a-f]{64}$'", name="report_digest"),
        sa.CheckConstraint("cpu_time_us IS NULL OR cpu_time_us >= 0", name="cpu"),
        sa.CheckConstraint("memory_peak_bytes IS NULL OR memory_peak_bytes >= 0", name="memory"),
    )
    for metric in ("cpu_time_us", "memory_peak_bytes"):
        op.create_index(
            f"ix_performance_{metric}_cohort",
            "evaluation_performance",
            ["comparison_group", sa.text("finished_at DESC"), sa.text("evaluation_id DESC")],
            postgresql_where=sa.text(f"{metric} IS NOT NULL"),
        )
    op.create_index(
        "ix_submissions_owned_problem_history",
        "submissions",
        ["account_id", "problem_version_id", sa.text("created_at DESC"), sa.text("id DESC")],
    )
    op.execute("""
        CREATE FUNCTION public.kf_trusted_performance() RETURNS trigger LANGUAGE plpgsql
        SET search_path = pg_catalog, public AS $$
        BEGIN
            IF TG_OP != 'INSERT' THEN
                RAISE EXCEPTION 'final performance is immutable' USING ERRCODE = '23514';
            END IF;
            IF NOT EXISTS (
                SELECT 1 FROM public.evaluations e
                JOIN public.judge_attempts a ON a.id=e.current_attempt_id AND a.evaluation_id=e.id
                JOIN public.submissions s ON s.id=e.submission_id
                WHERE e.id=NEW.evaluation_id AND e.state='finished' AND e.verdict='accepted'
                  AND e.failure_code IS NULL AND a.cleanup_complete IS TRUE
                  AND e.current_attempt_id=NEW.attempt_id AND s.id=NEW.submission_id
                  AND s.problem_version_id=NEW.problem_version_id AND s.language=NEW.language
                  AND a.measurement_epoch=NEW.measurement_epoch
                  AND e.final_report_digest=NEW.report_digest AND a.report_digest=NEW.report_digest
                  AND e.updated_at=NEW.finished_at
                  AND e.final_report->>'cleanup_complete'='true'
                  AND (e.final_report->>'run_cpu_time_us')::bigint
                      IS NOT DISTINCT FROM NEW.cpu_time_us
                  AND (e.final_report->>'run_memory_peak_bytes')::bigint
                      IS NOT DISTINCT FROM NEW.memory_peak_bytes
            ) THEN
                RAISE EXCEPTION 'performance requires a bound trusted final AC'
                    USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute("REVOKE ALL ON FUNCTION public.kf_trusted_performance() FROM PUBLIC")
    op.execute("""
        CREATE TRIGGER kf_trusted_performance BEFORE INSERT OR UPDATE OR DELETE
        ON public.evaluation_performance FOR EACH ROW
        EXECUTE FUNCTION public.kf_trusted_performance()
    """)
    op.execute("REVOKE ALL ON evaluation_performance FROM PUBLIC,kf_oj_platform,kf_oj_coordinator")
    op.execute("GRANT SELECT ON evaluation_performance TO kf_oj_platform,kf_oj_coordinator")
    op.execute("GRANT INSERT ON evaluation_performance TO kf_oj_coordinator")
    # Existing attempt INSERT permission covers the new column. No UPDATE epoch permission:
    # an unbound historical attempt must never be promoted by changing current configuration.


def downgrade() -> None:
    op.execute("""
        DO $$ BEGIN
            IF EXISTS(SELECT 1 FROM evaluation_performance) OR
                EXISTS(SELECT 1 FROM judge_attempts WHERE measurement_epoch IS NOT NULL) THEN
                RAISE EXCEPTION 'cannot discard trusted measurement bindings'
                    USING ERRCODE = '23514';
            END IF;
        END $$
    """)
    op.drop_table("evaluation_performance")
    op.execute("DROP FUNCTION public.kf_trusted_performance()")
    op.drop_index("ix_submissions_owned_problem_history", table_name="submissions")
    op.drop_constraint(op.f("ck_judge_attempts_measurement_epoch"), "judge_attempts", type_="check")
    op.drop_column("judge_attempts", "measurement_epoch")
