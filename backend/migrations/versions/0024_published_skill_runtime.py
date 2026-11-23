"""Allow the learning worker to resolve only the reviewed runtime projection."""

from alembic import op

revision = "0024_published_skill_runtime"
down_revision = "0023_skill_publications"
branch_labels = depends_on = None


def upgrade():
    op.execute("""
        CREATE FUNCTION public.kf_read_published_skill(version_id uuid)
        RETURNS TABLE (skill_id uuid, version bigint, digest text, declaration jsonb)
        LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
            SELECT p.draft_id, p.version, p.content_digest::text,
                jsonb_build_object(
                    'purpose', p.document->'purpose',
                    'title', p.document->'title',
                    'description', p.document->'description',
                    'parameters', p.document->'parameters',
                    'requested_tools', p.document->'requested_tools'
                )
            FROM public.skills_publications AS p
            WHERE p.id=version_id AND p.state='published'
            FOR SHARE OF p
        $$
    """)
    op.execute("REVOKE ALL ON FUNCTION public.kf_read_published_skill(uuid) FROM PUBLIC")
    op.execute(
        "GRANT EXECUTE ON FUNCTION public.kf_read_published_skill(uuid) "
        "TO kf_oj_platform,kf_oj_learning_worker"
    )


def downgrade():
    op.execute("DROP FUNCTION public.kf_read_published_skill(uuid)")
