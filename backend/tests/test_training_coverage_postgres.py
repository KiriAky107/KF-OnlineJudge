"""Use the real platform and learning Worker to plan across distinct knowledge tags."""

import asyncio
import secrets
from uuid import uuid4

import httpx
from sqlalchemy import event, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_problems import import_fixture
from test_submission_create import setup_app

from kf_oj.modules.learning.agent_worker import LearningWorker
from kf_oj.modules.problems.learning_models import ProblemLearningMetadata

ROOT = "/api/v1"
AGENT = f"{ROOT}/learning/agent"
ORIGIN = "http://localhost:5173"


def test_learning_plans_and_worker_preserve_requested_goal_coverage(
    postgres_url, tmp_path, monkeypatch
):
    async def exercise():
        app, platform_engine, module = await setup_app(postgres_url, tmp_path)
        monkeypatch.setattr(module, "build_engine", lambda _: platform_engine)
        worker_engine = create_async_engine(postgres_url)

        @event.listens_for(worker_engine.sync_engine, "connect")
        def restrict_worker(connection, _):
            cursor = connection.cursor()
            cursor.execute("SET ROLE kf_oj_learning_worker")
            cursor.close()

        async def candidate(slug, difficulty, tags):
            directory = tmp_path / slug
            import_fixture(directory, slug=slug)
            version = await app.state.problems.import_directory(directory)
            async with app.state.sessions.begin() as db:
                db.add(
                    ProblemLearningMetadata(
                        problem_version_id=version, tags=tags, difficulty=difficulty, revision=1
                    )
                )
            return version

        try:
            async with (
                app.router.lifespan_context(app),
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(app), base_url=ORIGIN, headers={"origin": ORIGIN}
                ) as browser,
            ):
                username = "coverage_learner"
                password = secrets.token_urlsafe(24)
                await app.state.identity.create_account(username, password)
                logged = await browser.post(
                    f"{ROOT}/auth/login", json={"username": username, "password": password}
                )
                assert logged.status_code == 200, logged.text
                browser.headers["x-csrf-token"] = browser.cookies["kf_csrf"]
                goals = ["二分查找", "前缀和", "数组", "图论"]
                left = await candidate("coverage-search", "easy", ["二分查找"])
                right = await candidate("coverage-range", "medium", ["前缀和", "数组"])
                await candidate("coverage-advanced", "hard", goals)
                coverage = {"covered": goals[:3], "missing": ["图论"]}
                parameters = {"tags": goals, "count": 2}
                strict = await browser.post(
                    f"{ROOT}/learning/recommendations", json=parameters | {"difficulty": "easy"}
                )
                assert strict.status_code == 200 and strict.json()["items"] == []
                easy = await browser.post(
                    f"{ROOT}/learning/recommendations",
                    json=parameters | {"difficulty": "easy", "cover_each_goal": True},
                )
                assert easy.status_code == 200, easy.text
                assert [row["problem_version_id"] for row in easy.json()["items"]] == [str(left)]
                assert easy.json()["goal_coverage"] == {"covered": goals[:1], "missing": goals[1:]}
                ordinary = parameters | {"difficulty": "medium", "cover_each_goal": True}
                planned = await browser.post(
                    f"{ROOT}/trainings", json=ordinary | {"idempotency_key": str(uuid4())}
                )
                assert planned.status_code == 201, planned.text
                assert planned.json()["rule_version"] == "learning-rules-v5"
                assert "schedule" not in planned.json()
                assert all("estimated_minutes" not in row for row in planned.json()["items"])
                assert planned.json()["goal_coverage"] == {
                    "covered": ["前缀和", "数组"],
                    "missing": ["二分查找", "图论"],
                }
                plan_path = f"{ROOT}/trainings/{planned.json()['id']}"

                catalog = (await browser.get(f"{AGENT}/skills")).json()["versions"]
                skill = next(item for item in catalog if item["purpose"] == "focused_training")
                configured = await browser.put(
                    f"{AGENT}/skills/{skill['version_id']}",
                    json={
                        "enabled": True,
                        "authorized_tools": skill["available_tools"],
                        "parameters": {
                            "tags": goals,
                            "cover_each_goal": True,
                            "daily_minutes": 40,
                            "goal": "比较两种区间题",
                            "maximum_difficulty": "medium",
                        },
                    },
                )
                assert configured.status_code == 200, configured.text
                assert configured.json()["parameters"]["cover_each_goal"] is True
                created = await browser.post(
                    f"{AGENT}/sessions", json={"purpose": "focused_training"}
                )
                assert created.status_code == 201, created.text
                session = created.json()["session_id"]
                worker = LearningWorker(async_sessionmaker(worker_engine, expire_on_commit=False))

                async def run():
                    queued = await browser.post(
                        f"{AGENT}/sessions/{session}/runs",
                        json={"request_id": str(uuid4()), "skill_version_id": skill["version_id"]},
                    )
                    assert queued.status_code == 202, queued.text
                    assert await worker.run_once()
                    url = f"{AGENT}/runs/{queued.json()['run_id']}"
                    finished = (await browser.get(url)).json()
                    assert finished["state"] == "succeeded", finished
                    result = finished["result"]
                    assert result["goal_coverage"] == coverage
                    assert {item["problem_version_id"] for item in result["items"]} == {
                        str(left),
                        str(right),
                    }
                    return url

                first = await run()
                trace = (await browser.get(f"{first}/trace")).json()
                assert trace["finished_tool_calls"] == 1
                saved = await browser.post(f"{first}/training")
                assert saved.status_code == 200, saved.text
                assert saved.json()["rule_version"] == "agent-rules-v5"
                assert saved.json()["goal_coverage"] == coverage
                schedule = {
                    "purpose": "focused_training",
                    "goal": "比较两种区间题",
                    "daily_minutes": 40,
                }
                assert saved.json()["schedule"] == schedule
                assert "schedule" not in saved.json()["parameters"]
                assert [item["estimated_minutes"] for item in saved.json()["items"]] == [20, 20]

                pending_confirmation = await run()
                async with app.state.sessions.begin() as db:
                    await db.execute(
                        update(ProblemLearningMetadata)
                        .where(ProblemLearningMetadata.problem_version_id == right)
                        .values(tags=["前缀和"], revision=2)
                    )
                changed = await browser.post(f"{pending_confirmation}/training")
                assert changed.status_code == 409
                original = await browser.get(plan_path)
                assert original.json()["goal_coverage"] == planned.json()["goal_coverage"]
                focused = await browser.get(f"{ROOT}/trainings/{saved.json()['id']}")
                assert focused.json()["schedule"] == schedule
                assert [item["estimated_minutes"] for item in focused.json()["items"]] == [20, 20]
        finally:
            await worker_engine.dispose()

    asyncio.run(exercise())
