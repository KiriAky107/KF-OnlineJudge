"""Pin the executable declaration of a saved private authoring revision."""

from alembic import op

from kf_oj.modules.skills.draft_models import SkillTrialRow

revision = "0030_skill_trials"
down_revision = "0029_learning_model_budget"
branch_labels = depends_on = None


def upgrade():
    SkillTrialRow.__table__.create(op.get_bind())
    op.execute("""
        REVOKE ALL ON public.skills_trials FROM
            PUBLIC,kf_oj_coordinator,kf_oj_learning_worker,kf_oj_platform
    """)
    op.execute("GRANT SELECT, INSERT ON public.skills_trials TO kf_oj_platform")
    op.execute("""
        CREATE FUNCTION public.kf_read_claimed_skill_trial(
            p_run uuid, p_revision bigint, p_version uuid, p_owner uuid
        ) RETURNS TABLE (release bigint, digest text, declaration jsonb)
        LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
            SELECT v.revision + 1, v.content_digest::text,
                v.declaration - 'title' - 'description'
            FROM public.skills_trials AS v
            JOIN public.skills_drafts AS d ON d.id=v.draft_id
            JOIN public.learning_agent_runs AS r ON r.skill_version_id=v.id
            JOIN public.learning_agent_sessions AS s ON s.id=r.session_id
            JOIN public.learning_agent_tasks AS t ON t.run_id=r.id
            WHERE v.id=p_version AND r.id=p_run AND r.revision=p_revision
              AND d.owner_id=p_owner AND s.owner_id=p_owner
              AND s.purpose=v.declaration->>'purpose'
              AND r.state='running' AND t.lease_expires_at > clock_timestamp()
        $$
    """)
    signature = "public.kf_read_claimed_skill_trial(uuid,bigint,uuid,uuid)"
    op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO kf_oj_learning_worker")


def downgrade():
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM public.skills_trials) THEN
                RAISE EXCEPTION 'cannot downgrade while private skill revisions are pinned';
            END IF;
        END $$
    """)
    op.execute("DROP FUNCTION public.kf_read_claimed_skill_trial(uuid,bigint,uuid,uuid)")
    SkillTrialRow.__table__.drop(op.get_bind())
