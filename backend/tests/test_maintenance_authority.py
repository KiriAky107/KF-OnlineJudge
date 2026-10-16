"""Real trusted control-plane identity checks; no grading/security VM claim."""

import asyncio
import secrets
from dataclasses import replace

import httpx
import pytest
from conftest import connect
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_identity import restrict_platform_role

from kf_oj.api.app import create_app
from kf_oj.contracts.identity import AccountCapabilities
from kf_oj.core.settings import Settings
from kf_oj.modules.identity.public import AuthenticationFailed, MaintenanceDenied, Principal
from kf_oj.modules.identity.service import IdentityService


def test_capability_wire_version_and_default_denial():
    assert AccountCapabilities().model_dump() == {
        "schema_version": 1,
        "problem_maintenance": False,
        "authorization_version": "1",
    }
    for invalid in (
        {"schema_version": 2},
        {"authorization_version": "0"},
        {"authorization_version": 1},
        {"role": "admin"},
    ):
        with pytest.raises(ValueError):
            AccountCapabilities.model_validate(invalid)


async def set_administrator(connection, account_id, enabled):
    username = await connection.fetchval("SELECT username FROM accounts WHERE id=$1", account_id)
    active = await connection.fetchval(
        "SELECT count(*) FROM accounts WHERE enabled AND is_administrator"
    )
    async with connection.transaction():
        await connection.execute("SET LOCAL ROLE kf_oj_identity_operator")
        operation = ("grant" if active else "bootstrap") if enabled else "revoke"
        await connection.execute(
            "SELECT * FROM public.kf_manage_account_authority($1,$2)", username, operation
        )


async def add_recovery_administrator(connection):
    await connection.execute(
        "INSERT INTO accounts(id,username,password_hash) "
        "VALUES(gen_random_uuid(),'authority_recovery',repeat('synthetic-only-',5))"
    )
    async with connection.transaction():
        await connection.execute("SET LOCAL ROLE kf_oj_identity_operator")
        await connection.execute(
            "SELECT * FROM public.kf_manage_account_authority('authority_recovery','grant')"
        )


def test_live_http_capabilities_and_old_cookies_after_grant_and_revoke(postgres_url):
    async def run():
        app = create_app(Settings(_env_file=None, environment="test", database_url=postgres_url))
        connection = await connect(postgres_url)
        try:
            async with app.router.lifespan_context(app):
                restrict_platform_role(app.state.sessions.kw["bind"])
                password = secrets.token_hex(16)
                account = await app.state.identity.create_account("authority_user", password)
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://localhost:8000"
                ) as client:
                    headers = {"Origin": "http://localhost:8000"}
                    body = {"username": account.username, "password": password}
                    ordinary = await client.post("/api/v1/auth/login", json=body, headers=headers)
                    assert ordinary.status_code == 200
                    assert ordinary.json()["user"]["capabilities"] == {
                        "schema_version": 1,
                        "problem_maintenance": False,
                        "authorization_version": "1",
                    }
                    await set_administrator(connection, account.account_id, True)
                    assert (await client.get("/api/v1/auth/me")).status_code == 401
                    fresh = await client.post("/api/v1/auth/login", json=body, headers=headers)
                    assert fresh.status_code == 200
                    user = fresh.json()["user"]
                    assert user["capabilities"]["problem_maintenance"] is True
                    assert user["capabilities"]["authorization_version"] == "2"
                    actual = await client.get(
                        "/api/v1/auth/me", headers={"X-Role": "admin", "X-User-ID": "invalid"}
                    )
                    assert actual.json() == user
                    assert actual.headers["cache-control"] == "no-store"
                    await add_recovery_administrator(connection)
                    await set_administrator(connection, account.account_id, False)
                    assert (await client.get("/api/v1/auth/me")).status_code == 401
                    refused = await client.post(
                        "/api/v1/auth/login", json=body | {"role": "admin"}, headers=headers
                    )
                    assert refused.status_code == 422
                    ordinary = await client.post("/api/v1/auth/login", json=body, headers=headers)
                    assert ordinary.json()["user"]["capabilities"]["problem_maintenance"] is False
                    assert ordinary.json()["user"]["capabilities"]["authorization_version"] == "3"
        finally:
            await connection.close()

    asyncio.run(run())


def test_write_transaction_rechecks_session_and_serializes_authority_change(postgres_url):
    async def run():
        engine = create_async_engine(postgres_url)
        restrict_platform_role(engine)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        identity = IdentityService(sessions)
        connection, other = await connect(postgres_url), await connect(postgres_url)
        pending = None
        try:
            password = secrets.token_hex(16)
            account = await identity.create_account("authority_user", password)
            await set_administrator(connection, account.account_id, True)
            issued = await identity.login(account.username, password, peer="authority-fixture")
            principal = await identity.authenticate(issued.token)
            assert principal.can_maintain_problems and principal.authorization_version == 2
            assert principal.session_token_hash not in repr(principal)
            await add_recovery_administrator(connection)
            async with sessions.begin() as session:
                await identity.require_problem_maintenance(session, principal)
                pending = asyncio.create_task(set_administrator(other, account.account_id, False))
                await asyncio.sleep(0.1)
                assert not pending.done()  # Real PG SHARE lock holds through the write transaction.
            await asyncio.wait_for(pending, timeout=5)
            with pytest.raises(AuthenticationFailed):
                await identity.authenticate(issued.token)
            async with sessions.begin() as session:
                with pytest.raises(MaintenanceDenied):
                    await identity.require_problem_maintenance(session, principal)
            await set_administrator(connection, account.account_id, True)
            issued = await identity.login(account.username, password, peer="authority-fresh")
            principal = await identity.authenticate(issued.token)
            async with sessions.begin() as session:
                for forged in (
                    Principal(account.account_id, account.username, True),
                    replace(principal, session_token_hash="0" * 64),
                    replace(principal, authorization_version=1),
                ):
                    with pytest.raises(MaintenanceDenied):
                        await identity.require_problem_maintenance(session, forged)
            await identity.logout(issued.token, issued.csrf_token)
            async with sessions.begin() as session:
                with pytest.raises(MaintenanceDenied):
                    await identity.require_problem_maintenance(session, principal)
            issued = await identity.login(account.username, password, peer="authority-expiry")
            principal = await identity.authenticate(issued.token)
            # Use actual wall-clock expiry even inside an already-open transaction.
            async with sessions.begin() as session:
                await session.connection()
                await connection.execute(
                    "UPDATE sessions SET expires_at=clock_timestamp()+interval '50 milliseconds' "
                    "WHERE token_hash=$1",
                    principal.session_token_hash,
                )
                await asyncio.sleep(0.1)
                with pytest.raises(MaintenanceDenied):
                    await identity.require_problem_maintenance(session, principal)
        finally:
            if pending is not None and not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
            await connection.close()
            await other.close()
            await engine.dispose()

    asyncio.run(run())
