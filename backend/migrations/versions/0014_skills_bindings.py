"""Store private user skills and immutable run configurations."""

from alembic import op

from kf_oj.modules.skills.binding_models import SkillBindingRow, SkillConfigurationRow

revision = "0014_skills_bindings"
down_revision = "0013_learning_agent_tools"
branch_labels = depends_on = None


def upgrade():
    for model in (SkillBindingRow, SkillConfigurationRow):
        model.__table__.create(op.get_bind())
    tables = "skills_bindings, skills_run_configurations"
    op.execute(f"REVOKE ALL ON {tables} FROM PUBLIC, kf_oj_coordinator, kf_oj_platform")
    op.execute(f"GRANT SELECT, INSERT ON {tables} TO kf_oj_platform")
    op.execute("""GRANT UPDATE (version_id, preferences, authorized_tools, enabled,
                 authorization_epoch) ON skills_bindings TO kf_oj_platform""")


def downgrade():
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM skills_bindings)
                OR EXISTS (SELECT 1 FROM skills_run_configurations) THEN
                RAISE EXCEPTION 'cannot downgrade while skill records exist';
            END IF;
        END $$
    """)
    for model in (SkillConfigurationRow, SkillBindingRow):
        model.__table__.drop(op.get_bind())
