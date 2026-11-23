"""Persist one private rating per account and published version."""

from alembic import op

from kf_oj.modules.skills.publication_models import SkillRatingRow

revision = "0026_skill_version_ratings"
down_revision = "0025_skill_favorites"
branch_labels = depends_on = None

TABLE = "skills_ratings"


def upgrade():
    SkillRatingRow.__table__.create(op.get_bind())
    op.execute(
        f"REVOKE ALL ON {TABLE} FROM PUBLIC, "
        "kf_oj_coordinator, kf_oj_learning_worker, kf_oj_platform"
    )
    op.execute(f"GRANT SELECT, INSERT, DELETE ON {TABLE} TO kf_oj_platform")
    op.execute(f"GRANT UPDATE (score) ON {TABLE} TO kf_oj_platform")


def downgrade():
    op.execute(f"""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM {TABLE}) THEN
                RAISE EXCEPTION 'cannot downgrade while skill ratings exist';
            END IF;
        END $$
    """)
    SkillRatingRow.__table__.drop(op.get_bind())
