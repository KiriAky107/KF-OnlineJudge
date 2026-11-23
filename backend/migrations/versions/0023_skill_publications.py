"""Store immutable submissions for administrator review and publication."""

from alembic import op

from kf_oj.modules.skills.publication_models import SkillPublicationRow

revision = "0023_skill_publications"
down_revision = "0022_skill_drafts"
branch_labels = depends_on = None

TABLE = "skills_publications"


def upgrade():
    SkillPublicationRow.__table__.create(op.get_bind())
    op.execute(
        f"REVOKE ALL ON {TABLE} FROM PUBLIC, "
        "kf_oj_coordinator, kf_oj_learning_worker, kf_oj_platform"
    )
    op.execute(f"GRANT SELECT, INSERT ON {TABLE} TO kf_oj_platform")
    op.execute(
        f"GRANT UPDATE (state, reviewer_id, review_note, decided_at) ON {TABLE} TO kf_oj_platform"
    )


def downgrade():
    op.execute(f"""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM {TABLE}) THEN
                RAISE EXCEPTION 'cannot downgrade while skill publications exist';
            END IF;
        END $$
    """)
    SkillPublicationRow.__table__.drop(op.get_bind())
