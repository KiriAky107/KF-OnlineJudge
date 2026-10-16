"""Real local authority/CLI transactions; run privilege faults in the independent VM."""

import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest
from conftest import connect
from sqlalchemy.engine import make_url

from kf_oj.modules.identity.authority import manage_authority


async def seed(connection, *usernames):
    for name in usernames:
        await connection.execute(
            "INSERT INTO accounts(id,username,password_hash) VALUES($1,$2,repeat('synthetic-',6))",
            uuid4(),
            name,
        )


async def authority(connection, username, operation):
    async with connection.transaction():
        await connection.execute("SET LOCAL ROLE kf_oj_identity_operator")
        return await connection.fetchrow(
            "SELECT * FROM public.kf_manage_account_authority($1,$2)", username, operation
        )


def test_explicit_bootstrap_repeated_grants_recovery_and_last_administrator(postgres_url):
    async def run():
        c = await connect(postgres_url)
        try:
            await seed(c, "first_admin", "second_admin")
            assert await c.fetchval("SELECT count(*) FROM accounts WHERE is_administrator") == 0
            with pytest.raises(asyncpg.CheckViolationError):
                await authority(c, "first_admin", "grant")
            first = await authority(c, "first_admin", "bootstrap")
            assert first["changed"] and first["authorization_version"] == 2
            assert not (await authority(c, "first_admin", "bootstrap"))["changed"]
            assert not (await authority(c, "first_admin", "grant"))["changed"]
            with pytest.raises(asyncpg.CheckViolationError):
                await authority(c, "second_admin", "bootstrap")
            for operation in ("disable", "revoke"):
                with pytest.raises(asyncpg.CheckViolationError):
                    await authority(c, "first_admin", operation)
            await authority(c, "second_admin", "grant")
            disabled = await authority(c, "first_admin", "disable")
            assert not disabled["enabled"] and disabled["is_administrator"]
            assert disabled["authorization_version"] == 3
            assert not (await authority(c, "first_admin", "disable"))["changed"]
            restored = await authority(c, "first_admin", "enable")
            assert restored["enabled"] and restored["authorization_version"] == 4
            revoked = await authority(c, "first_admin", "revoke")
            assert not revoked["is_administrator"] and revoked["authorization_version"] == 5
            assert not (await authority(c, "first_admin", "revoke"))["changed"]
            await authority(c, "first_admin", "grant")
            with pytest.raises(asyncpg.NoDataFoundError):
                await authority(c, "absent_user", "grant")
            for role in ("kf_oj_platform", "kf_oj_coordinator", "kf_oj_problem_import"):
                assert not await c.fetchval(
                    "SELECT has_function_privilege($1,"
                    "'kf_manage_account_authority(text,text)','EXECUTE')",
                    role,
                )
            for column in ("password_hash", "is_administrator", "enabled"):
                assert not await c.fetchval(
                    "SELECT has_column_privilege('kf_oj_identity_operator','accounts',$1,'SELECT')",
                    column,
                )
            async with c.transaction():
                await c.execute("SET LOCAL ROLE kf_oj_identity_operator")
                for sql in (
                    "SELECT password_hash FROM accounts",
                    "UPDATE accounts SET is_administrator=false",
                    "SELECT token_hash FROM sessions",
                ):
                    with pytest.raises(asyncpg.InsufficientPrivilegeError):
                        async with c.transaction():
                            await c.execute(sql)
        finally:
            await c.close()

    asyncio.run(run())


@pytest.mark.parametrize("isolation", ("read_committed", "repeatable_read", "serializable"))
def test_concurrent_different_account_revocations_preserve_one_admin(postgres_url, isolation):
    async def serialized_run():
        c, other = await connect(postgres_url), await connect(postgres_url)
        first_tx = second_tx = pending = None
        try:
            await seed(c, "first_admin", "second_admin")
            await authority(c, "first_admin", "bootstrap")
            await authority(c, "second_admin", "grant")
            first_tx, second_tx = (
                c.transaction(isolation=isolation),
                other.transaction(isolation=isolation),
            )
            await first_tx.start()
            await second_tx.start()
            assert (
                await other.fetchval(
                    "SELECT count(*) FROM accounts WHERE is_administrator AND enabled"
                )
                == 2
            )
            await authority(c, "first_admin", "revoke")
            pending = asyncio.create_task(authority(other, "second_admin", "revoke"))
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if await c.fetchval(
                    "SELECT EXISTS(SELECT 1 FROM pg_locks WHERE pid=$1 AND NOT granted)",
                    other.get_server_pid(),
                ):
                    break
                await asyncio.sleep(0.01)
            else:
                raise AssertionError("second authority operation never blocked on PG lock")
            assert not pending.done()
            await first_tx.commit()
            first_tx = None
            with pytest.raises((asyncpg.CheckViolationError, asyncpg.SerializationError)):
                await asyncio.wait_for(pending, 5)
            await second_tx.rollback()
            second_tx = None
            assert (
                await c.fetchval("SELECT count(*) FROM accounts WHERE is_administrator AND enabled")
                == 1
            )
            assert await c.fetchval(
                "SELECT is_administrator FROM accounts WHERE username='second_admin'"
            )
        finally:
            if pending is not None and not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
            if first_tx is not None:
                await first_tx.rollback()
            if second_tx is not None:
                await second_tx.rollback()
            await c.close()
            await other.close()

    asyncio.run(serialized_run())


def test_real_cli_uses_dedicated_login_and_never_prints_database_secret(postgres_url):
    role = "authority_test_" + uuid4().hex

    async def setup():
        c = await connect(postgres_url)
        try:
            await seed(c, "cli_admin", "cli_backup")
            await c.execute(
                f"CREATE ROLE {role} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
                "NOREPLICATION NOBYPASSRLS"
            )
            await c.execute(f"GRANT kf_oj_identity_operator TO {role}")
        finally:
            await c.close()

    asyncio.run(setup())
    url = make_url(postgres_url).set(username=role).render_as_string(hide_password=False)
    env = os.environ | {
        "KF_OJ_DATABASE_URL": postgres_url,
        "KF_OJ_IDENTITY_OPERATOR_DATABASE_URL": url,
    }
    cwd = Path(__file__).resolve().parents[1]

    def invoke(command, username, expected=0):
        result = subprocess.run(
            [sys.executable, "-m", "kf_oj.modules.identity.cli", command, username],
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert result.returncode == expected, result.stderr
        assert url not in result.stdout + result.stderr
        assert "Traceback" not in result.stderr
        return result.stdout

    try:
        assert "changed=True" in invoke("bootstrap-admin", "cli_admin")
        assert "changed=False" in invoke("bootstrap-admin", "cli_admin")
        invoke("revoke-admin", "cli_admin", 1)
        invoke("grant-admin", "cli_backup")
        invoke("disable", "cli_admin")
        invoke("enable", "cli_admin")
        invoke("revoke-admin", "cli_admin")
        invoke("grant-admin", "cli_admin")
    finally:

        async def cleanup():
            c = await connect(postgres_url)
            try:
                await c.execute(f"REVOKE kf_oj_identity_operator FROM {role}")
                await c.execute(f"DROP ROLE {role}")
            finally:
                await c.close()

        asyncio.run(cleanup())


def test_bad_cli_operation_rejected_without_connecting():
    async def run():
        for username, operation in (("invalid name", "grant"), ("valid_name", "unknown")):
            with pytest.raises(ValueError):
                await manage_authority(None, username, operation)

    asyncio.run(run())
