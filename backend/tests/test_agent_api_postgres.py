"""Verify the learning HTTP entry point with real Cookie sessions and the learning worker."""

import asyncio
import secrets
from uuid import uuid4

import httpx
from sqlalchemy import event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_submission_create import setup_app

from kf_oj.modules.learning.agent_worker import LearningWorker

ROOT = "/api/v1/learning/agent"
ORIGIN = "http://localhost:5173"


def test_learning_routes_use_cookie_and_actor_scope(postgres_url, tmp_path, monkeypatch):
    async def exercise():
        app, engine, module = await setup_app(postgres_url, tmp_path)
        monkeypatch.setattr(module, "build_engine", lambda _: engine)
        worker_engine = create_async_engine(postgres_url)

        @event.listens_for(worker_engine.sync_engine, "connect")
        def restrict_role(connection, _):
            cursor = connection.cursor()
            cursor.execute("SET ROLE kf_oj_learning_worker")
            cursor.close()

        try:
            async with app.router.lifespan_context(app):
                async with (
                    httpx.AsyncClient(
                        transport=httpx.ASGITransport(app),
                        base_url=ORIGIN,
                        headers={"origin": ORIGIN},
                    ) as owner,
                    httpx.AsyncClient(
                        transport=httpx.ASGITransport(app),
                        base_url=ORIGIN,
                        headers={"origin": ORIGIN},
                    ) as foreign,
                ):
                    before = await owner.get(f"{ROOT}/skills")
                    assert (
                        before.status_code == 401 and before.headers["cache-control"] == "no-store"
                    )
                    for client, username in ((owner, "agent_http_a"), (foreign, "agent_http_b")):
                        password = secrets.token_urlsafe(24)
                        await app.state.identity.create_account(username, password)
                        login = await client.post(
                            "/api/v1/auth/login", json={"username": username, "password": password}
                        )
                        assert login.status_code == 200 and "kf_session" in client.cookies
                        client.headers["x-csrf-token"] = client.cookies["kf_csrf"]

                    version = await app.state.problems.import_directory(tmp_path / "import")
                    catalog = await owner.get(f"{ROOT}/skills")
                    assert catalog.status_code == 200
                    skill = next(
                        entry
                        for entry in catalog.json()["versions"]
                        if entry["purpose"] == "periodic_review"
                    )
                    assert skill["binding"] is None
                    path = f"{ROOT}/skills/{skill['version_id']}"
                    config = {
                        "enabled": True,
                        "parameters": {"sessions_per_week": 1},
                        "authorized_tools": skill["available_tools"],
                    }
                    denied = await owner.put(path, json=config, headers={"x-csrf-token": ""})
                    assert denied.status_code == 403
                    assert (await owner.put(path, json=config)).status_code == 200
                    other_skills = await foreign.get(f"{ROOT}/skills")
                    assert all(item["binding"] is None for item in other_skills.json()["versions"])

                    response = await owner.post(
                        f"{ROOT}/sessions", json={"purpose": "periodic_review"}
                    )
                    assert response.status_code == 201
                    session_id = response.json()["session_id"]
                    queue = f"{ROOT}/sessions/{session_id}/runs"
                    start = {"request_id": str(uuid4()), "skill_version_id": skill["version_id"]}
                    response = await owner.post(queue, json=start)
                    assert response.status_code == 202 and response.json()["state"] == "queued"
                    run_id = response.json()["run_id"]
                    assert (
                        "owner_id" not in response.json() and "input_digest" not in response.json()
                    )
                    running = f"{ROOT}/runs/{run_id}"
                    for url in (running, running + "/cancel", running + "/training"):
                        inaccessible = (
                            await foreign.get(url) if url == running else await foreign.post(url)
                        )
                        assert inaccessible.status_code == 404
                    assert (await foreign.get(f"{ROOT}/runs")).json()["items"] == []
                    assert (await foreign.get(running + "/trace")).status_code == 404
                    waiting = (await owner.get(running + "/trace")).json()
                    assert waiting["attempts"] == 0 and waiting["queue_ms"] is None
                    assert waiting["duration_ms"] is None and waiting["steps"] == []

                    worker = LearningWorker(
                        async_sessionmaker(worker_engine, expire_on_commit=False)
                    )
                    assert await worker.run_once()
                    finished = await owner.get(running)
                    assert finished.json()["state"] == "succeeded"
                    response = await owner.get(running + "/trace")
                    trace = response.json()
                    assert response.headers["cache-control"] == "no-store"
                    assert trace["attempts"] == 1 and trace["tool_calls"] == 2
                    assert trace["finished_tool_calls"] == 2 and not trace["truncated"]
                    assert 0 <= trace["queue_ms"] <= trace["duration_ms"]
                    assert {step["invocation_version"] for step in trace["steps"]} == {
                        "submission_history-v1",
                        "training_candidates-v1",
                    }
                    assert all(step["state"] == "succeeded" for step in trace["steps"])
                    assert (
                        "arguments_digest" not in response.text
                        and "result_digest" not in response.text
                    )

                    assert finished.json()["result"]["items"][0]["problem_version_id"] == str(
                        version
                    )
                    saved = await owner.post(running + "/training")
                    assert saved.status_code == 200
                    assert (await owner.post(running + "/training")).json()["id"] == saved.json()[
                        "id"
                    ]
                    assert (await owner.post(queue, json=start)).json()["run_id"] == run_id

                    requests = [start | {"request_id": str(uuid4())} for _ in range(5)]
                    pending = await asyncio.gather(
                        *(owner.post(queue, json=item) for item in requests)
                    )
                    assert sorted(response.status_code for response in pending) == [202] * 4 + [429]
                    first = next(
                        (item, r)
                        for item, r in zip(requests, pending, strict=True)
                        if r.status_code == 202
                    )
                    retry = await owner.post(queue, json=first[0])
                    assert (
                        retry.status_code == 202
                        and retry.json()["run_id"] == first[1].json()["run_id"]
                    )
                    pending_id = retry.json()["run_id"]
                    cancelled = await owner.post(f"{ROOT}/runs/{pending_id}/cancel")
                    assert cancelled.status_code == 200 and cancelled.json()["state"] == "cancelled"
                    cancelled_trace = (await owner.get(f"{ROOT}/runs/{pending_id}/trace")).json()
                    assert cancelled_trace["attempts"] == 0 and cancelled_trace["duration_ms"] >= 0
                    assert cancelled_trace["queue_ms"] is None

                    history = await owner.get(f"{ROOT}/runs")
                    assert len(history.json()["items"]) == 5
                    assert all(
                        item["purpose"] == "periodic_review" for item in history.json()["items"]
                    )
        finally:
            await worker_engine.dispose()

    asyncio.run(exercise())
