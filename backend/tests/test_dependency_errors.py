"""Ordinary exception-boundary checks; real dependency failure is VM-only."""

import asyncio
import errno

import httpx
import pytest
from asyncpg import CannotConnectNowError
from sqlalchemy.exc import OperationalError, ProgrammingError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError

from kf_oj.api.app import create_app
from kf_oj.core.settings import Settings


@pytest.mark.parametrize(
    "error,expected",
    [
        (ConnectionRefusedError("private_connection_text"), 503),
        (TimeoutError("private_timeout_text"), 503),
        (CannotConnectNowError("private_database_text"), 503),
        (PoolTimeoutError("private_pool_text"), 503),
        (
            OperationalError(
                "private_query", {"secret": "private_value"}, RuntimeError("private_driver")
            ),
            503,
        ),
        (OSError(errno.ENOSPC, "private_source_path"), 503),
        (OSError(errno.EDQUOT, "private_quota_path"), 503),
        (FileNotFoundError(errno.ENOENT, "private_missing_path"), 500),
    ],
)
def test_dependency_boundary_preserves_status_without_exception_text(error, expected):
    app = create_app(Settings(_env_file=None, environment="test"))

    @app.get("/api/v1/auth/boundary-test")
    async def fail():
        raise error

    async def request():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get("/api/v1/auth/boundary-test")
            assert response.status_code == expected
            assert "private_" not in response.text
            assert response.headers["cache-control"] == "no-store"
            if expected == 503:
                assert response.headers["retry-after"] == "5"

    asyncio.run(request())


def test_programming_errors_are_not_classified_as_transient_dependency_failures():
    app = create_app(Settings(_env_file=None, environment="test"))
    assert ProgrammingError not in app.exception_handlers
    assert ValueError not in app.exception_handlers
