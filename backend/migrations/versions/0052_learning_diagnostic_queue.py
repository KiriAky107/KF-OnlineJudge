"""Persist authorized public-sample diagnostic requests separately from judging."""

from alembic import op

revision = "0052_learning_diagnostic_queue"
down_revision = "0051_learning_chat_full_output"
branch_labels = depends_on = None

_TABLE = "learning_diagnostic_requests"
_SESSION_PROBE = "public.kf_agent_session_administrator(uuid,bigint,text)"


def upgrade():
    op.execute("""
        CREATE TABLE learning_diagnostic_requests (
        owner_id UUID NOT NULL,
        id UUID NOT NULL,
        submission_id UUID NOT NULL,
        problem_version_id UUID NOT NULL,
        sample_ordinal INTEGER NOT NULL,
        language VARCHAR(5) NOT NULL,
        source_digest VARCHAR(64) NOT NULL,
        source_bytes INTEGER NOT NULL,
        input_digest VARCHAR(64) NOT NULL,
        input_bytes INTEGER NOT NULL,
        expected_digest VARCHAR(64) NOT NULL,
        expected_bytes INTEGER NOT NULL,
        authorization_version BIGINT NOT NULL,
        session_proof VARCHAR(64) NOT NULL,
        state VARCHAR(16) NOT NULL,
        created_at TIMESTAMP WITH TIME ZONE NOT NULL,
        queue_deadline TIMESTAMP WITH TIME ZONE NOT NULL,
        execution_id UUID,
        execution_deadline TIMESTAMP WITH TIME ZONE,
        finished_at TIMESTAMP WITH TIME ZONE,
        o2 VARCHAR(24),
        o0 VARCHAR(24),
        failure_code VARCHAR(32),
        CONSTRAINT pk_learning_diagnostic_requests PRIMARY KEY (owner_id, id),
        CONSTRAINT ck_learning_diagnostic_requests_language CHECK (language IN ('c17', 'cpp20')),
        CONSTRAINT ck_learning_diagnostic_requests_sample CHECK (sample_ordinal >= 0),
        CONSTRAINT ck_learning_diagnostic_requests_authority CHECK (authorization_version > 0),
        CONSTRAINT ck_learning_diagnostic_requests_digests CHECK (session_proof ~ '^[0-9a-f]{64}$'
            AND source_digest ~ '^[0-9a-f]{64}$' AND input_digest ~ '^[0-9a-f]{64}$' AND
            expected_digest ~ '^[0-9a-f]{64}$'),
        CONSTRAINT ck_learning_diagnostic_requests_artifact_sizes CHECK (source_bytes BETWEEN 1 AND
            262144 AND input_bytes BETWEEN 0 AND 65536 AND expected_bytes BETWEEN 0 AND 65536),
        CONSTRAINT ck_learning_diagnostic_requests_execution CHECK ((execution_id IS NULL AND
            execution_deadline IS NULL) OR (execution_id IS NOT NULL AND execution_deadline IS NOT
            NULL)),
        CONSTRAINT ck_learning_diagnostic_requests_state CHECK ((state = 'queued' AND execution_id
            IS NULL AND finished_at IS NULL) OR (state IN ('running', 'cancel_pending') AND
            execution_id IS NOT NULL AND finished_at IS NULL) OR (state IN ('expired',
            'unavailable', 'cancelled') AND finished_at IS NOT NULL) OR (state IN ('observed',
            'failed') AND execution_id IS NOT NULL AND finished_at IS NOT NULL)),
        CONSTRAINT ck_learning_diagnostic_requests_outcome CHECK ((state = 'observed') = (o2 IS NOT
            NULL) AND (o2 IS NOT NULL AND o2 IN ('wrong_answer', 'runtime_error')) = (o0 IS NOT
            NULL) AND (state = 'failed') = (failure_code IS NOT NULL)),
        CONSTRAINT ck_learning_diagnostic_requests_queue_deadline CHECK (queue_deadline >
            created_at),
        CONSTRAINT fk_learning_diagnostic_requests_owner_id_accounts FOREIGN KEY(owner_id)
            REFERENCES accounts (id),
        CONSTRAINT fk_learning_diagnostic_requests_submission_id_submissions FOREIGN
            KEY(submission_id) REFERENCES submissions (id),
        CONSTRAINT fk_learning_diagnostic_requests_problem_version_id_prob_20b0 FOREIGN
            KEY(problem_version_id) REFERENCES problem_versions (id),
        CONSTRAINT fk_learning_diagnostic_requests_source_digest_artifacts FOREIGN
            KEY(source_digest) REFERENCES artifacts (digest)
        )
    """)
    op.execute("""
        CREATE INDEX ix_learning_diagnostic_requests_owner_time ON learning_diagnostic_requests
            (owner_id, created_at)
    """)
    op.execute("""
        CREATE INDEX ix_learning_diagnostic_requests_state_time ON learning_diagnostic_requests
            (state, created_at)
    """)
    op.execute("""
        CREATE UNIQUE INDEX uq_learning_diagnostic_requests_running ON learning_diagnostic_requests
            ((true)) WHERE state IN ('running', 'cancel_pending')
    """)
    op.execute(f"REVOKE ALL ON {_TABLE} FROM PUBLIC, kf_oj_coordinator, kf_oj_learning_worker")
    op.execute(f"GRANT SELECT, INSERT ON {_TABLE} TO kf_oj_platform")
    op.execute(
        "GRANT UPDATE (state, execution_id, execution_deadline, finished_at, o2, o0, failure_code) "
        f"ON {_TABLE} TO kf_oj_platform"
    )
    op.execute(f"GRANT EXECUTE ON FUNCTION {_SESSION_PROBE} TO kf_oj_platform")


def downgrade():
    op.execute(f"""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM {_TABLE}) THEN
                RAISE EXCEPTION 'cannot downgrade while diagnostic requests exist';
            END IF;
        END $$
    """)
    op.execute(f"REVOKE EXECUTE ON FUNCTION {_SESSION_PROBE} FROM kf_oj_platform")
    op.drop_table(_TABLE)
