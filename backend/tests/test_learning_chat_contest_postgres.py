"""Focused acceptance of free-form chat data, consent and contest-time access."""

import asyncio
import json
import secrets
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
from conftest import connect
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_maintenance_authority import set_administrator
from test_submission_create import setup_app

from kf_oj.contracts.learning import LearningParameters
from kf_oj.contracts.submissions import CreateSubmission
from kf_oj.infra.responses_model import ResponsesLearningHint
from kf_oj.modules.contests import public as contests
from kf_oj.modules.learning.agent_worker import LearningWorker
from kf_oj.modules.learning.hint_policy import HintPolicy

ROOT = "/api/v1/learning/agent"
ORIGIN = "http://localhost:5173"


def test_free_form_chat_consent_and_contest_access(postgres_url, tmp_path, monkeypatch):
    monkeypatch.setenv("KF_OJ_LEARNING_MEMBER_MAX_HINT_LEVEL", "L1")

    async def exercise():
        app, engine, module = await setup_app(postgres_url, tmp_path)
        monkeypatch.setattr(module, "build_engine", lambda _: engine)
        operator = await connect(postgres_url)
        worker_engine = create_async_engine(postgres_url)

        @event.listens_for(worker_engine.sync_engine, "connect")
        def restrict_role(connection, _):
            cursor = connection.cursor()
            cursor.execute("SET ROLE kf_oj_learning_worker")
            cursor.close()

        calls = []
        instructions = []
        reasoning_published = asyncio.Event()
        release_reasoning = asyncio.Event()
        part_published = asyncio.Event()
        release_stream = asyncio.Event()
        selection_started = asyncio.Event()
        release_selection = asyncio.Event()
        streamed_reasoning = "先核对空区间。" * 1500
        streamed_answer = "用两个边界值。" * 1000

        class ProviderStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                def event(payload):
                    return (
                        b"data: " + json.dumps(payload, ensure_ascii=False).encode() + b"\r\n\r\n"
                    )

                yield event({"type": "response.reasoning_text.delta", "delta": streamed_reasoning})
                reasoning_published.set()
                await release_reasoning.wait()
                chunk = event({"type": "response.output_text.delta", "delta": streamed_answer})
                split = chunk.index("两".encode()) + 1
                yield chunk[:split]
                yield chunk[split:]
                part_published.set()
                await release_stream.wait()
                yield event({"type": "response.completed", "response": completed})

        completed = {
            "object": "response",
            "model": "fixture",
            "id": "fixture",
            "status": "completed",
            "output": [
                {
                    "type": "reasoning",
                    "content": [{"type": "reasoning_text", "text": "先核对空区间和边界。"}],
                },
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "用两个边界值手算。"}],
                },
            ],
        }

        async def provider(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/v1/responses"
            envelope = json.loads(request.content)
            assert envelope["store"] is False
            assert envelope["reasoning"] == {"effort": "low"}
            calls.append(json.loads(envelope["input"]))
            instructions.append(envelope["instructions"])
            question = calls[-1]["question"]
            if (choices := calls[-1].get("hint_choices")) is not None:
                assert envelope["max_output_tokens"] == 2048
                assert "stream" not in envelope
                assert 1 <= len(choices) <= 4
                if len(calls) == 1 or question == "提示输出期间收紧题单":
                    selection_started.set()
                    await release_selection.wait()
                answer = {"choice": len(choices) if calls[-1]["hint_level"] == "L2" else 1}
                if question == "尝试要求完整代码":
                    answer["answer"] = "THIS_UNAPPROVED_TEXT_MUST_NOT_BE_DELIVERED"
                return httpx.Response(
                    200,
                    json=completed
                    | {
                        "output": [
                            completed["output"][0],
                            {
                                "type": "message",
                                "role": "assistant",
                                "content": [{"type": "output_text", "text": json.dumps(answer)}],
                            },
                        ],
                    },
                )
            if (excerpt := calls[-1].get("source_excerpt")) is not None:
                assert envelope["max_output_tokens"] == 2048
                assert "stream" not in envelope
                assert "line 是原始行号" in envelope["instructions"]
                if question == "授权收回前已经到模型":
                    selection_started.set()
                    await release_selection.wait()
                answer = {
                    "line": excerpt["end_line"] + 1
                    if question == "尝试引用未授权行"
                    else excerpt["start_line"],
                    "check": "values",
                }
                return httpx.Response(
                    200,
                    json=completed
                    | {
                        "output": [
                            completed["output"][0],
                            {
                                "type": "message",
                                "role": "assistant",
                                "content": [{"type": "output_text", "text": json.dumps(answer)}],
                            },
                        ]
                    },
                )
            assert "max_output_tokens" not in envelope
            if question in {"取消后的草稿验证", "开赛前已开始的答疑"}:
                assert envelope["stream"] is True
                return httpx.Response(
                    200, headers={"content-type": "text/event-stream"}, stream=ProviderStream()
                )
            return httpx.Response(200, json=completed)

        client = httpx.AsyncClient
        monkeypatch.setattr(
            httpx,
            "AsyncClient",
            lambda **options: client(transport=httpx.MockTransport(provider), **options),
        )
        worker_sessions = async_sessionmaker(worker_engine, expire_on_commit=False)
        model = ResponsesLearningHint(
            "http://127.0.0.1/v1", "fixture", None, chat_reasoning_effort="low"
        )
        worker = LearningWorker(worker_sessions, model, hint_policy=HintPolicy(member="L1"))
        try:
            async with (
                app.router.lifespan_context(app),
                client(
                    transport=httpx.ASGITransport(app), base_url=ORIGIN, headers={"Origin": ORIGIN}
                ) as alice,
                client(
                    transport=httpx.ASGITransport(app), base_url=ORIGIN, headers={"Origin": ORIGIN}
                ) as bob,
            ):

                async def login(http, username, *, administrator=False):
                    password = secrets.token_urlsafe(24)
                    account = await app.state.identity.create_account(username, password)
                    if administrator:
                        await set_administrator(operator, account.account_id, True)
                    response = await http.post(
                        "/api/v1/auth/login",
                        json={
                            "username": username,
                            "password": password,
                        },
                    )
                    assert response.status_code == 200
                    http.headers["x-csrf-token"] = response.json()["csrf_token"]
                    return account

                owner = await login(alice, "chat_owner", administrator=True)
                other = await login(bob, "chat_other")
                version = await app.state.problems.import_directory(tmp_path / "import")
                payload = {"tags": ["数组"], "difficulty": "easy", "difficulty_key": "easy"}
                response = await alice.put(
                    f"/api/v1/admin/problem-versions/{version}/learning-metadata", json=payload
                )
                assert response.status_code == 200, response.text

                async def submit(account, source):
                    return await app.state.submissions.create(
                        account,
                        CreateSubmission(
                            problem_version_id=version,
                            language="c17",
                            idempotency_key=uuid4(),
                            source=source,
                        ),
                    )

                own = await submit(owner, "int value = 0;")
                foreign = await submit(other, "// foreign fixture")
                async with operator.transaction():
                    await operator.execute("SET LOCAL ROLE kf_oj_coordinator")
                    await operator.execute(
                        "UPDATE evaluations SET state='finished', verdict='accepted' WHERE id=$1",
                        own.evaluation_id,
                    )

                versions = (await alice.get(ROOT + "/skills")).json()["versions"]
                skill = next(row for row in versions if row["purpose"] == "guided_help")
                skill_id = skill["version_id"]
                for http in (alice, bob):
                    configured = await http.put(
                        f"{ROOT}/skills/{skill_id}",
                        json={
                            "enabled": True,
                            "authorized_tools": skill["available_tools"],
                        },
                    )
                    assert configured.status_code == 200

                async def session(http):
                    created = await http.post(f"{ROOT}/sessions", json={"purpose": "guided_help"})
                    assert created.status_code == 201, created.text
                    return f"{ROOT}/sessions/{created.json()['session_id']}/runs"

                endpoint = await session(alice)
                other_endpoint = await session(bob)

                def question(text, **chat):
                    return {
                        "request_id": str(uuid4()),
                        "skill_version_id": skill_id,
                        "chat": {"question": text, "problem_version_id": str(version), **chat},
                    }

                first = question("这题为什么一定要考虑空区间？")
                illegal = await alice.post(
                    endpoint, json=first | {"submission_id": str(own.submission_id)}
                )
                assert illegal.status_code == 422
                assert (
                    await alice.post(endpoint, json=question("跳过题意", hint_level="L1"))
                ).status_code == 422
                assert (
                    await alice.post(
                        endpoint,
                        json=question("初次提问携带评测", include_submission_details=True)
                        | {"submission_id": str(own.submission_id)},
                    )
                ).status_code == 422
                accepted = await alice.post(endpoint, json=first)
                assert accepted.status_code == 202, accepted.text
                first_id = accepted.json()["run_id"]
                assert accepted.json()["chat"]["question"] == first["chat"]["question"]
                assert accepted.json()["chat"]["hint_level"] == "L0"
                assert (await alice.post(endpoint, json=first)).json()["run_id"] == first_id
                work = asyncio.create_task(worker.run_once())
                try:
                    await asyncio.wait_for(selection_started.wait(), timeout=8)
                    current = (await alice.get(f"{ROOT}/runs/{first_id}")).json()
                    assert current["state"] == "running"
                    assert "partial_reasoning" not in current and "partial_answer" not in current
                    assert current["result"] is None
                    assert (await bob.get(f"{ROOT}/runs/{first_id}")).status_code == 404
                finally:
                    release_selection.set()
                    assert await work
                result = (await alice.get(f"{ROOT}/runs/{first_id}")).json()
                assert "partial_answer" not in result and "partial_reasoning" not in result
                assert (
                    await operator.fetchval("SELECT count(*) FROM learning_agent_partial_answers")
                    == 0
                )
                first_answer = result["result"]["model_hint"]["text"]
                assert first_answer == calls[0]["hint_choices"][0]
                assert "reasoning" not in result["result"]["model_hint"]
                session_id = result["session_id"]
                conversation = f"{ROOT}/sessions/{session_id}/conversation"
                page = (await alice.get(conversation)).json()
                assert "reasoning" not in page["runs"][0]
                assert page["runs"][0]["answer"] == first_answer
                assert (await bob.get(conversation)).status_code == 404
                assert calls[0]["question"] == first["chat"]["question"]
                assert calls[0]["problem_title"] and calls[0]["problem_statement"]
                assert calls[0]["recent_practice"] == [
                    {
                        "title": calls[0]["problem_title"],
                        "difficulty": "简单",
                        "difficulty_score": 800,
                        "accepted": True,
                    }
                ]
                assert set(calls[0]) == {
                    "question",
                    "hint_level",
                    "hint_choices",
                    "history",
                    "problem_title",
                    "problem_statement",
                    "public_sample_count",
                    "recent_practice",
                }
                assert calls[0]["hint_level"] == "L0"
                assert "只澄清题意" in instructions[0]
                usage_query = (
                    "SELECT first_provided_at FROM learning_hint_usage "
                    "WHERE owner_id=$1 AND version_id=$2"
                )
                assert await operator.fetchval(usage_query, owner.account_id, version) is None

                direction = await alice.post(
                    endpoint,
                    json=question("给我一个方向", previous_run_id=first_id, hint_level="L1"),
                )
                assert direction.status_code == 202, direction.text
                direction_id = direction.json()["run_id"]
                assert (
                    await alice.post(
                        endpoint,
                        json=question(
                            "上一步还未完成", previous_run_id=direction_id, hint_level="L2"
                        ),
                    )
                ).status_code == 404
                assert await worker.run_once()
                assert calls[1]["hint_level"] == "L1" and "探索的方向" in instructions[1]
                owner_hint_at = await operator.fetchval(usage_query, owner.account_id, version)
                assert owner_hint_at is not None
                assert calls[1]["history"] == [
                    {"question": first["chat"]["question"], "answer": first_answer}
                ]
                assert (
                    await alice.post(
                        endpoint,
                        json=question("跳过边界", previous_run_id=direction_id, hint_level="L3"),
                    )
                ).status_code == 422

                state_question = question(
                    "结合判定核对一个边界",
                    previous_run_id=direction_id,
                    hint_level="L2",
                    include_submission_details=True,
                )
                attach = {
                    "include_source_evidence": True,
                    "model_source_excerpt": {"start_line": 1, "end_line": 1},
                }
                own_submission = {"submission_id": str(own.submission_id)}
                assert (
                    await alice.post(endpoint, json=state_question | own_submission | attach)
                ).status_code == 422
                state_turn = await alice.post(endpoint, json=state_question | own_submission)
                assert state_turn.status_code == 202, state_turn.text
                state_id = state_turn.json()["run_id"]
                assert await worker.run_once()
                assert calls[2]["hint_level"] == "L2" and "公开手算" in instructions[2]
                assert calls[2]["submission"]["verdict"] == "accepted"
                assert "source_excerpt" not in calls[2]

                approved = (await alice.get(f"{ROOT}/runs/{state_id}")).json()
                assert approved["result"]["model_hint"]["text"] == calls[2]["hint_choices"][-1]
                assert "reasoning" not in approved["result"]["model_hint"]

                follow = question(
                    "那可以结合这次提交看吗？",
                    previous_run_id=state_id,
                    hint_level="L3",
                    include_submission_details=True,
                )
                assert (
                    await bob.post(
                        other_endpoint,
                        json=question(
                            "不能读取他人的对话",
                            previous_run_id=first_id,
                        ),
                    )
                ).status_code == 404
                assert (
                    await alice.post(
                        endpoint,
                        json=follow
                        | attach
                        | {
                            "submission_id": str(foreign.submission_id),
                        },
                    )
                ).status_code == 404
                accepted = await alice.post(
                    endpoint,
                    json=follow
                    | attach
                    | {
                        "submission_id": str(own.submission_id),
                    },
                )
                assert accepted.status_code == 202, accepted.text
                private_id = accepted.json()["run_id"]
                assert await worker.run_once()
                assert len(calls) == 4, (await alice.get(f"{ROOT}/runs/{private_id}")).text
                assert calls[3]["hint_level"] == "L3" and "原始行号" in instructions[3]
                assert calls[3]["history"] == calls[2]["history"]
                assert calls[3]["submission"]["verdict"] == "accepted"
                assert calls[3]["source_excerpt"]["text"] == "int value = 0;"
                diagnosis = (await alice.get(f"{ROOT}/runs/{private_id}")).json()
                diagnosis = diagnosis["result"]["model_hint"]
                assert diagnosis["source_line"] == 1
                assert "reasoning" not in diagnosis and "int value" not in diagnosis["text"]
                private_answer = diagnosis["text"]
                assert private_answer.startswith("核对第 **1** 行。")

                third = await alice.post(
                    endpoint,
                    json=question(
                        "接着讲边界条件",
                        previous_run_id=private_id,
                        hint_level="L3",
                    ),
                )
                assert third.status_code == 202, third.text
                assert await worker.run_once()
                assert len(calls) == 5 and calls[4]["history"] == calls[3]["history"]
                assert "submission" not in calls[4] and "source_excerpt" not in calls[4]

                third_id = third.json()["run_id"]
                chat_history = f"{ROOT}/runs/{third_id}/conversation"
                page = await alice.get(chat_history, params={"limit": 1})
                assert page.status_code == 200, page.text
                assert page.json() == {
                    "messages": [
                        {
                            "run_id": private_id,
                            "question": follow["chat"]["question"],
                            "answer": private_answer,
                            "state": "succeeded",
                        }
                    ],
                    "next_before": private_id,
                }
                previous = await alice.get(f"{ROOT}/runs/{private_id}/conversation")
                assert [row["run_id"] for row in previous.json()["messages"]] == [
                    first_id,
                    direction_id,
                    state_id,
                ]
                assert previous.json()["next_before"] is None
                assert (await bob.get(chat_history)).status_code == 404

                runs = f"{ROOT}/runs"
                flat = (await alice.get(runs)).json()["items"]
                assert [row["run_id"] for row in flat] == [
                    third_id,
                    private_id,
                    state_id,
                    direction_id,
                    first_id,
                ]
                grouped = (await alice.get(runs, params={"group_chat": True})).json()["items"]
                assert [row["run_id"] for row in grouped] == [third_id]
                assert grouped[0]["conversation_title"] == first["chat"]["question"]
                assert (await alice.get(runs, params={"problem_version_id": str(version)})).json()[
                    "items"
                ] == grouped
                assert (await alice.get(runs, params={"problem_version_id": str(uuid4())})).json()[
                    "items"
                ] == []
                assert (await bob.get(runs, params={"group_chat": True})).json()["items"] == []
                assert len(calls) == 5

                starts = datetime.now(UTC) + timedelta(minutes=5)
                ends = starts + timedelta(hours=2)
                schedule = {
                    "key": "chat-contest",
                    "title": "学习助手规则测试",
                    "starts_at": starts.isoformat(),
                    "ends_at": ends.isoformat(),
                }
                created = await alice.post("/api/v1/admin/contest-schedules", json=schedule)
                assert created.status_code == 201, created.text
                registered = await alice.put("/api/v1/contests/chat-contest/registration")
                assert registered.json() == {"confirmed": True}
                access = "/api/v1/contests/agent-access"
                assert (await alice.get(access)).json()["blocked_until"] is None
                queued = await alice.post(endpoint, json=question("开赛前排队的问题"))
                assert queued.status_code == 202, queued.text

                class Clock(datetime):
                    instant = starts

                    @classmethod
                    def now(cls, tz=None):
                        return cls.instant.astimezone(tz)

                monkeypatch.setattr(contests, "datetime", Clock)
                assert (await alice.get(access)).json()["blocked_until"] is not None
                assert (await alice.get(f"{ROOT}/runs/{first_id}")).status_code == 403
                assert (await alice.get(chat_history)).status_code == 403
                assert (
                    await alice.post(endpoint, json=question("比赛期间提问"))
                ).status_code == 403
                assert (
                    await alice.post(f"{ROOT}/sessions", json={"purpose": "focused_training"})
                ).status_code == 403
                assert (await bob.get(f"{ROOT}/skills")).status_code == 200
                assert await worker.run_once()
                assert len(calls) == 5

                Clock.instant = ends
                assert (await alice.get(access)).json()["blocked_until"] is None
                blocked_run = await alice.get(f"{ROOT}/runs/{queued.json()['run_id']}")
                assert blocked_run.status_code == 200, blocked_run.text
                assert blocked_run.json()["failure_code"] == "contest_in_progress"

                part_published.clear()
                release_reasoning.set()
                release_stream.clear()
                stopped = await alice.post(
                    endpoint,
                    json=question("取消后的草稿验证", problem_version_id=None),
                )
                assert stopped.status_code == 202, stopped.text
                cancelled_id = stopped.json()["run_id"]
                cancelled_path = f"{ROOT}/runs/{cancelled_id}"
                running_stream = asyncio.create_task(worker.run_once())
                try:
                    await asyncio.wait_for(reasoning_published.wait(), timeout=8)
                    await asyncio.wait_for(part_published.wait(), timeout=8)
                    preview = (await alice.get(cancelled_path)).json()
                    assert preview["partial_answer"] == streamed_answer
                    assert preview["partial_reasoning"] == streamed_reasoning
                    cancellation = await alice.post(f"{cancelled_path}/cancel")
                    assert cancellation.status_code == 200, cancellation.text
                    assert "partial_answer" not in cancellation.json()
                    assert "partial_reasoning" not in cancellation.json()
                    assert await asyncio.wait_for(running_stream, timeout=9)
                finally:
                    release_stream.set()
                    if not running_stream.done():
                        await running_stream
                cancelled = (await alice.get(cancelled_path)).json()
                assert cancelled["state"] == "cancelled" and "partial_answer" not in cancelled
                assert "partial_reasoning" not in cancelled
                assert (
                    await operator.fetchval("SELECT count(*) FROM learning_agent_partial_answers")
                    == 0
                )
                trace = (await alice.get(f"{cancelled_path}/trace")).json()
                assert trace["steps"][-1]["state"] == "cancelled"
                assert trace["model_elapsed_ms"] > 0 and "model_usage" not in trace
                Clock.instant = starts - timedelta(seconds=1)
                part_published.clear()
                release_stream.clear()
                started = await alice.post(
                    endpoint,
                    json=question("开赛前已开始的答疑", problem_version_id=None),
                )
                assert started.status_code == 202, started.text
                interrupted_path = f"{ROOT}/runs/{started.json()['run_id']}"
                running_stream = asyncio.create_task(worker.run_once())
                try:
                    await asyncio.wait_for(part_published.wait(), timeout=8)
                    assert (await alice.get(interrupted_path)).json()["partial_answer"]
                    Clock.instant = starts
                    assert (await alice.get(interrupted_path)).status_code == 403
                    assert await asyncio.wait_for(running_stream, timeout=9)
                finally:
                    release_stream.set()
                    if not running_stream.done():
                        await running_stream
                Clock.instant = ends
                interrupted = (await alice.get(interrupted_path)).json()
                assert interrupted["state"] == "cancelled" and "partial_answer" not in interrupted
                assert "partial_reasoning" not in interrupted
                assert interrupted["result"] is None
                assert (
                    await operator.fetchval("SELECT count(*) FROM learning_agent_partial_answers")
                    == 0
                )
                trace = (await alice.get(f"{interrupted_path}/trace")).json()
                assert trace["steps"][-1]["state"] == "cancelled"
                assert trace["model_elapsed_ms"] > 0 and "model_usage" not in trace

                # Browser role is resolved from the account. Worker role comes from the receipt.
                ceiling_path = f"{ROOT}/hint-ceiling"
                scope = {"problem_version_id": str(version)}
                assert (await alice.get(ceiling_path, params=scope)).json() == {
                    "max_hint_level": "L3"
                }
                assert (await bob.get(ceiling_path, params=scope)).json() == {
                    "max_hint_level": "L1"
                }
                created_plan = await alice.post(
                    "/api/v1/trainings",
                    json={"idempotency_key": str(uuid4()), "count": 1, "tags": ["数组"]},
                )
                assert created_plan.status_code == 201, created_plan.text
                training = created_plan.json()
                assert training["max_hint_level"] == "L3"
                assert training["items"][0]["problem_version_id"] == str(version)
                training_id = training["id"]
                scope["training_id"] = training_id
                update_path = f"/api/v1/trainings/{training_id}/hint-ceiling"
                changed = await alice.put(update_path, json={"max_hint_level": "L2"})
                assert changed.status_code == 200, changed.text
                assert (await alice.get(ceiling_path, params=scope)).json() == {
                    "max_hint_level": "L2"
                }
                assert (await bob.get(ceiling_path, params=scope)).status_code == 403
                assert (
                    await bob.put(update_path, json={"max_hint_level": "L0"})
                ).status_code == 404
                missing_item = scope | {"problem_version_id": str(uuid4())}
                assert (await alice.get(ceiling_path, params=missing_item)).status_code == 403
                async with worker_engine.connect() as connection:
                    assert not await connection.scalar(
                        text(
                            "SELECT has_column_privilege("
                            "'learning_plans', 'max_hint_level', 'SELECT')"
                        )
                    )

                assert (
                    await alice.post(
                        endpoint,
                        json=question(
                            "换作用范围不能继承原等级",
                            previous_run_id=first_id,
                            training_id=training_id,
                            hint_level="L1",
                        ),
                    )
                ).status_code == 404
                scoped = await alice.post(
                    endpoint, json=question("题单里先澄清题意", training_id=training_id)
                )
                assert scoped.status_code == 202, scoped.text
                scoped_id = scoped.json()["run_id"]
                assert scoped.json()["chat"]["training_id"] == training_id
                assert await worker.run_once()
                assert calls[-1]["hint_level"] == "L0"
                assert "training_id" not in calls[-1]

                def scoped_direction(message):
                    return question(
                        message, previous_run_id=scoped_id, training_id=training_id, hint_level="L1"
                    )

                above_site = await alice.post(endpoint, json=scoped_direction("已收紧站点上限"))
                assert above_site.status_code == 202, above_site.text
                dispatch_count = len(calls)
                limited_worker = LearningWorker(
                    worker_sessions, model, hint_policy=HintPolicy(site="L0")
                )
                assert await limited_worker.run_once()
                result = (await alice.get(f"{ROOT}/runs/{above_site.json()['run_id']}")).json()
                assert result["state"] == "failed"
                assert result["failure_code"] == "hint_level_unavailable"
                assert len(calls) == dispatch_count

                selection_started.clear()
                release_selection.clear()
                live = await alice.post(endpoint, json=scoped_direction("提示输出期间收紧题单"))
                assert live.status_code == 202, live.text
                live_path = f"{ROOT}/runs/{live.json()['run_id']}"
                pending = asyncio.create_task(worker.run_once())
                try:
                    await asyncio.wait_for(selection_started.wait(), timeout=8)
                    before = (await alice.get(live_path)).json()
                    assert "partial_reasoning" not in before and "partial_answer" not in before
                    assert (
                        await alice.put(update_path, json={"max_hint_level": "L0"})
                    ).status_code == 200
                    assert (
                        await alice.post(endpoint, json=scoped_direction("超出题单上限"))
                    ).status_code == 403
                finally:
                    release_selection.set()
                    assert await pending
                result = (await alice.get(live_path)).json()
                assert result["state"] == "failed" and result["result"] is None
                assert result["failure_code"] == "hint_level_unavailable"
                assert "partial_reasoning" not in result and "partial_answer" not in result
                assert (
                    await operator.fetchval("SELECT count(*) FROM learning_agent_partial_answers")
                    == 0
                )

                # Alice passed before requesting assistance. Later hints must not reclassify it.
                assert (
                    await operator.fetchval(usage_query, owner.account_id, version) == owner_hint_at
                )
                bob_start = await bob.post(other_endpoint, json=question("读一下题"))
                assert bob_start.status_code == 202, bob_start.text
                assert await worker.run_once()
                assert await operator.fetchval(usage_query, other.account_id, version) is None
                bob_direction = await bob.post(
                    other_endpoint,
                    json=question(
                        "先请给我一个方向",
                        previous_run_id=bob_start.json()["run_id"],
                        hint_level="L1",
                    ),
                )
                assert bob_direction.status_code == 202, bob_direction.text
                assert await worker.run_once()
                bob_path = f"{ROOT}/runs/{bob_direction.json()['run_id']}"
                bob_answer = (await bob.get(bob_path)).json()
                assert bob_answer["result"]["model_hint"]["text"] == calls[-1]["hint_choices"][0]
                bob_hint_at = await operator.fetchval(usage_query, other.account_id, version)
                assert bob_hint_at is not None

                solved = await submit(other, "// fixture after the visible directional hint")
                async with operator.transaction():
                    await operator.execute("SET LOCAL ROLE kf_oj_coordinator")
                    await operator.execute(
                        "UPDATE evaluations SET state='finished', verdict='accepted' WHERE id=$1",
                        solved.evaluation_id,
                    )
                learning = app.state.learning
                inputs = LearningParameters(tags=("数组",), count=1)
                future = datetime.now(UTC) + timedelta(days=8)
                independent = (await learning.recommend(owner, inputs, as_of=future)).items[0]
                assisted = (await learning.recommend(other, inputs, as_of=future)).items[0]
                assert independent.timing.first_acceptance_kind == "independent"
                assert assisted.timing.first_acceptance_kind == "assisted"
                assert independent.timing.status == "review_later"
                assert assisted.timing.status == "review_due"
                assert independent.previously_accepted and assisted.previously_accepted
                assert assisted.timing.last_accepted_at > bob_hint_at

                invalid = await alice.post(endpoint, json=question("尝试要求完整代码"))
                assert invalid.status_code == 202, invalid.text
                assert await worker.run_once()
                invalid_id = invalid.json()["run_id"]
                invalid_path = f"{ROOT}/runs/{invalid_id}"
                result = await alice.get(invalid_path)
                assert "THIS_UNAPPROVED_TEXT_MUST_NOT_BE_DELIVERED" not in result.text
                assert result.json()["result"]["model_hint"] is None
                assert "partial_reasoning" not in result.json()
                trace = (await alice.get(f"{invalid_path}/trace")).json()
                assert trace["steps"][-1]["error_code"] == "model_invalid_response"
                rejected = await alice.post(
                    endpoint,
                    json=question(
                        "拒绝的答复不解锁下一步", previous_run_id=invalid_id, hint_level="L1"
                    ),
                )
                assert rejected.status_code == 422

                old_request = {
                    "request_id": str(uuid4()),
                    "skill_version_id": skill_id,
                    "include_source_evidence": True,
                    "submission_id": str(foreign.submission_id),
                }
                denied = await bob.post(other_endpoint, json=old_request)
                assert denied.status_code == 403
                old_request.update(request_id=str(uuid4()), include_source_evidence=False)
                assert (await bob.post(other_endpoint, json=old_request)).status_code == 403

                def diagnostic_request(message):
                    return (
                        question(message, previous_run_id=third_id, hint_level="L3")
                        | attach
                        | {"submission_id": str(own.submission_id)}
                    )

                outside = await alice.post(endpoint, json=diagnostic_request("尝试引用未授权行"))
                assert outside.status_code == 202, outside.text
                assert await worker.run_once()
                outside_path = f"{ROOT}/runs/{outside.json()['run_id']}"
                result = (await alice.get(outside_path)).json()
                assert result["result"]["model_hint"] is None
                trace = (await alice.get(f"{outside_path}/trace")).json()
                assert trace["steps"][-1]["error_code"] == "model_invalid_response"

                selection_started.clear()
                release_selection.clear()
                in_flight = await alice.post(
                    endpoint, json=diagnostic_request("授权收回前已经到模型")
                )
                assert in_flight.status_code == 202, in_flight.text
                active_path = f"{ROOT}/runs/{in_flight.json()['run_id']}"
                pending = asyncio.create_task(worker.run_once())
                try:
                    await asyncio.wait_for(selection_started.wait(), timeout=8)
                    current = (await alice.get(active_path)).json()
                    assert "partial_answer" not in current and "partial_reasoning" not in current
                    configured = await alice.put(
                        f"{ROOT}/skills/{skill_id}",
                        json={
                            "enabled": True,
                            "authorized_tools": [
                                tool
                                for tool in skill["available_tools"]
                                if tool["name"] != "source_evidence"
                            ],
                        },
                    )
                    assert configured.status_code == 200, configured.text
                finally:
                    release_selection.set()
                    assert await pending
                result = (await alice.get(active_path)).json()
                assert result["state"] == "failed" and result["result"] is None
                assert result["failure_code"] == "authorization_unavailable"
        finally:
            await operator.close()
            await worker_engine.dispose()
            await engine.dispose()

    asyncio.run(exercise())
