"""Learning metadata separate from immutable problem versions."""

import sqlalchemy as sa
from alembic import op

from kf_oj.modules.problems.learning_models import ProblemLearningMetadata

revision = "0009_problem_learning_metadata"
down_revision = "0008_problem_maintenance"
branch_labels = depends_on = None
ACTIONS = (
    "'upload_created','upload_received','import_ready','import_failed','import_discarded','import_expired',"
    "'published','disabled','restored',"
    "'authority_bootstrap','authority_grant','authority_revoke','authority_enable','authority_disable'"
)


def upgrade():
    op.alter_column("problem_maintenance_events", "action", type_=sa.String(32))
    ProblemLearningMetadata.__table__.create(op.get_bind())
    op.execute("GRANT SELECT,INSERT,UPDATE ON problem_learning_metadata TO kf_oj_platform")
    op.drop_constraint(op.f("ck_problem_maintenance_events_action"), "problem_maintenance_events")
    op.create_check_constraint(
        "action",
        "problem_maintenance_events",
        "action IN (" + ACTIONS + ",'learning_metadata_updated')",
    )


def downgrade():
    op.drop_table("problem_learning_metadata")
    # Keep the additive audit action to preserve append-only historical events.
