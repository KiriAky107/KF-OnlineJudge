import asyncio
import os
import pty
import secrets
import select as io_select
import signal
import sys
import time
from datetime import timedelta
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import event, func, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from kf_oj.api.app import create_app
from kf_oj.contracts.identity import LoginRequest
from kf_oj.core.settings import Settings
from kf_oj.modules.identity.models import Account, Session
from kf_oj.modules.identity.passwords import hash_password, verify_password
from kf_oj.modules.identity.public import AuthenticationFailed, LoginThrottled
from kf_oj.modules.identity.service import IdentityService, token_digest


def restrict_platform_role(engine):
    @event.listens_for(engine.sync_engine, "connect")
    def apply_role(connection, _):
        cursor = connection.cursor()
        cursor.execute("SET ROLE kf_oj_platform")
        cursor.close()


def test_password_policy_and_credentials_redacted():
    password = secrets.token_hex(16)
    hashed = hash_password("fixture_user", password)
    assert hashed.startswith("$argon2id$v=19$m=19456,t=2,p=1$")
    assert verify_password(hashed, password)
    assert not verify_password(hashed, secrets.token_hex(16))
    assert not verify_password("$argon2id$v=19$m=999999999,t=2,p=1$invalid", password)
    assert password not in repr(LoginRequest(username="fixture_user", password=password))
    with pytest.raises(ValueError):
        hash_password("not canonical", password)
    with pytest.raises(ValueError):
        hash_password("fixture_user", "too short")


def test_production_cookie_configuration_requires_explicit_https_origins():
    with pytest.raises(ValueError):
        Settings(_env_file=None, environment="production")
    for invalid in ("https://oj.example/path", "https://user@oj.example", "*", "null"):
        with pytest.raises(ValueError):
            Settings(_env_file=None, browser_origins=(invalid,))


def test_real_login_session_revocation_expiry_and_throttle(postgres_url):
    async def run():
        engine = create_async_engine(postgres_url)
        restrict_platform_role(engine)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        identity = IdentityService(sessions, lifetime_seconds=300)
        password = secrets.token_hex(16)
        try:
            principal = await identity.create_account("fixture_user", password)
            with pytest.raises(AuthenticationFailed):
                await identity.login("fixture_user", secrets.token_hex(16), peer="fixture-peer")
            with pytest.raises(AuthenticationFailed):
                await identity.login("absent_user", password, peer="fixture-peer")
            issued = await identity.login("fixture_user", password, peer="fixture-peer")
            assert await identity.authenticate(issued.token) == principal
            assert issued.token not in repr(issued) and issued.csrf_token not in repr(issued)
            with pytest.raises(AuthenticationFailed):
                await identity.authenticate(str(principal.account_id))
            with pytest.raises(AuthenticationFailed):
                await identity.authenticate(issued.token, csrf_token="f" * 64)
            await identity.logout(issued.token, issued.csrf_token)
            with pytest.raises(AuthenticationFailed):
                await identity.authenticate(issued.token)
            issued = await identity.login("fixture_user", password, peer="expiry-peer")
            async with sessions.begin() as session:
                # Keep the expiry constraint valid while moving the entire session into the past.
                await session.execute(
                    update(Session).values(
                        created_at=func.now() - timedelta(minutes=10),
                        expires_at=func.now() - timedelta(minutes=5),
                    )
                )
            with pytest.raises(AuthenticationFailed):
                await identity.authenticate(issued.token)
            issued = await identity.login("fixture_user", password, peer="disable-peer")
            assert await identity.disable_account("fixture_user")
            with pytest.raises(AuthenticationFailed):
                await identity.authenticate(issued.token)
            with pytest.raises(AuthenticationFailed):
                await identity.login("fixture_user", password, peer="disable-peer")
            assert not await identity.disable_account("unknown_user")
            # Exactly ten real failed attempts consume the same persistent peer budget.
            for _ in range(10):
                with pytest.raises(AuthenticationFailed):
                    await identity.login("absent_user", password, peer="limited-peer")
            with pytest.raises(LoginThrottled):
                await identity.login("absent_user", password, peer="limited-peer")
            async with sessions() as session:
                account = await session.scalar(select(Account))
                assert password != account.password_hash and password not in account.password_hash
                rows = list(await session.scalars(select(Session)))
                assert all(row.token_hash != issued.token for row in rows)
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_real_http_cookie_csrf_and_identity_spoofing_rejection(postgres_url, caplog):
    async def run():
        app = create_app(
            Settings(
                _env_file=None,
                environment="production",
                database_url=postgres_url,
                browser_origins=("https://test",),
            )
        )
        async with app.router.lifespan_context(app):
            restrict_platform_role(app.state.sessions.kw["bind"])
            password = secrets.token_hex(16)
            first = await app.state.identity.create_account("first_user", password)
            second = await app.state.identity.create_account("second_user", secrets.token_hex(16))
            headers = {"Origin": "https://test"}
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="https://test"
            ) as client:
                body = {"username": "first_user", "password": password}
                assert (await client.get("/api/v1/auth/me")).status_code == 401
                assert (await client.post("/api/v1/auth/login", json=body)).status_code == 403
                assert (
                    await client.post(
                        "/api/v1/auth/login",
                        json=body,
                        headers={"Origin": "https://hostile.example"},
                    )
                ).status_code == 403
                invalid = await client.post(
                    "/api/v1/auth/login",
                    headers=headers,
                    json=body | {"user_id": str(second.account_id), "role": "admin"},
                )
                assert invalid.status_code == 422
                assert password not in invalid.text and "admin" not in invalid.text
                response = await client.post("/api/v1/auth/login", json=body, headers=headers)
                assert response.status_code == 200
                assert response.json()["user"]["id"] == str(first.account_id)
                assert (
                    await client.get(
                        "/api/v1/auth/me", headers={"X-User-ID": str(second.account_id)}
                    )
                ).json()["id"] == str(first.account_id)
                cookie = response.headers.get_list("set-cookie")[0]
                assert "HttpOnly" in cookie and "Secure" in cookie and "SameSite=strict" in cookie
                assert response.headers["cache-control"] == "no-store"
                assert client.cookies.get("kf_csrf") == response.json()["csrf_token"]
                assert (
                    await client.post("/api/v1/auth/logout", headers=headers)
                ).status_code == 403
                assert (
                    await client.post(
                        "/api/v1/auth/logout", headers=headers | {"X-CSRF-Token": "f" * 64}
                    )
                ).status_code == 403
                assert (
                    await client.post(
                        "/api/v1/auth/logout",
                        headers=headers | {"X-CSRF-Token": response.json()["csrf_token"]},
                    )
                ).status_code == 204
                assert (await client.get("/api/v1/auth/me")).status_code == 401
                assert not client.cookies.get("kf_session") and not client.cookies.get("kf_csrf")
                assert (
                    password not in caplog.text and response.json()["csrf_token"] not in caplog.text
                )

    asyncio.run(run())


def test_real_concurrent_logins_keep_bounded_active_sessions(postgres_url):
    async def run():
        engine = create_async_engine(postgres_url)
        restrict_platform_role(engine)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        identity = IdentityService(sessions)
        password = secrets.token_hex(16)
        try:
            await identity.create_account("fixture_user", password)
            # Independent peer budgets exercise session bounds without exhausting login retries.
            issued = []
            for _ in range(12):
                issued.append(await identity.login("fixture_user", password, peer=uuid4().hex))
            with pytest.raises(AuthenticationFailed):
                await identity.authenticate(issued[0].token)
            results = await asyncio.gather(
                *(identity.login("fixture_user", password, peer=uuid4().hex) for _ in range(3)),
                return_exceptions=True,
            )
            assert sum(isinstance(result, LoginThrottled) for result in results) == 1
            assert sum(not isinstance(result, Exception) for result in results) == 2
            async with sessions() as session:
                assert await session.scalar(select(func.count()).select_from(Session)) == 10
                assert (
                    await session.scalar(
                        select(func.count())
                        .select_from(Session)
                        .where(Session.token_hash == token_digest("0" * 64))
                    )
                    == 0
                )
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_account_cli_hidden_password_and_session_disable(postgres_url):
    password = secrets.token_hex(16)
    # The CLI prompts through a real private PTY; the password is never an argument/environment.
    pid, descriptor = pty.fork()
    if pid == 0:
        os.environ["KF_OJ_DATABASE_URL"] = postgres_url
        os.execv(
            sys.executable,
            [sys.executable, "-m", "kf_oj.modules.identity.cli", "create", "cli_user"],
        )
    output = b""
    deadline, prompts, status = time.monotonic() + 15, 0, None
    try:
        while time.monotonic() < deadline:
            if io_select.select([descriptor], [], [], 0.1)[0]:
                try:
                    block = os.read(descriptor, 4096)
                except OSError:
                    block = b""
                output += block
                if prompts == 0 and b"Password (12" in output:
                    os.write(descriptor, password.encode() + b"\n")
                    prompts = 1
                if prompts == 1 and b"Repeat password:" in output:
                    os.write(descriptor, password.encode() + b"\n")
                    prompts = 2
            result, status = os.waitpid(pid, os.WNOHANG)
            if result:
                break
        else:
            os.kill(pid, signal.SIGTERM)
            os.waitpid(pid, 0)
            pytest.fail("account CLI deadline exceeded")
        assert os.waitstatus_to_exitcode(status) == 0
        assert prompts == 2 and b"Account created." in output
        assert password.encode() not in output
    finally:
        os.close(descriptor)

    async def inspect():
        engine = create_async_engine(postgres_url)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            service = IdentityService(sessions)
            issued = await service.login("cli_user", password, peer="cli-fixture")
            assert await service.disable_account("cli_user")
            with pytest.raises(AuthenticationFailed):
                await service.authenticate(issued.token)
        finally:
            await engine.dispose()

    # CLI run uses the test DB only for this call; don't reuse a configured business database.
    original = os.environ.get("KF_OJ_DATABASE_URL")
    os.environ["KF_OJ_DATABASE_URL"] = postgres_url
    try:
        asyncio.run(inspect())
    finally:
        if original is None:
            os.environ.pop("KF_OJ_DATABASE_URL", None)
        else:
            os.environ["KF_OJ_DATABASE_URL"] = original
