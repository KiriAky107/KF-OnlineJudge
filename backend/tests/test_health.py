import asyncio

import httpx

from kf_oj.api.app import create_app
from kf_oj.core.settings import Settings


def test_liveness_without_database_or_queue() -> None:
    settings = Settings(_env_file=None, environment="test", database_url=None, amqp_url=None)

    async def request():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(settings)), base_url="http://test"
        ) as client:
            return await client.get("/api/v1/health/live")

    response = asyncio.run(request())
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "platform", "version": "0.0.1"}


def test_production_disables_api_documentation() -> None:
    app = create_app(
        Settings(_env_file=None, environment="production", browser_origins=("https://oj.example",))
    )

    async def request():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            for path in ("/api/docs", "/api/openapi.json"):
                assert (await client.get(path)).status_code == 404

    asyncio.run(request())
