"""Keep the hint limit with shared training snapshots."""

import sqlalchemy as sa
from alembic import op

revision = "0066_publication_hint_ceiling"
down_revision = "0065_self_test_comparisons"
branch_labels = depends_on = None

_PUBLICATIONS = "learning_training_publications"


def _replace_training_ceiling(*, with_publication: bool):
    maximum = (
        "LEAST(p.max_hint_level, COALESCE(s.max_hint_level, 'L3'))"
        if with_publication
        else "p.max_hint_level"
    )
    publication = (
        "LEFT JOIN public.learning_training_publications AS s ON s.id=p.publication_id"
        if with_publication
        else ""
    )
    op.execute(f"""
        CREATE OR REPLACE FUNCTION public.kf_training_hint_ceiling(
            actor uuid, plan uuid, version uuid
        )
        RETURNS text LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
            SELECT {maximum}::text FROM public.learning_plans AS p
            JOIN public.learning_plan_items AS i ON i.plan_id=p.id
            {publication}
            WHERE p.id=plan AND p.owner_id=actor AND i.version_id=version
            FOR SHARE OF p, i
        $$
    """)


def upgrade():
    op.add_column(
        _PUBLICATIONS,
        sa.Column("max_hint_level", sa.String(2), nullable=False, server_default="L3"),
    )
    op.create_check_constraint(
        "hint_ceiling", _PUBLICATIONS, "max_hint_level IN ('L0','L1','L2','L3')"
    )
    op.execute(f"""
        UPDATE {_PUBLICATIONS} AS s SET max_hint_level=p.max_hint_level
        FROM learning_plans AS p WHERE p.id=s.source_plan_id
    """)
    op.execute(f"""
        UPDATE learning_plans AS p SET max_hint_level=s.max_hint_level
        FROM {_PUBLICATIONS} AS s
        WHERE s.id=p.publication_id AND p.max_hint_level > s.max_hint_level
    """)
    _replace_training_ceiling(with_publication=True)


def downgrade():
    op.execute(f"""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM {_PUBLICATIONS} WHERE max_hint_level <> 'L3') THEN
                RAISE EXCEPTION 'cannot downgrade while published hint limits exist';
            END IF;
        END $$
    """)
    _replace_training_ceiling(with_publication=False)
    op.drop_constraint("hint_ceiling", _PUBLICATIONS, type_="check")
    op.drop_column(_PUBLICATIONS, "max_hint_level")
