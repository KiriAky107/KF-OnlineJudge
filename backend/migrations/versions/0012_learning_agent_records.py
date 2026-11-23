"""Store learning Agent sessions, runs, and steps independently of judging."""

from alembic import op

from kf_oj.modules.learning.agent_models import AgentRunRow, AgentSessionRow, AgentStepRow

revision = "0012_learning_agent_records"
down_revision = "0011_self_tests"
branch_labels = depends_on = None


def upgrade():
    for model in (AgentSessionRow, AgentRunRow, AgentStepRow):
        model.__table__.create(op.get_bind())
    tables = "learning_agent_sessions, learning_agent_runs, learning_agent_steps"
    op.execute(f"REVOKE ALL ON {tables} FROM PUBLIC, kf_oj_coordinator, kf_oj_platform")
    op.execute(f"GRANT SELECT, INSERT ON {tables} TO kf_oj_platform")
    op.execute("GRANT UPDATE (state, revision) ON learning_agent_runs TO kf_oj_platform")
    op.execute(
        "GRANT UPDATE (state, result_digest, error_code) ON learning_agent_steps TO kf_oj_platform"
    )


def downgrade():
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM learning_agent_sessions) THEN
                RAISE EXCEPTION 'cannot downgrade while learning Agent records exist';
            END IF;
        END $$
    """)
    for model in (AgentStepRow, AgentRunRow, AgentSessionRow):
        model.__table__.drop(op.get_bind())
