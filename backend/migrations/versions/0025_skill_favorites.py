"""Persist personal favorites independently of skill binding and review."""

from alembic import op

from kf_oj.modules.skills.publication_models import SkillFavoriteRow

revision = "0025_skill_favorites"
down_revision = "0024_published_skill_runtime"
branch_labels = depends_on = None

TABLE = "skills_favorites"


def upgrade():
    SkillFavoriteRow.__table__.create(op.get_bind())
    op.execute(
        f"REVOKE ALL ON {TABLE} FROM PUBLIC, "
        "kf_oj_coordinator, kf_oj_learning_worker, kf_oj_platform"
    )
    op.execute(f"GRANT SELECT, INSERT, DELETE ON {TABLE} TO kf_oj_platform")


def downgrade():
    op.execute(f"""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM {TABLE}) THEN
                RAISE EXCEPTION 'cannot downgrade while skill favorites exist';
            END IF;
        END $$
    """)
    SkillFavoriteRow.__table__.drop(op.get_bind())
