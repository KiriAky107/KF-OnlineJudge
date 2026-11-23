"""Model explanations for periodic reviews with real workflow and plan persistence."""

import asyncio
import secrets
from uuid import uuid4

import httpx
from sqlalchemy import event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_submission_create import setup_app

from kf_oj.contracts.learning_model import ModelHint, ModelHintUnavailable, PlanModelFacts
from kf_oj.modules.learning.agent_worker import LearningWorker

AGENT = "/api/v1/learning/agent"
ORIGIN = "http://localhost:5173"
CONSENT = "include_model_plan_explanation"


def test_plan_explanation_requires_consent_and_preserves_the_selected_plan(
    postgres_url, tmp_path, monkeypatch
):
    async def exercise():
        app, engine, module = await setup_app(postgres_url, tmp_path)
        monkeypatch.setattr(module, "build_engine", lambda _: engine)
        worker_engine = create_async_engine(postgres_url)

        @event.listens_for(worker_engine.sync_engine, "connect")
        def restrict_role(connection, _):
            cursor = connection.cursor()
            cursor.execute("SET ROLE kf_oj_learning_worker")
            cursor.close()

        calls: list[dict] = []

        class LocalModel:
            async def hint(self, content):
                assert isinstance(content, PlanModelFacts)
                calls.append(content.model_dump(exclude_none=True))
                if len(calls) == 2:
                    raise ModelHintUnavailable("fixture provider unavailable")
                return ModelHint(
                    text="先完成草案中的题目，再记录仍需复习的步骤。",
                    model="plan-test-double",
                    response_id="test-response",
                    latency_ms=0,
                )

        worker = LearningWorker(
            async_sessionmaker(worker_engine, expire_on_commit=False), LocalModel()
        )
        try:
            async with (
                app.router.lifespan_context(app),
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(app), base_url=ORIGIN, headers={"Origin": ORIGIN}
                ) as learner,
            ):
                username = "plan_explanation"
                password = secrets.token_urlsafe(24)
                await app.state.identity.create_account(username, password)
                response = await learner.post(
                    "/api/v1/auth/login", json={"username": username, "password": password}
                )
                assert response.status_code == 200
                learner.headers["x-csrf-token"] = learner.cookies["kf_csrf"]
                catalog = (await learner.get(f"{AGENT}/skills")).json()["versions"]

                async def configure(purpose, parameters):
                    skill = next(row for row in catalog if row["purpose"] == purpose)
                    configured = await learner.put(
                        f"{AGENT}/skills/{skill['version_id']}",
                        json={
                            "enabled": True,
                            "parameters": parameters,
                            "authorized_tools": skill["available_tools"],
                        },
                    )
                    assert configured.status_code == 200, configured.text
                    session = await learner.post(f"{AGENT}/sessions", json={"purpose": purpose})
                    assert session.status_code == 201
                    return (
                        f"{AGENT}/sessions/{session.json()['session_id']}/runs",
                        skill["version_id"],
                    )

                async def finish(queue, request):
                    waiting = await learner.post(queue, json=request)
                    assert waiting.status_code == 202, waiting.text
                    url = f"{AGENT}/runs/{waiting.json()['run_id']}"
                    assert await worker.run_once()
                    result = (await learner.get(url)).json()
                    assert result["state"] == "succeeded", result
                    return url, result["result"]

                queue, skill = await configure(
                    "periodic_review",
                    {
                        "sessions_per_week": 2,
                        "cycle_days": 21,
                        "stage_goal": "private learner text",
                    },
                )

                def request(**flags):
                    return {"skill_version_id": skill, "request_id": str(uuid4()), **flags}

                _, no_items = await finish(queue, request(**{CONSENT: True}))
                assert no_items["model_status"] == "insufficient_evidence"
                assert not calls
                version = await app.state.problems.import_directory(tmp_path / "import")
                plain_request = request()
                plain_url, plain = await finish(queue, plain_request)
                replay = await learner.post(queue, json=plain_request | {CONSENT: False})
                assert replay.status_code == 202 and plain_url.endswith(replay.json()["run_id"])
                assert (
                    await learner.post(queue, json=plain_request | {CONSENT: True})
                ).status_code == 409
                assert plain["model_status"] is None and not calls

                explained_request = request(**{CONSENT: True})
                explained_url, explained = await finish(queue, explained_request)
                assert explained["model_status"] == "generated"
                assert explained["items"] == plain["items"]
                assert explained["goal"] == "private learner text"
                projection = {
                    "easy": 0,
                    "medium": 0,
                    "hard": 0,
                    "unclassified": 1,
                    "cycle_days": 21,
                    "sessions_per_week": 2,
                }
                assert calls == [projection]
                replay = await learner.post(queue, json=explained_request)
                assert replay.status_code == 202 and explained_url.endswith(replay.json()["run_id"])
                trace = (await learner.get(explained_url + "/trace")).json()
                assert trace["steps"][-1]["invocation_version"] == "responses-plan-explanation-v1"
                assert trace["steps"][-1]["state"] == "succeeded"

                unavailable_url, unavailable = await finish(queue, request(**{CONSENT: True}))
                assert unavailable["model_status"] == "unavailable"
                assert unavailable["model_hint"] is None
                assert unavailable["items"] == plain["items"]
                saved = await learner.post(explained_url + "/training")
                assert saved.status_code == 200, saved.text
                assert [item["problem_version_id"] for item in saved.json()["items"]] == [
                    str(version)
                ]
                assert explained["model_hint"]["text"] not in saved.text
                schedule = {
                    "purpose": "periodic_review",
                    "goal": "private learner text",
                    "cycle_days": 21,
                    "sessions_per_week": 2,
                }
                assert saved.json()["schedule"] == schedule
                assert "schedule" not in saved.json()["parameters"]
                saved_plan = await learner.get(f"/api/v1/trainings/{saved.json()['id']}")
                assert saved_plan.json()["schedule"] == schedule
                assert all("estimated_minutes" not in item for item in saved_plan.json()["items"])
                assert (await learner.post(unavailable_url + "/training")).status_code == 200
                assert calls == [projection, projection]

                other_queue, other_skill = await configure("guided_help", {})
                wrong_purpose = await learner.post(
                    other_queue,
                    json={
                        "skill_version_id": other_skill,
                        "request_id": str(uuid4()),
                        CONSENT: True,
                    },
                )
                assert wrong_purpose.status_code == 422
        finally:
            await worker_engine.dispose()

    asyncio.run(exercise())
