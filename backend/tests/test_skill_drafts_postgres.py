"""Exercise private skill authoring using real Cookie sessions and PostgreSQL."""

import asyncio
import secrets
from uuid import uuid4

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from test_submission_create import setup_app

ROOT = "/api/v1/learning/agent"
ORIGIN = "http://localhost:5173"


def test_private_skill_drafts_copy_edit_and_roundtrip_without_binding(
    postgres_url, tmp_path, monkeypatch
):
    async def exercise():
        app, engine, module = await setup_app(postgres_url, tmp_path)
        monkeypatch.setattr(module, "build_engine", lambda _: engine)
        admin = create_async_engine(postgres_url)

        def http():
            return httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url=ORIGIN, headers={"origin": ORIGIN}
            )

        try:
            async with app.router.lifespan_context(app), http() as owner, http() as other:
                for client, name in ((owner, "draft_owner"), (other, "draft_other")):
                    password = secrets.token_urlsafe(24)
                    await app.state.identity.create_account(name, password)
                    logged = await client.post(
                        "/api/v1/auth/login",
                        json={
                            "username": name,
                            "password": password,
                        },
                    )
                    assert logged.status_code == 200
                    client.headers["x-csrf-token"] = client.cookies["kf_csrf"]

                skill = next(
                    item
                    for item in (await owner.get(f"{ROOT}/skills")).json()["versions"]
                    if item["purpose"] == "focused_training"
                )
                version_id = skill["version_id"]
                binding = await owner.put(
                    f"{ROOT}/skills/{version_id}",
                    json={
                        "enabled": True,
                        "parameters": {"goal": "private-training-value"},
                        "authorized_tools": [],
                    },
                )
                assert binding.status_code == 200
                url = f"{ROOT}/skill-drafts"
                requested = {"source_version_id": version_id, "request_id": str(uuid4())}
                unauthorized = await owner.post(url, json=requested, headers={"x-csrf-token": ""})
                assert unauthorized.status_code == 403
                copies = await asyncio.gather(*(owner.post(url, json=requested) for _ in range(2)))
                assert all(response.status_code == 201 for response in copies)
                draft = copies[0].json()
                assert copies[1].json() == draft
                assert draft["source_version_id"] == version_id and draft["revision"] == 0
                assert draft["document"]["parameters"]["goal"] == ""
                assert "private-training-value" not in copies[0].text
                assert version_id in draft["document"]["attribution"]

                path = f"{url}/{draft['draft_id']}"
                assert (await other.get(url)).json()["items"] == []
                absent = await other.get(path)
                assert absent.status_code == 404 and absent.headers["cache-control"] == "no-store"
                change = draft["document"] | {
                    "title": "我的复盘方法",
                    "method": "先看证据，再整理提纲。",
                }
                editing = {"document": change, "revision": draft["revision"]}
                assert (await other.put(path, json=editing)).status_code == 404
                updated = await owner.put(path, json=editing)
                assert updated.status_code == 200 and updated.json()["revision"] == 1
                assert updated.json()["content_digest"] != draft["content_digest"]
                assert (await owner.put(path, json=editing)).json() == updated.json()
                editing["document"] = change | {"title": "来自旧修订的写入"}
                assert (await owner.put(path, json=editing)).status_code == 409
                assert (await owner.get(path)).json() == updated.json()

                portable = updated.json()["document"]
                exported = await owner.post(
                    url,
                    json={
                        "request_id": str(uuid4()),
                        "document": portable,
                    },
                )
                assert exported.status_code == 201
                assert exported.json()["source_version_id"] is None
                assert exported.json()["content_digest"] == updated.json()["content_digest"]
                assert exported.json()["document"] == portable
                for invalid in (
                    portable | {"scripts": {"on_install": "would be executable"}},
                    portable | {"files": ["../some-file"]},
                    portable | {"requested_tools": [{"name": "source_evidence", "version": 1}]},
                    portable | {"parameters": {"daily_minutes": 300}},
                ):
                    response = await owner.post(
                        url,
                        json={
                            "request_id": str(uuid4()),
                            "document": invalid,
                        },
                    )
                    assert response.status_code == 422
                assert len((await owner.get(url)).json()["items"]) == 2
                same_key = await owner.post(
                    url,
                    json={
                        "request_id": requested["request_id"],
                        "document": portable,
                    },
                )
                assert same_key.status_code == 409
                async with admin.connect() as db:
                    assert not await db.scalar(
                        text(
                            "SELECT has_table_privilege("
                            "'kf_oj_learning_worker', 'skills_drafts', 'SELECT')"
                        )
                    )
        finally:
            await admin.dispose()

    asyncio.run(exercise())
