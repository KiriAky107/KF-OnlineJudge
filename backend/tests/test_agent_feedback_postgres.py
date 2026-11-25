"""Exercise private run feedback with real platform and worker database roles."""

import asyncio
import secrets
from uuid import uuid4

import httpx
import pytest
from conftest import connect
from sqlalchemy import event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_submission_create import setup_app

from kf_oj.modules.learning.agent_worker import LearningWorker

ROOT = "/api/v1/learning/agent"
ORIGIN = "http://localhost:5173"


@pytest.mark.parametrize("purpose", ["guided_help", "focused_training", "periodic_review"])
def test_owner_can_save_replace_and_withdraw_run_feedback(
    postgres_url, tmp_path, monkeypatch, purpose
):
    async def exercise():
        app, engine, module = await setup_app(postgres_url, tmp_path)
        monkeypatch.setattr(module, "build_engine", lambda _: engine)
        worker_engine = create_async_engine(postgres_url)
        operator = await connect(postgres_url)

        @event.listens_for(worker_engine.sync_engine, "connect")
        def restrict_role(connection, _):
            cursor = connection.cursor()
            cursor.execute("SET ROLE kf_oj_learning_worker")
            cursor.close()

        def client():
            return httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url=ORIGIN, headers={"Origin": ORIGIN}
            )

        try:
            async with app.router.lifespan_context(app), client() as owner, client() as foreign:
                for actor, username in ((owner, "feedback_owner"), (foreign, "feedback_foreign")):
                    password = secrets.token_urlsafe(24)
                    await app.state.identity.create_account(username, password)
                    login = await actor.post(
                        "/api/v1/auth/login", json={"username": username, "password": password}
                    )
                    assert login.status_code == 200
                    actor.headers["x-csrf-token"] = actor.cookies["kf_csrf"]

                rows = (await owner.get(f"{ROOT}/skills")).json()["versions"]
                skill = next(item for item in rows if item["purpose"] == purpose)
                skill_id = skill["version_id"]
                binding = await owner.put(
                    f"{ROOT}/skills/{skill_id}",
                    json={"enabled": True, "authorized_tools": skill["available_tools"]},
                )
                assert binding.status_code == 200
                created = await owner.post(f"{ROOT}/sessions", json={"purpose": purpose})
                assert created.status_code == 201
                receipt = await owner.post(
                    f"{ROOT}/sessions/{created.json()['session_id']}/runs",
                    json={"skill_version_id": skill_id, "request_id": str(uuid4())},
                )
                assert receipt.status_code == 202
                path = f"{ROOT}/runs/{receipt.json()['run_id']}"
                action = path + "/feedback"
                feedback = {"rating": "partly_helpful", "reason": "unclear_next_step"}
                assert (await owner.put(action, json=feedback)).status_code == 409

                worker = LearningWorker(async_sessionmaker(worker_engine, expire_on_commit=False))
                assert await worker.run_once()
                before = (await owner.get(path)).json()
                assert before["state"] == "succeeded", before
                assert before["result"]["purpose"] == purpose
                assert "feedback" not in before
                assert (await foreign.get(path)).status_code == 404
                assert (await foreign.put(action, json=feedback)).status_code == 404
                assert (
                    await owner.put(action, json=feedback, headers={"x-csrf-token": "invalid"})
                ).status_code == 403

                first, repeated = await asyncio.gather(
                    owner.put(action, json=feedback), owner.put(action, json=feedback)
                )
                assert first.status_code == repeated.status_code == 200
                assert first.headers["cache-control"] == "no-store"
                assert first.json() == repeated.json()
                stored = first.json()["feedback"]
                assert first.json() == before | {"feedback": stored}
                assert all(stored[name] == value for name, value in feedback.items())
                assert (await owner.get(path)).json() == first.json()
                assert not await operator.fetchval(
                    "SELECT has_table_privilege('kf_oj_learning_worker',"
                    " 'learning_agent_feedback', 'SELECT,INSERT,DELETE')"
                )
                assert not await operator.fetchval(
                    "SELECT has_column_privilege('kf_oj_learning_worker',"
                    " 'learning_agent_feedback', 'rating', 'UPDATE')"
                )
                assert await operator.fetchval("SELECT count(*) FROM learning_agent_feedback") == 1

                changed = await owner.put(action, json={"rating": "helpful"})
                assert changed.status_code == 200
                assert changed.json()["feedback"]["reason"] is None
                assert changed.json()["feedback"]["rating"] == "helpful"
                assert changed.json()["feedback"]["rated_at"] != stored["rated_at"]
                assert (
                    await owner.put(action, json={"rating": None, "reason": "unrelated_hint"})
                ).status_code == 422
                for _ in range(2):
                    withdrawn = await owner.put(action, json={"rating": None})
                    assert withdrawn.status_code == 200 and withdrawn.json() == before
                assert (await owner.get(path)).json() == before
                assert await operator.fetchval("SELECT count(*) FROM learning_agent_feedback") == 0
        finally:
            await worker_engine.dispose()
            await operator.close()

    asyncio.run(exercise())
