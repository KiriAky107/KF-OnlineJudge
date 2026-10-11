"""Optional real PostgreSQL acceptance; never reuse or reset an existing business DB."""

import asyncio
import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest
from sqlalchemy.engine import make_url


@pytest.fixture
def postgres_url():
    admin = os.environ.get("KF_OJ_TEST_POSTGRES")
    if admin is None:
        pytest.skip("real PostgreSQL acceptance requires KF_OJ_TEST_POSTGRES")
    url = make_url(admin)
    database = "kf_oj_test_" + uuid4().hex
    assert url.drivername == "postgresql+asyncpg"
    test_url = url.set(database=database).render_as_string(hide_password=False)

    async def manage(command):
        connection = await asyncpg.connect(
            url.set(drivername="postgresql").render_as_string(hide_password=False)
        )
        try:
            await connection.execute(command)
        finally:
            await connection.close()

    asyncio.run(manage(f'CREATE DATABASE "{database}"'))
    try:
        migrate(test_url, "upgrade", "head")
        yield test_url
    finally:
        asyncio.run(manage(f'DROP DATABASE "{database}"'))


def migrate(url: str, command: str, target: str | None = None) -> None:
    subprocess.run(
        [sys.executable, "-m", "alembic", command] + ([target] if target else []),
        cwd=Path(__file__).resolve().parents[1],
        env=os.environ | {"KF_OJ_DATABASE_URL": url},
        check=True,
        capture_output=True,
    )


async def connect(url: str):
    return await asyncpg.connect(url.replace("postgresql+asyncpg:", "postgresql:", 1))
