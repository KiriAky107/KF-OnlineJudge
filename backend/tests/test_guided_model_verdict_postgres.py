"""Check consent and owned evidence for verdict-aware model hints."""

import asyncio
import secrets
from uuid import uuid4

import httpx
from conftest import connect
from sqlalchemy import event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_submission_create import setup_app

from kf_oj.contracts.learning_model import GuidedHelpModelFacts, ModelHint
from kf_oj.contracts.submissions import CreateSubmission
from kf_oj.modules.learning.agent_worker import LearningWorker

ROOT = "/api/v1/learning/agent"
ORIGIN = "http://localhost:5173"


def test_guided_model_receives_only_explicitly_authorized_owned_verdict(
    postgres_url, tmp_path, monkeypatch
):
    async def exercise():
        app, engine, module = await setup_app(postgres_url, tmp_path)
        monkeypatch.setattr(module, "build_engine", lambda _: engine)
        operator = await connect(postgres_url)
        worker_engine = create_async_engine(postgres_url)
        calls = []

        @event.listens_for(worker_engine.sync_engine, "connect")
        def restrict_role(connection, _):
            cursor = connection.cursor()
            cursor.execute("SET ROLE kf_oj_learning_worker")
            cursor.close()

        class Model:
            async def hint(self, facts):
                data = (
                    facts.model_dump(mode="json")
                    if isinstance(facts, GuidedHelpModelFacts)
                    else facts
                )
                calls.append(data)
                return ModelHint(
                    text="先用最小输入复核本次搜索区间。",
                    model="local-fixture",
                    response_id="fixture",
                    latency_ms=0,
                )

        worker = LearningWorker(async_sessionmaker(worker_engine, expire_on_commit=False), Model())
        try:
            async with (
                app.router.lifespan_context(app),
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(app), base_url=ORIGIN, headers={"Origin": ORIGIN}
                ) as http,
            ):
                password = secrets.token_urlsafe(24)
                actor = await app.state.identity.create_account("verdict_owner", password)
                outsider = await app.state.identity.create_account(
                    "verdict_outsider", secrets.token_urlsafe(24)
                )
                login = await http.post(
                    "/api/v1/auth/login", json={"username": "verdict_owner", "password": password}
                )
                assert login.status_code == 200
                http.headers["x-csrf-token"] = http.cookies["kf_csrf"]
                problem = await app.state.problems.import_directory(tmp_path / "import")

                async def create(owner):
                    return await app.state.submissions.create(
                        owner,
                        CreateSubmission(
                            problem_version_id=problem,
                            language="cpp20",
                            idempotency_key=uuid4(),
                            source="// only in submission storage",
                        ),
                    )

                finished, pending, foreign = [
                    await create(account) for account in (actor, actor, outsider)
                ]
                async with operator.transaction():
                    await operator.execute("SET LOCAL ROLE kf_oj_coordinator")
                    await operator.execute(
                        "UPDATE evaluations SET state='finished', verdict='wrong_answer' "
                        "WHERE id IN ($1, $2)",
                        finished.evaluation_id,
                        foreign.evaluation_id,
                    )

                versions = (await http.get(ROOT + "/skills")).json()["versions"]
                skill = next(row for row in versions if row["purpose"] == "guided_help")
                skill_id = skill["version_id"]
                binding = await http.put(
                    f"{ROOT}/skills/{skill_id}",
                    json={"enabled": True, "authorized_tools": skill["available_tools"]},
                )
                assert binding.status_code == 200
                response = await http.post(f"{ROOT}/sessions", json={"purpose": "guided_help"})
                assert response.status_code == 201
                endpoint = f"{ROOT}/sessions/{response.json()['session_id']}/runs"

                def request(**body):
                    return {
                        "skill_version_id": skill_id,
                        "request_id": str(uuid4()),
                        "model_topic": "binary_search",
                        **body,
                    }

                async def run(body):
                    accepted = await http.post(endpoint, json=body)
                    assert accepted.status_code == 202, accepted.text
                    assert await worker.run_once()
                    url = f"{ROOT}/runs/{accepted.json()['run_id']}"
                    return url, (await http.get(url)).json()

                consent = {
                    "include_model_verdict": True,
                    "submission_id": str(finished.submission_id),
                }
                assert (
                    await http.post(endpoint, json=request(submission_id=consent["submission_id"]))
                ).status_code == 422
                assert (
                    await http.post(endpoint, json=request(include_model_verdict=True))
                ).status_code == 422
                assert (
                    await http.post(endpoint, json=request(include_source_evidence=True, **consent))
                ).status_code == 422

                _, baseline = await run(request())
                assert baseline["result"]["evidence"] is None
                assert baseline["result"]["model_status"] == "generated"
                assert calls == ["binary_search"]

                body = request(**consent)
                url, actual = await run(body)
                assert actual["state"] == "succeeded"
                result = actual["result"]
                assert result["hint"] == baseline["result"]["hint"]
                assert result["model_hint_basis"] == "topic_and_verdict"
                assert result["evidence"]["evaluation_id"] == str(finished.evaluation_id)
                assert result["code"] is None
                assert calls == [
                    "binary_search",
                    {"topic": "binary_search", "verdict": "wrong_answer"},
                ]
                steps = (await http.get(url + "/trace")).json()["steps"]
                assert [step["invocation_version"] for step in steps] == [
                    "submission_history-v1",
                    "responses-concept-verdict-v1",
                ]
                assert (await http.post(endpoint, json=body)).json() == actual

                _, missing = await run(
                    request(include_model_verdict=True, submission_id=str(pending.submission_id))
                )
                assert missing["result"]["model_status"] == "insufficient_evidence"
                assert missing["result"]["model_hint"] is None
                _, denied = await run(
                    request(include_model_verdict=True, submission_id=str(foreign.submission_id))
                )
                assert denied["state"] == "failed"
                assert denied["failure_code"] == "evidence_unavailable"
                assert len(calls) == 2
        finally:
            await worker_engine.dispose()
            await operator.close()

    asyncio.run(exercise())
