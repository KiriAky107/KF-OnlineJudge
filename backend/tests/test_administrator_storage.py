"""Actual temporary PG migration and authority storage; API/CLI acceptance is separate."""

import asyncio
import subprocess
from datetime import timedelta
from uuid import uuid4

import asyncpg
import pytest
from conftest import connect, migrate
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_database_foundation import fixture_rows

from kf_oj.modules.identity.public import AuthenticationFailed, Principal
from kf_oj.modules.identity.service import IdentityService, token_digest


class RollBackAuthorityChange(Exception):
    """Abort the test transaction before any authority change commits."""


async def submission_snapshot(connection):
    # Synthetic queued metadata only; this does not claim real program execution.
    return {
        table: list(
            await connection.fetch(
                f"SELECT to_jsonb(t)::text FROM {table} t ORDER BY to_jsonb(t)::text"
            )
        )
        for table in (
            "artifacts",
            "problems",
            "problem_versions",
            "problem_cases",
            "submissions",
            "evaluations",
            "outbox",
        )
    }


async def account_and_session(connection):
    account = uuid4()
    token, csrf = uuid4().hex * 2, uuid4().hex * 2
    await connection.execute(
        "INSERT INTO accounts(id,username,password_hash) VALUES($1,'storage_user',$2)",
        account,
        "synthetic-irreversible-placeholder-hash-" * 2,
    )
    now = await connection.fetchval("SELECT clock_timestamp()")
    await connection.execute(
        "INSERT INTO sessions(token_hash,csrf_hash,account_id,expires_at) VALUES($1,$2,$3,$4)",
        token_digest(token),
        token_digest(csrf),
        account,
        now + timedelta(hours=1),
    )
    return account, token, csrf


def test_upgrade_preserves_ordinary_account_and_current_session(postgres_url):
    migrate(postgres_url, "downgrade", "0004_performance_watermark")

    async def seed():
        connection = await connect(postgres_url)
        try:
            account, token, csrf = await account_and_session(connection)
            await fixture_rows(connection)
            return account, token, csrf, await submission_snapshot(connection)
        finally:
            await connection.close()

    account, token, csrf, before = asyncio.run(seed())
    migrate(postgres_url, "upgrade", "head")
    migrate(postgres_url, "check")

    async def inspect():
        connection = await connect(postgres_url)
        engine = create_async_engine(postgres_url)
        try:
            row = await connection.fetchrow("SELECT * FROM accounts WHERE id=$1", account)
            assert row["enabled"] and not row["is_administrator"]
            assert row["authorization_version"] == 1
            assert await submission_snapshot(connection) == before
            service = IdentityService(async_sessionmaker(engine, expire_on_commit=False))
            assert await service.authenticate(token, csrf_token=csrf) == Principal(
                account, "storage_user"
            )
        finally:
            await connection.close()
            await engine.dispose()

    asyncio.run(inspect())


def test_authority_columns_and_import_role_are_not_runtime_write_permissions(postgres_url):
    async def inspect():
        connection = await connect(postgres_url)
        try:
            for role in ("kf_oj_platform", "kf_oj_coordinator", "kf_oj_problem_import"):
                for column in ("is_administrator", "authorization_version"):
                    assert not await connection.fetchval(
                        "SELECT has_column_privilege($1,'accounts',$2,'UPDATE')", role, column
                    )
                    assert not await connection.fetchval(
                        "SELECT has_column_privilege($1,'accounts',$2,'INSERT')", role, column
                    )
            for table in (
                "evaluations",
                "judge_attempts",
                "case_results",
                "evaluation_performance",
            ):
                for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
                    assert not await connection.fetchval(
                        "SELECT has_table_privilege('kf_oj_problem_import',$1,$2)", table, privilege
                    )
            assert await connection.fetchval(
                "SELECT has_column_privilege('kf_oj_platform','accounts','enabled','UPDATE')"
            )
            assert not await connection.fetchval(
                "SELECT has_column_privilege('kf_oj_identity_operator',"
                "'accounts','is_administrator','UPDATE')"
            )
            assert not await connection.fetchval(
                "SELECT has_column_privilege('kf_oj_identity_operator',"
                "'accounts','authorization_version','UPDATE')"
            )
            assert not await connection.fetchval(
                "SELECT pg_has_role('kf_oj_platform','kf_oj_identity_operator','USAGE')"
            )
        finally:
            await connection.close()

    asyncio.run(inspect())


def test_authority_change_derives_version_and_revokes_old_session_atomically(postgres_url):
    async def inspect():
        connection = await connect(postgres_url)
        engine = create_async_engine(postgres_url)
        try:
            account, token, csrf = await account_and_session(connection)
            service = IdentityService(async_sessionmaker(engine, expire_on_commit=False))
            await service.authenticate(token, csrf_token=csrf)
            async with connection.transaction():
                await connection.execute("SET LOCAL ROLE kf_oj_identity_operator")
                await connection.execute(
                    "SELECT * FROM public.kf_manage_account_authority('storage_user','bootstrap')"
                )
            assert (
                await connection.fetchval(
                    "SELECT authorization_version FROM accounts WHERE id=$1", account
                )
                == 2
            )
            with pytest.raises(AuthenticationFailed):
                await service.authenticate(token, csrf_token=csrf)
            async with connection.transaction():
                await connection.execute("SET LOCAL ROLE kf_oj_identity_operator")
                await connection.execute(
                    "SELECT * FROM public.kf_manage_account_authority('storage_user','bootstrap')"
                )
            assert (
                await connection.fetchval(
                    "SELECT authorization_version FROM accounts WHERE id=$1", account
                )
                == 2
            )
            fresh_token, fresh_csrf = uuid4().hex * 2, uuid4().hex * 2
            await connection.execute(
                "INSERT INTO sessions(token_hash,csrf_hash,account_id,expires_at) "
                "VALUES($1,$2,$3,clock_timestamp()+interval '1 hour')",
                token_digest(fresh_token),
                token_digest(fresh_csrf),
                account,
            )
            await connection.execute(
                "INSERT INTO accounts(id,username,password_hash) "
                "VALUES(gen_random_uuid(),'storage_recovery',repeat('synthetic-only-',5))"
            )
            async with connection.transaction():
                await connection.execute("SET LOCAL ROLE kf_oj_identity_operator")
                await connection.execute(
                    "SELECT * FROM public.kf_manage_account_authority('storage_recovery','grant')"
                )
            with pytest.raises(RollBackAuthorityChange):
                async with connection.transaction():
                    await connection.execute(
                        "SELECT * FROM public.kf_manage_account_authority('storage_user','revoke')"
                    )
                    assert (
                        await connection.fetchval(
                            "SELECT authorization_version FROM accounts WHERE id=$1", account
                        )
                        == 3
                    )
                    raise RollBackAuthorityChange
            assert await connection.fetchval(
                "SELECT is_administrator FROM accounts WHERE id=$1", account
            )
            assert (
                await connection.fetchval(
                    "SELECT authorization_version FROM accounts WHERE id=$1", account
                )
                == 2
            )
            await service.authenticate(fresh_token, csrf_token=fresh_csrf)
            async with connection.transaction():
                await connection.execute("SET LOCAL ROLE kf_oj_identity_operator")
                await connection.execute(
                    "SELECT * FROM public.kf_manage_account_authority('storage_user','revoke')"
                )
            assert (
                await connection.fetchval(
                    "SELECT authorization_version FROM accounts WHERE id=$1", account
                )
                == 3
            )
            with pytest.raises(AuthenticationFailed):
                await service.authenticate(fresh_token, csrf_token=fresh_csrf)
        finally:
            await connection.close()
            await engine.dispose()

    asyncio.run(inspect())


def test_authority_history_cannot_be_erased_by_downgrade(postgres_url):
    async def change():
        connection = await connect(postgres_url)
        try:
            account, _, _ = await account_and_session(connection)
            await connection.execute("UPDATE accounts SET enabled=false WHERE id=$1", account)
            with pytest.raises(asyncpg.CheckViolationError):
                await connection.execute(
                    "UPDATE accounts SET authorization_version=1 WHERE id=$1", account
                )
        finally:
            await connection.close()

    asyncio.run(change())
    with pytest.raises(subprocess.CalledProcessError):
        migrate(postgres_url, "downgrade", "0004_performance_watermark")
