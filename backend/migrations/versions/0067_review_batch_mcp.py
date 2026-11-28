"""Project bounded batch evidence for an active, scoped review."""

from alembic import op

revision = "0067_review_batch_mcp"
down_revision = "0066_publication_hint_ceiling"
branch_labels = depends_on = None

_SIGNATURE = "public.kf_review_batch_context(uuid,bigint)"


def upgrade():
    op.execute("""
        CREATE FUNCTION public.kf_review_batch_context(review_id uuid, review_revision bigint)
        RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
        DECLARE
            scope record;
            outcomes jsonb;
        BEGIN
            SELECT s.owner_id, coalesce(convo.root_session_id, s.id) AS conversation_id,
                   t.authorization_version, t.session_proof, sub.id AS submission_id,
                   sub.problem_version_id AS version_id, sub.language, sub.source_digest
            INTO scope
            FROM public.learning_agent_runs AS r
            JOIN public.learning_agent_sessions AS s ON s.id=r.session_id
            JOIN public.learning_agent_tasks AS t ON t.run_id=r.id
            JOIN public.learning_agent_workflow_data AS w ON w.run_id=r.id
            LEFT JOIN public.learning_agent_conversation_sessions AS convo
                ON convo.session_id=s.id
            JOIN public.submissions AS sub
                ON sub.id=(w.input->>'submission_id')::uuid AND sub.account_id=s.owner_id
            JOIN public.artifacts AS a ON a.digest=sub.source_digest
            JOIN public.problem_versions AS v ON v.id=sub.problem_version_id
            JOIN public.problems AS p ON p.id=v.problem_id
            WHERE r.id=review_id AND r.revision=review_revision AND r.state='running'
                AND s.purpose='submission_review' AND t.lease_expires_at > clock_timestamp()
                AND sub.language IN ('c17','cpp20') AND a.state='ready'
                AND a.bytes BETWEEN 1 AND 262144 AND v.published_at IS NOT NULL AND p.enabled
            FOR SHARE OF p;
            IF NOT FOUND THEN RETURN NULL; END IF;
            IF NOT public.kf_lock_agent_session(
                scope.owner_id, scope.authorization_version, scope.session_proof
            ) THEN RETURN NULL; END IF;

            SELECT coalesce(jsonb_agg(to_jsonb(o)), '[]'::jsonb) INTO outcomes
            FROM (
                SELECT d.batch_manifest_digest AS manifest_sha256, d.batch_purpose AS purpose,
                       d.batch_outcome AS outcome
                FROM public.learning_diagnostic_requests AS d
                JOIN public.learning_agent_runs AS origin
                    ON origin.id::text=d.review_context->>'run_id'
                JOIN public.learning_agent_sessions AS old_session
                    ON old_session.id=origin.session_id
                LEFT JOIN public.learning_agent_conversation_sessions AS convo
                    ON convo.session_id=old_session.id
                WHERE d.owner_id=scope.owner_id AND old_session.owner_id=scope.owner_id
                    AND old_session.purpose='submission_review' AND origin.state='succeeded'
                    AND coalesce(convo.root_session_id, old_session.id)=scope.conversation_id
                    AND d.submission_id=scope.submission_id
                    AND d.problem_version_id=scope.version_id
                    AND d.source_digest=scope.source_digest AND d.language=scope.language
                    AND d.authorization_version=scope.authorization_version
                    AND d.session_proof=scope.session_proof
                    AND d.mode='batch' AND d.state='observed'
                ORDER BY d.finished_at DESC, d.id
                LIMIT 8
            ) AS o;
            RETURN jsonb_build_object('language', scope.language, 'outcomes', outcomes);
        END
        $$
    """)
    op.execute(f"REVOKE ALL ON FUNCTION {_SIGNATURE} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {_SIGNATURE} TO kf_oj_learning_worker")


def downgrade():
    op.execute(f"DROP FUNCTION {_SIGNATURE}")
