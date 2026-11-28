"""Exercise shared Responses budgets with a local HTTP fixture and real worker roles."""

import asyncio
import json
import secrets
from uuid import UUID, uuid4

import httpx
from conftest import connect
from sqlalchemy import event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_submission_create import setup_app

from kf_oj.infra.responses_model import ResponsesLearningHint
from kf_oj.modules.learning.agent_model_budget import AgentModelLimits
from kf_oj.modules.learning.agent_worker import LearningWorker

ROOT = "/api/v1/learning/agent"
ORIGIN = "http://localhost:5173"


def test_worker_replicas_share_one_paid_attempt_budget_without_external_requests(
    postgres_url, tmp_path, monkeypatch
):
    async def exercise():
        app, engine, module = await setup_app(postgres_url, tmp_path)
        monkeypatch.setattr(module, "build_engine", lambda _: engine)
        operator = await connect(postgres_url)
        worker_engine = create_async_engine(postgres_url)
        requests = []

        async def responses(reader, writer):
            try:
                head = await reader.readuntil(b"\r\n\r\n")
                first, *headers = head.split(b"\r\n")
                assert first == b"POST /responses HTTP/1.1"
                fields = dict(
                    (name.lower(), value.strip())
                    for name, value in (line.split(b":", 1) for line in headers if line)
                )
                body = json.loads(await reader.readexactly(int(fields[b"content-length"])))
                requests.append(body)
                if "图遍历" in body["input"]:
                    status = "200 OK"
                    data = {
                        "object": "response",
                        "status": "incomplete",
                        "incomplete_details": {"reason": "max_output_tokens"},
                        "usage": {"input_tokens": 12, "output_tokens": 128, "total_tokens": 140},
                        "output": [{"content": [{"text": "private partial provider output"}]}],
                    }
                else:
                    status = "200 OK"
                    data = {
                        "id": "resp-local-fixture",
                        "object": "response",
                        "status": "completed",
                        "model": "budget-fixture",
                        "usage": {"input_tokens": 10, "output_tokens": 3, "total_tokens": 13},
                        "output": [
                            {
                                "type": "message",
                                "role": "assistant",
                                "content": [{"type": "output_text", "text": "测试模型提示。"}],
                            }
                        ],
                    }
                payload = json.dumps(data).encode()
                writer.write(
                    f"HTTP/1.1 {status}\r\nContent-Type: application/json\r\n"
                    f"Content-Length: {len(payload)}\r\nConnection: close\r\n\r\n".encode()
                    + payload
                )
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()

        server = await asyncio.start_server(responses, "127.0.0.1", 0)
        model = ResponsesLearningHint(
            f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}", "budget-fixture", None
        )

        @event.listens_for(worker_engine.sync_engine, "connect")
        def restrict_role(connection, _):
            cursor = connection.cursor()
            cursor.execute("SET ROLE kf_oj_learning_worker")
            cursor.close()

        def client():
            return httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url=ORIGIN, headers={"Origin": ORIGIN}
            )

        async def login(http, username):
            password = secrets.token_urlsafe(24)
            await app.state.identity.create_account(username, password)
            response = await http.post(
                "/api/v1/auth/login", json={"username": username, "password": password}
            )
            assert response.status_code == 200
            assert "private partial provider output" not in response.text
            http.headers["x-csrf-token"] = http.cookies["kf_csrf"]

        async def chosen(http):
            rows = (await http.get(ROOT + "/skills")).json()["versions"]
            selected = next(row["version_id"] for row in rows if row["purpose"] == "guided_help")
            saved = await http.put(
                f"{ROOT}/skills/{selected}", json={"enabled": True, "authorized_tools": []}
            )
            assert saved.status_code == 200
            return selected

        async def start(http, selected, topic):
            session = await http.post(f"{ROOT}/sessions", json={"purpose": "guided_help"})
            assert session.status_code == 201
            result = await http.post(
                f"{ROOT}/sessions/{session.json()['session_id']}/runs",
                json={
                    "skill_version_id": selected,
                    "request_id": str(uuid4()),
                    "model_topic": topic,
                },
            )
            assert result.status_code == 202, result.text
            return f"{ROOT}/runs/{result.json()['run_id']}"

        async def finished_trace(http, url):
            response = await http.get(url + "/trace")
            assert response.status_code == 200
            trace = response.json()
            row = await operator.fetchrow(
                "SELECT started_at, finished_at, reported_usage::text AS usage "
                "FROM learning_agent_model_attempts WHERE run_id=$1",
                UUID(url.rsplit("/", 1)[-1]),
            )
            assert row is not None and row["finished_at"] is not None
            assert trace["model_elapsed_ms"] == int(
                (row["finished_at"] - row["started_at"]).total_seconds() * 1000
            )
            assert trace["model_usage"] == json.loads(row["usage"])
            assert set(trace).isdisjoint({"owner_id", "started_at", "finished_at", "expires_at"})
            return trace

        budget = AgentModelLimits(account_per_hour=1, service_per_hour=2)
        sessions = async_sessionmaker(worker_engine, expire_on_commit=False)
        workers = [LearningWorker(sessions, model, model_limits=budget) for _ in range(2)]
        try:
            async with (
                server,
                app.router.lifespan_context(app),
                client() as first,
                client() as second,
                client() as third,
            ):
                await login(first, "budget_first")
                await login(second, "budget_second")
                await login(third, "budget_third")

                first_id = await chosen(first)
                attempts = [await start(first, first_id, "binary_search") for _ in range(2)]
                assert all(await asyncio.gather(*(worker.run_once() for worker in workers)))
                statuses = {}
                for url in attempts:
                    run = (await first.get(url)).json()
                    assert run["state"] == "succeeded", run
                    statuses[run["result"]["model_status"]] = url
                assert set(statuses) == {"budget_exhausted", "generated"}
                assert len(requests) == 1 and "二分查找" in requests[0]["input"]
                assert "max_output_tokens" not in requests[0]
                assert requests[0]["store"] is False
                assert not any(key in requests[0] for key in ("tools", "user", "metadata"))
                generated = (await first.get(statuses["generated"])).json()["result"]
                assert generated["model_hint"]["usage"]["total_tokens"] == 13
                blocked = (await first.get(statuses["budget_exhausted"] + "/trace")).json()
                assert blocked["steps"] == [] and "model_elapsed_ms" not in blocked
                assert "model_usage" not in blocked
                success_trace = await finished_trace(first, statuses["generated"])
                assert success_trace["steps"][0]["state"] == "succeeded"
                assert success_trace["model_usage"] == generated["model_hint"]["usage"]
                assert (await second.get(statuses["generated"])).status_code == 404
                assert (await second.get(statuses["generated"] + "/trace")).status_code == 404

                degraded = await start(second, await chosen(second), "graph_traversal")
                assert await workers[1].run_once()
                failure = (await second.get(degraded)).json()
                assert failure["state"] == "succeeded"
                assert failure["result"]["model_status"] == "unavailable"
                assert failure["result"]["model_hint"] is None
                failed_trace = await finished_trace(second, degraded)
                assert failed_trace["model_usage"] == {
                    "input_tokens": 12,
                    "output_tokens": 128,
                    "total_tokens": 140,
                }
                step = failed_trace["steps"][0]
                assert step["state"] == "failed"
                assert step["error_code"] == "model_output_limit"

                shared = await start(third, await chosen(third), "dynamic_programming")
                assert await workers[0].run_once()
                exhausted = (await third.get(shared)).json()
                assert exhausted["state"] == "succeeded"
                assert exhausted["result"]["model_status"] == "budget_exhausted"
                assert len(requests) == 2
                budget_rows = await operator.fetchrow(
                    "SELECT count(*) AS attempts, count(finished_at) AS finished "
                    "FROM learning_agent_model_attempts"
                )
                assert tuple(budget_rows) == (2, 2)
        finally:
            await worker_engine.dispose()
            await operator.close()

    asyncio.run(exercise())
