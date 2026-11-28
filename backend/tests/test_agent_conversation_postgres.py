"""Exercise one owned conversation with ordered runs across learning purposes."""

import asyncio
import secrets
from uuid import UUID, uuid4

import httpx
import pytest
from conftest import connect
from sqlalchemy import event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_submission_create import setup_app

from kf_oj.contracts.learning_model import ModelHint
from kf_oj.modules.learning.agent_worker import LearningWorker

ROOT = "/api/v1/learning/agent"
ORIGIN = "http://localhost:5173"


@pytest.mark.parametrize("reasoning_kind", ["text", "summary"])
def test_mixed_learning_conversation_is_ordered_owned_and_serial(
    postgres_url, tmp_path, monkeypatch, reasoning_kind
):
    class Model:
        async def hint(self, _):
            return ModelHint(
                text="先手算最小输入。",
                reasoning="需要核对输入边界。",
                reasoning_kind=reasoning_kind,
                model="fixture",
                response_id="conversation-fixture",
                latency_ms=1,
            )

    async def exercise():
        app, engine, module = await setup_app(postgres_url, tmp_path)
        monkeypatch.setattr(module, "build_engine", lambda _: engine)
        worker_engine = create_async_engine(postgres_url)

        @event.listens_for(worker_engine.sync_engine, "connect")
        def restrict_worker(connection, _):
            cursor = connection.cursor()
            cursor.execute("SET ROLE kf_oj_learning_worker")
            cursor.close()

        try:
            async with (
                app.router.lifespan_context(app),
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(app), base_url=ORIGIN, headers={"origin": ORIGIN}
                ) as owner,
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(app), base_url=ORIGIN, headers={"origin": ORIGIN}
                ) as other,
            ):

                async def login(client, username):
                    password = secrets.token_urlsafe(24)
                    await app.state.identity.create_account(username, password)
                    response = await client.post(
                        "/api/v1/auth/login", json={"username": username, "password": password}
                    )
                    assert response.status_code == 200
                    client.headers["x-csrf-token"] = client.cookies["kf_csrf"]

                async def session(client, purpose, parent=None):
                    body = {"purpose": purpose}
                    if parent is not None:
                        body["conversation_session_id"] = parent
                    return await client.post(f"{ROOT}/sessions", json=body)

                async def history(client, key, **params):
                    return await client.get(f"{ROOT}/sessions/{key}/conversation", params=params)

                async def begin(key, body):
                    return await owner.post(f"{ROOT}/sessions/{key}/runs", json=body)

                await login(owner, "conversation_owner")
                await login(other, "conversation_other")
                problem_version = await app.state.problems.import_directory(tmp_path / "import")
                skill_rows = (await owner.get(f"{ROOT}/skills")).json()["versions"]
                skills = {item["purpose"]: item for item in skill_rows}
                for purpose in ("guided_help", "focused_training"):
                    skill = skills[purpose]
                    binding = await owner.put(
                        f"{ROOT}/skills/{skill['version_id']}",
                        json={"enabled": True, "authorized_tools": skill["available_tools"]},
                    )
                    assert binding.status_code == 200, binding.text

                created = await session(owner, "guided_help")
                assert created.status_code == 201, created.text
                root = created.json()["session_id"]
                assert (await history(owner, root)).json() == {
                    "conversation_id": root,
                    "runs": [],
                    "next_before": None,
                }
                assert (await history(other, root)).status_code == 404
                assert (await session(other, "focused_training", root)).status_code == 404
                joined = await session(owner, "focused_training", root)
                assert joined.status_code == 201, joined.text
                child = joined.json()["session_id"]
                assert (await history(owner, child)).json()["conversation_id"] == root

                question = "怎样从边界条件开始？"
                training_message = "帮我从题库组一道练习题"
                chat_request = {
                    "request_id": str(uuid4()),
                    "skill_version_id": skills["guided_help"]["version_id"],
                    "chat": {"question": question, "problem_version_id": str(problem_version)},
                }
                training_request = {
                    "request_id": str(uuid4()),
                    "skill_version_id": skills["focused_training"]["version_id"],
                    "training_set": {"title": "边界练习", "count": 1, "message": training_message},
                }
                started = await begin(root, chat_request)
                assert started.status_code == 202, started.text
                chat_run = started.json()["run_id"]
                assert (await begin(root, chat_request)).json()["run_id"] == chat_run
                assert (await begin(child, training_request)).status_code == 409

                sessions = async_sessionmaker(worker_engine, expire_on_commit=False)
                worker = LearningWorker(sessions)
                assert await worker.run_once()
                draft = await begin(child, training_request)
                assert draft.status_code == 202, draft.text
                draft_run = draft.json()["run_id"]
                assert await worker.run_once()

                combined = (await history(owner, root)).json()
                assert combined == (await history(owner, child)).json()
                assert [row["run_id"] for row in combined["runs"]] == [chat_run, draft_run]
                assert [row["question"] for row in combined["runs"]] == [question, training_message]
                assert [row["purpose"] for row in combined["runs"]] == [
                    "guided_help",
                    "focused_training",
                ]
                latest = (await history(owner, child, limit=1)).json()
                assert latest["next_before"] == draft_run
                previous = (await history(owner, root, limit=1, before=draft_run)).json()
                assert previous["next_before"] is None and previous["runs"][0]["run_id"] == chat_run
                assert "reasoning_kind" not in latest["runs"][0]
                assert (await history(owner, root, before=str(uuid4()))).status_code == 404

                unrelated = (await session(owner, "guided_help")).json()["session_id"]
                new = await begin(
                    unrelated,
                    chat_request
                    | {
                        "request_id": str(uuid4()),
                        "chat": {"question": "另开一段对话"},
                    },
                )
                assert new.status_code == 202, new.text
                recent = await owner.get(f"{ROOT}/conversations")
                assert recent.status_code == 200, recent.text
                unrelated_entry, mixed = recent.json()["items"]
                assert unrelated_entry["conversation_id"] == unrelated
                assert mixed["conversation_id"] == root
                assert mixed["session_id"] == child and mixed["run_id"] == draft_run
                assert mixed["conversation_title"] == question and mixed["has_training_request"]
                scoped = await owner.get(
                    f"{ROOT}/conversations", params={"problem_version_id": str(problem_version)}
                )
                assert scoped.status_code == 200 and scoped.json()["items"] == [mixed]
                assert (await other.get(f"{ROOT}/conversations")).json()["items"] == []
                assert await LearningWorker(sessions, model=Model()).run_once()
                if reasoning_kind == "summary":
                    # Old runs may still have the provider summary stored in JSON.
                    operator = await connect(postgres_url)
                    try:
                        await operator.execute(
                            "UPDATE learning_agent_workflow_data SET result=jsonb_set("
                            "result, '{model_hint}', (result->'model_hint') || "
                            "$2::jsonb) WHERE run_id=$1",
                            UUID(new.json()["run_id"]),
                            '{"reasoning":"旧摘要","reasoning_kind":"summary"}',
                        )
                    finally:
                        await operator.close()
                free_chat = (await history(owner, unrelated)).json()["runs"][0]
                assert free_chat["run_id"] == new.json()["run_id"]
                if reasoning_kind == "text":
                    assert free_chat["reasoning"] == "需要核对输入边界。"
                    assert free_chat["reasoning_kind"] == "text"
                else:
                    assert "reasoning" not in free_chat and "reasoning_kind" not in free_chat
        finally:
            await worker_engine.dispose()
            await engine.dispose()

    asyncio.run(exercise())
