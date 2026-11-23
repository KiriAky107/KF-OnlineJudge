"""Stop serving held versions to the learning runtime without altering their contents."""

from alembic import op

from kf_oj.modules.skills.publication_models import SkillVersionHoldRow

revision = "0028_skill_version_holds"
down_revision = "0027_skill_reports"
branch_labels = depends_on = None

TABLE = "skills_version_holds"


def upgrade():
    SkillVersionHoldRow.__table__.create(op.get_bind())
    op.execute(
        f"REVOKE ALL ON {TABLE} FROM PUBLIC, "
        "kf_oj_coordinator, kf_oj_learning_worker, kf_oj_platform"
    )
    op.execute(f"GRANT SELECT, INSERT ON {TABLE} TO kf_oj_platform")
    op.execute("""
        CREATE OR REPLACE FUNCTION public.kf_read_published_skill(version_id uuid)
        RETURNS TABLE (skill_id uuid, version bigint, digest text, declaration jsonb)
        LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
        DECLARE
            locked_version public.skills_publications%ROWTYPE;
        BEGIN
            SELECT p.* INTO locked_version
            FROM public.skills_publications AS p
            WHERE p.id=$1 AND p.state='published'
            FOR SHARE OF p;
            IF NOT FOUND OR EXISTS (
                SELECT 1 FROM public.skills_version_holds AS h WHERE h.version_id=$1
            ) THEN
                RETURN;
            END IF;
            RETURN QUERY SELECT locked_version.draft_id, locked_version.version,
                locked_version.content_digest::text,
                jsonb_build_object(
                    'purpose', locked_version.document->'purpose',
                    'title', locked_version.document->'title',
                    'description', locked_version.document->'description',
                    'parameters', locked_version.document->'parameters',
                    'requested_tools', locked_version.document->'requested_tools'
                );
        END
        $$
    """)


def downgrade():
    op.execute(f"""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM {TABLE}) THEN
                RAISE EXCEPTION 'cannot downgrade while skill versions are held';
            END IF;
        END $$
    """)
    op.execute("""
        CREATE OR REPLACE FUNCTION public.kf_read_published_skill(version_id uuid)
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
    SkillVersionHoldRow.__table__.drop(op.get_bind())
