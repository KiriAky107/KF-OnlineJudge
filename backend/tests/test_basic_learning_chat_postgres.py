"""Regular chat requires a session while registered tools require a separate grant."""

import asyncio
import json
import secrets
from uuid import UUID, uuid4

import httpx
import pytest
from conftest import connect
from sqlalchemy import event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_submission_create import setup_app

from kf_oj.contracts.learning_model import GuidedChatModelFacts, ModelHint
from kf_oj.contracts.submissions import CreateSubmission
from kf_oj.modules.learning import agent_application
from kf_oj.modules.learning.agent_worker import LearningWorker
from kf_oj.modules.skills.public import SkillBindingUnavailable
from kf_oj.runtime.storage import AgentRecordConflict

_ROOT = "/api/v1/learning/agent"
_ORIGIN = "http://localhost:5173"


def test_basic_chat_works_without_any_skill_and_rejects_capability_overrides(
    postgres_url, tmp_path, monkeypatch
):
    no_practice_question = "讲解二分边界，不读取练习记录。"

    class PlainModel:
        accepts_source_excerpt = False
        supports_review_mcp = True

        async def hint(self, content):
            assert isinstance(content, GuidedChatModelFacts)
            assert content.submission is None and content.source_excerpt is None
            questions.append(content)
            return ModelHint(
                text="喵，先用最小输入手算。",
                model="plain-chat-fixture",
                response_id="plain-chat-fixture",
                latency_ms=1,
            )

        async def stream_hint(self, content, _publish):
            return await self.hint(content)

        async def chat_with_tools(self, content, publish, read_practice, continue_request):
            if content.question == no_practice_question:
                return await self.hint(content)
            if invalidate_practice is not None:
                await publish("reasoning", "练习概览读取前的推理预览。")
                expected_error = await invalidate_practice()
                with pytest.raises(expected_error) as rejected:
                    await read_practice()
                raise rejected.value
            rows = json.loads(await read_practice())["recent_practice"]
            assert rows[0]["title"] == "Synthetic problem"
            practice_receipts.append(rows)
            assert await continue_request(("recent_practice",))
            hint = await self.hint(content)
            return hint.model_copy(update={"review_resources": ("recent_practice",)})

    questions = []
    practice_receipts = []
    practice_reads = []
    invalidate_practice = None
    read_recent = agent_application.recent_practice_versions

    async def track_recent(*args, **kwargs):
        practice_reads.append(kwargs.get("as_of"))
        return await read_recent(*args, **kwargs)

    monkeypatch.setattr(agent_application, "recent_practice_versions", track_recent)

    async def exercise():
        app, engine, module = await setup_app(postgres_url, tmp_path)
        monkeypatch.setattr(module, "build_engine", lambda _: engine)
        worker_engine = create_async_engine(postgres_url)

        @event.listens_for(worker_engine.sync_engine, "connect")
        def restrict_worker(connection, _):
            cursor = connection.cursor()
            cursor.execute("SET ROLE kf_oj_learning_worker")
            cursor.close()

        def client():
            return httpx.AsyncClient(
                transport=httpx.ASGITransport(app),
                base_url=_ORIGIN,
                headers={"origin": _ORIGIN},
            )

        async def login(browser, username):
            password = secrets.token_urlsafe(24)
            account = await app.state.identity.create_account(username, password)
            response = await browser.post(
                "/api/v1/auth/login", json={"username": username, "password": password}
            )
            assert response.status_code == 200
            browser.headers["x-csrf-token"] = browser.cookies["kf_csrf"]
            return account

        try:
            async with app.router.lifespan_context(app), client() as owner, client() as other:
                self_account = await login(owner, "basic_chat_owner")
                await login(other, "basic_chat_other")
                version = await app.state.problems.import_directory(tmp_path / "import")
                submission = await app.state.submissions.create(
                    self_account,
                    CreateSubmission(
                        problem_version_id=version,
                        language="c17",
                        source="int main(void) { return 0; }",
                        idempotency_key=uuid4(),
                    ),
                )
                catalog = (await owner.get(f"{_ROOT}/skills")).json()["versions"]
                assert catalog and all(version["binding"] is None for version in catalog)
                guided = next(version for version in catalog if version["purpose"] == "guided_help")
                created = await owner.post(f"{_ROOT}/sessions", json={"purpose": "guided_help"})
                assert created.status_code == 201
                session = created.json()["session_id"]
                url = f"{_ROOT}/sessions/{session}/runs"
                question = "用猫娘口吻解释如何手算最小输入。"

                basic = {"request_id": str(uuid4()), "chat": {"question": question}}
                assert (
                    await owner.post(url, json=basic | {"role": "administrator"})
                ).status_code == 422
                assert (
                    await owner.post(url, json=basic | {"tools": ["fixture_unavailable"]})
                ).status_code == 422
                assert (
                    await owner.post(
                        url,
                        json=basic
                        | {
                            "submission_id": str(uuid4()),
                            "chat": {"question": question, "include_submission_details": True},
                        },
                    )
                ).status_code == 422
                assert (
                    await owner.post(url, json=basic | {"include_model_review": True})
                ).status_code == 422
                assert (await other.post(url, json=basic)).status_code == 404
                assert (
                    await owner.post(url, json=basic | {"skill_version_id": guided["version_id"]})
                ).status_code == 404

                first = await owner.post(url, json=basic)
                assert first.status_code == 202, first.text
                first_id = first.json()["run_id"]
                assert (await owner.post(url, json=basic)).json()["run_id"] == first_id

                model = PlainModel()
                worker = LearningWorker(
                    async_sessionmaker(worker_engine, expire_on_commit=False), model
                )
                assert await worker.run_once()
                read = await owner.get(f"{_ROOT}/runs/{first_id}")
                assert read.json()["result"]["model_hint"]["text"] == "喵，先用最小输入手算。"
                trace = (await owner.get(f"{_ROOT}/runs/{first_id}/trace")).json()
                assert trace["tool_calls"] == 0

                next_turn = await owner.post(
                    url,
                    json={
                        "request_id": str(uuid4()),
                        "chat": {"question": "再讲详细些。", "previous_run_id": first_id},
                    },
                )
                assert next_turn.status_code == 202, next_turn.text
                assert await worker.run_once()
                assert questions[0].question == question
                assert all(not facts.recent_practice for facts in questions)
                assert questions[1].history[0].question == question
                assert questions[1].history[0].answer == "喵，先用最小输入手算。"
                assert all(
                    version["binding"] is None
                    for version in (await owner.get(f"{_ROOT}/skills")).json()["versions"]
                )

                binding = f"{_ROOT}/skills/{guided['version_id']}"
                tool = next(
                    tool
                    for tool in guided["available_tools"]
                    if tool["name"] == "submission_history"
                )

                async def ask_current(
                    tools,
                    *,
                    invalidate=None,
                    question="我的做题情况怎样？",
                    accept_after_queue=False,
                    practice_tool_only=False,
                ):
                    nonlocal invalidate_practice
                    changed = await owner.put(
                        binding, json={"enabled": True, "authorized_tools": tools}
                    )
                    assert changed.status_code == 200
                    reads_at_enqueue = len(practice_reads)
                    created = await owner.post(
                        url,
                        json={
                            "skill_version_id": guided["version_id"],
                            "request_id": str(uuid4()),
                            "chat": {
                                "question": question,
                                **({"practice_tool_only": True} if practice_tool_only else {}),
                            },
                        },
                    )
                    assert created.status_code == 202, created.text
                    assert len(practice_reads) == reads_at_enqueue
                    run_id = created.json()["run_id"]
                    if accept_after_queue:
                        db = await connect(postgres_url)
                        try:
                            assert (
                                await db.execute(
                                    "UPDATE evaluations SET state='finished', verdict='accepted', "
                                    "updated_at=clock_timestamp() WHERE id=$1",
                                    submission.evaluation_id,
                                )
                                == "UPDATE 1"
                            )
                        finally:
                            await db.close()

                    async def invalidate_pending_run():
                        path = f"{_ROOT}/runs/{run_id}"
                        active = (await owner.get(path)).json()
                        assert active["partial_reasoning"] == "练习概览读取前的推理预览。"
                        if invalidate == "lease":
                            db = await connect(postgres_url)
                            try:
                                assert (
                                    await db.execute(
                                        "UPDATE learning_agent_tasks "
                                        "SET lease_expires_at=clock_timestamp() "
                                        "- interval '1 second' WHERE run_id=$1",
                                        UUID(run_id),
                                    )
                                    == "UPDATE 1"
                                )
                            finally:
                                await db.close()
                            expected = AgentRecordConflict
                        else:
                            response = await owner.put(
                                binding, json={"enabled": True, "authorized_tools": []}
                            )
                            assert response.status_code == 200
                            expected = SkillBindingUnavailable
                        stale = (await owner.get(path)).json()
                        assert "partial_reasoning" not in stale
                        return expected

                    invalidate_practice = invalidate_pending_run if invalidate else None
                    try:
                        assert await worker.run_once()
                    finally:
                        invalidate_practice = None
                    return run_id

                await ask_current([])
                assert not questions[-1].recent_practice
                await ask_current([tool], question=no_practice_question, practice_tool_only=True)
                assert questions[-1].question == no_practice_question
                assert not practice_reads and not practice_receipts
                model.supports_review_mcp = False
                try:
                    await ask_current([tool], practice_tool_only=True)
                    assert not questions[-1].recent_practice
                    assert not practice_reads and not practice_receipts
                finally:
                    model.supports_review_mcp = True
                private_id = await ask_current(
                    [tool], accept_after_queue=True, practice_tool_only=True
                )
                assert not questions[-1].recent_practice
                assert len(practice_reads) == 1 and practice_reads[0] is not None
                assert len(practice_receipts) == 1 and not practice_receipts[0][0]["accepted"]
                private_result = (await owner.get(f"{_ROOT}/runs/{private_id}")).json()["result"]
                assert private_result["model_hint"]["review_resources"] == ["recent_practice"]
                assert (
                    await owner.put(binding, json={"enabled": True, "authorized_tools": []})
                ).is_success
                run = await owner.post(
                    url,
                    json={
                        "request_id": str(uuid4()),
                        "chat": {"question": "继续问一个通用问题。", "previous_run_id": private_id},
                    },
                )
                assert run.status_code == 202, run.text
                assert await worker.run_once()
                assert not questions[-1].recent_practice and not questions[-1].history
                last_model_turns = len(questions)
                for interruption in ("grant", "lease"):
                    interrupted_id = await ask_current([tool], invalidate=interruption)
                    interrupted = (await owner.get(f"{_ROOT}/runs/{interrupted_id}")).json()
                    assert interrupted["result"] is None
                    assert len(questions) == last_model_turns
                    assert len(practice_reads) == 1
        finally:
            await worker_engine.dispose()
            await engine.dispose()

    asyncio.run(exercise())
