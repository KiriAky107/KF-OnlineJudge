import asyncio
import json
from uuid import uuid4

import asyncpg
import pytest
from conftest import connect, migrate


def test_migration_roundtrip_and_role_safety(postgres_url):
    async def inspect():
        c = await connect(postgres_url)
        try:
            tables = set(await c.fetch("SELECT tablename FROM pg_tables WHERE schemaname='public'"))
            assert len(tables) == 15  # Fourteen domain/control tables and Alembic history.
            roles = await c.fetch("SELECT * FROM pg_roles WHERE rolname LIKE 'kf_oj_%'")
            assert {row["rolname"] for row in roles} == {
                "kf_oj_platform",
                "kf_oj_coordinator",
                "kf_oj_identity_operator",
                "kf_oj_problem_import",
                "kf_oj_identity_authority_owner",
            }
            assert all(
                not any(
                    r[k]
                    for k in (
                        "rolsuper",
                        "rolcanlogin",
                        "rolcreatedb",
                        "rolcreaterole",
                        "rolbypassrls",
                    )
                )
                for r in roles
            )
            await c.execute("SET ROLE kf_oj_platform")
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await c.execute("CREATE TABLE forbidden_table (x int)")
            await c.execute("RESET ROLE")
        finally:
            await c.close()

    asyncio.run(inspect())
    migrate(postgres_url, "downgrade", "base")
    migrate(postgres_url, "upgrade", "head")
    migrate(postgres_url, "check")
    asyncio.run(inspect())


async def fixture_rows(c):
    account, problem, version, case, submission, evaluation = (uuid4() for _ in range(6))
    await c.execute(
        "INSERT INTO accounts (id,username,password_hash) VALUES ($1,'fixture_user',$2)",
        account,
        "synthetic-irreversible-placeholder-hash-" * 2,
    )
    await c.execute("INSERT INTO artifacts (digest,bytes) VALUES ($1,3),($2,0)", "a" * 64, "b" * 64)
    await c.execute("INSERT INTO problems (id,slug) VALUES ($1,'fixture-problem')", problem)
    await c.execute(
        """INSERT INTO problem_versions
        (id,problem_id,revision,title,statement,languages,toolchain,compile_limits,run_limits,
         job_wall_time_us,public_samples)
        VALUES ($1,$2,1,'Fixture','Public statement','["c17"]','{}','{}','{}',10000,'[]')""",
        version,
        problem,
    )
    await c.execute(
        """INSERT INTO problem_cases
        (id,version_id,position,input_digest,expected_digest) VALUES ($1,$2,0,$3,$4)""",
        case,
        version,
        "b" * 64,
        "a" * 64,
    )
    await c.execute("UPDATE problem_versions SET published_at=now() WHERE id=$1", version)
    await c.execute("UPDATE problems SET active_version_id=$1 WHERE id=$2", version, problem)
    await c.execute(
        """INSERT INTO submissions
        (id,account_id,problem_id,problem_version_id,language,source_digest,idempotency_key,
        request_digest) VALUES ($1,$2,$3,$4,'c17',$5,$6,$7)""",
        submission,
        account,
        problem,
        version,
        "a" * 64,
        uuid4(),
        "c" * 64,
    )
    await c.execute(
        "INSERT INTO evaluations (id,submission_id,problem_version_id) VALUES ($1,$2,$3)",
        evaluation,
        submission,
        version,
    )
    await c.execute("INSERT INTO outbox (id,evaluation_id) VALUES ($1,$2)", uuid4(), evaluation)
    return account, problem, version, case, submission, evaluation


def test_database_write_authorities_and_immutable_versions(postgres_url):
    async def run():
        c = await connect(postgres_url)
        try:
            await c.execute("SET ROLE kf_oj_platform")
            ids = await fixture_rows(c)
            assert await c.fetchval("SELECT state FROM evaluations WHERE id=$1", ids[5]) == "queued"
            for command in (
                "UPDATE evaluations SET state='finished'",
                "INSERT INTO evaluations (id,submission_id,problem_version_id,state) "
                "SELECT gen_random_uuid(),submission_id,problem_version_id,'finished' "
                "FROM evaluations",
                "DELETE FROM evaluations",
                "UPDATE problem_versions SET title='altered'",
                "INSERT INTO judge_attempts (id) VALUES (gen_random_uuid())",
                "UPDATE artifacts SET bytes=42",
            ):
                with pytest.raises(asyncpg.InsufficientPrivilegeError):
                    await c.execute(command)
            with pytest.raises(asyncpg.CheckViolationError):
                await c.execute("UPDATE problem_versions SET published_at=now()")
            with pytest.raises(asyncpg.CheckViolationError):
                await c.execute(
                    """INSERT INTO problem_cases
                    (id,version_id,position,input_digest,expected_digest)
                    VALUES ($1,$2,1,$3,$3)""",
                    uuid4(),
                    ids[2],
                    "a" * 64,
                )
            with pytest.raises(asyncpg.ForeignKeyViolationError):
                await c.execute("UPDATE artifacts SET state='deleting' WHERE digest=$1", "a" * 64)
            await c.execute("RESET ROLE")
            await c.execute("SET ROLE kf_oj_coordinator")
            for command in (
                "UPDATE accounts SET enabled=false",
                "INSERT INTO problems (id,slug) VALUES (gen_random_uuid(),'forbidden')",
                "UPDATE submissions SET language='cpp20'",
            ):
                with pytest.raises(asyncpg.InsufficientPrivilegeError):
                    await c.execute(command)
            attempt = uuid4()
            async with c.transaction():
                await c.execute(
                    """INSERT INTO judge_attempts
                    (id,evaluation_id,worker_id,number,fencing_version,lease_token_hash,
                    expires_at,manifest,manifest_digest)
                    VALUES ($1,$2,$3,1,1,$4,now()+interval '30 seconds',$5,$6)""",
                    attempt,
                    ids[5],
                    uuid4(),
                    "d" * 64,
                    json.dumps({"fixture": True}),
                    "e" * 64,
                )
                await c.execute(
                    """UPDATE evaluations SET current_attempt_id=$1,state='claimed',
                    attempts_count=1,fencing_version=1 WHERE id=$2""",
                    attempt,
                    ids[5],
                )
            assert await c.fetchval("SELECT state FROM evaluations") == "claimed"
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await c.execute("UPDATE judge_attempts SET manifest='{}'")
            # Active-attempt FK is deferred but still rejects an unrelated/absent attempt at commit.
            with pytest.raises(asyncpg.ForeignKeyViolationError):
                async with c.transaction():
                    await c.execute("UPDATE evaluations SET current_attempt_id=$1", uuid4())
        finally:
            await c.close()

    asyncio.run(run())


def test_initial_creation_transaction_rolls_back_and_cross_version_fk_rejects(postgres_url):
    async def run():
        c = await connect(postgres_url)
        try:
            with pytest.raises(RuntimeError):
                async with c.transaction():
                    await c.execute("SET LOCAL ROLE kf_oj_platform")
                    await fixture_rows(c)
                    raise RuntimeError("simulated transaction failure")
            assert await c.fetchval("SELECT count(*) FROM submissions") == 0
            assert await c.fetchval("SELECT count(*) FROM outbox") == 0
            ids = await fixture_rows(c)
            with pytest.raises(asyncpg.ForeignKeyViolationError):
                await c.execute(
                    "UPDATE evaluations SET problem_version_id=$1 WHERE id=$2",
                    uuid4(),
                    ids[5],
                )
        finally:
            await c.close()

    asyncio.run(run())


def test_publication_serializes_concurrent_case_insertion(postgres_url):
    async def run():
        c, other = await connect(postgres_url), await connect(postgres_url)
        pending = None
        transaction = None
        try:
            ids = await fixture_rows(c)
            # A new draft version, separate from the immutable already published fixture.
            draft = uuid4()
            await c.execute(
                """INSERT INTO problem_versions
                (id,problem_id,revision,title,statement,languages,toolchain,compile_limits,
                run_limits,job_wall_time_us,public_samples)
                SELECT $1,problem_id,2,title,statement,languages,toolchain,compile_limits,
                    run_limits,job_wall_time_us,public_samples FROM problem_versions WHERE id=$2
                """,
                draft,
                ids[2],
            )
            await c.execute(
                """INSERT INTO problem_cases
                (id,version_id,position,input_digest,expected_digest)
                VALUES ($1,$2,0,$3,$3)""",
                uuid4(),
                draft,
                "a" * 64,
            )
            transaction = c.transaction()
            await transaction.start()
            await c.execute("UPDATE problem_versions SET published_at=now() WHERE id=$1", draft)
            pending = asyncio.create_task(
                other.execute(
                    """INSERT INTO problem_cases
                (id,version_id,position,input_digest,expected_digest)
                VALUES ($1,$2,1,$3,$3)""",
                    uuid4(),
                    draft,
                    "a" * 64,
                )
            )
            await asyncio.sleep(0.1)
            assert not pending.done()  # Actual row lock, not a simulated repository response.
            await transaction.commit()
            transaction = None
            with pytest.raises(asyncpg.CheckViolationError):
                await asyncio.wait_for(pending, timeout=5)
            assert (
                await c.fetchval("SELECT count(*) FROM problem_cases WHERE version_id=$1", draft)
                == 1
            )
            with pytest.raises(asyncpg.CheckViolationError):
                await c.execute(
                    """INSERT INTO problem_versions
                    (id,problem_id,revision,title,statement,languages,toolchain,compile_limits,
                    run_limits,job_wall_time_us,public_samples,published_at)
                    SELECT $1,problem_id,3,title,statement,languages,toolchain,compile_limits,
                        run_limits,job_wall_time_us,public_samples,now()
                    FROM problem_versions WHERE id=$2""",
                    uuid4(),
                    draft,
                )
        finally:
            if transaction:
                await transaction.rollback()
            if pending and not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
            await c.close()
            await other.close()

    asyncio.run(run())
