"""Persist owner-scoped declarative skill drafts."""

from alembic import op

from kf_oj.modules.skills.draft_models import SkillDraftRow

revision = "0022_skill_drafts"
down_revision = "0021_learning_agent_timings"
branch_labels = depends_on = None

TABLE = "skills_drafts"


def upgrade():
    SkillDraftRow.__table__.create(op.get_bind())
    op.execute(
        f"REVOKE ALL ON {TABLE} FROM PUBLIC, "
        "kf_oj_coordinator, kf_oj_learning_worker, kf_oj_platform"
    )
    op.execute(f"GRANT SELECT, INSERT ON {TABLE} TO kf_oj_platform")
    op.execute(
        "GRANT UPDATE (document, content_digest, revision, updated_at) "
        f"ON {TABLE} TO kf_oj_platform"
    )


def downgrade():
    op.execute(f"""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM {TABLE}) THEN
                RAISE EXCEPTION 'cannot downgrade while skill drafts exist';
            END IF;
        END $$
    """)
    SkillDraftRow.__table__.drop(op.get_bind())
