"""Keep only a one-use source excerpt alongside an opted-in learning run."""

import sqlalchemy as sa
from alembic import op

revision = "0039_learning_source_excerpt"
down_revision = "0038_learning_model_usage"
branch_labels = depends_on = None


def upgrade():
    op.create_table(
        "learning_agent_source_excerpts",
        sa.Column(
            "run_id",
            sa.Uuid(),
            sa.ForeignKey("learning_agent_runs.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("source_utf8", sa.LargeBinary(), nullable=False),
        sa.CheckConstraint("octet_length(source_utf8) BETWEEN 1 AND 2048", name="bytes"),
    )
    op.execute("GRANT INSERT ON learning_agent_source_excerpts TO kf_oj_platform")
    op.execute("""
        CREATE FUNCTION public.kf_take_learning_source(run_key uuid, lease_revision bigint)
        RETURNS bytea LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
            DELETE FROM public.learning_agent_source_excerpts AS excerpt
            USING public.learning_agent_runs AS run, public.learning_agent_tasks AS task
            WHERE excerpt.run_id=run_key AND run.id=excerpt.run_id AND task.run_id=run.id
                AND run.state='running' AND run.revision=lease_revision
                AND task.lease_expires_at>clock_timestamp()
            RETURNING excerpt.source_utf8
        $$
    """)
    op.execute("REVOKE ALL ON FUNCTION public.kf_take_learning_source(uuid,bigint) FROM PUBLIC")
    op.execute(
        "GRANT EXECUTE ON FUNCTION public.kf_take_learning_source(uuid,bigint) "
        "TO kf_oj_learning_worker"
    )
    op.execute("""
        CREATE FUNCTION public.kf_clear_learning_source()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
        BEGIN
            DELETE FROM public.learning_agent_source_excerpts WHERE run_id=NEW.id;
            RETURN NULL;
        END
        $$
    """)
    op.execute("REVOKE ALL ON FUNCTION public.kf_clear_learning_source() FROM PUBLIC")
    op.execute("""
        CREATE TRIGGER clear_learning_source AFTER UPDATE OF state ON learning_agent_runs
        FOR EACH ROW WHEN (
            NEW.state IN ('cancel_requested', 'cancelled', 'succeeded', 'failed')
            AND OLD.state IS DISTINCT FROM NEW.state
        ) EXECUTE FUNCTION public.kf_clear_learning_source()
    """)


def downgrade():
    op.execute("DROP TRIGGER clear_learning_source ON learning_agent_runs")
    op.execute("DROP FUNCTION public.kf_clear_learning_source()")
    op.execute("DROP FUNCTION public.kf_take_learning_source(uuid,bigint)")
    op.drop_table("learning_agent_source_excerpts")
