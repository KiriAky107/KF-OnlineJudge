"""Durable imports/candidates, append-only events and accepted-authority worker port."""

from alembic import op

from kf_oj.modules.operations.models import MaintenanceEvent
from kf_oj.modules.problems.import_models import (
    CandidateCase,
    ImportPool,
    ProblemCandidate,
    ProblemImportOperation,
)

revision = "0007_problem_imports"
down_revision = "0006_authority_operations"
branch_labels = depends_on = None


def sql(script):
    # asyncpg prepares one statement at a time; dollar-quoted bodies stay intact.
    buffer, quote, dollar, i = [], False, False, 0
    while i < len(script):
        if not quote and script[i : i + 2] == "$$":
            dollar = not dollar
            buffer.append("$$")
            i += 2
            continue
        character = script[i]
        if not dollar and character == "'":
            if quote and script[i : i + 2] == "''":
                buffer.append("''")
                i += 2
                continue
            quote = not quote
        if character == ";" and not dollar and not quote:
            if "".join(buffer).strip():
                op.execute("".join(buffer))
            buffer = []
        else:
            buffer.append(character)
        i += 1
    if "".join(buffer).strip():
        op.execute("".join(buffer))


def upgrade():
    connection = op.get_bind()
    # Cyclic candidate/operation link is installed only after both tables exist.
    for model in (
        ImportPool,
        ProblemImportOperation,
        ProblemCandidate,
        CandidateCase,
        MaintenanceEvent,
    ):
        model.__table__.create(connection)
    op.create_foreign_key(
        "fk_problem_import_candidate",
        "problem_import_operations",
        "problem_candidates",
        ["candidate_id"],
        ["id"],
    )
    op.execute("INSERT INTO problem_import_pool(id,reserved_bytes) VALUES(1,0)")
    sql("""
      CREATE FUNCTION public.kf_lock_reserved_maintenance(actor uuid, epoch bigint)
      RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
      DECLARE permitted boolean;
      BEGIN
        SELECT enabled AND is_administrator AND authorization_version=epoch
        INTO permitted FROM public.accounts WHERE id=actor FOR SHARE;
        IF permitted IS DISTINCT FROM true THEN
          RAISE EXCEPTION 'problem maintenance required' USING ERRCODE='42501';
        END IF;
      END $$;
      GRANT CREATE ON SCHEMA public TO kf_oj_identity_authority_owner;
      ALTER FUNCTION public.kf_lock_reserved_maintenance(uuid,bigint)
        OWNER TO kf_oj_identity_authority_owner;
      REVOKE CREATE ON SCHEMA public FROM kf_oj_identity_authority_owner;
      REVOKE ALL ON FUNCTION public.kf_lock_reserved_maintenance(uuid,bigint) FROM PUBLIC;
      GRANT EXECUTE ON FUNCTION public.kf_lock_reserved_maintenance(uuid,bigint)
        TO kf_oj_problem_import;
      GRANT SELECT,INSERT,UPDATE ON problem_import_operations TO kf_oj_platform;
      GRANT SELECT,UPDATE ON problem_import_pool TO kf_oj_platform,kf_oj_problem_import;
      GRANT SELECT ON problem_candidates,problem_candidate_cases TO kf_oj_platform;
      GRANT SELECT ON problem_import_operations TO kf_oj_problem_import;
      GRANT UPDATE(state,updated_at,lease_until,attempts,fence,
        reserved_bytes,failure_code,candidate_id)
        ON problem_import_operations TO kf_oj_problem_import;
      GRANT SELECT,INSERT,DELETE ON problem_candidates,problem_candidate_cases
        TO kf_oj_problem_import;
      GRANT SELECT,INSERT ON artifacts TO kf_oj_problem_import;
      GRANT INSERT ON problem_maintenance_events TO kf_oj_platform,kf_oj_problem_import;
      GRANT SELECT ON problem_maintenance_events TO kf_oj_platform;
    """)
    sql("""
      CREATE FUNCTION public.kf_immutable_import_candidate() RETURNS trigger
      LANGUAGE plpgsql SET search_path=pg_catalog,pg_temp AS $$
      DECLARE candidate uuid; current_state text;
      BEGIN
        IF TG_TABLE_NAME='problem_candidates' THEN candidate=OLD.id;
        ELSE candidate=OLD.candidate_id; END IF;
        SELECT o.state INTO current_state FROM public.problem_candidates c
          JOIN public.problem_import_operations o ON o.id=c.operation_id WHERE c.id=candidate;
        IF TG_OP='UPDATE' OR current_state NOT IN ('discarded','expired','failed') THEN
          RAISE EXCEPTION 'immutable candidate' USING ERRCODE='23514';
        END IF;
        RETURN OLD;
      END $$;
      CREATE TRIGGER immutable_candidate BEFORE UPDATE OR DELETE ON problem_candidates
        FOR EACH ROW EXECUTE FUNCTION public.kf_immutable_import_candidate();
      CREATE TRIGGER immutable_candidate_case BEFORE UPDATE OR DELETE ON problem_candidate_cases
        FOR EACH ROW EXECUTE FUNCTION public.kf_immutable_import_candidate();
      CREATE FUNCTION public.kf_insert_import_candidate() RETURNS trigger
      LANGUAGE plpgsql SET search_path=pg_catalog,pg_temp AS $$
      DECLARE operation uuid; current_state text;
      BEGIN
        IF TG_TABLE_NAME='problem_candidates' THEN operation=NEW.operation_id;
        ELSE SELECT operation_id INTO operation FROM public.problem_candidates
          WHERE id=NEW.candidate_id; END IF;
        SELECT state INTO current_state FROM public.problem_import_operations
          WHERE id=operation FOR SHARE;
        IF current_state IS DISTINCT FROM 'validating' THEN
          RAISE EXCEPTION 'candidate is not being validated' USING ERRCODE='23514';
        END IF; RETURN NEW;
      END $$;
      CREATE TRIGGER insert_candidate BEFORE INSERT ON problem_candidates
        FOR EACH ROW EXECUTE FUNCTION public.kf_insert_import_candidate();
      CREATE TRIGGER insert_candidate_case BEFORE INSERT ON problem_candidate_cases
        FOR EACH ROW EXECUTE FUNCTION public.kf_insert_import_candidate();
      CREATE FUNCTION public.kf_fixed_import_receipt() RETURNS trigger
      LANGUAGE plpgsql SET search_path=pg_catalog,pg_temp AS $$ BEGIN
        IF NEW.id IS DISTINCT FROM OLD.id OR NEW.owner_id IS DISTINCT FROM OLD.owner_id
          OR NEW.authorization_version IS DISTINCT FROM OLD.authorization_version
          OR NEW.archive_digest IS DISTINCT FROM OLD.archive_digest
          OR NEW.archive_bytes IS DISTINCT FROM OLD.archive_bytes
          OR NEW.created_at IS DISTINCT FROM OLD.created_at
          OR NEW.expires_at IS DISTINCT FROM OLD.expires_at THEN
          RAISE EXCEPTION 'immutable import receipt' USING ERRCODE='23514';
        END IF;
        IF OLD.state IN ('ready','published') AND NEW.state IN ('ready','published')
          AND NEW.candidate_id IS DISTINCT FROM OLD.candidate_id THEN
          RAISE EXCEPTION 'immutable completed candidate receipt' USING ERRCODE='23514';
        END IF;
        IF OLD.state='published' AND (NEW.state IS DISTINCT FROM OLD.state
          OR NEW.published_version_id IS DISTINCT FROM OLD.published_version_id) THEN
          RAISE EXCEPTION 'immutable published import receipt' USING ERRCODE='23514';
        END IF;
        IF NEW.state='ready' THEN
          IF (OLD.state IS DISTINCT FROM 'ready' AND
              (NOT pg_has_role(current_user,'kf_oj_problem_import','USAGE')
                OR OLD.state!='validating')) OR NEW.candidate_id IS NULL
            OR NOT EXISTS(SELECT 1 FROM public.problem_candidates c
              WHERE c.id=NEW.candidate_id AND c.operation_id=NEW.id
                AND c.case_count=(SELECT count(*) FROM public.problem_candidate_cases b
                  WHERE b.candidate_id=c.id)
                AND c.case_count-1=(SELECT max(position) FROM public.problem_candidate_cases b
                  WHERE b.candidate_id=c.id)) THEN
            RAISE EXCEPTION 'invalid candidate completion' USING ERRCODE='23514';
          END IF;
        END IF; RETURN NEW;
      END $$;
      CREATE TRIGGER fixed_import_receipt BEFORE UPDATE ON problem_import_operations
        FOR EACH ROW EXECUTE FUNCTION public.kf_fixed_import_receipt();
    """)


def downgrade():
    connection = op.get_bind()
    if connection.exec_driver_sql(
        "SELECT EXISTS(SELECT 1 FROM problem_import_operations)"
    ).scalar():
        raise RuntimeError("cannot discard durable import history")
    op.execute("DROP FUNCTION public.kf_lock_reserved_maintenance(uuid,bigint)")
    op.drop_constraint(
        "fk_problem_import_candidate", "problem_import_operations", type_="foreignkey"
    )
    for model in (
        MaintenanceEvent,
        CandidateCase,
        ProblemCandidate,
        ProblemImportOperation,
        ImportPool,
    ):
        model.__table__.drop(connection)
    sql(
        "DROP FUNCTION public.kf_immutable_import_candidate(); "
        "DROP FUNCTION public.kf_fixed_import_receipt(); "
        "DROP FUNCTION public.kf_insert_import_candidate()"
    )
