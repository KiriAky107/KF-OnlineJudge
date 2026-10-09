"""Immutable per-language judging configuration and extended submission language IDs.

Revision ID: 0002_multi_runtime
Revises: 0001_judging_foundation
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0002_multi_runtime"
down_revision = "0001_judging_foundation"
branch_labels = None
depends_on = None

CONFIGURATION_CHECK = """
    (judging_schema_version = 1 AND language_configurations IS NULL
     AND toolchain IS NOT NULL AND compile_limits IS NOT NULL
     AND run_limits IS NOT NULL AND job_wall_time_us IS NOT NULL)
    OR
    (judging_schema_version = 2 AND language_configurations IS NOT NULL
     AND toolchain IS NULL AND compile_limits IS NULL
     AND run_limits IS NULL AND job_wall_time_us IS NULL
     AND public.kf_valid_language_configurations(languages, language_configurations))
"""


def upgrade() -> None:
    # Metadata-only additions/widening; do not UPDATE any immutable published rows.
    op.add_column(
        "problem_versions",
        sa.Column("judging_schema_version", sa.SmallInteger(), nullable=False, server_default="1"),
    )
    op.add_column(
        "problem_versions",
        sa.Column("language_configurations", JSONB(none_as_null=True), nullable=True),
    )
    for column, kind in (
        ("toolchain", JSONB()),
        ("compile_limits", JSONB()),
        ("run_limits", JSONB()),
        ("job_wall_time_us", sa.BigInteger()),
    ):
        op.alter_column("problem_versions", column, existing_type=kind, nullable=True)
    op.alter_column(
        "submissions",
        "language",
        existing_type=sa.String(8),
        type_=sa.String(16),
        existing_nullable=False,
    )
    op.drop_constraint(op.f("ck_submissions_language"), "submissions", type_="check")
    op.create_check_constraint(
        op.f("ck_submissions_language"),
        "submissions",
        "language IN ('c17','cpp20','python313','java21')",
    )
    op.execute("""
        CREATE FUNCTION public.kf_valid_runtime_limits(limits jsonb)
        RETURNS boolean LANGUAGE plpgsql IMMUTABLE STRICT
        SET search_path = pg_catalog, public AS $$
        DECLARE entry record;
        BEGIN
            IF jsonb_typeof(limits) != 'object' OR
                (SELECT count(*) FROM jsonb_object_keys(limits)) != 10 THEN RETURN false; END IF;
            FOR entry IN SELECT * FROM (VALUES
                ('cpu_time_us',1000::numeric,60000000::numeric),
                ('wall_time_us',1000,120000000), ('memory_bytes',1,2147483648),
                ('stack_bytes',1,268435456), ('processes',1,128), ('open_files',3,1024),
                ('stdout_bytes',1,16777216), ('stderr_bytes',1,16777216),
                ('file_bytes',1,67108864), ('workdir_bytes',1,268435456)
            ) AS bounds(name,lo,hi) LOOP
                IF NOT limits ? entry.name OR jsonb_typeof(limits->entry.name) != 'number'
                    OR (limits->>entry.name) !~ '^(0|[1-9][0-9]*)$'
                    OR length(limits->>entry.name) > 19 THEN RETURN false; END IF;
                IF (limits->>entry.name)::numeric NOT BETWEEN entry.lo AND entry.hi
                    THEN RETURN false; END IF;
            END LOOP;
            RETURN (limits->>'stack_bytes')::numeric <= (limits->>'memory_bytes')::numeric
                AND (limits->>'file_bytes')::numeric <= (limits->>'workdir_bytes')::numeric
                AND (limits->>'stdout_bytes')::numeric <= (limits->>'file_bytes')::numeric
                AND (limits->>'stderr_bytes')::numeric <= (limits->>'file_bytes')::numeric
                AND (limits->>'stdout_bytes')::numeric + (limits->>'stderr_bytes')::numeric
                    <= (limits->>'workdir_bytes')::numeric;
        END $$
    """)
    op.execute("""
        CREATE FUNCTION public.kf_valid_language_configurations(languages jsonb, configs jsonb)
        RETURNS boolean LANGUAGE plpgsql IMMUTABLE STRICT
        SET search_path = pg_catalog, public AS $$
        DECLARE item record; cfg jsonb; runtime jsonb; lang text; seen text[] := ARRAY[]::text[];
            phase jsonb; preparing boolean;
        BEGIN
            IF jsonb_typeof(languages) != 'array' OR jsonb_typeof(configs) != 'array'
                THEN RETURN false; END IF;
            IF jsonb_array_length(configs) NOT BETWEEN 1 AND 4 OR
                jsonb_array_length(languages) != jsonb_array_length(configs)
                    THEN RETURN false; END IF;
            FOR item IN SELECT value, ordinality FROM jsonb_array_elements(configs)
                WITH ORDINALITY LOOP
                cfg := item.value;
                IF jsonb_typeof(cfg) != 'object' OR
                    (SELECT count(*) FROM jsonb_object_keys(cfg)) != 5 OR NOT cfg ?&
                    ARRAY['language','toolchain','compile_limits','run_limits','job_wall_time_us']
                    THEN RETURN false; END IF;
                IF jsonb_typeof(cfg->'language') != 'string' THEN RETURN false; END IF;
                lang := cfg->>'language';
                IF lang NOT IN ('c17','cpp20','python313','java21') OR lang = ANY(seen)
                    OR languages->(item.ordinality::int-1) != cfg->'language'
                    THEN RETURN false; END IF;
                seen := array_append(seen,lang); runtime := cfg->'toolchain';
                IF jsonb_typeof(runtime) != 'object' OR
                    (SELECT count(*) FROM jsonb_object_keys(runtime)) != 5 OR NOT runtime ?&
                    ARRAY['toolchain_id','image_kind','image_revision','image_digest','runtime_version']
                    THEN RETURN false; END IF;
                IF jsonb_typeof(runtime->'toolchain_id') != 'string' OR
                    jsonb_typeof(runtime->'image_kind') != 'string' OR
                    jsonb_typeof(runtime->'runtime_version') != 'string' OR
                    jsonb_typeof(runtime->'image_digest') != 'string' OR
                    (runtime->>'image_digest') !~ '^[0-9a-f]{64}$' OR
                    (runtime->>'runtime_version') !~ '^[A-Za-z0-9][A-Za-z0-9_.+-]{0,63}$' OR
                    jsonb_typeof(runtime->'image_revision') != 'number' OR
                    (runtime->>'image_revision') !~ '^[1-9][0-9]{0,9}$'
                    THEN RETURN false; END IF;
                IF (runtime->>'image_revision')::numeric > 4294967295 THEN RETURN false; END IF;
                IF lang IN ('c17','cpp20') AND (runtime->>'toolchain_id' != 'gnu-cxx-v1' OR
                    runtime->>'image_kind' != 'linux_gnu_cxx_x86_64_v1') THEN RETURN false; END IF;
                IF lang = 'python313' AND (runtime->>'toolchain_id' != 'cpython313-v1' OR
                    runtime->>'image_kind' != 'linux_cpython313_x86_64_v1' OR
                    runtime->>'runtime_version' != '3.13.16') THEN RETURN false; END IF;
                IF lang = 'java21' AND (runtime->>'toolchain_id' != 'openjdk21-v1' OR
                    runtime->>'image_kind' != 'linux_openjdk21_x86_64_v1' OR
                    (runtime->>'runtime_version') !~
                        '^21[.][0-9]+[.][0-9]+([.][0-9]+)?[+][1-9][0-9]*(-LTS)?$')
                    THEN RETURN false; END IF;
                IF NOT public.kf_valid_runtime_limits(cfg->'compile_limits') OR
                    NOT public.kf_valid_runtime_limits(cfg->'run_limits') THEN RETURN false; END IF;
                IF jsonb_typeof(cfg->'job_wall_time_us') != 'number' OR
                    (cfg->>'job_wall_time_us') !~ '^[1-9][0-9]{0,9}$' THEN RETURN false; END IF;
                IF (cfg->>'job_wall_time_us')::numeric NOT BETWEEN 1000 AND 3600000000 OR
                    (cfg->'compile_limits'->>'wall_time_us')::numeric +
                    (cfg->'run_limits'->>'wall_time_us')::numeric >
                        (cfg->>'job_wall_time_us')::numeric
                    THEN RETURN false; END IF;
                FOR preparing, phase IN SELECT true,cfg->'compile_limits' UNION ALL
                    SELECT false,cfg->'run_limits' LOOP
                    IF lang = 'python313' AND ((phase->>'cpu_time_us')::numeric < 10000 OR
                        (phase->>'wall_time_us')::numeric < 50000 OR
                        (phase->>'memory_bytes')::numeric < 33554432 OR
                        (phase->>'open_files')::numeric < 16) THEN RETURN false; END IF;
                    IF lang = 'java21' AND ((phase->>'cpu_time_us')::numeric <
                        CASE WHEN preparing THEN 1000000 ELSE 200000 END OR
                        (phase->>'wall_time_us')::numeric <
                        CASE WHEN preparing THEN 3000000 ELSE 1000000 END OR
                        (phase->>'memory_bytes')::numeric <
                        CASE WHEN preparing THEN 402653184 ELSE 268435456 END OR
                        (phase->>'processes')::numeric < 16 OR
                        (phase->>'open_files')::numeric < 32 OR
                        (phase->>'stack_bytes')::numeric < 262144 OR
                        (phase->>'memory_bytes')::numeric - 167772160 -
                        (phase->>'processes')::numeric * 262144 < 33554432)
                        THEN RETURN false; END IF;
                END LOOP;
            END LOOP;
            RETURN true;
        END $$
    """)
    op.execute("REVOKE ALL ON FUNCTION public.kf_valid_runtime_limits(jsonb) FROM PUBLIC")
    op.execute(
        "REVOKE ALL ON FUNCTION public.kf_valid_language_configurations(jsonb,jsonb) FROM PUBLIC"
    )
    op.execute("GRANT EXECUTE ON FUNCTION public.kf_valid_runtime_limits(jsonb) TO kf_oj_platform")
    op.execute(
        "GRANT EXECUTE ON FUNCTION public.kf_valid_language_configurations(jsonb,jsonb) "
        "TO kf_oj_platform"
    )
    op.create_check_constraint(
        op.f("ck_problem_versions_judging_schema"),
        "problem_versions",
        "judging_schema_version IN (1,2)",
    )
    op.create_check_constraint(
        op.f("ck_problem_versions_language_configurations"), "problem_versions", CONFIGURATION_CHECK
    )
    op.execute("""
        CREATE FUNCTION public.kf_submission_language() RETURNS trigger LANGUAGE plpgsql
        SET search_path = pg_catalog, public AS $$
        DECLARE supported jsonb;
        BEGIN
            SELECT languages INTO supported FROM public.problem_versions
                WHERE id=NEW.problem_version_id;
            IF FOUND AND NOT supported @> jsonb_build_array(NEW.language) THEN
                RAISE EXCEPTION 'submission language not supported by version'
                    USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute("REVOKE ALL ON FUNCTION public.kf_submission_language() FROM PUBLIC")
    op.execute("""
        CREATE TRIGGER kf_submission_language BEFORE INSERT OR UPDATE OF
            language,problem_version_id ON public.submissions
        FOR EACH ROW EXECUTE FUNCTION public.kf_submission_language()
    """)


def downgrade() -> None:
    # Losing a runtime configuration is not a rollback strategy. Preserve all user data.
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM problem_versions WHERE judging_schema_version = 2) OR
                EXISTS (SELECT 1 FROM submissions WHERE language NOT IN ('c17','cpp20')) THEN
                RAISE EXCEPTION 'cannot downgrade while multi-runtime data exists'
                    USING ERRCODE = '23514';
            END IF;
        END $$
    """)
    op.drop_constraint(
        op.f("ck_problem_versions_language_configurations"), "problem_versions", type_="check"
    )
    op.drop_constraint(
        op.f("ck_problem_versions_judging_schema"), "problem_versions", type_="check"
    )
    op.execute("DROP TRIGGER kf_submission_language ON public.submissions")
    op.execute("DROP FUNCTION public.kf_submission_language()")
    op.execute("DROP FUNCTION public.kf_valid_language_configurations(jsonb,jsonb)")
    op.execute("DROP FUNCTION public.kf_valid_runtime_limits(jsonb)")
    op.drop_column("problem_versions", "language_configurations")
    op.drop_column("problem_versions", "judging_schema_version")
    for column, kind in (
        ("toolchain", JSONB()),
        ("compile_limits", JSONB()),
        ("run_limits", JSONB()),
        ("job_wall_time_us", sa.BigInteger()),
    ):
        op.alter_column("problem_versions", column, existing_type=kind, nullable=False)
    op.drop_constraint(op.f("ck_submissions_language"), "submissions", type_="check")
    op.create_check_constraint(
        op.f("ck_submissions_language"), "submissions", "language IN ('c17','cpp20')"
    )
    op.alter_column(
        "submissions",
        "language",
        existing_type=sa.String(16),
        type_=sa.String(8),
        existing_nullable=False,
    )
