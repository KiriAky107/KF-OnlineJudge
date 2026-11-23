"""Store private skill version reports and grant only review field changes."""

from alembic import op

from kf_oj.modules.skills.report_models import SkillReportRow

revision = "0027_skill_reports"
down_revision = "0026_skill_version_ratings"
branch_labels = depends_on = None

TABLE = "skills_reports"


def upgrade():
    SkillReportRow.__table__.create(op.get_bind())
    op.execute(
        f"REVOKE ALL ON {TABLE} FROM PUBLIC, "
        "kf_oj_coordinator, kf_oj_learning_worker, kf_oj_platform"
    )
    op.execute(f"GRANT SELECT, INSERT ON {TABLE} TO kf_oj_platform")
    op.execute(f"GRANT UPDATE (state, reviewer_id, note, decided_at) ON {TABLE} TO kf_oj_platform")


def downgrade():
    op.execute(f"""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM {TABLE}) THEN
                RAISE EXCEPTION 'cannot downgrade while skill reports exist';
            END IF;
        END $$
    """)
    SkillReportRow.__table__.drop(op.get_bind())
